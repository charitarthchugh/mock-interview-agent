#!/usr/bin/env python3
"""Add speaker labels to the transcribed mock interviews.

    scripts/setup-diarise.sh                       # one-off, builds the venv
    .diarise-venv/bin/python scripts/diarise.py corpus/audio corpus/transcripts

The transcripts are `{start, end, text}` and say nothing about who is talking,
which makes them unusable as fine-tuning pairs: the whole signal in a mock
interview is which side said what. This does not re-transcribe. The Whisper text
is fine; only the attribution is missing, so a diarisation runs over the same
audio and each existing segment takes the speaker it overlaps most.

Re-transcribing with a speech model instead would be both slower and worse: the
audio is already turned into good text, and knowing who spoke is a different
problem from knowing what was said.

Every `.jsonl` gains a `speaker` field ("A", "B", ...) and, where it can be
worked out, a `role` of "interviewer" or "candidate". Originals are left alone;
output goes to `*.speakers.jsonl` so a bad run costs nothing.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import wave
from collections import defaultdict
from pathlib import Path

# NVIDIA's end-to-end diariser. Chosen over pyannote because every pyannote
# pipeline is gated behind a click-through licence, and the box this runs on is
# an NVIDIA one anyway. It handles up to four speakers.
NEMO_MODEL = "nvidia/diar_sortformer_4spk-v1"


def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    """Seconds two spans share."""
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def assign(segments: list[dict], turns: list[tuple[float, float, str]]) -> None:
    """Give each transcript segment the speaker it overlaps most.

    Whisper's segment boundaries and the diarisation's turn boundaries are drawn
    by different models and do not line up, so a segment is scored against every
    turn rather than matched to the one containing its midpoint — a short
    interjection inside a long turn would otherwise take the wrong speaker.
    """
    for seg in segments:
        scores: dict[str, float] = defaultdict(float)
        for start, end, who in turns:
            if end < seg["start"]:
                continue
            if start > seg["end"]:
                break
            scores[who] += overlap(seg["start"], seg["end"], start, end)
        if scores:
            seg["speaker"] = max(scores, key=scores.get)


def name_speakers(turns: list[tuple[float, float, str]]) -> dict[str, str]:
    """Stable short names, in order of first appearance."""
    names = {}
    for _, _, who in turns:
        if who not in names:
            names[who] = chr(ord("A") + len(names))
    return names


BUCKET = 300.0  # five minutes


def label_roles(segments: list[dict]) -> dict[str, str]:
    """Work out which speaker is running the session.

    Not "talks the most": in a group session the candidates together out-talk
    the interviewer. Not "speaks first" either — the opening hello can be
    clipped into a fragment of its own, and the greeting then becomes a
    speaker with twenty-odd seconds to its name.

    What holds across a recording is that the interviewer is there throughout,
    while any one candidate comes and goes. So the interviewer is whoever
    appears in the most five-minute buckets of the session, and the margin has
    to be clear — otherwise no role is written and a human can look.
    """
    seen: dict[str, set[int]] = defaultdict(set)
    for seg in segments:
        if seg.get("speaker"):
            seen[seg["speaker"]].add(int(seg["start"] // BUCKET))
    if len(seen) < 2:
        return {}
    ranked = sorted(seen, key=lambda s: -len(seen[s]))
    lead, runner = ranked[0], ranked[1]
    if len(seen[lead]) - len(seen[runner]) < 2:
        return {}
    return {s: ("interviewer" if s == lead else "candidate") for s in seen}


def diarise_window(model, audio: Path) -> list[tuple[float, float, str]]:
    """Speaker turns as (start, end, label), in time order.

    Sortformer answers with lines of "start end speaker_N", which is friendly
    to read and useless to compute with, so they are parsed once here.
    """
    turns = []
    for line in model.diarize(audio=[str(audio)], batch_size=1)[0]:
        start, end, who = line.split()
        turns.append((float(start), float(end), who))
    turns.sort()
    return turns


def cut(src: Path, start: float, end: float, dest: Path) -> None:
    """Write the span [start, end) of a wav to its own file."""
    with wave.open(str(src)) as w:
        rate = w.getframerate()
        w.setpos(int(start * rate))
        frames = w.readframes(int((end - start) * rate))
        with wave.open(str(dest), "w") as o:
            o.setnchannels(w.getnchannels())
            o.setsampwidth(w.getsampwidth())
            o.setframerate(rate)
            o.writeframes(frames)


def duration(audio: Path) -> float:
    with wave.open(str(audio)) as w:
        return w.getnframes() / w.getframerate()


EMBED_MODEL = "titanet_large"

# Two turns closer than this in cosine distance are taken to be one voice.
# Tuned by hand: below it, one speaker splits into several across the session;
# above it, the quieter speakers merge into the one running the session.
VOICE_DISTANCE = 0.80


def embed_turns(embedder, audio: Path, turns: list[tuple[float, float, str]],
                workdir: Path, longest: float = 6.0):
    """A voice fingerprint per turn, for turns long enough to have one.

    Anything under a second is a "yeah" or a cough — too little signal to
    identify, and including it drags the clustering around. Those turns are
    left out here and take their speaker from the turns around them later.
    """
    import torch

    keep, vectors = [], []
    clip = workdir / "turn.wav"
    for i, (start, end, _) in enumerate(turns):
        if end - start < 1.0:
            continue
        # The middle of a long turn: the ends are where speakers overlap.
        middle = (start + end) / 2
        span = min(longest, end - start)
        cut(audio, middle - span / 2, middle + span / 2, clip)
        with torch.no_grad():
            vectors.append(embedder.get_embedding(str(clip)).squeeze().cpu().numpy())
        keep.append(i)
    return keep, vectors


def cluster_voices(embedder, audio: Path,
                   turns: list[tuple[float, float, str]],
                   workdir: Path) -> list[tuple[float, float, str]]:
    """Give every turn a session-wide speaker, by voice rather than by seam.

    The diariser runs a window at a time, so its "speaker_0" means nothing
    outside the window it came from. Matching labels across the seam by who was
    talking during the overlap looked reasonable and was not: a speaker silent
    for those few seconds became a new person, and the labels ended up
    partitioning the session by time instead of by voice — speaker A for the
    first half hour, speaker C for the second.

    Clustering voice fingerprints has no seams in it at all.
    """
    import numpy as np
    from sklearn.cluster import AgglomerativeClustering

    keep, vectors = embed_turns(embedder, audio, turns, workdir)
    if len(vectors) < 2:
        return turns

    matrix = np.vstack(vectors)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    labels = AgglomerativeClustering(
        n_clusters=None, distance_threshold=VOICE_DISTANCE,
        metric="cosine", linkage="average",
    ).fit_predict(matrix)

    named = list(turns)
    for i, label in zip(keep, labels):
        s, e, _ = named[i]
        named[i] = (s, e, f"speaker_{label}")

    # The turns too short to fingerprint take the nearest labelled neighbour.
    settled = {i for i in keep}
    for i, (s, e, _) in enumerate(named):
        if i in settled:
            continue
        near = min(settled, key=lambda j: abs(named[j][0] - s), default=None)
        if near is not None:
            named[i] = (s, e, named[near][2])
    return named


def diarise(model, audio: Path, window: float, overlap_s: float,
            workdir: Path) -> list[tuple[float, float, str]]:
    """Where the turns fall, taken a window at a time.

    An hour-long session will not go through the model in one piece: it took
    110 GB and came within a whisker of exhausting the box. Windowing keeps
    the memory flat whatever the length. The labels each window
    produces are local to it, and are replaced wholesale by `cluster_voices`.
    """
    total = duration(audio)
    if total <= window:
        return [(s, e, f"w0_{w}") for s, e, w in diarise_window(model, audio)]

    turns: list[tuple[float, float, str]] = []
    start = 0.0
    while start < total:
        end = min(start + window, total)
        piece = workdir / "window.wav"
        cut(audio, start, end, piece)
        fresh = [(s + start, e + start, w)
                 for s, e, w in diarise_window(model, piece)]

        # Only take turns past the overlap, so a boundary is not described
        # twice. Identity is settled later, by voice.
        floor = start if not turns else start + overlap_s / 2
        turns += [(s, e, f"w{int(start)}_{w}") for s, e, w in fresh if e > floor]
        if end >= total:
            break
        start = end - overlap_s

    turns.sort()
    return turns


def run(audio: Path, transcript: Path, out: Path, model, embedder,
        window: float, overlap_s: float, workdir: Path) -> str:
    segments = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()]
    if not segments:
        return f"{transcript.name}: empty, skipped"

    turns = diarise(model, audio, window, overlap_s, workdir)
    if not turns:
        return f"{transcript.name}: no speech found"
    turns = cluster_voices(embedder, audio, turns, workdir)
    turns.sort()

    names = name_speakers(turns)
    turns = [(s, e, names[w]) for s, e, w in turns]
    assign(segments, turns)
    roles = label_roles(segments)
    for seg in segments:
        role = roles.get(seg.get("speaker", ""))
        if role:
            seg["role"] = role

    with out.open("w") as fh:
        for seg in segments:
            fh.write(json.dumps(seg) + "\n")

    named = sum(1 for s in segments if s.get("speaker"))
    return (f"{transcript.name}: {len(names)} speakers, "
            f"{named}/{len(segments)} segments labelled, "
            f"roles {'assigned' if roles else 'UNCLEAR — needs a human'}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", type=Path)
    ap.add_argument("transcripts", type=Path)
    ap.add_argument("--device", default="cuda",
                    help="cuda, or cpu when the GPU is busy serving the model")
    ap.add_argument("--window", type=float, default=480.0,
                    help="seconds of audio per pass; lower it if the GPU is tight")
    ap.add_argument("--overlap", type=float, default=30.0,
                    help="seconds shared between windows, used to match speakers")
    ap.add_argument("--relabel-only", action="store_true",
                    help="redo the roles on existing output; no model, no GPU")
    args = ap.parse_args()

    if args.relabel_only:
        for path in sorted(args.transcripts.glob("*.speakers.jsonl")):
            segments = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            roles = label_roles(segments)
            for seg in segments:
                seg.pop("role", None)
                role = roles.get(seg.get("speaker", ""))
                if role:
                    seg["role"] = role
            with path.open("w") as fh:
                for seg in segments:
                    fh.write(json.dumps(seg) + "\n")
            lead = next((s for s, r in roles.items() if r == "interviewer"), None)
            print(f"{path.name}: interviewer is {lead or 'UNCLEAR — needs a human'}")
        return 0

    from nemo.collections.asr.models import (
        EncDecSpeakerLabelModel, SortformerEncLabelModel,
    )

    model = SortformerEncLabelModel.from_pretrained(NEMO_MODEL)
    model.eval()
    embedder = EncDecSpeakerLabelModel.from_pretrained(EMBED_MODEL)
    embedder.eval()
    if args.device != "cuda":
        model.to(args.device)
        embedder.to(args.device)
    workdir = Path(tempfile.mkdtemp(prefix="diarise-"))

    written = 0
    for transcript in sorted(args.transcripts.glob("*.jsonl")):
        if transcript.name.endswith(".speakers.jsonl"):
            continue
        audio = args.audio / f"{transcript.stem}.wav"
        if not audio.exists():
            print(f"{transcript.name}: no audio beside it, skipped")
            continue
        out = transcript.with_suffix(".speakers.jsonl")
        print(run(audio, transcript, out, model, embedder, args.window,
                  args.overlap, workdir), flush=True)
        written += 1

    print(f"\nwrote {written} labelled transcripts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

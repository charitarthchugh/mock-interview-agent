#!/usr/bin/env python3
"""Turn the labelled transcripts into training pairs.

    python scripts/build_sft.py corpus/transcripts corpus/sft

The brief in app/prompts.py tries to make a nano model interview well by
describing the behaviour in four thousand words. Measured against the recorded
sessions it does not hold: a fifth of turns still open with a banned praise
word, and the model re-asks questions the candidate has already answered. Those
are the two things a prompt cannot fix and examples can, which is the whole
reason the transcripts were collected.

Each example is the conversation so far plus the interviewer's next turn as
the target. Whisper writes an utterance per breath, so consecutive lines from one
speaker are merged back into the turn they came from first.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Short on purpose. The behaviour is supposed to come from the examples; a long
# brief at training time teaches the model to expect one at inference time.
SYSTEM = (
    "You are running a live technical mock interview by voice. Coach the"
    " candidate through UMPIRE — understand, match, plan, implement, review,"
    " evaluate — and protect their clock. Ask questions; never hand them an"
    " approach or an answer. Speak briefly, the way a person speaks aloud."
)


# A speaker who stops for this long has finished a turn, even if they are the
# next to talk. Without it a lecture merges into one "turn": the first pass
# produced a single target of over two thousand words, which would teach exactly
# the monologue the brief spends four thousand words trying to prevent.
PAUSE = 2.0

# Longest target worth learning from. This is speech — the brief asks for two or
# three sentences, and anything much past that is the model being taught to
# lecture. Also the length at which a merge has probably swallowed a reply.
LONGEST = 120


def turns(segments: list[dict]) -> list[tuple[str, str]]:
    """Utterances merged back into speaking turns, split on real pauses."""
    merged: list[tuple[str, str]] = []
    last_end = None
    for seg in segments:
        role = seg.get("role")
        text = (seg.get("text") or "").strip()
        if not role or not text:
            continue
        joins = (merged and merged[-1][0] == role
                 and last_end is not None and seg["start"] - last_end <= PAUSE)
        if joins:
            merged[-1] = (role, f"{merged[-1][1]} {text}")
        else:
            merged.append((role, text))
        last_end = seg["end"]
    return merged


def examples(merged: list[tuple[str, str]], context: int) -> list[dict]:
    """One example per interviewer turn, with the conversation before it.

    An interviewer turn with nothing before it is dropped: there is nothing to
    learn from a reply to no question.
    """
    out = []
    for i, (role, text) in enumerate(merged):
        if role != "interviewer" or i == 0:
            continue
        if len(text.split()) > LONGEST:
            continue
        # Whisper capitalises the start of a sentence, so a target opening in
        # lower case began mid-sentence: the diarisation moved the boundary
        # into the middle of somebody talking, and the target is a fragment.
        if text[:1].islower():
            continue
        history = merged[max(0, i - context):i]
        if not any(r == "candidate" for r, _ in history):
            continue
        messages = [{"role": "system", "content": SYSTEM}]
        for r, t in history:
            messages.append({
                "role": "assistant" if r == "interviewer" else "user",
                "content": t,
            })
        messages.append({"role": "assistant", "content": text})
        out.append({"messages": messages})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("transcripts", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--context", type=int, default=8,
                    help="turns of history to keep before the target")
    ap.add_argument("--holdout", default="",
                    help="keep sessions whose filename contains this out, "
                         "for evaluation; empty keeps nothing back")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    train, held = [], []
    for path in sorted(args.transcripts.glob("*.speakers.jsonl")):
        segments = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        merged = turns(segments)
        if not merged:
            print(f"{path.name}: no roles assigned, skipped")
            continue
        rows = examples(merged, args.context)
        held_out = bool(args.holdout) and args.holdout in path.name
        (held if held_out else train).extend(rows)
        speech = sum(1 for r, _ in merged if r == "interviewer")
        print(f"{path.name}: {len(merged)} turns, {speech} interviewer, {len(rows)} examples")

    for name, rows in (("train.jsonl", train), ("holdout.jsonl", held)):
        with (args.out / name).open("w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    print(f"\nwrote {len(train)} training and {len(held)} held-out examples "
          f"to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

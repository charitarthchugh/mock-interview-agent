"""Transcribe extracted session audio with faster-whisper.

Run with: uv run --group corpus python scripts/transcribe.py <audio> <out>
The app itself does not use Whisper; this is corpus ingest only.

Writes one JSONL of timestamped segments per recording, plus a readable .txt.
Segments rather than a blob of text: the timestamps are what let a later pass
label speakers or carve a session into individual mock interviews without
transcribing three and a half hours again.

Resumable — a finished recording is skipped, so a crash costs one file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from faster_whisper import WhisperModel

MODEL = "small.en"


def transcribe(model: WhisperModel, wav: Path, out_dir: Path) -> None:
    started = time.time()
    segments, info = model.transcribe(
        str(wav),
        language="en",
        vad_filter=True,
        beam_size=1,
        # Hour-long recordings are where whisper's repetition loop lives: one
        # bad segment otherwise poisons every segment after it.
        condition_on_previous_text=False,
    )

    jsonl = out_dir / f"{wav.stem}.jsonl"
    text = out_dir / f"{wav.stem}.txt"
    count = 0
    with jsonl.open("w") as jf, text.open("w") as tf:
        for seg in segments:
            record = {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
            }
            jf.write(json.dumps(record) + "\n")
            tf.write(f"[{int(seg.start // 60):02d}:{int(seg.start % 60):02d}] "
                     f"{record['text']}\n")
            count += 1

    elapsed = time.time() - started
    print(f"{wav.stem[:55]}  {count} segments  "
          f"{info.duration / 60:.0f} min audio in {elapsed / 60:.1f} min",
          flush=True)


if __name__ == "__main__":
    src, dest = Path(sys.argv[1]), Path(sys.argv[2])
    dest.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=20)
    for wav in sorted(src.glob("*.wav")):
        if (dest / f"{wav.stem}.jsonl").exists():
            print(f"skip {wav.stem[:55]}", flush=True)
            continue
        transcribe(model, wav, dest)

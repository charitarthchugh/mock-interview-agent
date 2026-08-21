"""Pull a 16 kHz mono WAV out of each session recording.

PyAV rather than ffmpeg, which isn't installed on this box — and PyAV is
already a dependency because faster-whisper decodes with it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import av


def extract(src: Path, dst: Path, rate: int = 16000) -> float:
    container = av.open(str(src))
    out = av.open(str(dst), "w")
    stream = out.add_stream("pcm_s16le", rate=rate, layout="mono")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)

    seconds = 0.0
    for frame in container.decode(audio=0):
        for chunk in resampler.resample(frame):
            seconds += chunk.samples / rate
            for packet in stream.encode(chunk):
                out.mux(packet)
    for packet in stream.encode(None):
        out.mux(packet)
    out.close()
    container.close()
    return seconds


if __name__ == "__main__":
    videos = sorted(Path(sys.argv[1]).expanduser().glob("*.mp4"))
    dest = Path(sys.argv[2])
    dest.mkdir(parents=True, exist_ok=True)
    for video in videos:
        wav = dest / (video.stem + ".wav")
        if wav.exists():
            print(f"skip {wav.name}")
            continue
        secs = extract(video, wav)
        print(f"{wav.name}  {secs / 60:.1f} min  {wav.stat().st_size / 1e6:.0f} MB")

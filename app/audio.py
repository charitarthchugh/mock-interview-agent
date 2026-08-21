"""Deciding whether a recorded turn actually contains speech.

A tap on the talk button, a cough, or a release before saying anything all
produce a valid clip. Sending those to the model wastes a GPU round trip and,
worse, gets the interviewer to respond earnestly to nothing.
"""

from __future__ import annotations

import io
import math

import av
import numpy as np

MIN_SECONDS = 0.4  # shorter than this cannot carry a word
MIN_RMS = 0.006  # quieter than this is room noise, not speech
MIN_LOUD_SECONDS = 0.15  # speech needs a bit of sustained energy


RATE = 16000
WINDOW = RATE // 10  # 100ms


def measure(blob: bytes) -> tuple[float, float, float]:
    """Return (duration, overall RMS, seconds spent above the noise floor)."""
    container = av.open(io.BytesIO(blob))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)

    # Decoded chunks are far shorter than the window, so the signal has to be
    # assembled before it can be scanned for sustained energy.
    parts = [
        chunk.to_ndarray().reshape(-1)
        for frame in container.decode(audio=0)
        for chunk in resampler.resample(frame)
    ]
    container.close()
    if not parts:
        return 0.0, 0.0, 0.0

    samples = np.concatenate(parts).astype("float32") / 32768.0
    if samples.size == 0:
        return 0.0, 0.0, 0.0

    usable = samples.size - (samples.size % WINDOW)
    loud_windows = 0
    if usable:
        blocks = samples[:usable].reshape(-1, WINDOW)
        loud_windows = int((np.sqrt((blocks**2).mean(axis=1)) > MIN_RMS).sum())

    return (
        samples.size / RATE,
        math.sqrt(float((samples**2).mean())),
        loud_windows * WINDOW / RATE,
    )


def has_speech(blob: bytes) -> tuple[bool, str]:
    """Whether a clip is worth sending, and why not when it isn't."""
    if not blob:
        return False, "nothing was recorded"
    try:
        seconds, rms, loud = measure(blob)
    except Exception:
        # If the audio will not decode here it will not decode at the model
        # either — vLLM answers an unreadable clip with a 500.
        return False, "that recording could not be read"

    if not seconds:
        return False, "nothing was recorded"

    if seconds < MIN_SECONDS:
        return False, "that was too short to catch"
    if rms < MIN_RMS or loud < MIN_LOUD_SECONDS:
        return False, "I didn't hear anything"
    return True, ""

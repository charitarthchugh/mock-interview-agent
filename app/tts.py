"""Kokoro speaks the coach's replies, locally.

The browser's speechSynthesis was the last hop that left the Spark on some
platforms, and it sounded like a different coach on every device. Kokoro is
82M parameters, so it runs beside the 30B model without competing for room.
"""

from __future__ import annotations

import io
import os
import threading

import numpy as np
import soundfile as sf

VOICE = os.environ.get("TTS_VOICE", "af_heart")
DEVICE = os.environ.get("TTS_DEVICE", "cuda")
SPEED = float(os.environ.get("TTS_SPEED", "1.05"))
SAMPLE_RATE = 24000


class Kokoro:
    """Wraps KPipeline. Generation is not thread-safe, so calls are serialised."""

    def __init__(self) -> None:
        from kokoro import KPipeline

        # lang_code 'a' is American English; it selects the g2p, not the voice.
        self.pipeline = KPipeline(lang_code="a", device=DEVICE)
        self.lock = threading.Lock()

    def wav(self, text: str, voice: str = VOICE, speed: float = SPEED) -> bytes:
        """Synthesise `text` and return a complete WAV file."""
        with self.lock:
            chunks = [
                audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio)
                for _, _, audio in self.pipeline(text, voice=voice, speed=speed)
            ]
        if not chunks:
            chunks = [np.zeros(1, dtype="float32")]
        buf = io.BytesIO()
        sf.write(buf, np.concatenate(chunks), SAMPLE_RATE, format="WAV")
        return buf.getvalue()

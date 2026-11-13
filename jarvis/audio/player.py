"""Plays audio; stops immediately when the stop event is set."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def play(audio: np.ndarray, rate: int, device_name: str = "", volume: float = 1.0,
         stop: threading.Event | None = None) -> None:
    import sounddevice as sd

    from .mic import find_device

    if audio is None or audio.size == 0:
        return
    data = np.clip(audio.astype(np.float32) * volume, -1.0, 1.0)
    device = find_device(device_name, "output")
    block = int(rate * 0.05)
    try:
        with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=device) as stream:
            for i in range(0, len(data), block):
                if stop is not None and stop.is_set():
                    return
                stream.write(data[i:i + block].reshape(-1, 1))
    except Exception as exc:
        log.error("Could not play audio: %s", exc)


_chimes: dict[str, tuple[np.ndarray, int]] = {}


def chime(name: str, device_name: str = "", volume: float = 1.0) -> None:
    """Play start_conversation / end_conversation sounds without blocking."""
    import soundfile as sf
    if name not in _chimes:
        path = ASSETS / f"{name}.wav"
        if not path.exists():
            return
        audio, rate = sf.read(str(path), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        _chimes[name] = (audio, rate)
    audio, rate = _chimes[name]
    threading.Thread(target=play, args=(audio, rate, device_name, volume), daemon=True).start()

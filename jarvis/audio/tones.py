"""Built-in sounds made from math, so no sound files are needed."""
from __future__ import annotations

import numpy as np

RATE = 24000


def _note(freq: float, seconds: float, decay: float = 4.0, volume: float = 0.5) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    wave = np.sin(2 * np.pi * freq * t) + 0.35 * np.sin(4 * np.pi * freq * t) + 0.1 * np.sin(6 * np.pi * freq * t)
    env = np.exp(-decay * t) * np.minimum(1.0, t * 200)  # soft attack, bell-like fade
    return (wave * env * volume / 1.45).astype(np.float32)


def _silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), np.float32)


def alarm_cycle() -> np.ndarray:
    """A gentle rising marimba-style pattern (about 3 seconds)."""
    notes = [523.25, 659.25, 783.99, 1046.5]  # C E G C
    parts = []
    for f in notes:
        parts += [_note(f, 0.32, 5.0)]
    parts += [_note(783.99, 0.32, 5.0), _note(1046.5, 0.9, 3.0), _silence(0.8)]
    return np.concatenate(parts)


def timer_cycle() -> np.ndarray:
    """Beep-beep-beep, pause."""
    beep = _note(1318.5, 0.16, 8.0, 0.6)
    return np.concatenate([beep, _silence(0.08), beep, _silence(0.08), beep, _silence(0.9)])


def notice() -> np.ndarray:
    """Ding-dong before an announcement."""
    return np.concatenate([_note(880.0, 0.35, 4.0, 0.45), _note(659.25, 0.7, 3.0, 0.45)])

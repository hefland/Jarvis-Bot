"""Detects speech and decides when you've finished talking.

The key setting is end_of_speech_seconds: how long you must be silent before it answers.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from .mic import CHUNK, RATE

CHUNK_SECONDS = CHUNK / RATE


class SpeechDetector:
    """Says whether an 80 ms chunk contains a human voice.

    Uses Silero VAD (a small neural network that ships with faster-whisper). Unlike simpler detectors it
    ignores music, alarms, TV-style noise and fans, so Jarvis knows when YOU stop talking even with music on.
    Falls back to WebRTC VAD if Silero isn't available."""

    def __init__(self, aggressiveness: int = 2):
        self.aggressiveness = int(aggressiveness)
        # 0..3 setting -> how sure the network must be that it's a voice
        self.threshold = {0: 0.3, 1: 0.4, 2: 0.5, 3: 0.65}.get(self.aggressiveness, 0.5)
        self._buf = np.zeros(0, dtype=np.float32)
        self.last_probability = 0.0
        try:
            from faster_whisper.vad import get_vad_model
            self._silero = get_vad_model()
            self._webrtc = None
        except Exception:
            import webrtcvad
            self._silero = None
            self._webrtc = webrtcvad.Vad(self.aggressiveness)

    def probability(self, chunk: np.ndarray) -> float:
        x = chunk.astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, x])[-2560:]   # last 160 ms, 5 windows of 512 samples
        if len(self._buf) < 2560:
            return 0.0
        p = self._silero(self._buf).reshape(-1)
        self.last_probability = float(p[-3:].max())
        return self.last_probability

    def __call__(self, chunk: np.ndarray) -> bool:
        if np.sqrt(np.mean(chunk.astype(np.float32) ** 2)) < 60:  # basically silent
            self._buf = np.concatenate([self._buf, chunk.astype(np.float32) / 32768.0])[-2560:]
            return False
        if self._silero is not None:
            return self.probability(chunk) >= self.threshold
        frame = 320  # 20 ms
        votes = sum(1 for i in range(0, len(chunk) - frame + 1, frame)
                    if self._webrtc.is_speech(chunk[i:i + frame].tobytes(), RATE))
        return votes >= 2


class Recording:
    """Feed chunks with add(); it returns True when the recording is finished."""

    def __init__(self, is_speech: Callable[[np.ndarray], bool], end_silence: float, wait_for_speech: float,
                 max_seconds: float):
        self.is_speech = is_speech
        self.end_silence = end_silence
        self.wait_for_speech = wait_for_speech
        self.max_seconds = max_seconds
        self.chunks: list[np.ndarray] = []
        self.elapsed = 0.0
        self.silence = 0.0
        self.speech_time = 0.0
        self.heard_speech = False
        self.finished_reason = ""

    def add(self, chunk: np.ndarray) -> bool:
        self.chunks.append(chunk)
        self.elapsed += CHUNK_SECONDS
        if self.is_speech(chunk):
            self.speech_time += CHUNK_SECONDS
            self.silence = 0.0
            if self.speech_time >= 0.24:  # ignore single clicks/bumps
                self.heard_speech = True
        else:
            self.silence += CHUNK_SECONDS
        if self.heard_speech and self.silence >= self.end_silence:
            self.finished_reason = "silence"
            return True
        if not self.heard_speech and self.elapsed >= self.wait_for_speech:
            self.finished_reason = "no_speech"
            return True
        if self.elapsed >= self.max_seconds:
            self.finished_reason = "max_length"
            return True
        return False

    def audio(self) -> np.ndarray:
        """Float32 audio in -1..1, with the trailing silence trimmed (keeps 0.3 s)."""
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        data = np.concatenate(self.chunks).astype(np.float32) / 32768.0
        if self.finished_reason == "silence":
            keep = len(data) - int(max(0.0, self.silence - 0.3) * RATE)
            data = data[:max(keep, 0)]
        return data

"""Wake word detection. Two engines:

* openwakeword -- tiny, fast, accurate; works with its built-in words ("hey jarvis", "alexa", ...)
  or a custom .onnx model you train for free (see README).
* whisper -- listens for short bursts of speech and checks if they contain your wake phrase.
  Works with ANY word you type in settings. Uses a bit more graphics-card power.
"""
from __future__ import annotations

import difflib
import logging
import os
import re

import numpy as np

from .recorder import CHUNK_SECONDS, SpeechDetector

log = logging.getLogger(__name__)


class Detection:
    def __init__(self, leftover_text: str = ""):
        self.leftover_text = leftover_text  # words said right after the wake phrase (whisper engine)


class OpenWakeWordDetector:
    def __init__(self, model_name: str, custom_path: str, threshold: float):
        import openwakeword
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        if model_name == "custom":
            if not custom_path:
                raise ValueError("Wake word is set to 'custom' but no model file is set.")
            models = [custom_path]
        else:
            # downloads the small model files the first time (a few MB)
            paths = [p for p in openwakeword.get_pretrained_model_paths("onnx") if model_name in p]
            if not paths or not all(os.path.exists(p) for p in paths):
                download_models([model_name])
            models = [model_name]
        self.model = Model(wakeword_models=models, inference_framework="onnx")
        self.threshold = threshold
        self.name = model_name
        log.info("openWakeWord ready (%s).", model_name if model_name != "custom" else custom_path)

    def process(self, chunk: np.ndarray) -> Detection | None:
        scores = self.model.predict(chunk)
        score = max(scores.values()) if scores else 0.0
        if score >= self.threshold:
            log.info("Wake word detected (score %.2f).", score)
            self.reset()
            return Detection()
        return None

    def reset(self) -> None:
        self.model.reset()


_JUNK_WORDS = {"www", "com", "net", "org", "http", "https"}  # Whisper sometimes writes names as web addresses


def _normalize(text: str) -> str:
    words = re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()
    return " ".join(w for w in words if w not in _JUNK_WORDS)


def _matches_at(words: list[str], i: int, target: str, cutoff: float) -> int:
    """If the wake phrase starts at words[i], return how many words it used (else 0)."""
    n = len(target.split())
    for size in [n, n + 1] + ([n - 1] if n > 1 else []):
        window = " ".join(words[i:i + size])
        if i + size <= len(words) and difflib.SequenceMatcher(None, window, target).ratio() >= cutoff:
            return size
    return 0


def strip_wake_phrase(text: str, phrase: str, cutoff: float = 0.75) -> str:
    """Remove the wake phrase (said once or several times) from the start of what was heard."""
    target = _normalize(phrase)
    words = _normalize(text).split()
    if not target or not words:
        return text.strip()
    i, stripped = 0, False
    while i < len(words):
        used = _matches_at(words, i, target, cutoff)
        if not used:
            break
        i += used
        stripped = True
    if not stripped:
        return text.strip()
    return " ".join(words[i:])


def threshold_to_cutoff(threshold: float) -> float:
    """The sensitivity setting (0.1 = easy ... 0.95 = strict) as a text-similarity cutoff for the whisper engine."""
    return min(0.98, 0.6 + 0.4 * float(threshold))


def find_phrase(text: str, phrase: str, cutoff: float = 0.75, allow_partial: bool = False) -> str | None:
    """If `phrase` (fuzzy) appears in `text`, return whatever was said after it, else None.
    The WHOLE phrase must be there ("hey nova" needs both words) unless allow_partial is set."""
    words = _normalize(text).split()
    target = _normalize(phrase)
    n = len(target.split())
    if not words or not target:
        return None
    sizes = [n, n + 1] + ([n - 1] if allow_partial and n > 1 else [])
    for size in sizes:  # exact word count first, so we never swallow a real word after the phrase
        need = cutoff + (0.05 if size != n else 0.0)
        for i in range(0, len(words) - size + 1):
            window = " ".join(words[i:i + size])
            if difflib.SequenceMatcher(None, window, target).ratio() >= need:
                return strip_wake_phrase(" ".join(words[i + size:]), phrase, cutoff)
    return None


class WhisperPhraseDetector:
    """Listens for short bursts of speech and checks if they contain a phrase (used for the wake word
    and for the always-on cancel phrase)."""

    def __init__(self, phrase: str, whisper_model: str, device: str, vad_aggressiveness: int,
                 threshold: float = 0.5, name: str = "Wake phrase"):
        from .stt import load_whisper
        self.phrase = phrase.strip() or "Jarvis"
        self.model = load_whisper(whisper_model, device)
        self.is_speech = SpeechDetector(max(2, vad_aggressiveness))
        self.cutoff = threshold_to_cutoff(threshold)
        self.allow_partial = threshold < 0.4
        self.name = name
        self.chunks: list[np.ndarray] = []
        self.silence = 0.0
        self.speech = 0.0
        log.info("%s ready: '%s' (match strictness %.2f).", name, self.phrase, self.cutoff)

    def process(self, chunk: np.ndarray) -> Detection | None:
        speaking = self.is_speech(chunk)
        if speaking:
            self.chunks.append(chunk)
            self.speech += CHUNK_SECONDS
            self.silence = 0.0
        elif self.chunks:
            self.chunks.append(chunk)
            self.silence += CHUNK_SECONDS
        total = len(self.chunks) * CHUNK_SECONDS
        # check after a short pause, or every 4 seconds of continuous speech
        if self.chunks and (self.silence >= 0.4 or total >= 4.0):
            audio = np.concatenate(self.chunks).astype(np.float32) / 32768.0
            had_speech = self.speech >= 0.3
            self.reset()
            if not had_speech:
                return None
            text = self.transcribe(audio)
            if text:
                log.debug("%s listener heard: %s", self.name, text)
            leftover = find_phrase(text, self.phrase, self.cutoff, self.allow_partial)
            if leftover is not None:
                log.info("%s detected in: %r", self.name, text)
                return Detection(leftover)
        return None

    def transcribe(self, audio: np.ndarray) -> str:
        # No "hint" prompt here: hinting the phrase makes Whisper imagine it in silence.
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, condition_on_previous_text=False,
                                            without_timestamps=True)
        kept = [seg.text for seg in segments if seg.no_speech_prob < 0.6 and seg.avg_logprob > -1.0]
        return " ".join(kept).strip()

    def reset(self) -> None:
        self.chunks = []
        self.silence = 0.0
        self.speech = 0.0


def make_detector(settings):
    ww = settings.section("wake_word")
    if ww["engine"] == "none":
        return None
    if ww["engine"] == "whisper":
        return WhisperPhraseDetector(ww["phrase"], ww["whisper_model"], settings.get("speech_to_text", "device"),
                                     int(settings.get("listening", "vad_aggressiveness")), float(ww["threshold"]))
    return OpenWakeWordDetector(ww["openwakeword_model"], ww["custom_model_path"], float(ww["threshold"]))

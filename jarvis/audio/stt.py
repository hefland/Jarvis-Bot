"""Speech to text with faster-whisper (runs on your graphics card)."""
from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

_cache: dict[tuple[str, str], object] = {}
_cache_lock = threading.Lock()


def load_whisper(model: str, device: str = "auto"):
    """Load (and cache) a Whisper model. Falls back to the CPU if the graphics card can't be used."""
    from faster_whisper import WhisperModel

    with _cache_lock:
        key = (model, device)
        if key in _cache:
            return _cache[key]
        attempts = []
        if device in ("auto", "cuda"):
            attempts.append(("cuda", "float16"))
        if device in ("auto", "cpu"):
            attempts.append(("cpu", "int8"))
        last_exc: Exception | None = None
        for dev, compute in attempts:
            try:
                log.info("Loading Whisper '%s' on %s ...", model, dev)
                m = WhisperModel(model, device=dev, compute_type=compute)
                # run once so missing CUDA libraries fail here, not in the middle of a question
                list(m.transcribe(np.zeros(16000, dtype=np.float32), language="en")[0])
                _cache[key] = m
                log.info("Whisper '%s' ready on %s.", model, dev)
                return m
            except Exception as exc:
                last_exc = exc
                log.warning("Whisper on %s failed: %s", dev, exc)
        raise RuntimeError(f"Could not load Whisper model '{model}': {last_exc}")


def decode_audio_file(data) -> np.ndarray:
    """Decode a recording (webm/opus from Android, mp4/aac from iPhone, wav, ogg, mp3...) to 16 kHz mono float32.

    We do this ourselves instead of using faster-whisper's helper, which breaks with newer PyAV versions."""
    import io

    import av
    if isinstance(data, (bytes, bytearray)):
        data = io.BytesIO(data)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(data, mode="r") as container:
        stream = next(s for s in container.streams if s.type == "audio")
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):  # flush what's left
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class SpeechToText:
    def __init__(self, model: str, device: str, language: str, hint: str = ""):
        self.model = load_whisper(model, device)
        self.language = language or None
        self.hint = hint

    def transcribe(self, audio) -> str:
        """audio: float32 samples at 16 kHz, or an audio file (path or file-like, e.g. from the phone page)."""
        if not isinstance(audio, np.ndarray):
            audio = decode_audio_file(audio)
        if audio.size < 1600:
            return ""
        # No hint prompt: hints make Whisper "hear" words that weren't said.
        segments, _ = self.model.transcribe(
            audio, language=self.language, beam_size=5, vad_filter=True, condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6 or s.avg_logprob > -0.7).strip()
        return _drop_hallucinations(text)


# Whisper sometimes "hears" these in silence or noise.
_JUNK = {"", "you", "you.", ".", "thanks for watching!", "thank you for watching.", "thanks for watching.",
         "please subscribe.", "subtitles by the amara.org community"}


def _drop_hallucinations(text: str) -> str:
    return "" if text.strip().lower() in _JUNK else text

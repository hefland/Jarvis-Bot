"""Text to speech. Each engine turns a sentence into (audio, sample_rate).

Engines: kokoro (built in), openai_compatible (any voice server with an OpenAI-style
/audio/speech endpoint -- Kokoro-FastAPI, AllTalk, OpenedAI-Speech, ...), piper, espeak.
To add a new engine: write a class with a synth(text) method and add it to make_engine().
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class KokoroEngine:
    def __init__(self, voice: str, lang: str, speed: float):
        from kokoro import KPipeline
        try:
            self.pipeline = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
        except TypeError:  # older kokoro versions
            self.pipeline = KPipeline(lang_code=lang)
        self.voice, self.speed = voice, speed

    def synth(self, text: str):
        parts = [np.asarray(audio, dtype=np.float32).reshape(-1)
                 for _, _, audio in self.pipeline(text, voice=self.voice, speed=self.speed)
                 if audio is not None]
        return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), 24000


class HttpEngine:
    def __init__(self, url: str, model: str, voice: str, api_key: str, speed: float):
        self.url, self.model, self.voice, self.api_key, self.speed = url.rstrip("/"), model, voice, api_key, speed

    def synth(self, text: str):
        import httpx
        import soundfile as sf
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = httpx.post(f"{self.url}/audio/speech", headers=headers, timeout=60,
                              json={"model": self.model, "input": text, "voice": self.voice,
                                    "response_format": "wav", "speed": self.speed, "stream": False})
        except httpx.ConnectError as exc:
            raise RuntimeError(f"Can't reach the voice server at {self.url}. Is its Docker container running?") from exc
        if resp.status_code >= 400:
            raise RuntimeError(f"Voice server error {resp.status_code}: {resp.text[:200]}")
        try:
            audio, rate = sf.read(io.BytesIO(resp.content), dtype="float32")
        except Exception:
            audio, rate = read_loose_wav(resp.content)  # streamed WAV with placeholder sizes
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return audio, rate


def read_loose_wav(data: bytes):
    """Read 16-bit PCM WAV even when the size fields in the header are wrong (common with streaming servers)."""
    import struct
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RuntimeError("Voice server didn't send WAV audio.")
    pos, rate, channels = 12, 24000, 1
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            channels, rate = struct.unpack("<HI", data[pos + 10:pos + 16])
        elif cid == b"data":
            raw = data[pos + 8:]
            raw = raw[:len(raw) - len(raw) % (2 * channels)]
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            return audio.reshape(-1, channels) if channels > 1 else audio, rate
        pos += 8 + size + (size & 1)
    raise RuntimeError("Voice server sent audio I couldn't read.")


class PiperEngine:
    def __init__(self, command: str, model: str, speed: float):
        if not model or not Path(model).exists():
            raise ValueError("Piper needs a voice file (.onnx) set in settings.")
        cfg = Path(model + ".json")
        self.rate = json.loads(cfg.read_text())["audio"]["sample_rate"] if cfg.exists() else 22050
        self.command, self.model, self.speed = command, model, speed

    def synth(self, text: str):
        out = subprocess.run([self.command, "--model", self.model, "--output_raw",
                              "--length_scale", str(round(1.0 / max(self.speed, 0.1), 3))],
                             input=text.encode(), capture_output=True, timeout=60, check=True)
        return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0, self.rate


class EspeakEngine:
    def __init__(self, speed: float):
        self.exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not self.exe:
            raise ValueError("espeak-ng is not installed (sudo apt install espeak-ng).")
        self.speed = speed

    def synth(self, text: str):
        import soundfile as sf
        out = subprocess.run([self.exe, "--stdout", "-s", str(int(170 * self.speed)), text],
                             capture_output=True, timeout=60, check=True)
        audio, rate = sf.read(io.BytesIO(out.stdout), dtype="float32")
        return audio, rate


def make_engine(settings):
    t = settings.section("tts")
    engine = t["engine"]
    if engine == "none":
        return None
    if engine == "kokoro":
        return KokoroEngine(t["kokoro_voice"], t["kokoro_lang"], t["speed"])
    if engine == "openai_compatible":
        return HttpEngine(t["http_url"], t["http_model"], t["http_voice"], t["http_api_key"], t["speed"])
    if engine == "piper":
        return PiperEngine(t["piper_command"], t["piper_model"], t["speed"])
    return EspeakEngine(t["speed"])


def split_sentences(text: str) -> list[str]:
    """Speak sentence by sentence so the first words come out quickly."""
    pieces = re.split(r"(?<=[.!?;:])\s+|\n+", text.strip())
    out: list[str] = []
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if out and len(out[-1]) < 25:  # glue very short bits together
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


class Speaker:
    """Speaks text. Can be interrupted at any moment with stop().

    say(text) speaks and waits. stream() returns a SpeechStream you can keep adding sentences to
    while the AI is still writing, so speaking starts sooner."""

    def __init__(self, settings):
        self.settings = settings
        self._engine = None
        self._engine_key = None
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._engine_lock = threading.Lock()
        self.speaking = False

    def _get_engine(self):
        cfg = self.settings.section("tts")
        cfg.pop("volume", None)
        cfg.pop("output_device", None)  # these don't need the voice model reloaded
        key = json.dumps(cfg, sort_keys=True)
        with self._engine_lock:
            if key != self._engine_key:
                self._engine = None
                self._engine_key = key
                self._engine = make_engine(self.settings)
                log.info("Voice engine ready: %s", self.settings.get("tts", "engine"))
            return self._engine

    def warm_up(self) -> None:
        try:
            self._get_engine()
        except Exception as exc:
            log.error("Voice engine failed to load: %s", exc)

    def stop(self) -> None:
        self._stop.set()

    def stream(self) -> "SpeechStream":
        return SpeechStream(self)

    def say(self, text: str) -> None:
        st = self.stream()
        st.add(text)
        st.finish()
        st.wait()


class SpeechStream:
    def __init__(self, speaker: Speaker):
        import queue
        self.speaker = speaker
        self.texts: "queue.Queue[str | None]" = queue.Queue()
        self.audio: "queue.Queue[tuple | None]" = queue.Queue(maxsize=3)
        speaker._busy.acquire()          # one voice at a time
        speaker._stop.clear()
        self.stop = speaker._stop
        speaker.speaking = True
        self._producer = threading.Thread(target=self._produce, daemon=True)
        self._player = threading.Thread(target=self._play, daemon=True)
        self._producer.start()
        self._player.start()

    def add(self, text: str) -> None:
        for sentence in split_sentences(text):
            self.texts.put(sentence)

    def finish(self) -> None:
        self.texts.put(None)

    def wait(self, timeout: float | None = None) -> None:
        self._player.join(timeout)

    def _produce(self) -> None:
        try:
            engine = self.speaker._get_engine()
        except Exception as exc:
            log.error("Voice engine failed: %s", exc)
            engine = None
        while True:
            text = self.texts.get()
            if text is None or self.stop.is_set():
                break
            if engine is None:
                continue
            try:
                self.audio.put(engine.synth(text))
            except Exception as exc:
                log.error("Voice engine error: %s", exc)
        self.audio.put(None)

    def _play(self) -> None:
        from .player import play
        s = self.speaker.settings
        try:
            while True:
                item = self.audio.get()
                if item is None or self.stop.is_set():
                    break
                audio, rate = item
                play(audio, rate, s.get("tts", "output_device"), float(s.get("tts", "volume")), self.stop)
        finally:
            if self.stop.is_set():  # unblock the producer if we were interrupted
                self.texts.put(None)
                while not self.audio.empty():
                    try:
                        self.audio.get_nowait()
                    except Exception:
                        break
            self.speaker.speaking = False
            self.speaker._busy.release()

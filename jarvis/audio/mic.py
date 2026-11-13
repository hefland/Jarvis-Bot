"""Microphone input: delivers 80 ms chunks of 16 kHz mono int16 audio."""
from __future__ import annotations

import logging
import queue

import numpy as np

log = logging.getLogger(__name__)

RATE = 16000
CHUNK = 1280  # 80 ms -- the size openWakeWord wants


def list_devices() -> dict[str, list[str]]:
    import sounddevice as sd
    inputs, outputs = [], []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            inputs.append(d["name"])
        if d["max_output_channels"] > 0:
            outputs.append(d["name"])
    return {"input": inputs, "output": outputs}


def find_device(name: str, kind: str):
    """Find a device index by (part of) its name. Empty name = system default."""
    if not name:
        return None
    import sounddevice as sd
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and d["name"] == name:
            return i
    for i, d in enumerate(sd.query_devices()):
        if d[key] > 0 and name.lower() in d["name"].lower():
            return i
    log.warning("Audio device '%s' not found, using the default.", name)
    return None


class Microphone:
    def __init__(self, device_name: str = ""):
        self.device = find_device(device_name, "input")
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._stream = None
        self._resample = None
        self._buffer = np.zeros(0, dtype=np.int16)

    def start(self) -> None:
        import sounddevice as sd
        try:
            self._stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                                          device=self.device, callback=self._callback)
            self._stream.start()
            log.info("Microphone open at 16 kHz.")
        except Exception as exc:
            # Some microphones can't record at 16 kHz -- record at their own rate and convert.
            from math import gcd

            from scipy.signal import resample_poly

            native = int(sd.query_devices(self.device, "input")["default_samplerate"])
            log.info("Mic doesn't support 16 kHz (%s); recording at %d Hz and converting.", exc, native)
            g = gcd(RATE, native)
            up, down = RATE // g, native // g
            self._resample = lambda x: resample_poly(x.astype(np.float32), up, down)
            self._stream = sd.InputStream(samplerate=native, channels=1, dtype="int16",
                                          blocksize=int(CHUNK * native / RATE),
                                          device=self.device, callback=self._callback)
            self._stream.start()

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("Mic status: %s", status)
        data = indata[:, 0].copy()
        if self._resample is not None:
            data = np.clip(self._resample(data), -32768, 32767).astype(np.int16)
        self._buffer = np.concatenate([self._buffer, data])
        while len(self._buffer) >= CHUNK:
            chunk, self._buffer = self._buffer[:CHUNK], self._buffer[CHUNK:]
            try:
                self.queue.put_nowait(chunk)
            except queue.Full:  # the listener fell behind; drop the oldest audio
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
                self.queue.put_nowait(chunk)

    def read(self, timeout: float = 1.0) -> np.ndarray | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

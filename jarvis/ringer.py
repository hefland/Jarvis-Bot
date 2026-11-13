"""Rings alarms and timers until you stop them (wake word, 'stop', the Stop button, or a time limit)."""
from __future__ import annotations

import logging
import threading
import time

import numpy as np

from .audio import tones
from .context import ctx

log = logging.getLogger(__name__)


class Ringer:
    def __init__(self, settings):
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ringing: dict | None = None        # what is ringing right now
        self.last_rang: dict | None = None      # for "snooze" right after it stopped
        self.pending_briefing: float | None = None
        self.briefing_done_date: str | None = None

    def is_ringing(self) -> bool:
        return self.ringing is not None

    def ring(self, kind: str, label: str, item_id: int | None = None, seconds: float | None = None,
             speak: str = "") -> None:
        self.stop(reason="replaced")
        info = {"kind": kind, "label": label, "id": item_id, "started": time.time()}
        self.ringing = info
        self._stop = threading.Event()
        limit = seconds or (float(self.settings.get("alarms", "ring_minutes")) * 60)
        self._thread = threading.Thread(target=self._run, args=(info, limit, speak, self._stop), daemon=True)
        self._thread.start()

    def stop(self, reason: str = "user") -> dict | None:
        info = self.ringing
        if info is None:
            return None
        self._stop.set()
        if ctx.speaker is not None and reason != "replaced":
            ctx.speaker.stop()
        self.ringing = None
        self.last_rang = dict(info, stopped=time.time(), reason=reason)
        log.info("%s stopped (%s).", info["kind"].title(), reason)
        if info["kind"] == "alarm" and reason != "snooze":
            self._maybe_queue_briefing(info)
        return info

    def _maybe_queue_briefing(self, info: dict) -> None:
        from datetime import datetime
        now = datetime.now()
        today = now.date().isoformat()
        if self.settings.get("alarms", "morning_briefing") and now.hour < 12 and self.briefing_done_date != today:
            self.pending_briefing = time.time()

    # ------------------------------------------------------------------
    def _run(self, info: dict, limit: float, speak: str, stop: threading.Event) -> None:
        import sounddevice as sd

        from .audio.mic import find_device

        a = self.settings.section("alarms")
        cycle, rate = (tones.alarm_cycle(), tones.RATE) if info["kind"] == "alarm" else (tones.timer_cycle(), tones.RATE)
        if info["kind"] == "alarm" and a["sound_file"]:
            try:
                import soundfile as sf
                data, rate = sf.read(a["sound_file"], dtype="float32")
                cycle = data.mean(axis=1) if data.ndim > 1 else data
            except Exception as exc:
                log.error("Couldn't play alarm sound file (%s); using the built-in chime.", exc)
        if ctx.media is not None:
            ctx.media.duck(True)
        gentle = float(a["gentle_seconds"]) if info["kind"] == "alarm" else 0.0
        volume = float(a["volume"])
        start = time.time()
        spoken = False
        block = int(rate * 0.05)
        try:
            device = find_device(self.settings.get("tts", "output_device"), "output")
            while not stop.is_set() and time.time() - start < limit:
                with sd.OutputStream(samplerate=rate, channels=1, dtype="float32", device=device) as out:
                    for i in range(0, len(cycle), block):
                        if stop.is_set():
                            break
                        elapsed = time.time() - start
                        gain = volume * (min(1.0, 0.15 + elapsed / gentle) if gentle > 0 else 1.0)
                        out.write(np.clip(cycle[i:i + block] * gain, -1, 1).reshape(-1, 1))
                if speak and not spoken and not stop.is_set() and ctx.speaker is not None:
                    spoken = True
                    ctx.speaker.say(speak)
        except Exception as exc:
            log.error("Alarm sound failed: %s", exc)
        finally:
            if ctx.media is not None:
                ctx.media.duck(False)
            if not stop.is_set() and self.ringing is info:
                self.stop(reason="timeout")

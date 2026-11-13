"""Away mode: silently records voices and nearby sounds while you're gone, and alerts your phone.

Low energy: it only measures loudness/voice on the microphone audio Jarvis already reads (a few
math operations per 80 ms). Whisper runs only on saved clips, and only if you turn that on.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import timeparse as tp
from .config import data_dir
from .context import ctx

log = logging.getLogger(__name__)

RATE = 16000
CHUNK_S = 0.08


class SecurityMonitor:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.stt = None                  # set by the voice loop so clips can be transcribed
        self.is_speech = None            # set by the voice loop (webrtc VAD)
        self.folder = data_dir() / "security"
        self._pre = collections.deque(maxlen=int(3 / CHUNK_S))   # 3 s before the sound started
        self._triggers: collections.deque[float] = collections.deque(maxlen=50)
        self._speech_hits: collections.deque[float] = collections.deque(maxlen=50)
        self._clip: list[np.ndarray] | None = None
        self._clip_speech = 0
        self._clip_started = 0.0
        self._clip_wall = 0.0
        self._last_trigger = 0.0
        self._floor = None
        self._peak_floor = None
        self._was_away = False
        self._unsent: list[dict] = []
        self._last_alert = 0.0
        self._lock = threading.Lock()
        self._audio_clock = 0.0          # seconds of audio heard (keeps timing right even if audio arrives in bursts)

    # ---------------------------------------------------------------- on / off
    def _state(self) -> dict:
        return self.db.get("away_state", {"manual": None, "override_until": 0, "since": 0})

    def scheduled_now(self, now: datetime | None = None) -> bool:
        s = self.settings.section("security")
        if not s["schedule_enabled"]:
            return False
        now = now or tp.now_local()
        days = tp.parse_days(s["days"]) or list(range(7))
        try:
            sh, sm = (int(x) for x in s["start"].split(":"))
            eh, em = (int(x) for x in s["end"].split(":"))
        except ValueError:
            return False
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end <= start:  # overnight window, e.g. 22:00-06:00
            if now >= start:
                return now.weekday() in days
            yesterday = (now - timedelta(days=1)).weekday()
            return now < end and yesterday in days
        return now.weekday() in days and start <= now < end

    def away(self) -> bool:
        st = self._state()
        if st.get("manual") is True:
            return True
        if st.get("override_until", 0) > time.time():
            return False
        return self.scheduled_now()

    def set_away(self, on: bool, source: str = "voice") -> dict:
        st = self._state()
        if on:
            st.update(manual=True, override_until=0, since=time.time())
        else:
            st.update(manual=None)
            if self.scheduled_now():  # "I'm home" during the scheduled window: stay off until it ends
                st["override_until"] = time.time() + 16 * 3600
        self.db.set("away_state", st)
        log.info("Away mode %s (%s).", "ON" if on else "OFF", source)
        return self.summary_since(st.get("since", 0)) if not on else {"ok": True, "away": True}

    def summary_since(self, since: float) -> dict:
        # clips include ~3 s from before the sound started, so allow a little slack
        rows = self.db.all("SELECT * FROM security_events WHERE time >= ? ORDER BY time", (since - 10,))
        voices = sum(1 for r in rows if r["kind"] == "voices")
        return {"ok": True, "away": False, "recordings_while_away": len(rows), "with_voices": voices,
                "with_other_sounds": len(rows) - voices}

    # ---------------------------------------------------------------- listening
    def process(self, chunk: np.ndarray) -> None:
        """Feed every 80 ms microphone chunk while away mode is on."""
        self._audio_clock += CHUNK_S
        now = self._audio_clock
        x = chunk.astype(np.float32)
        rms = float(np.sqrt(np.mean(x * x)))
        peak = float(np.max(np.abs(x)))
        if self._floor is None:
            self._floor, self._peak_floor = max(rms, 30.0), max(peak, 200.0)
        sens = float(self.settings.get("security", "sensitivity"))
        factor = 1.6 + (10 - sens) * 0.45
        min_abs = 60 + (10 - sens) * 25
        loud = rms > self._floor * factor and rms > min_abs
        click = peak > self._peak_floor * factor * 1.5 and peak > min_abs * 6
        speech = bool(self.is_speech(chunk)) if self.is_speech is not None else False
        triggered = loud or click or speech
        if not triggered:  # slowly learn the room's normal background noise
            self._floor = 0.995 * self._floor + 0.005 * max(rms, 20.0)
            self._peak_floor = 0.995 * self._peak_floor + 0.005 * max(peak, 150.0)
        else:
            self._triggers.append(now)
            self._last_trigger = now
            if speech:
                self._speech_hits.append(now)

        if self._clip is None:
            self._pre.append(chunk)
            recent = sum(1 for t in self._triggers if now - t <= 3.0)
            recent_speech = sum(1 for t in self._speech_hits if now - t <= 1.5)
            if recent >= 3 or recent_speech >= 2:
                self._clip = list(self._pre)
                self._clip_started = now - len(self._pre) * CHUNK_S
                self._clip_wall = time.time() - len(self._pre) * CHUNK_S
                self._clip_speech = recent_speech
                self._pre.clear()
            return
        self._clip.append(chunk)
        if speech:
            self._clip_speech += 1
        if now - self._last_trigger >= 6.0 or now - self._clip_started >= 120:
            clip, speech_chunks, started = self._clip, self._clip_speech, self._clip_wall
            self._clip = None
            threading.Thread(target=self._save, args=(clip, speech_chunks, started), daemon=True).start()

    def _save(self, chunks: list[np.ndarray], speech_chunks: int, started: float) -> None:
        audio = np.concatenate(chunks)
        duration = len(audio) / RATE
        kind = "voices" if speech_chunks >= 4 else "sounds"
        when = datetime.fromtimestamp(started)
        folder = self.folder / when.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (when.strftime("%H-%M-%S") + ".ogg")
        try:
            import soundfile as sf
            try:
                sf.write(str(path), audio, RATE, format="OGG", subtype="VORBIS")
            except Exception:
                path = path.with_suffix(".flac")
                sf.write(str(path), audio, RATE, format="FLAC")
        except Exception as exc:
            log.error("Couldn't save security recording: %s", exc)
            return
        transcript = ""
        if kind == "voices" and self.stt is not None and self.settings.get("security", "transcribe"):
            try:
                transcript = self.stt.transcribe(audio.astype(np.float32) / 32768.0)
            except Exception as exc:
                log.warning("Couldn't transcribe security clip: %s", exc)
        peak = float(np.max(np.abs(audio))) / 32768.0
        rid = self.db.execute("INSERT INTO security_events(time, kind, duration, path, transcript, peak) VALUES(?,?,?,?,?,?)",
                              (started, kind, duration, str(path), transcript, peak))
        log.info("Away mode: recorded %s (%.0f s) -> %s", kind, duration, path)
        with self._lock:
            self._unsent.append({"id": rid, "time": started, "kind": kind, "duration": duration,
                                 "path": str(path), "transcript": transcript})
        self._send_alerts()

    def _send_alerts(self) -> None:
        every = float(self.settings.get("security", "alert_every_minutes")) * 60
        with self._lock:
            if not self._unsent or time.time() - self._last_alert < every:
                return
            batch, self._unsent = self._unsent, []
            self._last_alert = time.time()
        style = self.settings.get("general", "time_format")
        first = batch[0]
        voices = any(b["kind"] == "voices" for b in batch)
        what = "voices" if voices else "sounds (like typing, clicking or movement)"
        at = tp.say_time(datetime.fromtimestamp(first["time"]), style)
        msg = f"Picking up human activity near your computer: {what}, starting {at}."
        if len(batch) > 1:
            msg += f" {len(batch)} recordings so far."
        heard = [b["transcript"] for b in batch if b["transcript"]]
        if heard:
            msg += f' I heard: "{heard[0][:200]}"'
        attach = first["path"] if self.settings.get("security", "attach_audio") else None
        if ctx.notifier is not None:
            ctx.notifier.send(msg, title="Jarvis security alert", kind="security", priority=4, tags="rotating_light",
                              attachment=attach)
        for b in batch:
            self.db.execute("UPDATE security_events SET notified=1 WHERE id=?", (b["id"],))

    # ---------------------------------------------------------------- housekeeping
    def tick(self, now: float) -> None:
        away = self.away()
        if away != self._was_away:
            self._was_away = away
            st = self._state()
            if away:
                st["since"] = now
                self.db.set("away_state", st)
                self._floor = None
            elif self._clip is not None:
                clip, self._clip = self._clip, None
                threading.Thread(target=self._save, args=(clip, self._clip_speech, self._clip_wall), daemon=True).start()
            log.info("Away mode is now %s.", "on" if away else "off")
        if self._unsent:
            self._send_alerts()

    def cleanup(self, now: float) -> None:
        days = float(self.settings.get("security", "keep_days"))
        for r in self.db.all("SELECT * FROM security_events WHERE time < ?", (now - days * 86400,)):
            Path(r["path"]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM security_events WHERE id=?", (r["id"],))

    def delete(self, event_id: int | None = None) -> int:
        rows = self.db.all("SELECT * FROM security_events" + (" WHERE id=?" if event_id else ""),
                           (event_id,) if event_id else ())
        for r in rows:
            Path(r["path"]).unlink(missing_ok=True)
            self.db.execute("DELETE FROM security_events WHERE id=?", (r["id"],))
        return len(rows)

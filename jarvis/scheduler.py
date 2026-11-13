"""Checks once a second for alarms, timers, reminders and events that are due."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import timeparse as tp
from .context import ctx

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self._stop = threading.Event()
        self._warned: set[str] = set()
        self._last_cleanup = 0.0

    def start(self) -> None:
        threading.Thread(target=self._run, name="scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.tick(time.time())
            except Exception:
                log.exception("Scheduler problem")

    # ------------------------------------------------------------------
    def tick(self, now: float) -> None:
        self._timers(now)
        self._alarms(now)
        self._reminders(now)
        self._event_warnings(now)
        self._briefing(now)
        if ctx.security is not None:
            ctx.security.tick(now)
        if now - self._last_cleanup > 3600:
            self._last_cleanup = now
            self._cleanup(now)

    def _away(self) -> bool:
        return ctx.security is not None and ctx.security.away()

    def _timers(self, now: float) -> None:
        for t in self.db.all("SELECT * FROM timers WHERE ends <= ?", (now,)):
            self.db.execute("DELETE FROM timers WHERE id=?", (t["id"],))
            name = t["name"] or "timer"
            text = f"Your {name} is done." if name.lower().endswith("timer") else f"Your {name} timer is done."
            log.info("Timer done: %s", name)
            if self._away():
                if ctx.notifier:
                    ctx.notifier.send(text, kind="alarms", tags="hourglass")
                continue
            if ctx.ringer:
                ctx.ringer.ring("timer", name, t["id"], seconds=float(self.settings.get("alarms", "timer_ring_seconds")),
                                speak=text)

    def _alarms(self, now: float) -> None:
        from .tools.alarms import schedule_next
        rows = self.db.all("SELECT * FROM alarms WHERE enabled=1 AND "
                           "((snooze_until IS NOT NULL AND snooze_until <= ?) OR (snooze_until IS NULL AND next_fire <= ?))",
                           (now, now))
        for a in rows:
            snooze_ended = a["snooze_until"] is not None
            if snooze_ended and a["next_fire"] > now:
                self.db.execute("UPDATE alarms SET snooze_until=NULL WHERE id=?", (a["id"],))
            else:
                nxt = schedule_next(a, now)  # None for one-time alarms
                if nxt is None:
                    self.db.execute("UPDATE alarms SET enabled=0, snooze_until=NULL WHERE id=?", (a["id"],))
                else:
                    self.db.execute("UPDATE alarms SET next_fire=?, snooze_until=NULL WHERE id=?", (nxt, a["id"]))
            when = tp.say_time(datetime.fromtimestamp(now).astimezone(), self.settings.get("general", "time_format"))
            label = a["label"] or "Alarm"
            log.info("Alarm ringing: %s", label)
            if self._away():
                if ctx.notifier:
                    ctx.notifier.send(f"{label} rang at {when} while you were away.", kind="alarms", tags="alarm_clock")
                continue
            if ctx.ringer:
                speak = f"It's {when}." + (f" {a['label']}." if a["label"] else "")
                ctx.ringer.ring("alarm", label, a["id"], speak=speak)

    def _reminders(self, now: float) -> None:
        from .tools.reminders import next_repeat
        nag_every = float(self.settings.get("reminders", "nag_minutes")) * 60
        for r in self.db.all("SELECT * FROM reminders WHERE active=1 AND next_fire IS NOT NULL AND next_fire <= ?", (now,)):
            nxt = next_repeat(r["next_fire"], r["repeat"])
            if r["needs_confirmation"]:
                self.db.execute("UPDATE reminders SET awaiting=1, nag_count=0, next_nag=?, next_fire=? WHERE id=?",
                                (now + nag_every, nxt, r["id"]))
                extra = " Let me know when you've done it."
            else:
                self.db.execute("UPDATE reminders SET next_fire=?, active=? WHERE id=?",
                                (nxt, 1 if nxt else 0, r["id"]))
                extra = ""
            if ctx.announcer:
                ctx.announcer.say(f"Reminder: {r['text']}.{extra}", push_kind="reminders")
        nag_max = int(self.settings.get("reminders", "nag_max"))
        for r in self.db.all("SELECT * FROM reminders WHERE awaiting=1 AND next_nag <= ?", (now,)):
            count = r["nag_count"] + 1
            if count > nag_max:
                self.db.execute("UPDATE reminders SET awaiting=0, next_nag=NULL, active=? WHERE id=?",
                                (1 if r["next_fire"] else 0, r["id"]))
                if ctx.notifier:
                    ctx.notifier.send(f"You didn't confirm: {r['text']}", kind="reminders", tags="warning")
                continue
            self.db.execute("UPDATE reminders SET nag_count=?, next_nag=? WHERE id=?", (count, now + nag_every, r["id"]))
            if ctx.announcer:
                ctx.announcer.say(f"Just checking: did you {r['text'].rstrip('.')}? Tell me when it's done.",
                                  push_kind="reminders")

    def _event_warnings(self, now: float) -> None:
        mins = float(self.settings.get("reminders", "event_heads_up_minutes"))
        if mins <= 0 or ctx.announcer is None:
            return
        horizon = now + mins * 60
        events = [dict(e, source="jarvis") for e in
                  self.db.all("SELECT * FROM events WHERE reminded=0 AND start > ? AND start <= ?", (now, horizon))]
        if ctx.gcal is not None:
            events += [e for e in ctx.gcal.upcoming_cached() if now < e["start"] <= horizon and not e.get("all_day")]
        for e in events:
            key = f"{e['source']}:{e['id']}:{e['start']}"
            if key in self._warned:
                continue
            self._warned.add(key)
            if e["source"] == "jarvis":
                self.db.execute("UPDATE events SET reminded=1 WHERE id=?", (e["id"],))
            left = max(1, round((e["start"] - now) / 60))
            ctx.announcer.say(f"Heads up: {e['title']} starts in {left} minute{'s' * (left != 1)}.", push_kind="reminders")

    def _briefing(self, now: float) -> None:
        r = ctx.ringer
        if r is None or r.pending_briefing is None or r.is_ringing():
            return
        if now - r.pending_briefing < 4:
            return
        if ctx.voice is not None and ctx.voice.status.state in ("listening", "thinking", "speaking"):
            return
        r.pending_briefing = None
        r.briefing_done_date = datetime.now().date().isoformat()
        from .briefing import build_briefing
        threading.Thread(target=lambda: ctx.announcer and ctx.announcer.say(build_briefing(self.settings)),
                         daemon=True).start()

    def _cleanup(self, now: float) -> None:
        days = float(self.settings.get("history", "keep_days"))
        self.db.execute("DELETE FROM history WHERE time < ?", (now - days * 86400,))
        self.db.execute("UPDATE alarms SET enabled=0 WHERE enabled=1 AND days='' AND snooze_until IS NULL AND next_fire < ?",
                        (now - 3600,))
        if ctx.security is not None:
            ctx.security.cleanup(now)

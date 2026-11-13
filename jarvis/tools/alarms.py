"""Alarms and timers."""
from __future__ import annotations

import difflib
import time
from datetime import datetime

from .. import timeparse as tp
from ..context import ctx
from . import tool


def _style(settings) -> str:
    return settings.get("general", "time_format")


def _fmt_alarm(a: dict, settings) -> str:
    when = datetime.fromtimestamp(a["snooze_until"] or a["next_fire"]).astimezone()
    days = [int(d) for d in a["days"].split(",") if d != ""]
    label = f" '{a['label']}'" if a["label"] else ""
    return f"#{a['id']}{label}: {tp.say_when(when, style=_style(settings))} ({tp.say_days(days)})"


def schedule_next(alarm: dict, now: float | None = None) -> float | None:
    """After an alarm fires: next time for repeating alarms, None for one-time alarms."""
    days = [int(d) for d in alarm["days"].split(",") if d != ""]
    if not days:
        return None
    after = max(now or time.time(), alarm["next_fire"] or 0)
    return tp.next_occurrence(alarm["hour"], alarm["minute"], days,
                              datetime.fromtimestamp(after).astimezone()).timestamp()


@tool(
    "alarm",
    "Alarms. action: 'set' (needs time; optional days like 'weekdays', 'every day', 'mon wed fri', and label), "
    "'list', 'cancel' (alarm_id, or time/label, or 'all' in label), 'snooze' (optional minutes), 'stop'.",
    {
        "action": {"type": "string", "enum": ["set", "list", "cancel", "snooze", "stop"]},
        "time": {"type": "string", "description": "When, in the user's words, e.g. '6:30 am', 'tomorrow at 7'"},
        "days": {"type": "string", "description": "Repeat days, e.g. 'weekdays'. Empty = one time."},
        "label": {"type": "string", "description": "Optional name, e.g. 'gym'"},
        "alarm_id": {"type": "integer"},
        "minutes": {"type": "number", "description": "Snooze minutes"},
    },
    ["action"],
)
def alarm(settings, action: str, time: str = "", days: str = "", label: str = "", alarm_id: int = 0,
          minutes: float = 0):
    db = ctx.db
    now = tp.now_local()
    if action == "set":
        if not time:
            return {"error": "What time should the alarm be for?"}
        when = tp.parse_when(time, now)
        day_list = tp.parse_days(days)
        fire = tp.next_occurrence(when.hour, when.minute, day_list, now) if day_list else when
        new_id = db.execute("INSERT INTO alarms(label, hour, minute, days, next_fire, enabled, created) VALUES(?,?,?,?,?,1,?)",
                            (label.strip(), when.hour, when.minute, ",".join(map(str, day_list)), fire.timestamp(),
                             now.timestamp()))
        return {"ok": True, "alarm_id": new_id, "rings": tp.say_when(fire, now, _style(settings)),
                "repeats": tp.say_days(day_list), "time_from_now": tp.say_duration(fire.timestamp() - now.timestamp())}
    if action == "list":
        rows = db.all("SELECT * FROM alarms WHERE enabled=1 ORDER BY COALESCE(snooze_until, next_fire)")
        return {"alarms": [_fmt_alarm(a, settings) for a in rows] or "no alarms set"}
    if action == "cancel":
        rows = db.all("SELECT * FROM alarms WHERE enabled=1")
        if alarm_id:
            rows = [a for a in rows if a["id"] == alarm_id]
        elif label.strip().lower() == "all":
            pass
        elif label or time:
            matches = []
            for a in rows:
                if label and difflib.SequenceMatcher(None, label.lower(), (a["label"] or "").lower()).ratio() > 0.6:
                    matches.append(a)
                elif time:
                    try:
                        w = tp.parse_when(time, now)
                        if (w.hour, w.minute) == (a["hour"], a["minute"]):
                            matches.append(a)
                    except ValueError:
                        pass
            rows = matches
        elif len(rows) > 1:
            return {"error": "Which alarm?", "alarms": [_fmt_alarm(a, settings) for a in rows]}
        if not rows:
            return {"error": "I couldn't find that alarm."}
        for a in rows:
            db.execute("UPDATE alarms SET enabled=0 WHERE id=?", (a["id"],))
        return {"ok": True, "cancelled": [_fmt_alarm(a, settings) for a in rows]}
    if action in ("snooze", "stop"):
        ringer = ctx.ringer
        info = ringer.ringing if ringer else None
        if info is None and ringer and ringer.last_rang and time_since(ringer.last_rang.get("stopped")) < 900:
            info = ringer.last_rang
        if info is None:
            return {"error": "Nothing is ringing right now."}
        if ringer.ringing:
            ringer.stop(reason="snooze" if action == "snooze" else "user")
        if action == "stop":
            return {"ok": True, "stopped": info["kind"]}
        mins = float(minutes or settings.get("alarms", "snooze_minutes"))
        until = time_now() + mins * 60
        if info["kind"] == "alarm" and info.get("id"):
            db.execute("UPDATE alarms SET snooze_until=?, enabled=1 WHERE id=?", (until, info["id"]))
        else:  # snoozing a timer = a new timer
            db.execute("INSERT INTO timers(name, duration, ends, created) VALUES(?,?,?,?)",
                       (info["label"], mins * 60, until, time_now()))
        ringer.pending_briefing = None
        return {"ok": True, "snoozed_minutes": mins}
    return {"error": f"unknown action {action}"}


def time_now() -> float:
    return time.time()


def time_since(ts: float | None) -> float:
    return time.time() - ts if ts else 1e9


def _find_timers(name: str) -> list[dict]:
    rows = ctx.db.all("SELECT * FROM timers ORDER BY ends")
    if not name or name.lower() in ("all", "timer", "the timer"):
        return rows
    scored = [(difflib.SequenceMatcher(None, name.lower(), (t["name"] or "").lower()).ratio(), t) for t in rows]
    good = [t for s, t in scored if s > 0.55]
    return good


@tool(
    "timer",
    "Countdown timers (several at once, each can have a name). action: 'start' (duration like '10 minutes', "
    "optional name like 'pasta'), 'status' (time left), 'cancel' (name, or all), 'add_time' (name + duration).",
    {
        "action": {"type": "string", "enum": ["start", "status", "cancel", "add_time"]},
        "duration": {"type": "string", "description": "e.g. '10 minutes', 'an hour and a half'"},
        "name": {"type": "string", "description": "Optional timer name"},
    },
    ["action"],
)
def timer(settings, action: str, duration: str = "", name: str = ""):
    db = ctx.db
    now = time.time()
    name = (name or "").strip()
    if action == "start":
        secs = tp.parse_duration(duration)
        label = name or tp.say_duration(secs)
        tid = db.execute("INSERT INTO timers(name, duration, ends, created) VALUES(?,?,?,?)", (label, secs, now + secs, now))
        return {"ok": True, "timer_id": tid, "name": label, "length": tp.say_duration(secs)}
    timers = _find_timers(name)
    if action == "status":
        if not timers:
            return {"timers": "no timers running"}
        return {"timers": [{"name": t["name"], "time_left": tp.say_duration(max(0, t["ends"] - now))} for t in timers]}
    if action == "cancel":
        if not timers:
            return {"error": "I couldn't find that timer."}
        if len(timers) > 1 and name and name.lower() != "all":
            timers = timers[:1]
        elif len(timers) > 1 and not name:
            return {"error": "Which timer?", "timers": [t["name"] for t in timers]}
        for t in timers:
            db.execute("DELETE FROM timers WHERE id=?", (t["id"],))
        return {"ok": True, "cancelled": [t["name"] for t in timers]}
    if action == "add_time":
        if not timers:
            return {"error": "I couldn't find that timer."}
        secs = tp.parse_duration(duration)
        t = timers[0]
        db.execute("UPDATE timers SET ends=ends+?, duration=duration+? WHERE id=?", (secs, secs, t["id"]))
        return {"ok": True, "name": t["name"], "time_left": tp.say_duration(t["ends"] + secs - now)}
    return {"error": f"unknown action {action}"}

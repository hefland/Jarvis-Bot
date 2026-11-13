"""Calendar: Jarvis's own calendar on this PC, plus Google Calendar if connected."""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta

from .. import timeparse as tp
from ..context import ctx
from . import tool


def day_range(text: str, now: datetime) -> tuple[datetime, datetime, str]:
    t = (text or "today").lower().strip()
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if t in ("today", "now", ""):
        return now if "rest" in t else start_today, start_today + timedelta(days=1), "today"
    if t == "tomorrow":
        return start_today + timedelta(days=1), start_today + timedelta(days=2), "tomorrow"
    if "next week" in t:
        s = start_today + timedelta(days=7 - now.weekday())
        return s, s + timedelta(days=7), "next week"
    if "week" in t:
        return start_today, start_today + timedelta(days=7 - now.weekday()), "this week"
    if "weekend" in t:
        s = start_today + timedelta(days=(5 - now.weekday()) % 7)
        return s, s + timedelta(days=2), "this weekend"
    if re.search(r"next (\d+|few|couple) days|coming days|upcoming", t):
        return now, start_today + timedelta(days=8), "the next 7 days"
    d = tp.parse_when(t, now).replace(hour=0, minute=0, second=0, microsecond=0)
    return d, d + timedelta(days=1), f"{tp.DAY_NAMES[d.weekday()]}, {d.strftime('%B')} {d.day}"


def all_events(start: datetime, end: datetime) -> list[dict]:
    rows = ctx.db.all("SELECT * FROM events WHERE start < ? AND end > ? ORDER BY start",
                      (end.timestamp(), start.timestamp()))
    events = [dict(r, source="jarvis", all_day=False) for r in rows]
    if ctx.gcal is not None and ctx.gcal.enabled():
        try:
            events += ctx.gcal.events(start, end)
        except Exception as exc:
            events.append({"title": f"(couldn't read Google Calendar: {exc})", "start": start.timestamp(),
                           "end": start.timestamp(), "source": "error", "all_day": True, "id": 0, "location": ""})
    return sorted(events, key=lambda e: e["start"])


def describe(e: dict, now: datetime, style: str) -> str:
    st = datetime.fromtimestamp(e["start"]).astimezone()
    when = "all day" if e.get("all_day") else tp.say_time(st, style)
    loc = f" at {e['location']}" if e.get("location") else ""
    return f"{when}: {e['title']}{loc}"


@tool(
    "calendar",
    "The user's calendar and schedule. action 'schedule' with day ('today', 'tomorrow', 'friday', 'this week', "
    "'next week', 'this weekend', a date). action 'add' with title + when (e.g. 'tuesday at 2pm') + optional "
    "duration_minutes + location. action 'delete' with title (and optional day).",
    {
        "action": {"type": "string", "enum": ["schedule", "add", "delete"]},
        "day": {"type": "string"},
        "title": {"type": "string"},
        "when": {"type": "string"},
        "duration_minutes": {"type": "number"},
        "location": {"type": "string"},
    },
    ["action"],
)
def calendar(settings, action: str, day: str = "", title: str = "", when: str = "", duration_minutes: float = 60,
             location: str = ""):
    now = tp.now_local()
    style = settings.get("general", "time_format")
    if action == "schedule":
        start, end, label = day_range(day, now)
        events = all_events(start, end)
        if not events:
            return {"day": label, "events": "nothing scheduled"}
        by_day: dict[str, list[str]] = {}
        for e in events:
            d = datetime.fromtimestamp(e["start"]).astimezone()
            key = tp.say_when(d, now, style).split(" at ")[0]
            by_day.setdefault(key, []).append(describe(e, now, style))
        return {"day": label, "events": by_day}
    if action == "add":
        if not title or not when:
            return {"error": "I need a title and a time for the event."}
        start = tp.parse_when(when, now)
        end = start + timedelta(minutes=float(duration_minutes or 60))
        use_google = ctx.gcal is not None and ctx.gcal.enabled() and settings.get("reminders", "new_events_go_to") == "google"
        if use_google:
            ctx.gcal.add(title, start, end, location)
            where = "Google Calendar"
        else:
            ctx.db.execute("INSERT INTO events(title, start, end, location, created) VALUES(?,?,?,?,?)",
                           (title, start.timestamp(), end.timestamp(), location, now.timestamp()))
            where = "Jarvis calendar"
        return {"ok": True, "title": title, "when": tp.say_when(start, now, style), "saved_to": where}
    if action == "delete":
        if day:
            start, end, _ = day_range(day, now)
        else:
            start, end = now - timedelta(days=1), now + timedelta(days=90)
        events = [e for e in all_events(start, end) if e["source"] in ("jarvis", "google")]
        scored = sorted(((difflib.SequenceMatcher(None, title.lower(), e["title"].lower()).ratio()
                          + (0.4 if title.lower() in e["title"].lower() else 0), e) for e in events),
                        key=lambda x: -x[0])
        if not scored or scored[0][0] < 0.5:
            return {"error": "I couldn't find that event."}
        e = scored[0][1]
        if e["source"] == "google":
            ctx.gcal.delete(e["id"])
        else:
            ctx.db.execute("DELETE FROM events WHERE id=?", (e["id"],))
        return {"ok": True, "deleted": e["title"],
                "was": tp.say_when(datetime.fromtimestamp(e["start"]).astimezone(), now, style)}
    return {"error": f"unknown action {action}"}

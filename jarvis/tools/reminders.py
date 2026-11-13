"""Reminders, including repeating ones and ones that need a 'yes, I did it' (like medication)."""
from __future__ import annotations

import calendar as _cal
import difflib
import re
import time
from datetime import datetime, timedelta

from .. import timeparse as tp
from ..context import ctx
from . import tool


def normalize_repeat(text: str) -> str:
    t = (text or "").lower().strip()
    if not t or t in ("none", "no", "once", "one time"):
        return "none"
    m = re.search(r"every\s+(\d+|[a-z]+)\s+(hour|minute)", tp.words_to_numbers(t))
    if m and m.group(1).isdigit():
        return f"every {m.group(1)} {m.group(2)}s"
    if re.search(r"hourly|every hour", t):
        return "every 1 hours"
    if re.search(r"month", t):
        return "monthly"
    if re.search(r"week(?!day|end)", t) and not tp.parse_days(t.replace("week", "")):
        return "weekly"
    days = tp.parse_days(t)
    if days == list(range(7)):
        return "daily"
    if days:
        return "days:" + ",".join(map(str, days))
    return "none"


def say_repeat(rep: str) -> str:
    if rep == "none":
        return "once"
    if rep.startswith("days:"):
        return tp.say_days([int(d) for d in rep[5:].split(",")])
    return rep.replace("1 hours", "hour").replace("daily", "every day").replace("weekly", "every week") \
        .replace("monthly", "every month")


def next_repeat(fired: float, rep: str) -> float | None:
    dt = datetime.fromtimestamp(fired).astimezone()
    if rep == "none":
        return None
    if rep == "daily":
        return (dt + timedelta(days=1)).timestamp()
    if rep == "weekly":
        return (dt + timedelta(days=7)).timestamp()
    if rep == "monthly":
        month = dt.month % 12 + 1
        year = dt.year + (dt.month == 12)
        day = min(dt.day, _cal.monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day).timestamp()
    if rep.startswith("days:"):
        days = [int(d) for d in rep[5:].split(",")]
        return tp.next_occurrence(dt.hour, dt.minute, days, dt).timestamp()
    m = re.match(r"every (\d+) (hour|minute)s", rep)
    if m:
        step = int(m.group(1)) * (3600 if m.group(2) == "hour" else 60)
        nxt = fired + step
        while nxt <= time.time():
            nxt += step
        return nxt
    return None


def _match(rows: list[dict], text: str) -> list[dict]:
    if not text:
        return rows
    scored = [(difflib.SequenceMatcher(None, text.lower(), r["text"].lower()).ratio()
               + (0.4 if text.lower() in r["text"].lower() else 0), r) for r in rows]
    scored.sort(key=lambda x: -x[0])
    return [r for s, r in scored if s >= 0.5][:1]


@tool(
    "reminder",
    "Reminders. action 'set': text (what to remind) + when ('in 20 minutes', 'tomorrow at 5pm', '8am') + optional "
    "repeat ('daily', 'weekdays', 'every 4 hours', 'mondays', 'weekly', 'monthly') + needs_confirmation=true for things "
    "like medication (I'll keep asking until the user says they did it). "
    "'list'. 'cancel' (text or reminder_id). 'confirm' = the user says they did it / took it.",
    {
        "action": {"type": "string", "enum": ["set", "list", "cancel", "confirm"]},
        "text": {"type": "string"},
        "when": {"type": "string"},
        "repeat": {"type": "string"},
        "needs_confirmation": {"type": "boolean"},
        "reminder_id": {"type": "integer"},
    },
    ["action"],
)
def reminder(settings, action: str, text: str = "", when: str = "", repeat: str = "",
             needs_confirmation: bool = False, reminder_id: int = 0):
    db = ctx.db
    now = tp.now_local()
    style = settings.get("general", "time_format")
    if action == "set":
        if not text:
            return {"error": "What should I remind you about?"}
        if not when:
            return {"error": "When should I remind you?"}
        fire = tp.parse_when(when, now)
        rep = normalize_repeat(repeat)
        if rep.startswith("days:") and fire.weekday() not in [int(d) for d in rep[5:].split(",")]:
            fire = datetime.fromtimestamp(next_repeat(fire.timestamp(), rep)).astimezone()
        rid = db.execute("INSERT INTO reminders(text, next_fire, repeat, needs_confirmation, active, created) "
                         "VALUES(?,?,?,?,1,?)", (text.strip(), fire.timestamp(), rep, int(bool(needs_confirmation)),
                                                 now.timestamp()))
        return {"ok": True, "reminder_id": rid, "text": text, "when": tp.say_when(fire, now, style),
                "repeats": say_repeat(rep), "will_ask_for_confirmation": bool(needs_confirmation)}
    rows = db.all("SELECT * FROM reminders WHERE active=1 ORDER BY next_fire")
    if action == "list":
        out = []
        for r in rows:
            when_txt = tp.say_when(datetime.fromtimestamp(r["next_fire"]).astimezone(), now, style) if r["next_fire"] else "waiting for you to confirm"
            out.append(f"#{r['id']} {r['text']} -- {when_txt} ({say_repeat(r['repeat'])})")
        return {"reminders": out or "no reminders"}
    if action == "cancel":
        target = [r for r in rows if r["id"] == reminder_id] if reminder_id else \
            (rows if text.strip().lower() == "all" else _match(rows, text))
        if not target:
            return {"error": "I couldn't find that reminder.", "reminders": [r["text"] for r in rows]}
        for r in target:
            db.execute("UPDATE reminders SET active=0, awaiting=0 WHERE id=?", (r["id"],))
        return {"ok": True, "cancelled": [r["text"] for r in target]}
    if action == "confirm":
        waiting = [r for r in rows if r["awaiting"]]
        target = _match(waiting, text) if text and len(waiting) > 1 else waiting[-1:]
        if not target:
            return {"ok": True, "note": "Nothing was waiting for confirmation."}
        r = target[0]
        db.execute("UPDATE reminders SET awaiting=0, nag_count=0, next_nag=NULL, active=? WHERE id=?",
                   (1 if r["repeat"] != "none" else 0, r["id"]))
        return {"ok": True, "confirmed": r["text"]}
    return {"error": f"unknown action {action}"}

"""The morning briefing: time, weather, today's schedule, reminders, word of the day, headlines."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import timeparse as tp
from .context import ctx
from .tools import tool

log = logging.getLogger(__name__)


def _part(fn):
    try:
        return fn()
    except Exception as exc:
        log.warning("Briefing part failed: %s", exc)
        return ""


def build_briefing(settings) -> str:
    now = tp.now_local()
    g = settings.section("general")
    style = g["time_format"]
    hello = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
    parts = [f"{hello}{', ' + g['user_name'] if g['user_name'] else ''}. It's {tp.say_time(now, style)} on "
             f"{now.strftime('%A, %B')} {now.day}."]

    def weather():
        if not g["location"]:
            return ""
        from .tools.weather import get_weather
        w = get_weather(settings)
        today = w["forecast"][0]
        return (f"Right now it's {w['now']['temperature']} and {w['now']['conditions']}. Today's high is "
                f"{today['high']}, low {today['low']}, with a {today['chance_of_rain']} chance of rain.")

    def schedule():
        from .tools.calendar import all_events
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = [e for e in all_events(start, start + timedelta(days=1)) if e["source"] != "error"]
        if not events:
            return "Your calendar is clear today."
        items = []
        for e in events[:5]:
            st = datetime.fromtimestamp(e["start"]).astimezone()
            items.append(e["title"] + ("" if e.get("all_day") else f" at {tp.say_time(st, style)}"))
        n = len(events)
        return f"You have {n} thing{'s' * (n != 1)} today: " + "; ".join(items) + "."

    def reminders():
        end = now.replace(hour=23, minute=59).timestamp()
        rows = ctx.db.all("SELECT * FROM reminders WHERE active=1 AND next_fire BETWEEN ? AND ? ORDER BY next_fire",
                          (now.timestamp(), end))
        if not rows:
            return ""
        items = [f"{r['text']} at {tp.say_time(datetime.fromtimestamp(r['next_fire']).astimezone(), style)}" for r in rows[:4]]
        return "Reminders for today: " + "; ".join(items) + "."

    def word():
        if ctx.tutor is None or not settings.get("learning", "tutor_enabled"):
            return ""
        w = ctx.tutor.word_of_the_day()
        return f"Your word of the day is {w['word']}. It means {w['definition']}." if w else ""

    def news():
        if not settings.get("alarms", "briefing_news"):
            return ""
        from .tools.news import headlines
        items = headlines(settings, "", 3)
        return ("In the news: " + " ... ".join(i["title"] for i in items) + ".") if items else ""

    for fn in (weather, schedule, reminders, word, news):
        text = _part(fn)
        if text:
            parts.append(text)
    return " ".join(parts)


@tool("briefing", "Give the user their daily briefing (time, weather, schedule, reminders, word of the day, headlines). "
                  "Use when they ask for their briefing, a rundown of their day, or 'what's my day look like'.")
def briefing(settings):
    if ctx.ringer is not None:  # they asked for it, so don't play the automatic one after the alarm too
        from datetime import date
        ctx.ringer.pending_briefing = None
        ctx.ringer.briefing_done_date = date.today().isoformat()
    return {"say_this": build_briefing(settings)}

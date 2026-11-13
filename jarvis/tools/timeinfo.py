"""Time and date tools (exact, from the computer's clock)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import geo, tool


def fmt_time(dt: datetime, style: str) -> str:
    if style == "24h":
        return dt.strftime("%H:%M")
    return dt.strftime("%I:%M %p").lstrip("0")


def fmt_date(dt: datetime | date) -> str:
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


@tool(
    "get_time",
    "Get the current time and date. Leave 'location' empty for the user's home. "
    "Give a city for the time somewhere else, e.g. 'Tokyo'.",
    {"location": {"type": "string", "description": "Optional city name for another time zone."}},
)
def get_time(settings, location: str = ""):
    style = settings.get("general", "time_format")
    if location and location.strip().lower() not in ("here", "home", "local"):
        place = geo.geocode(location)
        now = datetime.now(ZoneInfo(place["timezone"]))
        return {"place": place["label"], "time": fmt_time(now, style), "date": fmt_date(now),
                "timezone": place["timezone"]}
    now = datetime.now().astimezone()
    return {"time": fmt_time(now, style), "date": fmt_date(now), "timezone": str(now.tzname())}


@tool(
    "date_difference",
    "Count exactly how many days are between two dates (e.g. 'how many days until Christmas', "
    "'how old is someone born on ...'). Dates in YYYY-MM-DD format. Leave 'start' empty for today.",
    {"start": {"type": "string", "description": "YYYY-MM-DD, empty = today"},
     "end": {"type": "string", "description": "YYYY-MM-DD"}},
    ["end"],
)
def date_difference(settings, end: str, start: str = ""):
    d0 = date.fromisoformat(start) if start else date.today()
    d1 = date.fromisoformat(end)
    days = (d1 - d0).days
    years = d1.year - d0.year - ((d1.month, d1.day) < (d0.month, d0.day))
    weeks, rem = divmod(abs(days), 7)
    return {"from": fmt_date(d0), "to": fmt_date(d1), "days": days, "weeks_and_days": f"{weeks} weeks {rem} days",
            "whole_years": years, "weekday_of_end": d1.strftime("%A")}

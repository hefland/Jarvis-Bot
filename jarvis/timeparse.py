"""Understands times and durations the way people say them.

    parse_duration("an hour and a half")        -> 5400 seconds
    parse_when("tomorrow at 7:30")              -> datetime
    parse_when("in 20 minutes") / ("5pm") / ("next tuesday at 2") / ("noon")
    parse_days("weekdays")                       -> [0, 1, 2, 3, 4]
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

ONES = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
        "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
        "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
        "ninety": 90}
FUZZY = {"couple": 2, "few": 3}
UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400, "week": 604800, "weeks": 604800,
}
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
            "sunday": 6, "sun": 6}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def now_local() -> datetime:
    return datetime.now().astimezone()


def words_to_numbers(text: str) -> str:
    """'twenty five minutes' -> '25 minutes'; 'seven thirty' -> '7 30'."""
    out: list[str] = []
    for tok in re.findall(r"[a-z]+|\d+(?:\.\d+)?|[^\sa-z\d]+", text.lower()):
        val = ONES.get(tok, TENS.get(tok))
        if tok == "oh":
            val = None if not (out and out[-1].isdigit()) else 0
        if val is None:
            out.append(tok)
            continue
        prev = out[-1] if out else ""
        if prev.isdigit() and int(prev) >= 20 and int(prev) % 10 == 0 and val < 10 and tok in ONES:
            out[-1] = str(int(prev) + val)
        else:
            out.append(str(val) if tok != "oh" else "oh")
    return " ".join(out)


# ---------------------------------------------------------------- durations
def parse_duration(text: str) -> float:
    """Return seconds. Raises ValueError if no duration found."""
    t = words_to_numbers(str(text)).replace("-", " ")
    t = re.sub(r"(\d+(?:\.\d+)?)([a-z]+)", r"\1 \2", t)  # 5m -> 5 m
    tokens = re.findall(r"\d+(?:\.\d+)?|[a-z]+", t)
    total, num, num_is_article, last_unit, seen_unit = 0.0, None, False, None, False

    def unit_ahead(i: int) -> bool:
        for tok in tokens[i + 1:]:
            if tok in UNITS:
                return True
            if re.match(r"\d", tok) or tok in ONES or tok in TENS:
                return False
        return False

    for i, tok in enumerate(tokens):
        if re.match(r"\d", tok):
            num, num_is_article = float(tok), False
        elif tok in FUZZY:
            num, num_is_article = float(FUZZY[tok]), False
        elif tok in ("a", "an"):
            if num is None:
                num, num_is_article = 1.0, True
        elif tok in ("half", "quarter"):
            frac = 0.5 if tok == "half" else 0.25
            if unit_ahead(i):
                num = frac if (num is None or num_is_article) else num + frac
                num_is_article = False
            elif last_unit:
                total += frac * last_unit
                num = None
        elif tok in UNITS:
            if tok == "m" and i + 1 < len(tokens) and tokens[i + 1] == "s":
                continue
            u = UNITS[tok]
            total += (num if num is not None else 1.0) * u
            last_unit, num, seen_unit = u, None, True
    if num is not None and not num_is_article:  # trailing number: "1 hour 30" or just "10"
        total += num * (last_unit / 60 if last_unit and last_unit >= 60 else 60) if seen_unit else num * 60
    if total <= 0:
        raise ValueError(f"I couldn't understand the length of time '{text}'")
    return total


def say_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' * (seconds != 1)}"
    parts = []
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    for val, name in ((d, "day"), (h, "hour"), (m, "minute")):
        if val:
            parts.append(f"{val} {name}{'s' * (val != 1)}")
    if s and not d and not h:
        parts.append(f"{s} second{'s' * (s != 1)}")
    return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + " and " + parts[-1]


# ---------------------------------------------------------------- clock times
def parse_clock(text: str) -> tuple[int, int, str | None] | None:
    """Find a clock time. Returns (hour, minute, 'am'/'pm'/None) -- hour is 0-23 if am/pm known."""
    t = words_to_numbers(text)
    t = re.sub(r"(\d)\s*:\s*(\d)", r"\1:\2", t)
    t = re.sub(r"(\d{1,2}) oh (\d)\b", r"\1:0\2", t)
    t = t.replace("a.m.", "am").replace("p.m.", "pm").replace("a. m.", "am").replace("p. m.", "pm")
    t = re.sub(r"\b([ap]) m\b", r"\1m", t)
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return 12, 0, "pm"
    if re.search(r"\bmidnight\b", t):
        return 0, 0, "am"
    ampm = None
    if re.search(r"\bam\b|\bin the morning\b|\bthis morning\b|\bmorning\b", t):
        ampm = "am"
    if re.search(r"\bpm\b|\bin the (afternoon|evening)\b|\bat night\b|\btonight\b|\bevening\b|\bafternoon\b", t):
        ampm = "pm"
    m = re.search(r"\b(half|quarter|\d{1,2}) (past|after|to|till|before) (\d{1,2})\b", t)
    if m:
        mins = 30 if m.group(1) == "half" else 15 if m.group(1) == "quarter" else int(m.group(1))
        hour = int(m.group(3))
        if m.group(2) in ("to", "till", "before"):
            hour, mins = (hour - 1) % 24, 60 - mins
        return _apply_ampm(hour, mins, ampm)
    m = re.search(r"\b(\d{1,2})(?::|\s)(\d{2})\b(?!\s*(?:minutes?|mins?|seconds?|hours?))", t)
    if m and int(m.group(2)) < 60:
        return _apply_ampm(int(m.group(1)), int(m.group(2)), ampm)
    m = re.search(r"\b(\d{3,4})\s*(am|pm)\b", t)  # "730pm"
    if m:
        v = int(m.group(1))
        return _apply_ampm(v // 100, v % 100, m.group(2))
    m = re.search(r"\b(\d{1,2})\s*(?:o'?clock|am|pm)\b", t) or re.search(r"(?:^|\bat )(\d{1,2})\b(?!\s*(?:minutes?|mins?|hours?|days?|seconds?|st|nd|rd|th|/))", t)
    if m:
        return _apply_ampm(int(m.group(1)), 0, ampm)
    return None


def _apply_ampm(hour: int, minute: int, ampm: str | None):
    if hour > 23 or minute > 59:
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 12:
        ampm = ampm or "pm"
    return hour, minute, ampm


def _resolve_hour(hour: int, ampm: str | None, base: datetime, now: datetime, minute: int,
                  day_given: bool) -> datetime:
    if ampm is not None or hour == 0 or hour > 12:
        return base.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if day_given:  # "tomorrow at 3" -> 3 pm; "tomorrow at 8" -> 8 am
        h = hour % 12 + 12 if 1 <= hour <= 6 else hour
        return base.replace(hour=h, minute=minute, second=0, microsecond=0)
    # time only: the next time the clock shows this (am or pm)
    options = [base.replace(hour=hour % 12, minute=minute, second=0, microsecond=0),
               base.replace(hour=hour % 12 + 12, minute=minute, second=0, microsecond=0)]
    options += [o + timedelta(days=1) for o in options]
    return min(o for o in options if o > now)


# ---------------------------------------------------------------- full dates
def parse_when(text: str, now: datetime | None = None) -> datetime:
    """Turn 'tomorrow at 5', 'in 20 minutes', 'next tuesday 2pm', '2026-12-25 09:00' into a datetime."""
    now = now or now_local()
    raw = str(text).strip()
    t = raw.lower().strip(" .?!")
    try:  # ISO format from the AI model
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=now.tzinfo)
    except ValueError:
        pass
    t = re.sub(r"\bfrom now\b|\blater\b", "", t).strip()
    m = re.match(r"^(?:in|after)\s+(.+)$", t)
    if m:
        return now + timedelta(seconds=parse_duration(m.group(1)))

    day = None
    day_given = False
    if re.search(r"\bday after tomorrow\b", t):
        day, day_given = now + timedelta(days=2), True
    elif re.search(r"\btomorrow\b|\btmrw\b", t):
        day, day_given = now + timedelta(days=1), True
    elif re.search(r"\btoday\b|\btonight\b|\bthis (morning|afternoon|evening)\b", t):
        day, day_given = now, True
    else:
        m = re.search(r"\b(next|this|on)?\s*(" + "|".join(WEEKDAYS) + r")\b", t)
        if m:
            target = WEEKDAYS[m.group(2)]
            ahead = (target - now.weekday()) % 7
            if m.group(1) == "next" and ahead == 0:
                ahead = 7
            day, day_given = now + timedelta(days=ahead), True
            if ahead == 0:
                day_given = "weekday-today"

    if day is None and re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b|\d{1,2}/\d{1,2}", t):
        return _dateparser(raw, now)

    clock = parse_clock(t)
    if day is not None:
        if clock is None:
            hour, minute, ampm = 9, 0, "am"
            for word, h in (("morning", 9), ("afternoon", 15), ("evening", 19), ("tonight", 20), ("night", 21)):
                if word in t:
                    hour, ampm = h, ("am" if h < 12 else "pm")
        else:
            hour, minute, ampm = clock
        if "tonight" in t and ampm is None and hour < 12:
            hour, ampm = hour + 12, "pm"
        dt = _resolve_hour(hour, ampm, day, now, minute, True)
        if day_given == "weekday-today" and dt <= now:
            dt += timedelta(days=7)
        return dt
    if clock is not None:
        hour, minute, ampm = clock
        dt = _resolve_hour(hour, ampm, now, now, minute, False)
        if dt <= now:
            dt += timedelta(days=1)
        return dt
    return _dateparser(raw, now)


def _dateparser(text: str, now: datetime) -> datetime:
    try:
        import dateparser
    except ImportError as exc:
        raise ValueError(f"I couldn't understand the time '{text}'") from exc
    dt = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now.replace(tzinfo=None),
                                          "RETURN_AS_TIMEZONE_AWARE": False})
    if dt is None:
        raise ValueError(f"I couldn't understand the time '{text}'")
    if dt.hour == 0 and dt.minute == 0 and not re.search(r"midnight|12\s*am|00:00", text.lower()):
        dt = dt.replace(hour=9)
    return dt.replace(tzinfo=now.tzinfo)


# ---------------------------------------------------------------- repeating days
def parse_days(text: str | None) -> list[int]:
    """'weekdays' -> [0..4], 'every day' -> [0..6], 'mon wed fri' -> [0,2,4], '' -> [] (one time)."""
    t = (text or "").lower()
    if not t.strip() or t.strip() in ("once", "one time", "none", "no"):
        return []
    if re.search(r"every ?day|daily|each day|all days", t):
        return list(range(7))
    days: set[int] = set()
    if "weekday" in t or "work day" in t or "workday" in t:
        days |= {0, 1, 2, 3, 4}
    if "weekend" in t:
        days |= {5, 6}
    for word in re.findall(r"[a-z]+", t):
        word = word.rstrip("s") if word.endswith("days") else word
        if word in WEEKDAYS:
            days.add(WEEKDAYS[word])
    return sorted(days)


def say_days(days: list[int]) -> str:
    if not days:
        return "once"
    if days == list(range(7)):
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "on weekdays"
    if days == [5, 6]:
        return "on weekends"
    return "on " + ", ".join(DAY_NAMES[d] + "s" for d in days)


def next_occurrence(hour: int, minute: int, days: list[int], after: datetime) -> datetime:
    cand = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for i in range(8):
        d = cand + timedelta(days=i)
        if d > after and (not days or d.weekday() in days):
            return d
    return cand + timedelta(days=1)


def say_time(dt: datetime, style: str = "12h") -> str:
    if style == "24h":
        return dt.strftime("%H:%M")
    return dt.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def say_when(dt: datetime, now: datetime | None = None, style: str = "12h") -> str:
    now = now or now_local()
    days = (dt.date() - now.date()).days
    if days == 0:
        day = "today"
    elif days == 1:
        day = "tomorrow"
    elif 1 < days < 7:
        day = DAY_NAMES[dt.weekday()]
    else:
        day = f"{DAY_NAMES[dt.weekday()]}, {dt.strftime('%B')} {dt.day}"
    return f"{day} at {say_time(dt, style)}"

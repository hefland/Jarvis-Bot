"""Away mode tool."""
from __future__ import annotations

from ..context import ctx
from . import tool


@tool(
    "away_mode",
    "Security away mode. 'on' when the user is leaving the house / going to work ('I'm leaving', 'watch the place'). "
    "'off' when they're back. 'status' to check. While on, Jarvis stays silent and records sounds near the PC.",
    {"action": {"type": "string", "enum": ["on", "off", "status"]}},
    ["action"],
)
def away_mode(settings, action: str):
    sec = ctx.security
    if sec is None:
        return {"error": "Security monitor isn't running."}
    if action == "on":
        sec.set_away(True, "voice")
        return {"ok": True, "away_mode": "on", "say": "Away mode is on. I'll keep quiet and watch the place.",
                "phone_alerts": bool(ctx.notifier and ctx.notifier.enabled("security"))}
    if action == "off":
        return sec.set_away(False, "voice")
    return {"away_mode": "on" if sec.away() else "off", "scheduled": settings.get("security", "schedule_enabled")}

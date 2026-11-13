"""Music, sleep sound, and volume tools."""
from __future__ import annotations

from .. import media as m
from ..context import ctx
from . import tool


@tool(
    "music",
    "Music from the user's music folder, the sleep sound, and other players (Spotify etc). action: 'play' "
    "(query = song, artist, album or folder; empty = shuffle everything), 'sleep_sound' (plays their rain/sleep sound "
    "on a loop; minutes optional), 'pause', 'resume', 'next', 'previous', 'stop', 'volume' (value 0-100 for the music), "
    "'now_playing'.",
    {
        "action": {"type": "string", "enum": ["play", "sleep_sound", "pause", "resume", "next", "previous", "stop",
                                               "volume", "now_playing"]},
        "query": {"type": "string"},
        "value": {"type": "number"},
        "minutes": {"type": "number"},
    },
    ["action"],
)
def music(settings, action: str, query: str = "", value: float | None = None, minutes: float | None = None):
    p = ctx.media
    if p is None:
        return {"error": "The music player isn't running."}
    if action == "play":
        return p.play_music(query)
    if action == "sleep_sound":
        return p.play_sleep(minutes)
    if action == "now_playing":
        info = p.now_playing()
        if not info["playing"]:
            other = m.playerctl_status()
            return {"playing": bool(other), "other_player": other} if other else {"playing": False}
        return info
    if action in ("pause", "stop"):
        p.forget_paused()  # they stopped it on purpose: don't resume it when we finish talking
    if p.is_active():
        return p.control(action, value)
    if action in ("pause", "resume", "next", "previous", "stop") and m.playerctl(action):
        return {"ok": True, "action": action, "player": "other app"}
    return {"error": "Nothing is playing right now."}


@tool(
    "volume",
    "The computer's main speaker volume. action: 'up', 'down' (value = how much, default 10), 'set' (value 0-100), "
    "'mute', 'unmute', 'get'.",
    {"action": {"type": "string", "enum": ["up", "down", "set", "mute", "unmute", "get"]}, "value": {"type": "number"}},
    ["action"],
)
def volume(settings, action: str, value: float | None = None):
    return m.system_volume(action, value)

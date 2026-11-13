"""Open things in your web browser (Firefox or whatever is your default): YouTube videos, Pandora, websites.

YouTube search uses yt-dlp (open source, no account or API key needed).
"""
from __future__ import annotations

import difflib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import quote, quote_plus

from . import tool
from .pc import _gui_env

log = logging.getLogger(__name__)

SITES = {
    "youtube": "https://www.youtube.com", "pandora": "https://www.pandora.com", "netflix": "https://www.netflix.com",
    "gmail": "https://mail.google.com", "google": "https://www.google.com", "amazon": "https://www.amazon.com",
    "facebook": "https://www.facebook.com", "reddit": "https://www.reddit.com", "spotify": "https://open.spotify.com",
    "twitter": "https://x.com", "x": "https://x.com", "weather": "https://weather.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "calendar": "https://calendar.google.com", "hulu": "https://www.hulu.com",
    "twitch": "https://www.twitch.tv", "wikipedia": "https://en.wikipedia.org", "rumble": "https://rumble.com",
}
LATEST_WORDS = r"\b(the |most )?(latest|newest|new|recent|last|today'?s)\b|\bvideos?\b|\bepisodes?\b|\bupload(ed)?\b|\bfrom\b|\bby\b"


def open_url(url: str) -> None:
    subprocess.Popen(["xdg-open", url], env=_gui_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _browser_playing() -> bool:
    """Is a browser (Firefox, Chrome...) playing media right now? (asks the desktop's media controls)"""
    if not shutil.which("playerctl"):
        return False
    from ..media import _session_env
    env = _session_env()
    try:
        names = subprocess.run(["playerctl", "-l"], capture_output=True, text=True, timeout=3, env=env).stdout.split()
        for n in names:
            if n.split(".")[0] in ("firefox", "chromium", "chrome", "brave", "vivaldi", "opera", "plasma-browser-integration"):
                st = subprocess.run(["playerctl", "-p", n, "status"], capture_output=True, text=True, timeout=3,
                                    env=env).stdout.strip()
                if st == "Playing":
                    return True
    except Exception:
        pass
    return False


def press_play_when_loaded(wait: float = 7.0) -> str:
    """Make a just-opened YouTube video actually start.

    Browsers often block autoplay in a window that isn't in front. If the video hasn't started after a few
    seconds, bring the browser window forward and press YouTube's play key ('k'). We check first, so a video
    that already started is never paused by mistake."""
    end = time.time() + wait
    while time.time() < end:
        if _browser_playing():
            return "started by itself"
        time.sleep(0.5)
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or not shutil.which("xdotool"):
        return "couldn't press play (xdotool not available)"
    env = _gui_env()
    try:
        ids = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "YouTube"], capture_output=True,
                             text=True, timeout=5, env=env).stdout.split()
        if not ids:
            return "couldn't find the YouTube window"
        wid = ids[-1]
        subprocess.run(["xdotool", "windowactivate", "--sync", wid], capture_output=True, timeout=5, env=env)
        time.sleep(0.8)
        if _browser_playing():  # bringing it to the front was enough
            return "started when brought to the front"
        subprocess.run(["xdotool", "key", "--window", wid, "--clearmodifiers", "k"], capture_output=True, timeout=5, env=env)
        time.sleep(1.5)
        return "pressed play" if _browser_playing() else "pressed play (couldn't confirm)"
    except Exception as exc:
        return f"couldn't press play: {exc}"


def _ydl():
    from yt_dlp import YoutubeDL
    return YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": "in_playlist",
                      "noplaylist": True, "playlistend": 5})


def _entry_url(e: dict) -> str:
    vid = e.get("id")
    return f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url", "")


def youtube_find(query: str, latest: bool) -> dict:
    """Find a YouTube video. latest=True: the newest upload from the channel that best matches the query."""
    with _ydl() as ydl:
        if latest:
            name = re.sub(r"\s+", " ", re.sub(LATEST_WORDS, " ", query, flags=re.I)).strip() or query
            res = ydl.extract_info(f"ytsearch10:{name}", download=False)
            entries = [e for e in (res.get("entries") or []) if e]
            best, best_score = None, 0.0
            for e in entries:
                ch = (e.get("channel") or e.get("uploader") or "").lower()
                score = max(difflib.SequenceMatcher(None, name.lower(), ch).ratio(),
                            0.9 if ch and (name.lower() in ch or ch in name.lower()) else 0)
                if score > best_score:
                    best, best_score = e, score
            if best is not None and best_score >= 0.6:
                chan = best.get("channel_url") or (f"https://www.youtube.com/channel/{best['channel_id']}"
                                                   if best.get("channel_id") else "")
                if chan:
                    vids = ydl.extract_info(chan.rstrip("/") + "/videos", download=False)
                    newest = next((e for e in (vids.get("entries") or []) if e), None)
                    if newest:
                        return {"title": newest.get("title", ""), "channel": best.get("channel") or name,
                                "url": _entry_url(newest)}
            res = ydl.extract_info(f"ytsearchdate1:{name}", download=False)
        else:
            res = ydl.extract_info(f"ytsearch1:{query}", download=False)
        e = next((e for e in (res.get("entries") or []) if e), None)
        if not e:
            raise LookupError(f"I couldn't find a YouTube video for '{query}'.")
        return {"title": e.get("title", ""), "channel": e.get("channel") or e.get("uploader") or "", "url": _entry_url(e)}


@tool(
    "browser",
    "Open things in the web browser on the PC. action 'youtube': find and play a YouTube video (query = what to "
    "find; latest=true for 'the latest/newest X video'). Also use it for 'play X music on YouTube'. "
    "action 'pandora': open Pandora for an artist, genre or mood (query, e.g. 'cyberpunk'). "
    "action 'website': open a site (query = name like 'netflix' or an address like techcrunch.com). "
    "action 'search': a web search page for the query.",
    {
        "action": {"type": "string", "enum": ["youtube", "pandora", "website", "search"]},
        "query": {"type": "string"},
        "latest": {"type": "boolean"},
    },
    ["action", "query"],
)
def browser(settings, action: str, query: str, latest: bool = False):
    q = (query or "").strip()
    if action == "youtube":
        if not latest and re.search(r"\b(latest|newest|most recent|new)\b", q, re.I):
            latest = True
        try:
            found = youtube_find(q, latest)
        except ImportError:
            open_url(f"https://www.youtube.com/results?search_query={quote_plus(q)}")
            return {"ok": True, "note": "yt-dlp isn't installed, so I opened YouTube's search results instead."}
        except Exception as exc:
            log.warning("YouTube search failed: %s", exc)
            open_url(f"https://www.youtube.com/results?search_query={quote_plus(q)}")
            return {"ok": True, "note": "I couldn't pick a video automatically, so I opened YouTube's search results."}
        open_url(found["url"])

        def start_it():
            log.info("YouTube: %s", press_play_when_loaded())
        threading.Thread(target=start_it, daemon=True).start()
        return {"ok": True, "playing": found["title"], "channel": found["channel"]}
    if action == "pandora":
        open_url(f"https://www.pandora.com/search/{quote(q)}/all")
        return {"ok": True, "opened": f"Pandora search for {q}",
                "note": "Pandora doesn't let other programs start a station, so click the station you want."}
    if action == "website":
        key = q.lower().removeprefix("the ").strip()
        if key in SITES:
            url = SITES[key]
        elif re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", q):
            url = q if q.startswith("http") else f"https://{q}"
        else:  # jump straight to the best match
            url = f"https://duckduckgo.com/?q=!ducky+{quote_plus(q)}"
        open_url(url)
        return {"ok": True, "opened": url}
    if action == "search":
        open_url(f"https://duckduckgo.com/?q={quote_plus(q)}")
        return {"ok": True, "searched": q}
    return {"error": f"unknown action {action}"}

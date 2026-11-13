"""Drive time with live traffic, from TomTom (free key: developer.tomtom.com)."""
from __future__ import annotations

import difflib
import functools
from urllib.parse import quote

import httpx

from .. import timeparse as tp
from . import tool

BASE = "https://api.tomtom.com"


def _places(settings) -> dict[str, str]:
    t = settings.section("traffic")
    places = {}
    if t["home_address"]:
        places["home"] = t["home_address"]
    if t["work_address"]:
        places["work"] = t["work_address"]
    for line in t["places"].splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if k.strip() and v.strip():
                places[k.strip().lower()] = v.strip()
    return places


def _resolve(settings, name: str) -> str:
    places = _places(settings)
    key = (name or "").lower().strip()
    key = key.replace("my ", "").replace("the ", "").replace("office", "work").replace("job", "work").replace("house", "home")
    if key in places:
        return places[key]
    close = difflib.get_close_matches(key, list(places), n=1, cutoff=0.75)
    return places[close[0]] if close else name


@functools.lru_cache(maxsize=64)
def _geocode(key: str, address: str) -> tuple[float, float, str]:
    resp = httpx.get(f"{BASE}/search/2/geocode/{quote(address)}.json", params={"key": key, "limit": 1}, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        raise ValueError(f"I couldn't find the address '{address}'.")
    r = results[0]
    return r["position"]["lat"], r["position"]["lon"], r.get("address", {}).get("freeformAddress", address)


@tool(
    "traffic",
    "Driving time with current traffic. destination = 'work', 'home', a saved place name, or an address. "
    "origin defaults to home (or 'work' if they're going home).",
    {"destination": {"type": "string"}, "origin": {"type": "string"}},
    ["destination"],
    enabled=lambda s: bool(s.get("traffic", "tomtom_api_key")),
)
def traffic(settings, destination: str, origin: str = ""):
    key = settings.get("traffic", "tomtom_api_key")
    dest_addr = _resolve(settings, destination)
    if not origin:
        origin = "work" if destination.lower().strip() in ("home", "my house", "house") else "home"
    orig_addr = _resolve(settings, origin)
    if orig_addr.lower() in ("home", "work"):
        return {"error": f"Your {orig_addr} address isn't set (Settings > Traffic)."}
    a = _geocode(key, orig_addr)
    b = _geocode(key, dest_addr)
    resp = httpx.get(f"{BASE}/routing/1/calculateRoute/{a[0]},{a[1]}:{b[0]},{b[1]}/json",
                     params={"key": key, "traffic": "true", "travelMode": "car", "departAt": "now"}, timeout=20)
    resp.raise_for_status()
    s = resp.json()["routes"][0]["summary"]
    imperial = settings.get("general", "units") == "imperial"
    dist = s["lengthInMeters"] / (1609.344 if imperial else 1000)
    delay = s.get("trafficDelayInSeconds", 0)
    arrive = tp.now_local().timestamp() + s["travelTimeInSeconds"]
    from datetime import datetime
    return {
        "from": a[2], "to": b[2],
        "drive_time": tp.say_duration(s["travelTimeInSeconds"]),
        "traffic_delay": tp.say_duration(delay) if delay >= 60 else "no real delay",
        "distance": f"{dist:.1f} {'miles' if imperial else 'km'}",
        "arrive_around": tp.say_time(datetime.fromtimestamp(arrive).astimezone(), settings.get("general", "time_format")),
    }

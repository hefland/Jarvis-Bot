"""Turns a place name into coordinates + time zone using the free Open-Meteo geocoder (no account)."""
from __future__ import annotations

import functools

import httpx

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_transport: httpx.BaseTransport | None = None  # tests can replace this


class PlaceNotFound(LookupError):
    pass


def http_client(timeout: float = 15.0) -> httpx.Client:
    return httpx.Client(timeout=timeout, transport=_transport, headers={"User-Agent": "Jarvis-Bot/1.0"})


@functools.lru_cache(maxsize=128)
def geocode(place: str) -> dict:
    """Return {'name','lat','lon','timezone','label'} for a place like 'Dallas, TX' or 'Paris, France'."""
    parts = [p.strip() for p in place.split(",") if p.strip()]
    if not parts:
        raise PlaceNotFound("no place given")
    city = parts[0]
    hint = parts[1] if len(parts) > 1 else ""
    hint_full = US_STATES.get(hint.upper(), hint).lower()
    with http_client() as client:
        resp = client.get(GEOCODE_URL, params={"name": city, "count": 10, "language": "en", "format": "json"})
        resp.raise_for_status()
        results = resp.json().get("results") or []
    if not results:
        raise PlaceNotFound(f"I couldn't find a place called '{place}'")
    best = results[0]
    if hint_full:
        for r in results:
            fields = [str(r.get(k, "")).lower() for k in ("admin1", "admin2", "country", "country_code")]
            if any(f == hint_full or (len(hint_full) > 2 and f.startswith(hint_full)) for f in fields):
                best = r
                break
    label = ", ".join(x for x in (best.get("name"), best.get("admin1"), best.get("country")) if x)
    return {"name": best.get("name"), "lat": best["latitude"], "lon": best["longitude"],
            "timezone": best.get("timezone") or "UTC", "label": label}

"""Weather from Open-Meteo (free, no account, no API key)."""
from __future__ import annotations

from . import geo, tool

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
    82: "violent showers", 85: "light snow showers", 86: "heavy snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}


@tool(
    "get_weather",
    "Get current weather and the forecast. Leave 'location' empty for the user's home. "
    "'days' = how many days of forecast (1 = today only, up to 7).",
    {"location": {"type": "string", "description": "Optional city, e.g. 'Chicago, IL'"},
     "days": {"type": "integer", "description": "1-7, default 1"}},
)
def get_weather(settings, location: str = "", days: int = 1):
    place_name = (location or "").strip() or settings.get("general", "location")
    if not place_name:
        return {"error": "No home location is set. Ask the user to set it on the settings page "
                         "(General > Home location), or to name a city."}
    place = geo.geocode(place_name)
    imperial = settings.get("general", "units") == "imperial"
    days = max(1, min(7, int(days or 1)))
    params = {
        "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto", "forecast_days": days,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,precipitation",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
        "temperature_unit": "fahrenheit" if imperial else "celsius",
        "wind_speed_unit": "mph" if imperial else "kmh",
        "precipitation_unit": "inch" if imperial else "mm",
    }
    with geo.http_client() as client:
        resp = client.get(FORECAST_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    t_unit = "°F" if imperial else "°C"
    w_unit = "mph" if imperial else "km/h"
    cur = data.get("current", {})
    out = {
        "place": place["label"],
        "now": {
            "conditions": WMO.get(cur.get("weather_code"), "unknown"),
            "temperature": f"{round(cur.get('temperature_2m', 0))}{t_unit}",
            "feels_like": f"{round(cur.get('apparent_temperature', 0))}{t_unit}",
            "humidity": f"{cur.get('relative_humidity_2m')}%",
            "wind": f"{round(cur.get('wind_speed_10m', 0))} {w_unit}",
        },
        "forecast": [],
    }
    daily = data.get("daily", {})
    for i, day in enumerate(daily.get("time", [])):
        out["forecast"].append({
            "date": day,
            "conditions": WMO.get(daily["weather_code"][i], "unknown"),
            "high": f"{round(daily['temperature_2m_max'][i])}{t_unit}",
            "low": f"{round(daily['temperature_2m_min'][i])}{t_unit}",
            "chance_of_rain": f"{daily['precipitation_probability_max'][i]}%",
            "sunrise": daily["sunrise"][i][-5:], "sunset": daily["sunset"][i][-5:],
        })
    return out

"""The smart_home tool: lights (Govee) and anything in Home Assistant."""
from __future__ import annotations

import difflib
import re

from .. import smarthome as sh
from . import tool


def _enabled(settings) -> bool:
    return settings.get("smarthome", "govee_mode") != "off" or bool(settings.get("smarthome", "homeassistant_url"))


def _apply(settings, lights, action: str, value: str) -> list[str]:
    done = []
    rgb, kelvin = sh.parse_color(value) if action in ("color", "set") else (None, None)
    m = re.search(r"(\d{1,3})\s*%?", value or "")
    pct = int(m.group(1)) if m and action in ("brightness", "set") else None
    for light in lights:
        if action == "on":
            light.power(settings, True)
        elif action == "off":
            light.power(settings, False)
        else:
            if rgb or kelvin:
                light.color(settings, rgb, kelvin)
            if pct is not None:
                light.brightness(settings, pct)
            if action == "brightness" and pct is None:
                raise ValueError("What brightness (1 to 100)?")
            if action == "color" and not (rgb or kelvin):
                raise ValueError(f"I don't know the color '{value}'.")
        done.append(light.name)
    return done


def run_scene(settings, name: str) -> dict:
    scenes = sh.parse_scenes(settings.get("smarthome", "scenes"))
    match = difflib.get_close_matches(name.lower().replace(" mode", "").strip(), list(scenes), n=1, cutoff=0.5)
    if not match:
        return {"error": f"No scene called '{name}'.", "scenes": list(scenes)}
    lights = sh.configured_lights(settings)
    results = []
    for who, what in scenes[match[0]]:
        targets = sh.find_lights(lights, who)
        if what.lower().strip() == "off":
            results += _apply(settings, targets, "off", "")
        else:
            results += _apply(settings, targets, "set", what)
    return {"ok": True, "scene": match[0], "lights": sorted(set(results))}


@tool(
    "smart_home",
    "Control lights and smart devices. action: 'on', 'off', 'brightness' (value 1-100), 'color' (value like 'blue', "
    "'warm white', 'purple'), 'scene' (target = scene name like 'movie'), 'list' (what devices exist). "
    "target = device or room name, or 'all'.",
    {
        "action": {"type": "string", "enum": ["on", "off", "brightness", "color", "scene", "list"]},
        "target": {"type": "string", "description": "Light/device name, scene name, or 'all'"},
        "value": {"type": "string", "description": "Brightness % or color"},
    },
    ["action"],
    enabled=_enabled,
)
def smart_home(settings, action: str, target: str = "all", value: str = ""):
    if action == "scene":
        return run_scene(settings, target)
    lights = sh.configured_lights(settings)
    ha = sh.HomeAssistant(settings)
    if action == "list":
        out = {"lights": [l.name for l in lights],
               "scenes": list(sh.parse_scenes(settings.get("smarthome", "scenes")))}
        if ha.enabled():
            out["home_assistant"] = [f"{d['name']} ({d['state']})" for d in ha.devices()]
        return out
    targets = sh.find_lights(lights, target)
    if targets:
        done = _apply(settings, targets, action, value)
        return {"ok": True, "action": action, "value": value, "devices": done}
    if ha.enabled():
        dev = ha.find(target)
        if dev:
            rgb, kelvin = sh.parse_color(value) if action == "color" else (None, None)
            m = re.search(r"\d+", value or "")
            bright = int(m.group()) if m and action == "brightness" else None
            ha.act(dev["entity"], "on" if action in ("on", "color", "brightness") else "off", bright, rgb, kelvin)
            return {"ok": True, "action": action, "device": dev["name"]}
    names = [l.name for l in lights]
    return {"error": f"I couldn't find a device called '{target}'.", "known_lights": names or
            "none set up yet -- add them in Settings > Smart home"}

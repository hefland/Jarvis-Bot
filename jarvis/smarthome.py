"""Smart home control.

Govee LAN API (private, over your Wi-Fi): turn on "LAN Control" for each light in the Govee Home app.
  - discovery: UDP multicast to 239.255.255.250:4001, lights answer on port 4002
  - commands:  UDP to <light ip>:4003
Govee cloud API (for models without LAN control): needs an API key from the Govee Home app.
Home Assistant (optional): controls anything Home Assistant knows, via its REST API.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import socket
import time

import httpx

log = logging.getLogger(__name__)

COLORS = {
    "red": (255, 0, 0), "orange": (255, 110, 0), "yellow": (255, 220, 0), "green": (0, 255, 0), "lime": (150, 255, 0),
    "teal": (0, 200, 160), "cyan": (0, 255, 255), "blue": (0, 60, 255), "light blue": (100, 180, 255),
    "sky blue": (100, 180, 255), "navy": (0, 0, 140), "purple": (140, 0, 255), "violet": (160, 60, 255),
    "magenta": (255, 0, 200), "pink": (255, 90, 160), "hot pink": (255, 20, 147), "white": (255, 255, 255),
    "gold": (255, 180, 0), "amber": (255, 150, 0), "turquoise": (64, 224, 208), "lavender": (190, 150, 255),
}
KELVIN = {"warm white": 2700, "warm": 2700, "soft white": 3000, "cool white": 5000, "daylight": 6500, "candle": 2000}

GOVEE_CLOUD = "https://openapi.api.govee.com/router/api/v1"


def parse_color(text: str) -> tuple[tuple[int, int, int] | None, int | None]:
    t = (text or "").lower().strip()
    for name in sorted(KELVIN, key=len, reverse=True):
        if name in t:
            return None, KELVIN[name]
    m = re.search(r"#?([0-9a-f]{6})\b", t)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)), None
    for name in sorted(COLORS, key=len, reverse=True):
        if name in t:
            return COLORS[name], None
    close = difflib.get_close_matches(t, list(COLORS), n=1, cutoff=0.6)
    if close:
        return COLORS[close[0]], None
    return None, None


# ---------------------------------------------------------------- Govee LAN
def _lan_ip() -> str | None:
    """This PC's address on the home network (not Docker's or a VPN's)."""
    try:
        from .netwatch import lan_network
        net, _ = lan_network()
        return str(net.ip) if net else None
    except Exception:
        return None


def _listener(timeout: float) -> socket.socket:
    """Govee lights answer on UDP port 4002."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(("", 4002))
    except OSError as exc:
        sock.close()
        raise RuntimeError(f"Couldn't listen for Govee lights (UDP port 4002 is busy): {exc}") from exc
    sock.settimeout(timeout)
    return sock


def govee_discover(timeout: float = 3.0) -> list[dict]:
    """Ask every Govee light on the Wi-Fi to report itself (lights need 'LAN Control' turned on)."""
    found: dict[str, dict] = {}
    listener = _listener(0.3)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    lan_ip = _lan_ip()
    if lan_ip:  # send through the home network, not through Docker's or a VPN's network
        try:
            sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(lan_ip))
        except OSError:
            pass
    msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    try:
        end = time.time() + timeout
        next_send = 0.0
        while time.time() < end:
            if time.time() >= next_send:  # ask a few times; Wi-Fi sometimes drops one message
                sender.sendto(msg, ("239.255.255.250", 4001))
                next_send = time.time() + 1.0
            try:
                data, addr = listener.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                info = json.loads(data.decode())["msg"]
                if info.get("cmd") != "scan":
                    continue
                info = info["data"]
            except Exception:
                continue
            ip = info.get("ip", addr[0])
            found[ip] = {"ip": ip, "device": info.get("device", ""), "model": info.get("sku", "")}
    finally:
        listener.close()
        sender.close()
    return sorted(found.values(), key=lambda d: d["ip"])


def govee_status(ip: str, timeout: float = 2.0) -> dict | None:
    """Ask one light whether it's on, its brightness and color. None = no answer."""
    listener = _listener(timeout)
    try:
        govee_lan_send(ip, "devStatus", {})
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = listener.recvfrom(4096)
            except socket.timeout:
                break
            if addr[0] != ip:
                continue
            try:
                msg = json.loads(data.decode())["msg"]
            except Exception:
                continue
            if msg.get("cmd") == "devStatus":
                return msg.get("data", {})
    finally:
        listener.close()
    return None


def govee_blink(ip: str) -> dict | None:
    """Flash a light off and on so you can see which one it is."""
    status = govee_status(ip)
    was_on = (status or {}).get("onOff", 1)
    for value in (0, 1, 0, 1):
        govee_lan_send(ip, "turn", {"value": value})
        time.sleep(0.6)
    if not was_on:
        govee_lan_send(ip, "turn", {"value": 0})
    return status


def govee_lan_send(ip: str, cmd: str, data: dict) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, 4003))


# ---------------------------------------------------------------- Govee cloud
def _cloud(settings, method: str, path: str, body: dict | None = None) -> dict:
    key = settings.get("smarthome", "govee_api_key")
    if not key:
        raise RuntimeError("No Govee API key set (Settings > Smart home).")
    resp = httpx.request(method, GOVEE_CLOUD + path, headers={"Govee-API-Key": key, "Content-Type": "application/json"},
                         json=body, timeout=15)
    resp.raise_for_status()
    return resp.json()


def govee_cloud_devices(settings) -> list[dict]:
    data = _cloud(settings, "GET", "/user/devices")
    return [{"device": d["device"], "model": d["sku"], "name": d.get("deviceName", d["device"])}
            for d in data.get("data", [])]


def govee_cloud_control(settings, device: str, model: str, capability: dict) -> None:
    import uuid
    _cloud(settings, "POST", "/device/control",
           {"requestId": str(uuid.uuid4()), "payload": {"sku": model, "device": device, "capability": capability}})


# ---------------------------------------------------------------- devices by name
class Light:
    def __init__(self, name: str, ip: str = "", device: str = "", model: str = "", via: str = "lan"):
        self.name, self.ip, self.device, self.model, self.via = name, ip, device, model, via

    def power(self, settings, on: bool) -> None:
        if self.via == "lan":
            govee_lan_send(self.ip, "turn", {"value": 1 if on else 0})
        else:
            govee_cloud_control(settings, self.device, self.model,
                                {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": int(on)})

    def brightness(self, settings, pct: int) -> None:
        pct = max(1, min(100, int(pct)))
        if self.via == "lan":
            govee_lan_send(self.ip, "turn", {"value": 1})
            govee_lan_send(self.ip, "brightness", {"value": pct})
        else:
            govee_cloud_control(settings, self.device, self.model,
                                {"type": "devices.capabilities.range", "instance": "brightness", "value": pct})

    def color(self, settings, rgb, kelvin) -> None:
        if self.via == "lan":
            govee_lan_send(self.ip, "turn", {"value": 1})
            if kelvin:
                govee_lan_send(self.ip, "colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": kelvin})
            else:
                r, g, b = rgb
                govee_lan_send(self.ip, "colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0})
        else:
            if kelvin:
                cap = {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK", "value": kelvin}
            else:
                r, g, b = rgb
                cap = {"type": "devices.capabilities.color_setting", "instance": "colorRgb",
                       "value": (r << 16) + (g << 8) + b}
            govee_cloud_control(settings, self.device, self.model, cap)


def configured_lights(settings) -> list[Light]:
    mode = settings.get("smarthome", "govee_mode")
    if mode == "off":
        return []
    lights = []
    for line in settings.get("smarthome", "govee_names").splitlines():
        if "=" not in line:
            continue
        name, target = (p.strip() for p in line.split("=", 1))
        if not name or not target:
            continue
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", target) and "lan" in mode:
            lights.append(Light(name, ip=target))
        elif "cloud" in mode:
            lights.append(Light(name, device=target, via="cloud"))
    if "cloud" in mode and settings.get("smarthome", "govee_api_key"):
        named = {l.device for l in lights}
        try:
            for d in govee_cloud_devices(settings):
                existing = next((l for l in lights if l.via == "cloud" and l.device == d["device"]), None)
                if existing:
                    existing.model = d["model"]
                elif d["device"] not in named:
                    lights.append(Light(d["name"], device=d["device"], model=d["model"], via="cloud"))
        except Exception as exc:
            log.warning("Govee cloud device list failed: %s", exc)
    return lights


def find_lights(lights: list[Light], target: str) -> list[Light]:
    t = (target or "").lower().strip()
    if t in ("", "all", "lights", "the lights", "all lights", "all the lights", "everything", "every light"):
        return lights
    exact = [l for l in lights if l.name.lower() in t or t in l.name.lower()]
    if exact:
        return exact
    names = {l.name.lower(): l for l in lights}
    close = difflib.get_close_matches(t, list(names), n=2, cutoff=0.5)
    return [names[c] for c in close]


# ---------------------------------------------------------------- Home Assistant
class HomeAssistant:
    def __init__(self, settings):
        self.url = settings.get("smarthome", "homeassistant_url").rstrip("/")
        self.token = settings.get("smarthome", "homeassistant_token")

    def enabled(self) -> bool:
        return bool(self.url and self.token)

    def _req(self, method: str, path: str, body: dict | None = None):
        resp = httpx.request(method, f"{self.url}/api{path}", headers={"Authorization": f"Bearer {self.token}"},
                             json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def devices(self) -> list[dict]:
        keep = ("light.", "switch.", "fan.", "cover.", "media_player.", "climate.", "lock.", "scene.", "script.",
                "input_boolean.")
        return [{"entity": s["entity_id"], "name": s["attributes"].get("friendly_name", s["entity_id"]),
                 "state": s["state"]} for s in self._req("GET", "/states") if s["entity_id"].startswith(keep)]

    def find(self, target: str) -> dict | None:
        devs = self.devices()
        names = {d["name"].lower(): d for d in devs}
        t = target.lower()
        for n, d in names.items():
            if n == t or t == d["entity"]:
                return d
        close = difflib.get_close_matches(t, list(names), n=1, cutoff=0.55)
        if close:
            return names[close[0]]
        part = [d for n, d in names.items() if t in n or n in t]
        return part[0] if part else None

    def act(self, entity: str, action: str, brightness: int | None = None, rgb=None, kelvin=None) -> None:
        domain = entity.split(".")[0]
        if domain in ("scene", "script"):
            self._req("POST", f"/services/{domain}/turn_on", {"entity_id": entity})
            return
        if domain == "lock":
            self._req("POST", f"/services/lock/{'lock' if action == 'on' else 'unlock'}", {"entity_id": entity})
            return
        service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action, "turn_on")
        body: dict = {"entity_id": entity}
        if domain == "light" and service == "turn_on":
            if brightness is not None:
                body["brightness_pct"] = brightness
            if rgb:
                body["rgb_color"] = list(rgb)
            if kelvin:
                body["color_temp_kelvin"] = kelvin
        self._req("POST", f"/services/{domain if domain != 'input_boolean' else 'input_boolean'}/{service}", body)


def parse_scenes(text: str) -> dict[str, list[tuple[str, str]]]:
    """'Movie = TV strip: blue 20, Desk lamp: off' -> {'movie': [('TV strip','blue 20'), ('Desk lamp','off')]}"""
    scenes = {}
    for line in (text or "").splitlines():
        if "=" not in line:
            continue
        name, body = line.split("=", 1)
        steps = []
        for part in body.split(","):
            if ":" in part:
                who, what = part.split(":", 1)
                steps.append((who.strip(), what.strip()))
        if steps:
            scenes[name.strip().lower()] = steps
    return scenes

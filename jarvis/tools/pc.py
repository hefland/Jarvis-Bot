"""PC control: lock screen, open apps, system status, sleep/restart/shutdown (with confirmation)."""
from __future__ import annotations

import configparser
import difflib
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import tool

APP_DIRS = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            Path.home() / ".local/share/applications", Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications", Path("/var/lib/snapd/desktop/applications")]


def _gui_env() -> dict:
    """Jarvis runs as a background service; give launched apps the desktop's display."""
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    xauth = Path.home() / ".Xauthority"
    if "XAUTHORITY" not in env and xauth.exists():
        env["XAUTHORITY"] = str(xauth)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus")
    return env


def installed_apps() -> dict[str, str]:
    apps: dict[str, str] = {}
    for d in APP_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                cp.read(f, encoding="utf-8")
                entry = cp["Desktop Entry"]
            except Exception:
                continue
            if entry.get("NoDisplay", "false").lower() == "true" or entry.get("Type", "") != "Application":
                continue
            name = entry.get("Name", f.stem)
            apps[name.lower()] = f.stem
            for kw in entry.get("Keywords", "").split(";")[:3]:
                if kw.strip():
                    apps.setdefault(kw.strip().lower(), f.stem)
    return apps


def _run_first(cmds: list[list[str]]) -> bool:
    for cmd in cmds:
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, env=_gui_env(), capture_output=True, timeout=10)
            if r.returncode == 0:
                return True
    return False


def status() -> dict:
    import psutil
    out = {
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "memory_used": f"{psutil.virtual_memory().percent:.0f}%",
        "disk_free": f"{shutil.disk_usage('/').free / 1e9:.0f} GB",
        "uptime": f"{(time.time() - psutil.boot_time()) / 3600:.1f} hours",
    }
    try:
        temps = psutil.sensors_temperatures()
        for key in ("k10temp", "coretemp", "zenpower"):
            if key in temps and temps[key]:
                out["cpu_temp"] = f"{temps[key][0].current:.0f}°C"
                break
    except Exception:
        pass
    if shutil.which("nvidia-smi"):
        r = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            t, u, mu, mt = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            out["gpu"] = f"{t}°C, {u}% busy, {int(mu) / 1024:.1f} of {int(mt) / 1024:.0f} GB memory used"
    return out


@tool(
    "computer",
    "Control this PC. action: 'lock' (lock the screen), 'open_app' (name = app like 'firefox', 'files', 'calculator'), "
    "'status' (CPU, memory, disk, graphics card temperature), 'sleep', 'restart', 'shutdown'. For sleep/restart/shutdown "
    "ALWAYS ask the user to confirm first; only call with confirmed=true after they say yes.",
    {
        "action": {"type": "string", "enum": ["lock", "open_app", "status", "sleep", "restart", "shutdown"]},
        "name": {"type": "string"},
        "confirmed": {"type": "boolean"},
    },
    ["action"],
)
def computer(settings, action: str, name: str = "", confirmed: bool = False):
    if action == "status":
        return status()
    if action == "lock":
        ok = _run_first([["cinnamon-screensaver-command", "--lock"], ["loginctl", "lock-session"],
                         ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]])
        return {"ok": ok} if ok else {"error": "I couldn't lock the screen."}
    if action == "open_app":
        if not settings.get("pc", "allow_open_apps"):
            return {"error": "Opening apps is turned off in settings."}
        apps = installed_apps()
        q = name.lower().strip()
        match = apps.get(q)
        if not match:
            close = difflib.get_close_matches(q, list(apps), n=1, cutoff=0.6)
            part = [k for k in apps if q and q in k]
            key = close[0] if close else (part[0] if part else None)
            match = apps.get(key) if key else None
        if not match:
            return {"error": f"I couldn't find an app called '{name}'."}
        subprocess.Popen(["gtk-launch", match], env=_gui_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"ok": True, "opened": match}
    if action in ("sleep", "restart", "shutdown"):
        if not settings.get("pc", "allow_power"):
            return {"error": "Power controls are turned off in settings."}
        if not confirmed:
            return {"needs_confirmation": True, "ask_user": f"Are you sure you want me to {action} the computer?"}
        cmd = {"sleep": ["systemctl", "suspend"], "restart": ["systemctl", "reboot"],
               "shutdown": ["systemctl", "poweroff"]}[action]
        subprocess.Popen(["sh", "-c", f"sleep 4; {' '.join(cmd)}"], start_new_session=True)
        return {"ok": True, "doing": action, "in_seconds": 4}
    return {"error": f"unknown action {action}"}

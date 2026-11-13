"""Network watch: tells your phone when another device on your Wi-Fi connects to this PC,
and (optionally) when a brand-new device joins your network.

Uses the standard Linux tools `ss` and `ip` -- very light (one quick check every 5 seconds).
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import logging
import re
import socket
import subprocess
import threading
import time

from .context import ctx

log = logging.getLogger(__name__)

PORT_NAMES = {22: "remote login (SSH)", 139: "file sharing", 445: "file sharing (Samba)", 3389: "remote desktop",
              5900: "screen sharing (VNC)", 5901: "screen sharing (VNC)", 8765: "the Jarvis settings page",
              11434: "Ollama (AI model)", 8880: "Kokoro (voice)", 631: "printer sharing", 2049: "file sharing (NFS)",
              21: "FTP file transfer", 80: "a web server", 443: "a web server", 8080: "a web server",
              8123: "Home Assistant", 9090: "a web server", 1714: "KDE Connect / GSConnect", 1716: "KDE Connect / GSConnect"}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def _split_addr(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    host = host.strip("[]").replace("::ffff:", "")
    if "%" in host:
        host = host.split("%")[0]
    try:
        return host, int(port)
    except ValueError:
        return host, 0


def lan_network() -> tuple[ipaddress.IPv4Interface | None, str]:
    route = _run(["ip", "-4", "route", "show", "default"])
    m = re.search(r"\bdev (\S+)", route)
    if not m:
        return None, ""
    dev = m.group(1)
    addr = _run(["ip", "-4", "-o", "addr", "show", "dev", dev])
    m2 = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr)
    return (ipaddress.ip_interface(m2.group(1)), dev) if m2 else (None, dev)


def listening_ports() -> set[int]:
    ports = set()
    for line in _run(["ss", "-Htln"]).splitlines():
        parts = line.split()
        if len(parts) >= 4:
            ports.add(_split_addr(parts[3])[1])
    return ports


def established() -> list[tuple[int, str]]:
    """(local_port, peer_ip) for every open TCP connection."""
    out = []
    for line in _run(["ss", "-Htn", "state", "established"]).splitlines():
        parts = line.split()
        if len(parts) >= 4:
            _, lport = _split_addr(parts[2])
            peer, _ = _split_addr(parts[3])
            out.append((lport, peer))
    return out


def neighbors() -> dict[str, str]:
    """ip -> MAC address for devices this PC has seen on the network."""
    out = {}
    for line in _run(["ip", "neigh"]).splitlines():
        m = re.match(r"(\d+\.\d+\.\d+\.\d+) .*lladdr ([0-9a-f:]{17}) (\w+)", line)
        if m and m.group(3) not in ("FAILED", "INCOMPLETE"):
            out[m.group(1)] = m.group(2)
    return out


def device_name(ip: str) -> str:
    result: list[str] = []

    def lookup():
        try:
            result.append(socket.gethostbyaddr(ip)[0])
        except Exception:
            pass
    t = threading.Thread(target=lookup, daemon=True)
    t.start()
    t.join(2)
    return result[0] if result else ""


class NetWatch:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self._alerted: dict[tuple[str, int], float] = {}
        self._ports: set[int] = set()
        self._ports_time = 0.0
        self._last_sweep = 0.0
        self.recent: list[dict] = []

    def start(self) -> None:
        threading.Thread(target=self._run, name="netwatch", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                s = self.settings.section("security")
                if s["netwatch_enabled"]:
                    self.check_connections()
                if s["netwatch_new_devices"] and time.time() - self._last_sweep > 600:
                    self._last_sweep = time.time()
                    self.check_new_devices()
            except Exception:
                log.exception("Network watch problem")
            time.sleep(5)

    def _ignored(self) -> set[str]:
        return {x.strip() for x in self.settings.get("security", "netwatch_ignore").split(",") if x.strip()}

    def check_connections(self) -> list[dict]:
        net, _ = lan_network()
        if net is None:
            return []
        if time.time() - self._ports_time > 60:
            self._ports, self._ports_time = listening_ports(), time.time()
        ignore = self._ignored()
        jarvis_port = int(self.settings.get("web", "port"))
        cooldown = float(self.settings.get("security", "netwatch_cooldown_minutes")) * 60
        new = []
        for lport, peer in established():
            try:
                pip = ipaddress.ip_address(peer)
            except ValueError:
                continue
            if lport not in self._ports or pip not in net.network or pip == net.ip or peer in ignore:
                continue
            if lport == jarvis_port and self.settings.get("security", "netwatch_ignore_jarvis"):
                continue
            key = (peer, lport)
            if time.time() - self._alerted.get(key, 0) < cooldown:
                continue
            self._alerted[key] = time.time()
            name = device_name(peer)
            mac = neighbors().get(peer, "")
            what = PORT_NAMES.get(lport, f"port {lport}")
            who = f"{name} ({peer})" if name else peer
            msg = f"A device on your network, {who}, connected to your PC through {what}."
            if mac:
                msg += f" Device ID (MAC): {mac}."
            event = {"time": time.time(), "message": msg}
            self.recent = (self.recent + [event])[-50:]
            new.append(event)
            log.info("Network: %s", msg)
            if ctx.notifier is not None:
                ctx.notifier.send(msg, title="Jarvis network alert", kind="network", priority=4, tags="computer")
        return new

    def check_new_devices(self) -> list[dict]:
        net, _ = lan_network()
        if net is None or net.network.num_addresses > 1024:
            return []
        hosts = [str(h) for h in net.network.hosts() if h != net.ip]
        with cf.ThreadPoolExecutor(max_workers=64) as pool:  # gentle one-ping sweep so devices show up
            list(pool.map(lambda h: _run(["ping", "-c", "1", "-W", "1", h]), hosts))
        seen = neighbors()
        known: dict = self.db.get("known_devices", {})
        first_run = not self.db.get("netwatch_learned", False)
        found = []
        for ip, mac in seen.items():
            if mac in known:
                known[mac]["ip"], known[mac]["last_seen"] = ip, time.time()
                continue
            name = device_name(ip)
            known[mac] = {"ip": ip, "name": name, "first_seen": time.time(), "last_seen": time.time()}
            if not first_run and ip not in self._ignored():
                msg = f"A new device joined your Wi-Fi: {name + ' ' if name else ''}({ip}, MAC {mac})."
                found.append({"time": time.time(), "message": msg})
                log.info("Network: %s", msg)
                if ctx.notifier is not None:
                    ctx.notifier.send(msg, title="Jarvis network alert", kind="network", priority=3, tags="wifi")
        self.db.set("known_devices", known)
        if first_run:
            self.db.set("netwatch_learned", True)
            log.info("Network watch learned %d existing devices.", len(known))
        self.recent = (self.recent + found)[-50:]
        return found

"""Phone notifications through ntfy (free app for Android and iPhone: https://ntfy.sh).

You pick a secret "topic" name (like a password) and subscribe to it in the ntfy app.
Use the public server https://ntfy.sh, or run your own ntfy in Docker for full privacy.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings):
        self.settings = settings

    def enabled(self, kind: str | None = None) -> bool:
        n = self.settings.section("phone")
        if not (n["enabled"] and n["topic"].strip()):
            return False
        return True if kind is None else bool(n.get(f"send_{kind}", True))

    def send(self, message: str, title: str = "Jarvis", kind: str | None = None, priority: int = 3,
             tags: str = "", attachment: str | Path | None = None, wait: bool = False) -> bool:
        """Send a notification. kind = reminders/security/network/alarms (each can be switched off)."""
        if not self.enabled(kind):
            return False
        if wait:
            return self._send(message, title, priority, tags, attachment)
        threading.Thread(target=self._send, args=(message, title, priority, tags, attachment), daemon=True).start()
        return True

    def _send(self, message, title, priority, tags, attachment) -> bool:
        n = self.settings.section("phone")
        url = f"{n['server'].rstrip('/')}/{n['topic'].strip()}"
        headers = {"Title": _ascii(title), "Priority": str(priority)}
        if tags:
            headers["Tags"] = tags
        if n["token"]:
            headers["Authorization"] = f"Bearer {n['token']}"
        try:
            with httpx.Client(timeout=30) as client:
                if attachment and Path(attachment).exists():
                    headers["Filename"] = Path(attachment).name
                    headers["Message"] = _ascii(message)
                    resp = client.put(url, content=Path(attachment).read_bytes(), headers=headers)
                else:
                    resp = client.post(url, content=message.encode("utf-8"), headers=headers)
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.error("Phone notification failed: %s", exc)
            return False


def _ascii(text: str) -> str:
    """HTTP headers must be plain text."""
    return text.encode("ascii", "ignore").decode().strip() or "Jarvis"

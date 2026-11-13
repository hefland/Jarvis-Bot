"""Google Calendar connection (optional). Only calendar data goes to Google -- never your voice.

One-time setup (see README): make a free Google Cloud "Desktop app" credential, download the .json,
put its path in settings, then click 'Connect Google' on the Dashboard.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx

from .config import config_dir

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.readonly"]
API = "https://www.googleapis.com/calendar/v3"


class GoogleCalendar:
    def __init__(self, settings):
        self.settings = settings
        self.token_path = config_dir() / "google_token.json"
        self._creds = None
        self._lock = threading.Lock()
        self._cache: list[dict] = []
        self._cache_time = 0.0
        self.last_error = ""

    def enabled(self) -> bool:
        return bool(self.settings.get("reminders", "google_enabled")) and self.token_path.exists()

    def status(self) -> str:
        if not self.settings.get("reminders", "google_enabled"):
            return "off"
        if not self.token_path.exists():
            return "not connected"
        return "connected" if not self.last_error else f"error: {self.last_error}"

    def connect(self) -> str:
        """Opens a Google sign-in page in the browser on this PC. Blocks until done (max 5 minutes)."""
        from google_auth_oauthlib.flow import InstalledAppFlow
        secrets = self.settings.get("reminders", "google_credentials_file").strip()
        if not secrets or not Path(secrets).expanduser().exists():
            raise RuntimeError("Set 'Google credentials file' in Settings > Reminders & calendar first.")
        flow = InstalledAppFlow.from_client_secrets_file(str(Path(secrets).expanduser()), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300,
                                      success_message="Jarvis is connected to your Google Calendar. You can close this tab.")
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        self.token_path.chmod(0o600)
        self._creds = creds
        self.last_error = ""
        return "connected"

    def disconnect(self) -> None:
        self.token_path.unlink(missing_ok=True)
        self._creds = None

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        with self._lock:
            if self._creds is None:
                self._creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            if not self._creds.valid:
                self._creds.refresh(Request())
                self.token_path.write_text(self._creds.to_json())
            return self._creds.token

    def _call(self, method: str, path: str, **kw) -> dict:
        try:
            resp = httpx.request(method, API + path, headers={"Authorization": f"Bearer {self._token()}"},
                                 timeout=20, **kw)
            resp.raise_for_status()
            self.last_error = ""
            return resp.json() if resp.content else {}
        except Exception as exc:
            self.last_error = str(exc)[:200]
            raise

    def _cal_id(self) -> str:
        from urllib.parse import quote
        return quote(self.settings.get("reminders", "google_calendar_id") or "primary", safe="")

    def events(self, start: datetime, end: datetime) -> list[dict]:
        data = self._call("GET", f"/calendars/{self._cal_id()}/events", params={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
            "orderBy": "startTime", "maxResults": 100})
        out = []
        for e in data.get("items", []):
            s, en = e.get("start", {}), e.get("end", {})
            all_day = "date" in s
            st = datetime.fromisoformat(s.get("dateTime") or s["date"])
            et = datetime.fromisoformat(en.get("dateTime") or en.get("date") or s.get("dateTime") or s["date"])
            if st.tzinfo is None:
                st, et = st.astimezone(), et.astimezone()
            out.append({"id": e["id"], "title": e.get("summary", "(no title)"), "start": st.timestamp(),
                        "end": et.timestamp(), "location": e.get("location", ""), "all_day": all_day, "source": "google"})
        return out

    def add(self, title: str, start: datetime, end: datetime, location: str = "") -> str:
        body = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
        if location:
            body["location"] = location
        return self._call("POST", f"/calendars/{self._cal_id()}/events", json=body).get("id", "")

    def delete(self, event_id: str) -> None:
        self._call("DELETE", f"/calendars/{self._cal_id()}/events/{event_id}")

    def upcoming_cached(self) -> list[dict]:
        """Next 24 hours, refreshed every 5 minutes (for 'event starting soon' warnings)."""
        if self.enabled() and time.time() - self._cache_time > 300:
            self._cache_time = time.time()
            try:
                now = datetime.now().astimezone()
                self._cache = self.events(now, datetime.fromtimestamp(now.timestamp() + 86400).astimezone())
            except Exception as exc:
                log.warning("Google Calendar refresh failed: %s", exc)
        return self._cache

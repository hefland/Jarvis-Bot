"""Speaks things Jarvis starts on its own (reminders, timer done, event warnings, briefing).

Waits politely until you're not in the middle of talking to it. In away mode it stays silent
and sends the message to your phone instead.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

from .context import ctx

log = logging.getLogger(__name__)


class Announcer:
    def __init__(self, settings):
        self.settings = settings
        self.queue: queue.Queue[tuple[str, str | None, bool]] = queue.Queue()
        threading.Thread(target=self._run, name="announcer", daemon=True).start()

    def say(self, text: str, push_kind: str | None = None, chime: bool = True) -> None:
        """Announce out loud. push_kind ('reminders', ...) also sends it to the phone if enabled."""
        self.queue.put((text, push_kind, chime))

    def clear(self) -> None:
        """Drop announcements that haven't been spoken yet (cancel phrase)."""
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except Exception:
                break

    def _away(self) -> bool:
        return ctx.security is not None and ctx.security.away()

    def _run(self) -> None:
        while True:
            text, push_kind, chime = self.queue.get()
            try:
                away = self._away()
                if push_kind and ctx.notifier is not None and (away or push_kind == "reminders"):
                    ctx.notifier.send(text, title="Jarvis", kind=push_kind, tags="bell")
                if away:
                    log.info("Away mode: not speaking announcement: %s", text)
                    continue
                self._wait_until_free()
                log.info("Announcing: %s", text)
                if ctx.voice is not None:
                    ctx.voice.status.set(last_reply=text)
                if ctx.media is not None:
                    ctx.media.duck(True)
                try:
                    if chime:
                        from .audio import tones
                        from .audio.player import play
                        play(tones.notice(), tones.RATE, self.settings.get("tts", "output_device"),
                             float(self.settings.get("tts", "volume")))
                    if ctx.speaker is not None:
                        ctx.speaker.say(text)
                finally:
                    if ctx.media is not None:
                        ctx.media.duck(False)
            except Exception:
                log.exception("Announcement failed")

    def _wait_until_free(self, max_wait: float = 45.0) -> None:
        start = time.time()
        while time.time() - start < max_wait:
            busy = (ctx.voice is not None and ctx.voice.status.state in ("listening", "thinking", "speaking")) or \
                   (ctx.ringer is not None and ctx.ringer.is_ringing())
            if not busy:
                return
            time.sleep(0.5)

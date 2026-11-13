"""One place where the running parts of Jarvis can find each other.

Tools use `ctx.media`, `ctx.db`, etc. Anything that isn't running (e.g. in tests) is None.
"""
from __future__ import annotations

from typing import Any


class Context:
    settings: Any = None      # config.Settings
    db: Any = None            # storage.Database
    speaker: Any = None       # audio.tts.Speaker
    voice: Any = None         # assistant.VoiceAssistant (None with --no-voice)
    brain: Any = None         # brain.Brain
    announcer: Any = None     # announcer.Announcer
    ringer: Any = None        # ringer.Ringer
    media: Any = None         # media.MediaPlayer
    security: Any = None      # security.SecurityMonitor
    netwatch: Any = None      # netwatch.NetWatch
    tutor: Any = None         # tutor.Tutor
    gcal: Any = None          # gcal.GoogleCalendar
    notifier: Any = None      # notify.Notifier

    def reset(self) -> None:
        for name in list(vars(self)):
            setattr(self, name, None)


ctx = Context()

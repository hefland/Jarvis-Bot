"""Start Jarvis:  python -m jarvis   (or ./run.sh)

Options:
  --no-voice     don't use the microphone/speakers (settings page + typed chat only)
  --port N       use a different port for the settings page
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Private voice assistant")
    parser.add_argument("--no-voice", action="store_true", help="settings page and typed chat only")
    parser.add_argument("--port", type=int, help="port for the settings page")
    parser.add_argument("--open", action="store_true", help="open the settings page in your browser")
    parser.add_argument("--debug", action="store_true", help="more detailed logs")
    args = parser.parse_args()

    from .config import Settings, data_dir

    settings = Settings()
    port = args.port or int(settings.get("web", "port"))
    url = f"http://localhost:{port}"
    if _already_running(port):
        print(f"Jarvis is already running. Settings page: {url}")
        if args.open:
            webbrowser.open(url)
        return

    from .web.server import LogBuffer

    buffer = LogBuffer()
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(data_dir() / "jarvis.log", maxBytes=2_000_000, backupCount=2),
        buffer,
    ]
    for h in handlers[:2]:
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, handlers=handlers, force=True)
    for noisy in ("httpx", "httpcore", "urllib3", "faster_whisper", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("jarvis")

    log.info("Settings file: %s", settings.path)

    from .announcer import Announcer
    from .audio.tts import Speaker
    from .brain import Brain
    from .context import ctx
    from .gcal import GoogleCalendar
    from .media import MediaPlayer
    from .netwatch import NetWatch
    from .notify import Notifier
    from .ringer import Ringer
    from .scheduler import Scheduler
    from .security import SecurityMonitor
    from .storage import Database
    from .tools.calculator import SANDBOX
    from .tutor import Tutor

    ctx.settings = settings
    ctx.db = db = Database(data_dir() / "jarvis.db")
    ctx.notifier = Notifier(settings)
    ctx.speaker = speaker = Speaker(settings)
    ctx.brain = brain = Brain(settings)
    ctx.ringer = Ringer(settings)
    ctx.announcer = Announcer(settings)
    ctx.media = MediaPlayer(settings)
    ctx.media.index.ensure()  # start indexing the music folder in the background
    ctx.security = SecurityMonitor(settings, db)
    ctx.netwatch = NetWatch(settings, db)
    ctx.tutor = Tutor(settings, db)
    ctx.gcal = GoogleCalendar(settings)
    SANDBOX.warm_up()
    scheduler = Scheduler(settings, db)
    scheduler.start()
    ctx.netwatch.start()

    def _prepare_dictionary():
        from .tools.dictionary import ensure_wordnet
        ensure_wordnet()
    import threading
    threading.Thread(target=_prepare_dictionary, daemon=True).start()

    voice = None
    if not args.no_voice:
        from .assistant import VoiceAssistant
        ctx.voice = voice = VoiceAssistant(settings, brain, speaker)
        voice.start()

    import uvicorn

    from .web.server import create_app

    app = create_app(settings, brain, speaker, voice, buffer)
    host = "0.0.0.0" if settings.get("web", "allow_network") else "127.0.0.1"
    log.info("Settings page: %s", url)
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        if voice:
            voice.shutdown()
        scheduler.stop()
        ctx.media.shutdown()


def _already_running(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    main()

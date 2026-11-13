"""The settings page + a small API it uses. Opens at http://localhost:8765"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from .. import timeparse as tp
from ..config import SCHEMA
from ..context import ctx

STATIC = Path(__file__).resolve().parent / "static"


class LogBuffer(logging.Handler):
    """Keeps the last few hundred log lines for the 'Activity' box on the page."""

    def __init__(self, size: int = 300):
        super().__init__(level=logging.INFO)
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


def create_app(settings, brain, speaker, voice=None, log_buffer: LogBuffer | None = None) -> FastAPI:
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/schema")
    def schema():
        return SCHEMA

    @app.get("/api/settings")
    def get_settings():
        return settings.as_dict(mask_secrets=True)

    @app.post("/api/settings")
    def save_settings(changes: dict = Body(...)):
        try:
            changed = settings.update(changes)
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc))
        needs_restart = any(k in {("web", "allow_network"), ("web", "port")} for k in changed)
        return {"changed": [f"{a}.{b}" for a, b in sorted(changed)], "app_restart_needed": needs_restart}

    @app.get("/api/status")
    def status():
        if voice is None:
            st = {"state": "off", "voice_ok": False, "message": "Voice is off (started with --no-voice).",
                  "last_heard": "", "last_reply": ""}
        else:
            s = voice.status
            st = {"state": s.state, "voice_ok": s.voice_ok, "message": s.message,
                  "last_heard": s.last_heard, "last_reply": s.last_reply}
        st["log"] = list(log_buffer.lines)[-80:] if log_buffer else []
        return st

    @app.post("/api/chat")
    def chat(payload: dict = Body(...)):
        text = str(payload.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "empty message")
        reply = brain.ask(text, source="typed")
        if payload.get("speak"):
            threading.Thread(target=speaker.say, args=(reply,), daemon=True).start()
        return {"reply": reply}

    # ------------------------------------------------------------------ talk from a phone / any browser
    phone_stt: dict = {}

    def _stt():
        if ctx.voice is not None and getattr(ctx.voice, "stt", None) is not None:
            return ctx.voice.stt
        if "stt" not in phone_stt:
            from ..audio.stt import SpeechToText
            phone_stt["stt"] = SpeechToText(settings.get("speech_to_text", "model"), settings.get("speech_to_text", "device"),
                                            settings.get("speech_to_text", "language"))
        return phone_stt["stt"]

    def _voice_reply(text: str) -> str | None:
        """Speak the reply into a WAV file for the phone to play (base64)."""
        import base64
        import io

        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly

        from ..audio.tts import split_sentences
        engine = speaker._get_engine()
        if engine is None:
            return None
        parts, rate = [], 24000
        for sentence in split_sentences(text):
            audio, r = engine.synth(sentence)
            if parts and r != rate:
                audio = resample_poly(audio, rate, r)
            elif not parts:
                rate = r
            parts.append(np.asarray(audio, dtype=np.float32))
        if not parts:
            return None
        buf = io.BytesIO()
        sf.write(buf, np.concatenate(parts), rate, format="WAV", subtype="PCM_16")
        return base64.b64encode(buf.getvalue()).decode()

    @app.post("/api/voice")
    async def voice_chat(request: Request, reply_audio: int = 1):
        """The phone page sends recorded audio; we transcribe it, answer, and send back the answer as speech."""
        import io
        data = await request.body()
        if len(data) < 1000:
            raise HTTPException(400, "The recording was empty.")

        def work():
            try:
                return answer()
            except Exception as exc:
                logging.getLogger("jarvis").exception("Tap-to-talk failed")
                raise HTTPException(500, f"Something went wrong on the Jarvis PC: {exc}") from exc

        def answer():
            from ..assistant import is_cancel
            heard = _stt().transcribe(io.BytesIO(data))
            if not heard.strip() or is_cancel(heard, settings.get("listening", "cancel_phrase")):
                return {"heard": heard, "reply": "", "audio": None}
            reply = brain.ask(heard, source="phone")
            audio = None
            if reply_audio:
                try:
                    audio = _voice_reply(reply)
                except Exception as exc:
                    logging.getLogger("jarvis").warning("Couldn't make voice reply for the phone: %s", exc)
            return {"heard": heard, "reply": reply, "audio": audio}
        return await run_in_threadpool(work)

    @app.post("/api/reset")
    def reset():
        brain.reset()
        return {"ok": True}

    @app.post("/api/talk")
    def talk():
        if voice is None:
            raise HTTPException(400, "Voice is off.")
        voice.talk_now()
        return {"ok": True}

    @app.post("/api/stop")
    def stop_speaking():
        speaker.stop()
        if ctx.ringer is not None:
            ctx.ringer.stop(reason="button")
        return {"ok": True}

    @app.post("/api/cancel")
    def cancel_everything():
        """The 'Stop everything' button: same as saying the cancel phrase."""
        if voice is not None:
            voice.cancel_everything()
        else:
            speaker.stop()
            if ctx.ringer is not None and ctx.ringer.is_ringing():
                ctx.ringer.stop(reason="button")
            if ctx.announcer is not None:
                ctx.announcer.clear()
            if ctx.media is not None:
                ctx.media.stop_everything()
        return {"ok": True}

    @app.post("/api/test-voice")
    def test_voice():
        name = settings.get("general", "assistant_name") or "Jarvis"
        threading.Thread(target=speaker.say, args=(f"Hello, I'm {name}. This is how I sound.",),
                         daemon=True).start()
        return {"ok": True}

    @app.get("/api/options/{kind}")
    def options(kind: str):
        try:
            if kind == "models":
                return {"options": brain.llm.list_models()}
            if kind in ("input_devices", "output_devices"):
                from ..audio.mic import list_devices
                return {"options": list_devices()["input" if kind == "input_devices" else "output"]}
        except Exception as exc:
            return JSONResponse({"error": str(exc), "options": []}, status_code=200)
        raise HTTPException(404, "unknown list")

    # ------------------------------------------------------------------ dashboard
    def _when(ts: float | None) -> str:
        if not ts:
            return ""
        return tp.say_when(datetime.fromtimestamp(ts).astimezone(), style=settings.get("general", "time_format"))

    @app.get("/api/dashboard")
    def dashboard():
        db = ctx.db
        now = time.time()
        from ..tools.calendar import all_events
        from ..tools.reminders import say_repeat
        start = tp.now_local()
        try:
            events = all_events(start, datetime.fromtimestamp(now + 7 * 86400).astimezone())
        except Exception as exc:
            events = [{"title": f"(calendar error: {exc})", "start": now, "source": "error", "id": 0}]
        lists: dict[str, list] = {}
        for r in db.all("SELECT * FROM list_items ORDER BY list, id"):
            lists.setdefault(r["list"], []).append({"id": r["id"], "item": r["item"]})
        return {
            "alarms": [{"id": a["id"], "label": a["label"] or "Alarm", "when": _when(a["snooze_until"] or a["next_fire"]),
                        "repeats": tp.say_days([int(d) for d in a["days"].split(",") if d])}
                       for a in db.all("SELECT * FROM alarms WHERE enabled=1 ORDER BY COALESCE(snooze_until, next_fire)")],
            "timers": [{"id": t["id"], "name": t["name"], "left": tp.say_duration(max(0, t["ends"] - now))}
                       for t in db.all("SELECT * FROM timers ORDER BY ends")],
            "reminders": [{"id": r["id"], "text": r["text"], "when": _when(r["next_fire"]) or "waiting for confirmation",
                           "repeats": say_repeat(r["repeat"]), "awaiting": bool(r["awaiting"])}
                          for r in db.all("SELECT * FROM reminders WHERE active=1 OR awaiting=1 ORDER BY next_fire")],
            "events": [{"id": e["id"], "title": e["title"], "when": _when(e["start"]), "source": e["source"]}
                       for e in events],
            "lists": lists,
            "memories": [{"id": m["id"], "text": m["text"]} for m in db.all("SELECT * FROM memories ORDER BY id DESC")],
            "ringing": ctx.ringer.ringing["label"] if ctx.ringer is not None and ctx.ringer.ringing else "",
            "playing": ctx.media.now_playing() if ctx.media is not None else {"playing": False},
            "google": ctx.gcal.status() if ctx.gcal is not None else "off",
            "away": bool(ctx.security and ctx.security.away()),
        }

    TABLES = {"alarm": "UPDATE alarms SET enabled=0 WHERE id=?", "timer": "DELETE FROM timers WHERE id=?",
              "reminder": "UPDATE reminders SET active=0, awaiting=0 WHERE id=?", "event": "DELETE FROM events WHERE id=?",
              "list_item": "DELETE FROM list_items WHERE id=?", "memory": "DELETE FROM memories WHERE id=?",
              "vocab": "DELETE FROM vocab WHERE word=?", "history": "DELETE FROM history WHERE id=?"}

    @app.post("/api/delete")
    def delete(payload: dict = Body(...)):
        kind, item = payload.get("kind"), payload.get("id")
        if kind == "security":
            return {"deleted": ctx.security.delete(int(item) if item else None)}
        if kind == "history_all":
            ctx.db.execute("DELETE FROM history")
            return {"ok": True}
        if kind == "google_event":
            ctx.gcal.delete(str(item))
            return {"ok": True}
        if kind not in TABLES:
            raise HTTPException(400, "unknown kind")
        ctx.db.execute(TABLES[kind], (item,))
        return {"ok": True}

    @app.post("/api/music")
    def music(payload: dict = Body(...)):
        from ..tools.media import music as music_tool
        return music_tool(settings, payload.get("action", "stop"), payload.get("query", ""))

    # ------------------------------------------------------------------ security
    @app.get("/api/security")
    def security():
        sec = ctx.security
        rows = ctx.db.all("SELECT * FROM security_events ORDER BY time DESC LIMIT 200")
        known = ctx.db.get("known_devices", {})
        return {
            "away": bool(sec and sec.away()),
            "scheduled": bool(sec and sec.scheduled_now()),
            "phone": bool(ctx.notifier and ctx.notifier.enabled()),
            "clips": [{"id": r["id"], "when": _when(r["time"]), "kind": r["kind"], "seconds": round(r["duration"]),
                       "transcript": r["transcript"]} for r in rows],
            "network": [{"when": _when(e["time"]), "message": e["message"]}
                        for e in reversed(ctx.netwatch.recent if ctx.netwatch else [])],
            "devices": sorted(({"mac": mac, **d} for mac, d in known.items()), key=lambda d: d.get("ip", "")),
        }

    @app.post("/api/away")
    def away(payload: dict = Body(...)):
        return ctx.security.set_away(bool(payload.get("on")), "settings page")

    @app.get("/api/security/clip/{clip_id}")
    def clip(clip_id: int):
        row = ctx.db.one("SELECT * FROM security_events WHERE id=?", (clip_id,))
        if not row:
            raise HTTPException(404, "not found")
        return FileResponse(row["path"])

    @app.post("/api/notify-test")
    def notify_test():
        if ctx.notifier is None or not ctx.notifier.enabled():
            raise HTTPException(400, "Turn on phone notifications and set a topic first (Settings > Phone notifications).")
        ok = ctx.notifier.send("Hello from Jarvis! Phone notifications are working.", title="Jarvis test", wait=True)
        if not ok:
            raise HTTPException(500, "Sending failed -- check the server address and topic.")
        return {"ok": True}

    # ------------------------------------------------------------------ google / govee
    @app.post("/api/google/connect")
    def google_connect():
        if not settings.get("reminders", "google_enabled"):
            raise HTTPException(400, "Turn on 'Use my Google Calendar' in Settings > Reminders & calendar first.")

        def go():
            try:
                ctx.gcal.connect()
                logging.getLogger("jarvis").info("Google Calendar connected.")
            except Exception as exc:
                logging.getLogger("jarvis").error("Google connection failed: %s", exc)
                ctx.gcal.last_error = str(exc)
        threading.Thread(target=go, daemon=True).start()
        return {"ok": True, "note": "A Google sign-in page is opening in the browser on the Jarvis computer."}

    @app.post("/api/google/disconnect")
    def google_disconnect():
        ctx.gcal.disconnect()
        return {"ok": True}

    @app.post("/api/govee/discover")
    def govee_discover():
        from ..smarthome import configured_lights, govee_cloud_devices, govee_discover
        named = {(l.ip or l.device): l.name for l in configured_lights(settings)}
        found = []
        try:
            found += [{"name": d["model"], "address": d["ip"], "how": "LAN", "saved_as": named.get(d["ip"], "")}
                      for d in govee_discover()]
        except Exception as exc:
            found.append({"name": f"LAN search failed: {exc}", "address": "", "how": ""})
        if settings.get("smarthome", "govee_api_key"):
            try:
                found += [{"name": d["name"], "address": d["device"], "how": "cloud",
                           "saved_as": named.get(d["device"], "")} for d in govee_cloud_devices(settings)]
            except Exception as exc:
                found.append({"name": f"Cloud search failed: {exc}", "address": "", "how": ""})
        return {"devices": found}

    @app.post("/api/govee/add")
    def govee_add(payload: dict = Body(...)):
        """Save a found light under a friendly name (adds a 'Name = address' line to the Light names setting)."""
        name, address = str(payload.get("name", "")).strip(), str(payload.get("address", "")).strip()
        if not name or not address or "=" in name:
            raise HTTPException(400, "Give the light a name (without '=').")
        lines = [ln for ln in settings.get("smarthome", "govee_names").splitlines()
                 if ln.strip() and ln.split("=", 1)[-1].strip() != address]
        lines.append(f"{name} = {address}")
        changes = {"govee_names": "\n".join(lines)}
        if settings.get("smarthome", "govee_mode") == "off":
            changes["govee_mode"] = "lan"
        settings.update({"smarthome": changes})
        return {"ok": True, "lines": lines}

    @app.post("/api/govee/blink")
    def govee_blink_light(payload: dict = Body(...)):
        from ..smarthome import govee_blink
        status = govee_blink(str(payload.get("address", "")))
        return {"ok": True, "answered": status is not None, "status": status}

    # ------------------------------------------------------------------ vocabulary & history
    @app.get("/api/vocab")
    def vocab():
        words = ctx.db.all("SELECT * FROM vocab ORDER BY added DESC")
        return {"level": ctx.tutor.level() if ctx.tutor else {},
                "word_of_the_day": ctx.tutor.word_of_the_day() if ctx.tutor else None,
                "words": [{"word": w["word"], "definition": w["definition"], "source": w["source"],
                           "correct": w["correct"], "wrong": w["wrong"],
                           "next_review": _when(w["next_review"])} for w in words]}

    @app.get("/api/history")
    def history(q: str = ""):
        if q:
            rows = ctx.db.all("SELECT * FROM history WHERE user LIKE ? OR reply LIKE ? ORDER BY time DESC LIMIT 300",
                              (f"%{q}%", f"%{q}%"))
        else:
            rows = ctx.db.all("SELECT * FROM history ORDER BY time DESC LIMIT 300")
        return {"items": [{"id": r["id"], "when": _when(r["time"]), "source": r["source"], "user": r["user"],
                           "reply": r["reply"]} for r in rows]}

    return app

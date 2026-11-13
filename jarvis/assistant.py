"""The voice loop: wake word -> record -> transcribe -> think -> speak -> (follow-up).

Also: feeds the security monitor in away mode (and stays silent), stops ringing alarms when you
say the wake word, and turns the music down while you talk.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

from .audio.recorder import Recording, SpeechDetector
from .config import VOICE_RESTART_KEYS
from .context import ctx

log = logging.getLogger(__name__)

CANCEL_PHRASES = {"cancel", "cancel that", "never mind", "nevermind", "forget it", "stop", "stop it",
                  "be quiet", "shut up", "nothing", "that's all", "thats all", "no thanks", "no thank you",
                  "okay stop", "ok stop", "thank you", "thanks"}


@dataclass
class Status:
    state: str = "starting"          # starting, idle, listening, thinking, speaking, error, off
    voice_ok: bool = False
    message: str = ""
    last_heard: str = ""
    last_reply: str = ""
    updated: float = field(default_factory=time.time)

    def set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.updated = time.time()


def is_cancel(text: str, phrase: str = "") -> bool:
    """True if the whole message is a cancel ("never mind"), or it ENDS with the cancel phrase
    ("set a timer for... ah, cancel that")."""
    t = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if t in CANCEL_PHRASES:
        return True
    if phrase:
        from .audio.wakeword import _normalize
        import difflib
        words, target = t.split(), _normalize(phrase)
        n = len(target.split())
        if n and len(words) >= n and difflib.SequenceMatcher(None, " ".join(words[-n:]), target).ratio() >= 0.8:
            return True
    return False


def _away() -> bool:
    return ctx.security is not None and ctx.security.away()


class VoiceAssistant:
    def __init__(self, settings, brain, speaker):
        self.settings = settings
        self.brain = brain
        self.speaker = speaker
        self.status = Status()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._talk_now = threading.Event()
        self._followup = threading.Event()
        self._generation = 0
        self._ducked = False
        self.stt = None
        self._thread: threading.Thread | None = None
        settings.on_change(self._on_settings_change)

    # ---- public controls (used by the web page) ----------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_forever, name="voice", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.speaker.stop()

    def talk_now(self) -> None:
        """Like saying the wake word (the 'Talk' button)."""
        self._talk_now.set()

    def _on_settings_change(self, changed: set[tuple[str, str]]) -> None:
        if changed & VOICE_RESTART_KEYS:
            log.info("Voice settings changed; restarting the listener.")
            self._restart.set()

    def _wake_phrase(self) -> str:
        ww = self.settings.section("wake_word")
        if ww["engine"] == "whisper":
            return ww["phrase"]
        return ww["openwakeword_model"].replace("_", " ") if ww["openwakeword_model"] != "custom" else ""

    def _end_conversation(self, stealth: bool = False) -> None:
        """Back to waiting for the wake word: play the end chime, music back up, status idle."""
        if self.settings.get("listening", "chimes") and not stealth:
            from .audio.player import chime
            chime("end_conversation", self.settings.get("tts", "output_device"))
        self._duck(False)
        self.status.set(state="idle", message="Ready.")

    def _duck(self, on: bool) -> None:
        if ctx.media is not None and on != self._ducked:
            self._ducked = on
            ctx.media.duck(on)

    # ---- main loop ----------------------------------------------------
    def _run_forever(self) -> None:
        while not self._stop.is_set():
            self._restart.clear()
            try:
                self._session()
            except Exception as exc:
                log.exception("Voice system error")
                self.status.set(state="error", voice_ok=False,
                                message=f"Voice system problem: {exc}. Fix it in settings; retrying in 10 s.")
                self._restart.wait(10)

    def _session(self) -> None:
        from .audio import wakeword
        from .audio.mic import Microphone
        from .audio.stt import SpeechToText

        s = self.settings
        self.status.set(state="starting", message="Loading speech models (first time can take a few minutes)...")
        stt = SpeechToText(s.get("speech_to_text", "model"), s.get("speech_to_text", "device"),
                           s.get("speech_to_text", "language"),
                           hint=f"{s.get('general', 'assistant_name')}.")
        self.stt = stt
        detector = wakeword.make_detector(s)
        is_speech = SpeechDetector(int(s.get("listening", "vad_aggressiveness")))
        cancel_det = None
        if s.get("listening", "cancel_enabled") and s.get("listening", "cancel_phrase").strip():
            cancel_det = wakeword.WhisperPhraseDetector(
                s.get("listening", "cancel_phrase"), s.get("wake_word", "whisper_model"),
                s.get("speech_to_text", "device"), int(s.get("listening", "vad_aggressiveness")), 0.6,
                name="Cancel phrase")
        if ctx.security is not None:
            ctx.security.stt = stt
            ctx.security.is_speech = SpeechDetector(2)
        threading.Thread(target=self.speaker.warm_up, daemon=True).start()
        mic = Microphone(s.get("listening", "mic_device"))
        mic.start()
        engine = s.get("wake_word", "engine")
        how = {"openwakeword": f"Say '{s.get('wake_word', 'openwakeword_model').replace('_', ' ')}'",
               "whisper": f"Say '{s.get('wake_word', 'phrase')}'",
               "none": "Wake word off -- use the Talk button"}[engine]
        self.status.set(state="idle", voice_ok=True, message=f"Ready. {how}.")
        log.info("Voice assistant ready. %s.", how)

        recording: Recording | None = None
        prefix = ""
        stealth = False
        try:
            while not self._stop.is_set() and not self._restart.is_set():
                chunk = mic.read(0.5)
                if chunk is None:
                    continue
                away = _away()
                if away and ctx.security is not None:
                    ctx.security.process(chunk)
                lst = s.section("listening")

                # The cancel phrase works without the wake word whenever I'm doing something.
                if cancel_det is not None and not away:
                    if self._active(recording):
                        if cancel_det.process(chunk) is not None:
                            recording, prefix = None, ""
                            if detector is not None:
                                detector.reset()
                            cancel_det.reset()
                            self.cancel_everything()
                            continue
                    else:
                        cancel_det.reset()

                if recording is None:
                    start, prefix_text, wait = False, "", float(lst["wait_for_speech_seconds"])
                    followup = False
                    if self._talk_now.is_set():
                        self._talk_now.clear()
                        start = True
                    elif self._followup.is_set():
                        self._followup.clear()
                        _drain(mic)
                        start, wait = (not away), float(lst["followup_seconds"])
                        followup = True
                        chunk = None
                    elif detector is not None:
                        hit = detector.process(chunk)
                        if hit is not None:
                            start, prefix_text = True, hit.leftover_text
                            if prefix_text:  # they already said the question with the wake word
                                wait = min(wait, float(lst["end_of_speech_seconds"]))
                            chunk = None
                    if start:
                        stealth = away
                        self._interrupt()
                        if lst["chimes"] and not stealth and not followup:
                            from .audio.player import chime
                            chime("start_conversation", s.get("tts", "output_device"))
                        recording = Recording(is_speech, float(lst["end_of_speech_seconds"]), wait,
                                              float(lst["max_recording_seconds"]))
                        prefix = prefix_text
                        if not stealth:
                            self._duck(True)
                            self.status.set(state="listening", message="Listening...")
                    if recording is None or chunk is None:
                        continue

                if recording.add(chunk):
                    rec, recording = recording, None
                    if detector is not None:
                        detector.reset()
                    if not rec.heard_speech and not prefix:
                        self._end_conversation(stealth)
                        continue
                    self._generation += 1
                    target = self._handle_stealth if stealth else self._handle
                    threading.Thread(target=target, args=(rec.audio(), prefix, stt, self._generation),
                                     daemon=True).start()
        finally:
            mic.stop()
            self._duck(False)

    def _active(self, recording) -> bool:
        """Am I doing anything right now (so the cancel phrase should be listened for)?"""
        if recording is not None or self.speaker.speaking or self._followup.is_set():
            return True
        if self.status.state in ("listening", "thinking", "speaking"):
            return True
        if ctx.ringer is not None and ctx.ringer.is_ringing():
            return True
        return ctx.media is not None and ctx.media.busy()

    def cancel_everything(self) -> None:
        """The cancel phrase: stop talking, ringing, music, browser videos, announcements; stop listening."""
        log.info("Cancel phrase heard: stopping everything.")
        self._generation += 1
        self._followup.clear()
        self._talk_now.clear()
        self.speaker.stop()
        if ctx.ringer is not None:
            ctx.ringer.pending_briefing = None
            if ctx.ringer.is_ringing():
                ctx.ringer.stop(reason="cancel")
        if ctx.announcer is not None:
            ctx.announcer.clear()
        if ctx.media is not None:
            ctx.media.stop_everything()
        self._duck(False)
        self._end_conversation()

    def _interrupt(self) -> None:
        """Stop talking / ringing / abandon the current answer (barge-in)."""
        self._generation += 1
        self.speaker.stop()
        if ctx.ringer is not None and ctx.ringer.is_ringing():
            ctx.ringer.stop(reason="wake word")

    def _handle(self, audio, prefix: str, stt, generation: int) -> None:
        stream = None

        def on_sentence(sentence: str) -> None:
            nonlocal stream
            if generation != self._generation:
                return
            if stream is None:
                self.status.set(state="speaking", message="Speaking...")
                stream = self.speaker.stream()
            stream.add(sentence)

        try:
            self.status.set(state="thinking", message="Thinking...")
            from .audio.wakeword import strip_wake_phrase
            raw = f"{prefix} {stt.transcribe(audio) if audio.size else ''}".strip()
            phrase = self._wake_phrase()
            text = strip_wake_phrase(raw, phrase) if phrase else raw
            log.info("Heard: %r", raw)
            self.status.set(last_heard=raw)
            if raw and not text:  # they only said the wake word (again): just listen
                self.talk_now()
                return
            if not text or is_cancel(text, self.settings.get("listening", "cancel_phrase")):
                if text:
                    log.info("Cancelled: not answering.")
                self._end_conversation()
                return
            reply = self.brain.ask(text, on_sentence=on_sentence, source="voice")
            log.info("Reply: %r", reply)
            self.status.set(last_reply=reply)
            if stream is not None:
                stream.finish()
                stream.wait()
            if generation == self._generation:
                if float(self.settings.get("listening", "followup_seconds")) > 0 and not _away():
                    self._followup.set()
                else:
                    self._end_conversation()
        except Exception as exc:
            log.exception("Error handling request")
            self.status.set(message=f"Error: {exc}")
            if stream is not None:
                stream.finish()
        finally:
            if generation == self._generation and self.status.state in ("thinking", "speaking"):
                self.status.set(state="idle", message="Ready.")
            if generation == self._generation and not self._followup.is_set():
                self._duck(False)

    def _handle_stealth(self, audio, prefix: str, stt, generation: int) -> None:
        """Away mode: only the disarm phrase gets a response. Everything else is silently ignored."""
        from .audio.wakeword import find_phrase
        try:
            text = f"{prefix} {stt.transcribe(audio) if audio.size else ''}".strip()
            phrase = self.settings.get("security", "disarm_phrase") or "I'm home"
            log.info("Away mode, heard after wake word: %r", text)
            if text and find_phrase(text, phrase, cutoff=0.8) is not None and ctx.security is not None:
                summary = ctx.security.set_away(False, "disarm phrase")
                n = summary.get("recordings_while_away", 0)
                msg = "Welcome back. Away mode is off."
                if n:
                    msg += (f" While you were gone I recorded {n} clip{'s' * (n != 1)}"
                            + (f", {summary['with_voices']} with voices" if summary.get("with_voices") else "")
                            + ". You can listen to them on the Security page.")
                else:
                    msg += " It was quiet while you were gone."
                self.status.set(state="speaking", last_reply=msg)
                self.speaker.say(msg)
                self.status.set(state="idle", message="Ready.")
        except Exception:
            log.exception("Error in away-mode handling")


def _drain(mic) -> None:
    """Throw away audio recorded while we were talking (so we don't hear ourselves)."""
    while mic.read(0) is not None:
        pass

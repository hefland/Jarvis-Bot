"""The brain: builds the instructions, keeps short-term memory, lets the model use tools,
streams the answer sentence-by-sentence, and lets the vocabulary tutor chime in now and then."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

from . import tools
from .context import ctx
from .llm import LLMClient, LLMError, looks_like_tool_call

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 6

BASE_PROMPT = """You are {name}, a voice assistant running privately on {user}'s home computer.
Everything you say is read aloud by a speech engine, so:
- Answer in plain spoken sentences. Usually 1 to 3 sentences. No lists, no markdown, no emojis, no code.
- Say numbers, times and units the way a person would say them out loud.
- Get to the point. Do not repeat the question back.

You have tools. They give exact, real answers and do real things. Rules:
- MATH: never calculate anything yourself, not even simple sums. Always call calculate (or convert_units) and
  give the result it returns. Briefly mention what you calculated, e.g. "17 times 23 is 391."
- TIME and DATES: never guess. Use get_time, and date_difference for counting days.
- WEATHER: use get_weather. Never make up weather.
- WORDS: when asked what a word means or how to spell it, ALWAYS use the dictionary tool. Never define from memory.
- ALARM = wake-up at a clock time. TIMER = countdown ("10 minutes"). REMINDER = tell the user something at a time.
  For things like medication, set needs_confirmation=true. When the user says they did/took it, call reminder
  with action 'confirm'. Pass times in the user's own words (e.g. "tomorrow at 7am"); the tools understand them.
- SCHEDULE / CALENDAR questions: use the calendar tool. "What's my day look like" / "my briefing": use briefing.
- LISTS (shopping, to-do) and MEMORY ("remember that...", "where did I put...") have their own tools.
- LIGHTS and devices: smart_home. MUSIC from the user's own files, rain / sleep sound: music.
  YOUTUBE videos, PANDORA, or opening a website: browser. Speaker volume: volume.
- "I'm leaving" / "watch the house": away_mode on. "I'm home": away_mode off.
- Earlier messages in this conversation may be OUT OF DATE. Never repeat an earlier answer for the time, date,
  weather, timers, alarms or reminders -- call the tool again every single time.
- Never say you did something (set, started, added, turned on...) unless a tool did it in THIS turn.
- If a tool says something is not set up, tell the user which settings page to visit, in one sentence.
- If a tool returns an error, say plainly what went wrong in one sentence. Do not invent an answer.
- If a tool result contains "say_exactly" or "say_this", say that text (you may smooth it slightly).
- If you do not know something{search_rule}
- If a request needs an ability you don't have, say so honestly in one sentence.
- The user's speech is transcribed by a computer and may contain mis-heard words. Guess the most likely
  meaning; ask a short question only if you truly can't tell.

Right now it is {now}. The user's home is {location}. Units: {units}."""

SEARCH_ON = ", or it may have changed recently, use web_search{wiki}, then answer briefly from the results."
SEARCH_OFF = ", say so{wiki_off}."


# Requests that must be answered by a tool. If the model answers without one, we send it back once.
REQUIRED_TOOLS: list[tuple[re.Pattern, set[str]]] = [(re.compile(p, re.I), set(t)) for p, t in [
    (r"\bwhat(?:'s| is)? the time\b|\bwhat time is it\b|\btime is it\b|\bcurrent time\b|\btime (?:right )?now\b",
     ["get_time"]),
    (r"\bwhat(?:'s| is) (?:the |today's )?date\b|\bwhat day is (?:it|today)\b|\bdays? (?:until|till|since)\b",
     ["get_time", "date_difference", "calendar"]),
    (r"\btimers?\b", ["timer", "alarm"]),
    (r"\balarms?\b|\bwake me\b|\bsnooze\b", ["alarm"]),
    (r"\bremind(?:er|ers)?\b", ["reminder"]),
    (r"\bweather\b|\bforecast\b|\bgoing to rain\b|\btemperature outside\b", ["get_weather"]),
    (r"\d\s*(?:[-+*/x×^%]|times|plus|minus|divided|percent|squared|cubed|to the power)\s*\d?|square root",
     ["calculate", "convert_units"]),
    (r"\bconvert\b|\bhow many (?:cups|ounces|grams|pounds|inches|feet|miles|meters|liters|kilometers)\b",
     ["convert_units", "calculate"]),
    (r"\bwhat does \w+ mean\b|\bdefin(?:e|ition)\b|\bhow do you spell\b|\bmeaning of\b", ["dictionary"]),
    (r"\bmy (?:schedule|calendar)\b|\bon my calendar\b|\bappointments?\b", ["calendar", "briefing"]),
    (r"\b(?:shopping|grocery|groceries|to-?do) list\b", ["lists"]),
    (r"\byou ?tube\b|\bpandora\b", ["browser"]),
    (r"\bplay (?:some |my |the )?(?:music|songs?|rain|sleep)\b", ["music"]),
    (r"\b(?:turn|switch) (?:on|off) the lights?\b|\blights? (?:on|off)\b", ["smart_home"]),
    (r"\bbriefing\b|\bmorning brief\b", ["briefing"]),
]]


def required_tools(text: str, available: set[str]) -> set[str]:
    """Which tools this request should use (empty = no requirement)."""
    for pattern, names in REQUIRED_TOOLS:
        if pattern.search(text):
            usable = names & available
            if usable:
                return usable
    return set()


class SentenceStream:
    """Collects streamed text and hands out whole sentences as soon as they're complete."""

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.buf = ""
        self.started = False
        self.blocked = False
        self.emitted: list[str] = []

    def feed(self, piece: str) -> None:
        self.buf += piece
        if not self.started:
            stripped = self.buf.lstrip()
            if stripped.lower().startswith("<think>"):
                if "</think>" not in stripped.lower():
                    return
                self.buf = re.split(r"(?i)</think>", self.buf, maxsplit=1)[1]
                stripped = self.buf.lstrip()
            if len(stripped) < 12:
                return
            self.started = True
            if looks_like_tool_call(stripped):
                self.blocked = True
        if self.blocked:
            return
        while True:
            m = next((mm for mm in re.finditer(r"[.!?][\"')\]]*\s+", self.buf) if mm.end() >= 20), None)
            if m is None:
                break
            sentence, self.buf = self.buf[:m.end()], self.buf[m.end():]
            self._out(sentence)

    def _out(self, text: str) -> None:
        text = to_speech(text)
        if text:
            self.emitted.append(text)
            self.emit(text)

    def flush(self) -> None:
        if not self.blocked and self.started:
            self._out(self.buf)
        elif not self.started and self.buf.strip() and not looks_like_tool_call(self.buf):
            self._out(self.buf)
        self.buf = ""


class Brain:
    def __init__(self, settings, llm: LLMClient | None = None):
        self.settings = settings
        self.llm = llm or LLMClient(settings)
        self.history: list[dict[str, Any]] = []
        self.last_used = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def system_prompt(self) -> str:
        g = self.settings.section("general")
        now = datetime.now().astimezone()
        clock = now.strftime("%H:%M") if g["time_format"] == "24h" else now.strftime("%I:%M %p").lstrip("0")
        search_on = self.settings.get("search", "provider") != "none"
        wiki = bool(self.settings.get("knowledge", "kiwix_url"))
        rule = (SEARCH_ON.format(wiki=" (or wikipedia for general knowledge)" if wiki else "") if search_on else
                SEARCH_OFF.format(wiki_off="; you can use wikipedia for general knowledge" if wiki else
                                  ". Web search is turned off, so you cannot look things up"))
        prompt = BASE_PROMPT.format(
            name=g["assistant_name"] or "Jarvis",
            user=g["user_name"] or "the user",
            now=f"{now.strftime('%A, %B')} {now.day}, {now.year} at {clock} ({now.tzname()})",
            location=g["location"] or "not set",
            units=g["units"],
            search_rule=rule,
        )
        if g["user_name"]:
            prompt += f"\nThe user's name is {g['user_name']}."
        state = self._live_state()
        if state:
            prompt += "\n\nCurrent situation:\n" + "\n".join(f"- {s}" for s in state)
        extra = self.settings.get("llm", "custom_prompt").strip()
        if extra:
            prompt += ("\n\nExtra instructions from the user (follow them, but the tool rules above "
                       "always win):\n" + extra)
        return prompt

    def _live_state(self) -> list[str]:
        out = []
        try:
            if ctx.ringer is not None:
                if ctx.ringer.is_ringing():
                    out.append(f"A {ctx.ringer.ringing['kind']} is ringing ('stop' / 'snooze' use the alarm tool).")
                else:
                    last = getattr(ctx.ringer, "last_rang", None)
                    if last and time.time() - last.get("stopped", 0) < 120:
                        out.append(f"A {last['kind']} just stopped ringing; 'snooze' means alarm action snooze.")
            if ctx.db is not None:
                waiting = ctx.db.all("SELECT text FROM reminders WHERE awaiting=1")
                if waiting:
                    out.append("Waiting for the user to confirm: " + "; ".join(r["text"] for r in waiting)
                               + ". If they say they did it, call reminder action 'confirm'.")
            mode = getattr(ctx.media, "mode", "") if ctx.media is not None else ""
            if mode:
                out.append(f"Jarvis's player is playing ({mode}).")
            if ctx.tutor is not None:
                note = ctx.tutor.context_note()
                if note:
                    out.append(note)
        except Exception:
            log.exception("Couldn't build live state")
        return out

    def reset(self) -> None:
        with self._lock:
            self.history = []

    def _trim_history(self) -> None:
        forget = float(self.settings.get("llm", "forget_after_minutes"))
        if forget and self.last_used and time.time() - self.last_used > forget * 60:
            self.history = []
        turns = int(self.settings.get("llm", "memory_turns"))
        user_idx = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if turns == 0:
            self.history = []
        elif len(user_idx) > turns:
            self.history = self.history[user_idx[-turns]:]

    # ------------------------------------------------------------------
    def ask(self, text: str, on_sentence: Callable[[str], None] | None = None, source: str = "text") -> str:
        """Answer one user message. Always returns something speakable.
        on_sentence (optional) receives each sentence as soon as it's ready, for faster speaking."""
        with self._lock:
            self._trim_history()
            self.last_used = time.time()
            tutor = ctx.tutor
            had_question = bool(tutor and tutor.pending)
            if tutor is not None:
                try:
                    tutor.observe(text)
                except Exception:
                    log.exception("Tutor observe failed")
            answer, spoken, steps = self._answer(text, on_sentence)
            if tutor is not None:
                try:
                    if had_question:
                        tutor.turn_done()
                    extra = tutor.maybe_followup(text, answer)
                    if extra:
                        answer = f"{answer} {extra}"
                        if on_sentence:
                            on_sentence(extra)
                except Exception:
                    log.exception("Tutor follow-up failed")
            if on_sentence and not spoken:
                on_sentence(answer)
            # Keep the tool steps in memory, so the model sees that it used tools before (and keeps doing so).
            self.history.extend(steps + [{"role": "assistant", "content": answer}])
            self._save_history(source, text, answer)
            return answer

    def _answer(self, text: str, on_sentence) -> tuple[str, bool, list[dict[str, Any]]]:
        """Returns (answer, already_spoken, messages_for_memory)."""
        new_messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        available = tools.available(self.settings)
        schemas = [t.schema() for t in available]
        must_use = required_tools(text, {t.name for t in available})
        used: set[str] = set()
        nudged = False
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                messages = [{"role": "system", "content": self.system_prompt()}] + self.history + new_messages
                # Don't start speaking until the required tool has actually been used.
                hold_voice = bool(must_use) and not (used & must_use)
                stream = SentenceStream(on_sentence) if on_sentence and not hold_voice else None
                reply = self.llm.chat(messages, schemas or None, on_text=stream.feed if stream else None)
                if not reply["tool_calls"]:
                    if must_use and not (used & must_use) and not nudged:
                        nudged = True
                        names = " or ".join(sorted(must_use))
                        log.info("Model answered without using %s; asking it to use the tool.", names)
                        new_messages.append({"role": "system", "content":
                                             f"You answered without calling a tool. That answer may be wrong or made up. "
                                             f"Call the {names} tool NOW to handle the user's last message. Do not "
                                             f"answer from memory or from earlier in the conversation."})
                        continue
                    if stream is not None:
                        stream.flush()
                    answer = to_speech(reply["content"]) or "Sorry, I don't have an answer for that."
                    return answer, bool(stream and stream.emitted), _memory(new_messages)
                if stream is not None and stream.emitted:
                    stream.flush()  # it said something like "Let me check" before using a tool
                new_messages.append({
                    "role": "assistant", "content": reply["content"] or "",
                    "tool_calls": [{"id": c["id"], "type": "function",
                                    "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                                   for c in reply["tool_calls"]],
                })
                for call in reply["tool_calls"]:
                    log.info("Tool call: %s(%s)", call["name"], call["arguments"])
                    used.add(call["name"])
                    result = tools.run(call["name"], call["arguments"], self.settings)
                    log.info("Tool result: %s", result[:500])
                    new_messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                         "content": result})
            return "Sorry, I got stuck working that out. Could you ask in a different way?", False, _memory(new_messages)
        except LLMError as exc:
            log.error("LLM error: %s", exc)
            return str(exc), False, [{"role": "user", "content": text}]

    def _save_history(self, source: str, text: str, answer: str) -> None:
        if ctx.db is None or not self.settings.get("history", "enabled"):
            return
        try:
            ctx.db.execute("INSERT INTO history(time, source, user, reply) VALUES(?,?,?,?)",
                           (time.time(), source, text, answer))
        except Exception:
            log.exception("Couldn't save history")


def _memory(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What to keep in conversation memory: the turn without our nudges, with long tool results shortened."""
    out = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "tool" and len(m["content"]) > 400:
            m = dict(m, content=m["content"][:400] + "...")
        out.append(m)
    return out


_MD = [
    (re.compile(r"```.*?```", re.DOTALL), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)\*(.+?)\*(?!\w)"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"^\s*[-*•]\s+", re.MULTILINE), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),
]


def to_speech(text: str) -> str:
    """Remove markdown so the voice doesn't read out asterisks and hashes."""
    out = text or ""
    for pattern, repl in _MD:
        out = pattern.sub(repl, out)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{2,}", "\n", out)
    return out.strip()

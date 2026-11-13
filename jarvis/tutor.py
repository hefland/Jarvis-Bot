"""The vocabulary tutor.

* Remembers every word you look up (and every word it teaches you).
* Quizzes you later using spaced repetition: words you know come back less often.
* Only speaks up while you're already talking to Jarvis -- never out of nowhere.
* Estimates your vocabulary level from the words you use, and teaches words a bit above it.
"""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import date

from .context import ctx
from .data.words import RIDDLES, WORDS

log = logging.getLogger(__name__)

STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on
once only or other our ours ourselves out over own same she should so some such than that the their theirs them
themselves then there these they this those through to too under until up very was we were what when where which while
who whom why will with would you your yours yourself yourselves jarvis okay ok yeah yes please thanks thank hey""".split())


def _zipf(word: str) -> float | None:
    try:
        from wordfreq import zipf_frequency
        return zipf_frequency(word, "en")
    except Exception:
        return None


class Tutor:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.pending: dict | None = None

    # ---------------------------------------------------------------- learning about the user
    def observe(self, text: str) -> None:
        for w in set(re.findall(r"[a-z']{3,}", text.lower())):
            w = w.strip("'")
            if w and w not in STOPWORDS and not w.endswith("'s"):
                self.db.execute("INSERT INTO user_words(word, count) VALUES(?,1) "
                                "ON CONFLICT(word) DO UPDATE SET count=count+1", (w,))

    def level(self) -> dict:
        rows = self.db.all("SELECT word FROM user_words")
        words = [r["word"] for r in rows]
        zipfs = sorted(z for z in (_zipf(w) for w in words) if z is not None and z > 0)
        if len(zipfs) < 40:
            return {"level": 5, "out_of": 10, "words_heard": len(words),
                    "note": "Still learning how you talk -- I need to hear more words before I can judge well."}
        rare = zipfs[:max(5, len(zipfs) // 7)]
        rare_avg = sum(rare) / len(rare)
        lvl = max(1, min(10, round(1 + (4.8 - rare_avg) * 3.5)))
        labels = {1: "everyday", 3: "solid", 5: "strong", 7: "advanced", 9: "expert"}
        label = labels[max(k for k in labels if k <= lvl)]
        return {"level": lvl, "out_of": 10, "description": label, "words_heard": len(words)}

    # ---------------------------------------------------------------- the word list
    def add_word(self, word: str, definition: str, source: str = "looked_up") -> None:
        word = word.lower().strip()
        if not word:
            return
        self.db.execute("INSERT INTO vocab(word, definition, source, added, next_review) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(word) DO UPDATE SET definition=excluded.definition",
                        (word, definition, source, time.time(), time.time() + 86400))

    def record(self, word: str, result: str) -> dict:
        """result: correct / partly / wrong / skipped. Spaced repetition: right answers come back later."""
        row = self.db.one("SELECT * FROM vocab WHERE word=?", (word.lower().strip(),))
        if row is None:
            return {"error": f"'{word}' isn't in your word list"}
        interval, ease = row["interval_days"], row["ease"]
        result = result.lower()
        if result == "correct":
            interval, ease = max(2.0, interval * ease), min(3.0, ease + 0.1)
            self.db.execute("UPDATE vocab SET correct=correct+1 WHERE word=?", (row["word"],))
        elif result == "partly":
            interval = max(1.0, interval * 1.3)
        elif result == "wrong":
            interval, ease = 1.0, max(1.3, ease - 0.2)
            self.db.execute("UPDATE vocab SET wrong=wrong+1 WHERE word=?", (row["word"],))
        else:
            interval = max(1.0, interval)
        self.db.execute("UPDATE vocab SET interval_days=?, ease=?, reps=reps+1, last_result=?, next_review=? WHERE word=?",
                        (interval, ease, result, time.time() + interval * 86400, row["word"]))
        if self.pending and self.pending.get("word") == row["word"]:
            self.pending = None
        return {"ok": True, "word": row["word"], "next_review_in_days": round(interval, 1)}

    def due(self) -> list[dict]:
        return self.db.all("SELECT * FROM vocab WHERE next_review <= ? ORDER BY next_review", (time.time(),))

    def pick_new_word(self) -> dict | None:
        known = {r["word"] for r in self.db.all("SELECT word FROM vocab")}
        candidates = [w for w in WORDS if w[0] not in known]
        if not candidates:
            return None
        lvl = self.level()["level"]
        target = 4.8 - (lvl - 1) / 3.5 - 0.4  # a little above the user's level
        scored = []
        for word, tier, definition, example in candidates:
            z = _zipf(word)
            if z is None or z == 0:
                z = {1: 3.6, 2: 2.8, 3: 2.0}[tier]
            scored.append((abs(z - target) + random.random() * 0.6, word, definition, example))
        scored.sort()
        _, word, definition, example = scored[0]
        return {"word": word, "definition": definition, "example": example}

    def word_of_the_day(self) -> dict | None:
        today = date.today().isoformat()
        saved = self.db.get("word_of_the_day")
        if saved and saved.get("date") == today:
            return saved
        w = self.pick_new_word()
        if not w:
            return None
        self.add_word(w["word"], w["definition"], "word_of_the_day")
        w["date"] = today
        self.db.set("word_of_the_day", w)
        return w

    # ---------------------------------------------------------------- deciding when to speak up
    def _allowed_now(self) -> bool:
        L = self.settings.section("learning")
        if not L["tutor_enabled"] or (ctx.security is not None and ctx.security.away()):
            return False
        if ctx.ringer is not None and ctx.ringer.is_ringing():
            return False
        last = self.db.get("tutor_last", 0)
        if time.time() - last < float(L["min_minutes_between"]) * 60:
            return False
        today = date.today().isoformat()
        count = self.db.get(f"tutor_count_{today}", 0)
        return count < int(L["max_per_day"])

    def maybe_followup(self, user_text: str, reply: str, force: str | None = None) -> str | None:
        """Called after Jarvis answers. Sometimes returns a short quiz / new word / challenge to add."""
        if self.pending:  # don't stack questions
            return None
        if force is None:
            if not self._allowed_now() or len(user_text.split()) < 2:
                return None
            if reply.lower().startswith(("sorry", "can't reach", "the ai", "error")):
                return None
            if random.random() * 100 >= float(self.settings.get("learning", "chance_percent")):
                return None
        text = self._make_activity(force)
        if text:
            today = date.today().isoformat()
            self.db.set("tutor_last", time.time())
            self.db.set(f"tutor_count_{today}", self.db.get(f"tutor_count_{today}", 0) + 1)
        return text

    def _make_activity(self, force: str | None) -> str | None:
        L = self.settings.section("learning")
        choices = []
        due = self.due()
        if force in (None, "quiz") and due:
            choices.append("quiz")
        if force in (None, "teach") and L["teach_new_words"]:
            choices.append("teach")
        if force in (None, "challenge") and L["challenges"]:
            choices.append("challenge")
        if not choices:
            return None
        weights = {"quiz": 5, "teach": 3, "challenge": 2}
        kind = random.choices(choices, weights=[weights[c] for c in choices])[0]
        now = time.time()
        if kind == "quiz":
            w = due[0]
            if L["sentence_practice"] and random.random() < 0.35:
                self.pending = {"type": "sentence", "word": w["word"], "definition": w["definition"], "asked": now}
                return f"Quick vocab practice: can you use the word '{w['word']}' in a sentence?"
            self.pending = {"type": "define", "word": w["word"], "definition": w["definition"], "asked": now}
            return f"By the way, quick vocab check: do you remember what '{w['word']}' means?"
        if kind == "teach":
            w = self.pick_new_word()
            if not w:
                return None
            self.add_word(w["word"], w["definition"], "taught")
            ask = L["sentence_practice"] and random.random() < 0.5
            if ask:
                self.pending = {"type": "sentence", "word": w["word"], "definition": w["definition"], "asked": now}
            return (f"Here's a new word for you: '{w['word']}'. It means {w['definition']}. For example: {w['example']}"
                    + (" Want to try using it in a sentence?" if ask else ""))
        if random.random() < 0.5:
            q, a = random.choice(RIDDLES)
            self.pending = {"type": "riddle", "question": q, "answer": a, "asked": now}
            return f"Here's a riddle for you: {q}"
        a, b = random.randint(12, 49), random.randint(3, 9)
        op = random.choice(["times", "plus", "minus"])
        ans = a * b if op == "times" else a + b * 11 if op == "plus" else a * 3 - b
        q = f"what's {a} times {b}" if op == "times" else f"what's {a} plus {b * 11}" if op == "plus" else f"what's {a * 3} minus {b}"
        self.pending = {"type": "math", "question": q, "answer": str(ans), "asked": now}
        return f"Brain challenge, no calculator: {q}?"

    # ---------------------------------------------------------------- telling the AI what's going on
    def context_note(self) -> str:
        p = self.pending
        if not p:
            return ""
        if time.time() - p["asked"] > 600:
            if p.get("word"):
                self.record(p["word"], "skipped")
            self.pending = None
            return ""
        if p["type"] in ("define", "sentence"):
            task = ("tell you what it means" if p["type"] == "define" else "use it correctly in a sentence")
            return (f"TUTOR: You just asked the user to {task} for the word '{p['word']}' "
                    f"(correct meaning: {p['definition']}). If their message is an attempt, judge it kindly and "
                    f"generously, say briefly if they got it (and give the meaning if they missed), then call the "
                    f"learning tool with action 'quiz_result', word '{p['word']}' and result correct/partly/wrong. "
                    f"If they ignore it or say skip, just answer their message and call it with result 'skipped'.")
        return (f"TUTOR: You just gave the user a {p['type']}: '{p['question']}'. The correct answer is: "
                f"'{p['answer']}'. If their message is an answer, tell them if it's right (accept close wording), give "
                f"the answer if wrong, and call the learning tool with action 'challenge_result'. If they skip, just "
                f"answer their message.")

    def turn_done(self) -> None:
        """Called after the user's reply to a tutor question has been handled.
        If the AI didn't grade it (record() clears pending), count it as skipped."""
        p = self.pending
        if p and p.get("word"):
            self.record(p["word"], "skipped")
        self.pending = None

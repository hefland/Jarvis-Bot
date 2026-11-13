"""Dictionary (offline WordNet, with an optional online backup) and the learning tool."""
from __future__ import annotations

import difflib
import functools
import logging

import httpx

from ..config import data_dir
from ..context import ctx
from . import tool

log = logging.getLogger(__name__)
POS = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}


@functools.lru_cache(maxsize=1)
def _wordnet():
    import nltk
    path = str(data_dir() / "nltk")
    if path not in nltk.data.path:
        nltk.data.path.insert(0, path)
    from nltk.corpus import wordnet as wn
    wn.ensure_loaded()
    return wn


WORDNET_URL = "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip"


def ensure_wordnet() -> bool:
    """Download the offline dictionary once (about 10 MB) into ~/.local/share/jarvis-bot/nltk."""
    try:
        _wordnet()
        return True
    except Exception:
        _wordnet.cache_clear()
    try:
        import io
        import zipfile
        target = data_dir() / "nltk" / "corpora"
        target.mkdir(parents=True, exist_ok=True)
        log.info("Downloading the offline dictionary (one time)...")
        resp = httpx.get(WORDNET_URL, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        zipfile.ZipFile(io.BytesIO(resp.content)).extractall(target)
        _wordnet()
        log.info("Offline dictionary ready.")
        return True
    except Exception as exc:
        _wordnet.cache_clear()
        log.warning("Offline dictionary unavailable: %s", exc)
        return False


def offline_lookup(word: str) -> list[dict]:
    wn = _wordnet()
    senses = []
    for syn in wn.synsets(word.replace(" ", "_"))[:6]:
        senses.append({
            "part_of_speech": POS.get(syn.pos(), syn.pos()),
            "definition": syn.definition(),
            "example": (syn.examples() or [""])[0],
            "synonyms": [l.name().replace("_", " ") for l in syn.lemmas() if l.name().lower() != word.lower()][:4],
        })
    return senses


def online_lookup(word: str) -> list[dict]:
    resp = httpx.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}", timeout=10)
    if resp.status_code != 200:
        return []
    senses = []
    for entry in resp.json()[:1]:
        for meaning in entry.get("meanings", [])[:3]:
            for d in meaning.get("definitions", [])[:2]:
                senses.append({"part_of_speech": meaning.get("partOfSpeech", ""), "definition": d.get("definition", ""),
                               "example": d.get("example", ""), "synonyms": d.get("synonyms", [])[:4]})
    return senses


@functools.lru_cache(maxsize=1)
def _all_words() -> list[str]:
    return [w for w in _wordnet().all_lemma_names() if w.isalpha()]


def lookup(word: str, settings) -> dict:
    word = word.strip().lower().strip(".?!'\"")
    senses: list[dict] = []
    try:
        senses = offline_lookup(word)
    except Exception as exc:
        log.warning("Offline dictionary error: %s", exc)
    if not senses and settings.get("learning", "online_dictionary"):
        try:
            senses = online_lookup(word)
        except Exception as exc:
            log.warning("Online dictionary error: %s", exc)
    if not senses:
        out = {"word": word, "error": "not found in the dictionary"}
        try:
            out["did_you_mean"] = difflib.get_close_matches(word, _all_words(), n=3, cutoff=0.8)
        except Exception:
            pass
        return out
    return {"word": word, "spelled": "-".join(word.upper()), "meanings": senses[:3]}


@tool(
    "dictionary",
    "Look up a word: meaning, part of speech, example, synonyms, and spelling. ALWAYS use this when the user asks "
    "what a word means or how to spell it -- never define words from memory. The word is saved so the user can "
    "practice it later.",
    {"word": {"type": "string", "description": "The word (as the user said it; it may be mis-heard)"}},
    ["word"],
)
def dictionary(settings, word: str):
    result = lookup(word, settings)
    if "meanings" in result and ctx.tutor is not None:
        ctx.tutor.add_word(result["word"], result["meanings"][0]["definition"], "looked_up")
    return result


@tool(
    "learning",
    "Vocabulary tutor and brain games. action: 'quiz_result' (word + result correct/partly/wrong/skipped -- after "
    "grading a vocab answer), 'challenge_result' (after a riddle/math challenge), 'quiz_me' (user wants a vocab quiz now), "
    "'teach_me' (user wants a new word now), 'challenge_me' (riddle or mental math now), 'my_level' (user's vocabulary "
    "level), 'my_words' (how many words they're learning).",
    {
        "action": {"type": "string", "enum": ["quiz_result", "challenge_result", "quiz_me", "teach_me", "challenge_me",
                                               "my_level", "my_words"]},
        "word": {"type": "string"},
        "result": {"type": "string", "enum": ["correct", "partly", "wrong", "skipped"]},
    },
    ["action"],
)
def learning(settings, action: str, word: str = "", result: str = "correct"):
    t = ctx.tutor
    if t is None:
        return {"error": "The tutor isn't running."}
    if action == "quiz_result":
        return t.record(word or (t.pending or {}).get("word", ""), result)
    if action == "challenge_result":
        t.pending = None
        return {"ok": True}
    if action in ("quiz_me", "teach_me", "challenge_me"):
        t.pending = None
        kind = {"quiz_me": "quiz", "teach_me": "teach", "challenge_me": "challenge"}[action]
        text = t.maybe_followup("", "", force=kind)
        if text is None and kind == "quiz":
            text = t.maybe_followup("", "", force="teach")
            if text:
                text = "You don't have any words due for review yet, so let's learn a new one. " + text
        return {"say_exactly": text or "I don't have anything ready right now."}
    if action == "my_level":
        return t.level()
    if action == "my_words":
        total = ctx.db.one("SELECT COUNT(*) AS n FROM vocab")["n"]
        known = ctx.db.one("SELECT COUNT(*) AS n FROM vocab WHERE correct >= 2")["n"]
        return {"words_in_your_list": total, "words_you_know_well": known, "due_for_review": len(t.due())}
    return {"error": f"unknown action {action}"}

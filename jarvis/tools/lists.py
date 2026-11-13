"""Shopping list, to-do list (any list name), and personal memory ('remember that...')."""
from __future__ import annotations

import difflib
import re
import time

from ..context import ctx
from . import tool

STOP = {"the", "a", "an", "my", "is", "are", "where", "what", "did", "i", "put", "of", "in", "on", "to", "do", "you",
        "remember", "when", "was", "it", "that", "for", "at", "and", "me", "about", "know"}


def _list_name(name: str) -> str:
    n = (name or "shopping").lower().strip()
    n = re.sub(r"\b(my|the|list)\b", "", n).strip()
    if n in ("grocery", "groceries", "shop", "store"):
        n = "shopping"
    if n in ("todo", "to do", "to-do", "tasks", "task", "things to do"):
        n = "to-do"
    return n or "shopping"


def _split_items(items: str) -> list[str]:
    parts = re.split(r",|\band\b|\n", items or "")
    return [p.strip(" .") for p in parts if p.strip(" .")]


@tool(
    "lists",
    "Shopping list, to-do list, or any named list. action 'add' (items, comma separated), 'show', 'remove' (items), "
    "'clear'. list_name e.g. 'shopping', 'to-do', 'hardware store'.",
    {
        "action": {"type": "string", "enum": ["add", "show", "remove", "clear"]},
        "list_name": {"type": "string"},
        "items": {"type": "string"},
    },
    ["action"],
)
def lists(settings, action: str, list_name: str = "shopping", items: str = ""):
    db = ctx.db
    name = _list_name(list_name)
    rows = db.all("SELECT * FROM list_items WHERE list=? ORDER BY id", (name,))
    if action == "add":
        new = _split_items(items)
        existing = {r["item"].lower() for r in rows}
        added = []
        for it in new:
            if it.lower() not in existing:
                db.execute("INSERT INTO list_items(list, item, created) VALUES(?,?,?)", (name, it, time.time()))
                added.append(it)
        return {"ok": True, "list": name, "added": added, "already_there": [i for i in new if i not in added],
                "items_now": len(rows) + len(added)}
    if action == "show":
        return {"list": name, "items": [r["item"] for r in rows] or "empty",
                "other_lists": sorted({r["list"] for r in db.all("SELECT DISTINCT list FROM list_items")} - {name})}
    if action == "remove":
        removed = []
        for it in _split_items(items):
            names = {r["item"].lower(): r for r in rows}
            match = difflib.get_close_matches(it.lower(), list(names), n=1, cutoff=0.6)
            if match:
                db.execute("DELETE FROM list_items WHERE id=?", (names[match[0]]["id"],))
                removed.append(names[match[0]]["item"])
                rows = [r for r in rows if r["item"] != names[match[0]]["item"]]
        return {"ok": bool(removed), "list": name, "removed": removed}
    if action == "clear":
        db.execute("DELETE FROM list_items WHERE list=?", (name,))
        return {"ok": True, "cleared": name, "removed_count": len(rows)}
    return {"error": f"unknown action {action}"}


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 1}


@tool(
    "memory",
    "Personal facts the user asks you to remember ('remember the spare key is in the blue drawer', 'my car's oil "
    "change was at 82,000 miles'). action 'remember' (text = the fact, written in third person, e.g. 'The spare key "
    "is in the blue drawer'), 'recall' (text = what they're asking about), 'forget' (text), 'list'.",
    {"action": {"type": "string", "enum": ["remember", "recall", "forget", "list"]}, "text": {"type": "string"}},
    ["action"],
)
def memory(settings, action: str, text: str = ""):
    db = ctx.db
    rows = db.all("SELECT * FROM memories ORDER BY id DESC")
    if action == "remember":
        if not text.strip():
            return {"error": "What should I remember?"}
        db.execute("INSERT INTO memories(text, created) VALUES(?,?)", (text.strip(), time.time()))
        return {"ok": True, "remembered": text.strip()}
    if action == "list":
        return {"memories": [r["text"] for r in rows] or "nothing saved yet"}
    q = _keywords(text)
    scored = []
    for r in rows:
        k = _keywords(r["text"])
        overlap = len(q & k) + 0.5 * sum(1 for a in q for b in k if a != b and (a in b or b in a) and len(a) > 3)
        fuzzy = difflib.SequenceMatcher(None, text.lower(), r["text"].lower()).ratio()
        scored.append((overlap + fuzzy, r))
    scored.sort(key=lambda x: -x[0])
    best = [r for s, r in scored if s >= 1.0][:3]
    if action == "recall":
        if not best:
            return {"found": False, "note": "I don't have anything saved about that."}
        return {"found": True, "memories": [
            f"{r['text']} (saved {time.strftime('%B %d, %Y', time.localtime(r['created']))})" for r in best]}
    if action == "forget":
        if not best:
            return {"error": "I couldn't find that memory."}
        db.execute("DELETE FROM memories WHERE id=?", (best[0]["id"],))
        return {"ok": True, "forgot": best[0]["text"]}
    return {"error": f"unknown action {action}"}

"""Tools are the assistant's "hands": real code that gives exact answers.

The AI model never computes math, time, or weather itself -- it asks a tool.
To add a new ability, make a new file in this folder with a function decorated
with @tool(...) and import it in load_all() below.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    enabled: Callable[[Any], bool] = field(default=lambda settings: True)

    def schema(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


REGISTRY: dict[str, Tool] = {}
TOOL_TIME_LIMIT = 60


def tool(name: str, description: str, properties: dict[str, Any] | None = None,
         required: list[str] | None = None, enabled: Callable[[Any], bool] | None = None):
    """Decorator that registers a function as a tool. The function gets (settings, **arguments)."""
    def wrap(func):
        REGISTRY[name] = Tool(
            name=name,
            description=description,
            parameters={"type": "object", "properties": properties or {}, "required": required or []},
            func=func,
            enabled=enabled or (lambda settings: True),
        )
        return func
    return wrap


def load_all() -> None:
    from . import (alarms, browser, calculator, calendar, dictionary, home, lists, media, news,  # noqa: F401
                   pc, reminders, security, timeinfo, traffic, weather, websearch)
    from .. import briefing  # noqa: F401  (registers the briefing tool)


def available(settings) -> list[Tool]:
    load_all()
    return [t for t in REGISTRY.values() if t.enabled(settings)]


def run(name: str, arguments: dict[str, Any], settings) -> str:
    """Run a tool and always return text for the model (errors included, never raise)."""
    load_all()
    t = REGISTRY.get(name)
    if t is None:
        return f"ERROR: there is no tool called '{name}'."
    params = t.parameters.get("properties", {})
    clean = {k: v for k, v in (arguments or {}).items() if k in params}
    box: dict[str, Any] = {}

    def call():
        try:
            box["result"] = t.func(settings, **clean)
        except TypeError as exc:
            box["error"] = f"ERROR: wrong inputs for {name}: {exc}"
        except Exception as exc:  # tools must never crash the assistant
            log.exception("Tool %s failed", name)
            box["error"] = f"ERROR: {name} failed: {exc}"

    # A tool that hangs (slow network drive, website not answering...) must never freeze Jarvis.
    worker = threading.Thread(target=call, name=f"tool-{name}", daemon=True)
    worker.start()
    worker.join(TOOL_TIME_LIMIT)
    if worker.is_alive():
        log.error("Tool %s took longer than %d seconds; giving up on it.", name, TOOL_TIME_LIMIT)
        return f"ERROR: {name} took too long (over {TOOL_TIME_LIMIT} seconds) and was abandoned."
    if "error" in box:
        return box["error"]
    result = box.get("result")
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)

"""Talks to the AI model server.

Two "dialects" are supported:
  * Ollama's own API (/api/chat) -- lets us set the context size so all of Jarvis's tools fit.
  * The OpenAI-style API (/v1/chat/completions) -- LM Studio, llama.cpp, vLLM, cloud services...
Both can stream, so Jarvis can start speaking before the whole answer is written.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._transport = transport  # lets tests plug in a fake server

    # ------------------------------------------------------------------
    def style(self) -> str:
        cfg = self.settings.section("llm")
        if cfg["api_style"] != "auto":
            return cfg["api_style"]
        return "ollama" if ":11434" in cfg["base_url"] else "openai"

    def _root(self) -> str:
        url = self.settings.get("llm", "base_url").rstrip("/")
        if self.style() == "ollama":
            url = re.sub(r"/v1$", "", url)
        return url

    def _client(self) -> httpx.Client:
        cfg = self.settings.section("llm")
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        return httpx.Client(base_url=self._root(), headers=headers, timeout=cfg["timeout_seconds"],
                            transport=self._transport)

    # ------------------------------------------------------------------
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             on_text: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Send messages; return {'content': str, 'tool_calls': [{'id','name','arguments'}]}.
        If on_text is given and streaming is on, text pieces are passed to it as they arrive."""
        cfg = self.settings.section("llm")
        stream = bool(on_text) and cfg["stream_replies"]
        ollama = self.style() == "ollama"
        if ollama:
            path = "/api/chat"
            body: dict[str, Any] = {"model": cfg["model"], "messages": _to_ollama(messages), "stream": stream,
                                    "options": {"temperature": cfg["temperature"], "num_ctx": int(cfg["context_length"])}}
        else:
            path = "/chat/completions"
            body = {"model": cfg["model"], "messages": messages, "temperature": cfg["temperature"], "stream": stream}
        if tools:
            body["tools"] = tools
        try:
            with self._client() as client:
                if not stream:
                    resp = client.post(path, json=body)
                    _check(resp, cfg)
                    data = resp.json()
                    msg = data.get("message") if ollama else (data.get("choices") or [{}])[0].get("message")
                    if msg is None:
                        raise LLMError(f"Unexpected reply from AI server: {str(data)[:300]}")
                    return normalize_message(msg)
                with client.stream("POST", path, json=body) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        _check(resp, cfg)
                    return _read_stream(resp, ollama, on_text)
        except httpx.ConnectError as exc:
            raise LLMError(f"Can't reach the AI server at {cfg['base_url']}. Is Ollama running?") from exc
        except httpx.TimeoutException as exc:
            raise LLMError("The AI model took too long to answer.") from exc

    def list_models(self) -> list[str]:
        with self._client() as client:
            if self.style() == "ollama":
                resp = client.get("/api/tags")
                resp.raise_for_status()
                return sorted(m["name"] for m in resp.json().get("models", []))
            resp = client.get("/models")
            resp.raise_for_status()
            return sorted(m["id"] for m in resp.json().get("data", []))


def _check(resp: httpx.Response, cfg) -> None:
    if resp.status_code == 404 and "model" in resp.text.lower():
        raise LLMError(f"The model '{cfg['model']}' isn't installed on the AI server.")
    if resp.status_code >= 400:
        raise LLMError(f"AI server error {resp.status_code}: {resp.text[:300]}")


def _to_ollama(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI-style history to Ollama's format (arguments as objects, tool name on results)."""
    out = []
    for m in messages:
        m = dict(m)
        if m.get("tool_calls"):
            calls = []
            for c in m["tool_calls"]:
                args = c["function"]["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args or "{}")
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"function": {"name": c["function"]["name"], "arguments": args}})
            m["tool_calls"] = calls
        if m.get("role") == "tool":
            m = {"role": "tool", "content": m["content"], "tool_name": m.get("name", "")}
        out.append(m)
    return out


def _read_stream(resp: httpx.Response, ollama: bool, on_text) -> dict[str, Any]:
    content: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    for line in resp.iter_lines():
        line = line.strip()
        if not line:
            continue
        if not ollama:
            if not line.startswith("data:"):
                continue
            line = line[5:].strip()
            if line == "[DONE]":
                break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ollama:
            msg = chunk.get("message") or {}
            piece = msg.get("content") or ""
            for c in msg.get("tool_calls") or []:
                calls[len(calls)] = {"id": c.get("id"), "function": c.get("function", {})}
        else:
            delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
            piece = delta.get("content") or ""
            for c in delta.get("tool_calls") or []:
                slot = calls.setdefault(c.get("index", 0), {"id": None, "function": {"name": "", "arguments": ""}})
                slot["id"] = c.get("id") or slot["id"]
                fn = c.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                args = fn.get("arguments")
                if isinstance(args, dict):
                    slot["function"]["arguments"] = args
                elif args:
                    slot["function"]["arguments"] += args
        if piece:
            content.append(piece)
            on_text(piece)
        if ollama and chunk.get("done"):
            break
    return normalize_message({"content": "".join(content), "tool_calls": [calls[k] for k in sorted(calls)]})


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    text = _THINK_RE.sub("", text or "")
    if "</think>" in text:  # opening tag missing
        text = text.split("</think>", 1)[1]
    return text.strip()


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Make tool calls uniform. Small models sometimes write the tool call as JSON text
    instead of using the proper field -- we rescue those too."""
    content = strip_thinking(message.get("content") or "")
    calls = []
    for call in message.get("tool_calls") or []:
        fn = call.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                args = {}
        calls.append({"id": call.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                      "name": fn.get("name", ""), "arguments": args if isinstance(args, dict) else {}})
    if not calls and content:
        rescued = _rescue_text_tool_call(content)
        if rescued:
            calls = [rescued]
            content = ""
    return {"content": content, "tool_calls": calls}


def looks_like_tool_call(text: str) -> bool:
    t = text.lstrip()
    return t.startswith(("{", "```", "<tool_call>", "[{"))


def _rescue_text_tool_call(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    candidate = re.sub(r"^```(?:json)?|```$", "", candidate).strip()
    candidate = re.sub(r"^<tool_call>|</tool_call>$", "", candidate).strip()
    if not (candidate.startswith("{") and candidate.endswith("}")):
        return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "name": obj["name"], "arguments": args or {}}

"""Web search. Pick the engine on the settings page. Only the search words leave your PC.

To add another search engine: write a function like _tavily below, add it to PROVIDERS,
and add its name to the 'provider' options in config.py.
"""
from __future__ import annotations

import httpx

from . import geo, tool


def _searxng(settings, query: str, n: int) -> dict:
    base = settings.get("search", "searxng_url").rstrip("/")
    with geo.http_client(20) as client:
        resp = client.get(f"{base}/search", params={"q": query, "format": "json"})
    if resp.status_code == 403:
        return {"error": "SearXNG refused JSON output. Enable 'json' under search > formats in its settings.yml."}
    resp.raise_for_status()
    data = resp.json()
    results = [{"title": r.get("title"), "snippet": r.get("content"), "url": r.get("url")}
               for r in data.get("results", [])[:n]]
    out = {"results": results}
    if data.get("answers"):
        out["quick_answer"] = data["answers"][0] if isinstance(data["answers"][0], str) else str(data["answers"][0])
    return out


def _tavily(settings, query: str, n: int) -> dict:
    key = settings.get("search", "tavily_api_key")
    if not key:
        return {"error": "No Tavily API key is set on the settings page."}
    with geo.http_client(30) as client:
        resp = client.post("https://api.tavily.com/search",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"query": query, "max_results": n, "include_answer": True})
    resp.raise_for_status()
    data = resp.json()
    out = {"results": [{"title": r.get("title"), "snippet": r.get("content"), "url": r.get("url")}
                       for r in data.get("results", [])[:n]]}
    if data.get("answer"):
        out["quick_answer"] = data["answer"]
    return out


def _brave(settings, query: str, n: int) -> dict:
    key = settings.get("search", "brave_api_key")
    if not key:
        return {"error": "No Brave Search API key is set on the settings page."}
    with geo.http_client(20) as client:
        resp = client.get("https://api.search.brave.com/res/v1/web/search",
                          headers={"X-Subscription-Token": key, "Accept": "application/json"},
                          params={"q": query, "count": n})
    resp.raise_for_status()
    data = resp.json()
    return {"results": [{"title": r.get("title"), "snippet": r.get("description"), "url": r.get("url")}
                        for r in data.get("web", {}).get("results", [])[:n]]}


PROVIDERS = {"searxng": _searxng, "tavily": _tavily, "brave": _brave}


@tool(
    "web_search",
    "Search the internet. Use for news, current events, sports scores, prices, store hours, "
    "facts you are not sure about, or anything that may have changed recently.",
    {"query": {"type": "string", "description": "What to search for"}},
    ["query"],
    enabled=lambda settings: settings.get("search", "provider") in PROVIDERS,
)
def web_search(settings, query: str):
    provider = settings.get("search", "provider")
    fn = PROVIDERS.get(provider)
    if fn is None:
        return {"error": "Web search is turned off in settings."}
    try:
        return fn(settings, query, int(settings.get("search", "max_results")))
    except httpx.HTTPError as exc:
        return {"error": f"{provider} search failed: {exc}"}

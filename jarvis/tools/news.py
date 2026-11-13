"""News headlines from RSS feeds (no account) and offline Wikipedia through Kiwix."""
from __future__ import annotations

import difflib
import html
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

import httpx

from . import tool

_cache: dict[str, tuple[float, list[dict]]] = {}


def _feeds(settings) -> dict[str, str]:
    out = {}
    for line in settings.get("knowledge", "news_feeds").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+", v.strip()):
                out[k.strip()] = v.strip()
    return out


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


_discovered: dict[str, str] = {}
COMMON_FEED_PATHS = ["/feed/", "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml", "/rss/index.xml"]


def _is_feed(content: bytes) -> bool:
    head = content[:600].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf" in head


def _get(url: str) -> httpx.Response:
    return httpx.get(url, timeout=15, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Jarvis-Bot/1.0"})


def find_feed(url: str) -> str:
    """Accept a normal website (techcrunch.com) and find its news feed automatically."""
    if url in _discovered:
        return _discovered[url]
    if not url.startswith("http"):
        url = "https://" + url
    resp = _get(url)
    resp.raise_for_status()
    if _is_feed(resp.content):
        _discovered[url] = url
        return url
    # 1) the page announces its feed: <link rel="alternate" type="application/rss+xml" href="...">
    for tag in re.findall(r"<link[^>]+>", resp.text, re.I):
        if re.search(r"application/(rss|atom)\+xml", tag, re.I):
            m = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if m:
                feed = urljoin(str(resp.url), html.unescape(m.group(1)))
                _discovered[url] = feed
                return feed
    # 2) try the usual places
    base = f"{resp.url.scheme}://{resp.url.host}"
    for path in COMMON_FEED_PATHS:
        try:
            r = _get(base + path)
            if r.status_code == 200 and _is_feed(r.content):
                _discovered[url] = base + path
                return base + path
        except httpx.HTTPError:
            continue
    raise ValueError(f"I couldn't find a news feed on {url}.")


def fetch_feed(url: str) -> list[dict]:
    if url in _cache and time.time() - _cache[url][0] < 600:
        return _cache[url][1]
    resp = _get(find_feed(url))
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = summary = ""
        for child in it:
            ct = child.tag.split("}")[-1]
            if ct == "title":
                title = _clean(child.text or "")
            elif ct in ("description", "summary") and not summary:
                summary = _clean(child.text or "")[:240]
        if title:
            items.append({"title": title, "summary": summary})
    _cache[url] = (time.time(), items)
    return items


def headlines(settings, topic: str = "", count: int = 5) -> list[dict]:
    feeds = _feeds(settings)
    if not feeds:
        return []
    name = next(iter(feeds))
    if topic:
        match = difflib.get_close_matches(topic.lower(), [k.lower() for k in feeds], n=1, cutoff=0.4)
        if match:
            name = next(k for k in feeds if k.lower() == match[0])
    return fetch_feed(feeds[name])[:count]


@tool(
    "news",
    "Latest news headlines. topic = one of the user's feeds (e.g. 'top news', 'world', 'technology', 'sports') or empty.",
    {"topic": {"type": "string"}, "count": {"type": "integer"}},
)
def news(settings, topic: str = "", count: int = 5):
    items = headlines(settings, topic, max(1, min(10, int(count or 5))))
    if not items:
        return {"error": "No news feeds are set up (Settings > News & offline Wikipedia)."}
    return {"feeds_available": list(_feeds(settings)), "headlines": items}


# ---------------------------------------------------------------- offline Wikipedia (Kiwix)
def _text_from_html(page: str, limit: int = 1800) -> str:
    page = re.sub(r"(?s)<(script|style|table|sup)[^>]*>.*?</\1>", " ", page)
    paras = re.findall(r"(?s)<p[^>]*>(.*?)</p>", page)
    text = " ".join(_clean(p) for p in paras if len(_clean(p)) > 40)
    return text[:limit]


@tool(
    "wikipedia",
    "Look something up in the offline Wikipedia on this PC (private; nothing goes online). Good for general "
    "knowledge: people, places, history, science, how things work.",
    {"query": {"type": "string"}},
    ["query"],
    enabled=lambda s: bool(s.get("knowledge", "kiwix_url")),
)
def wikipedia(settings, query: str):
    base = settings.get("knowledge", "kiwix_url").rstrip("/") + "/"
    book = settings.get("knowledge", "kiwix_book").strip()
    params = {"pattern": query, "format": "xml", "pageLength": 3}
    if book:
        params["books.name"] = book
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        resp = client.get(urljoin(base, "search"), params=params)
        links = []
        if resp.status_code == 200 and resp.text.lstrip().startswith("<?xml"):
            root = ET.fromstring(resp.content)
            for item in root.iter("item"):
                link = item.findtext("link")
                if link:
                    links.append((item.findtext("title") or "", link))
        if not links:  # older kiwix-serve: HTML results page
            resp = client.get(urljoin(base, "search"), params={"pattern": query, **({"content": book} if book else {})})
            for href, title in re.findall(r'<a href="([^"]+)">\s*(.*?)\s*</a>', resp.text)[:3]:
                if "/content/" in href or "/A/" in href:
                    links.append((_clean(title), href))
        if not links:
            return {"found": False, "note": f"Nothing about '{query}' in offline Wikipedia."}
        title, link = links[0]
        page = client.get(urljoin(base, link.lstrip("/")))
        return {"found": True, "article": title, "text": _text_from_html(page.text),
                "other_matches": [t for t, _ in links[1:]]}

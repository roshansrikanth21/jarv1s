"""
web_search.py — Multi-mode web search (Mark-XLVII pattern, JARVIS stack).

Modes: search (default), news, research, price, compare.
Uses duckduckgo-search when installed; falls back to DDG HTML scrape.
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from typing import Any

MODES = ("search", "news", "research", "price", "compare")
PANEL_MIN_CHARS = 120


def _ddg_html(query: str, max_results: int = 6) -> list[dict[str, str]]:
    q = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={q}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        html = resp.read().decode("utf-8", errors="ignore")
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>', html, re.DOTALL)
    urls = re.findall(r'class="result__url"[^>]*>(.*?)</a>', html, re.DOTALL)
    out: list[dict[str, str]] = []
    for i in range(min(max_results, max(len(titles), len(snippets), 1))):
        title = re.sub(r"<[^>]+>", "", titles[i]).strip() if i < len(titles) else ""
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        href = re.sub(r"<[^>]+>", "", urls[i]).strip() if i < len(urls) else ""
        if title or snippet:
            out.append({"title": title, "snippet": snippet, "url": href})
    return out


def _ddg_api(query: str, max_results: int = 6, *, kind: str = "text") -> list[dict[str, str]]:
    """DDG API-backed search. `kind='news'` uses ddgs.news() which returns real article
    URLs with sources + timestamps — much better than ddgs.text() for news queries,
    which returns website homepages instead of articles."""
    try:
        try:
            from ddgs import DDGS  # type: ignore
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore
        results: list[dict[str, str]] = []
        with DDGS() as ddgs:
            if kind == "news":
                for r in ddgs.news(query, max_results=max_results):
                    # news() returns: date, title, body, url, image, source
                    src = r.get("source", "") or ""
                    date = (r.get("date", "") or "")[:10]   # yyyy-mm-dd slice
                    header = f"{src} — {date}" if (src or date) else ""
                    results.append({
                        "title":   r.get("title", "") or "",
                        "snippet": (f"[{header}] " if header else "") + (r.get("body", "") or ""),
                        "url":     r.get("url", "") or "",
                    })
            else:
                for r in ddgs.text(query, max_results=max_results):
                    results.append({
                        "title":   r.get("title", "") or "",
                        "snippet": r.get("body", "") or "",
                        "url":     r.get("href", "") or "",
                    })
        if results:
            return results
    except Exception:
        pass
    return _ddg_html(query, max_results)


def _format_results(query: str, results: list[dict[str, str]], header: str | None = None) -> str:
    if not results:
        return f"No results found for: {query}"
    lines = [header or f"Search results for: {query}", ""]
    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        if r.get("url"):
            lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _shape_query(query: str, mode: str, items: list[str] | None, aspect: str) -> str:
    mode = (mode or "search").lower().strip()
    if mode == "news":
        return f"latest news {query} today".strip()
    if mode == "research":
        return f"comprehensive overview {query}".strip()
    if mode == "price":
        return f"{query} price buy cost".strip()
    if mode == "compare" and items:
        joined = " vs ".join(items[:4])
        asp = f" {aspect}" if aspect else ""
        return f"compare{asp}: {joined}"
    return query.strip()


def search(
    query: str,
    mode: str = "search",
    *,
    items: list[str] | None = None,
    aspect: str = "",
    max_results: int = 6,
) -> dict[str, Any]:
    """
    Returns {text, title, panel_body, mode}.
    panel_body is set when the formatted result is long enough for the UI content panel.
    """
    mode = (mode or "search").lower().strip()
    if mode not in MODES:
        mode = "search"

    if mode == "compare" and items and len(items) >= 2:
        blocks: list[str] = []
        for item in items[:4]:
            shaped = _shape_query(item, "research", None, aspect)
            res = _ddg_api(shaped, max_results=3)
            blocks.append(_format_results(item, res, header=f"=== {item} ==="))
        text = "\n\n".join(blocks)
        title = f"COMPARE — {', '.join(items[:3])}"
    else:
        shaped = _shape_query(query, mode, items, aspect)
        # News mode → ddgs.news() for real article URLs with source+date, not homepages.
        kind = "news" if mode == "news" else "text"
        results = _ddg_api(shaped, max_results=max_results if mode != "research" else 8,
                           kind=kind)
        label = mode.upper()
        title = f"{label} — {query[:48]}"
        text = _format_results(shaped, results, header=f"{label}: {query}")

    panel_body = text if len(text) >= PANEL_MIN_CHARS else None
    return {"text": text, "title": title, "panel_body": panel_body, "mode": mode}


def fetch_headlines(n: int = 5) -> tuple[list[str], str]:
    """Headlines for morning briefing — returns (titles, full_display_text).
    Uses ddgs.news() so we get real article URLs with sources + dates, not homepages."""
    results = _ddg_api("world news today headlines", max_results=max(n, 6), kind="news")
    titles = [r["title"] for r in results if r.get("title")][:n]
    body = _format_results("world news today", results, header="Latest headlines")
    return titles, body

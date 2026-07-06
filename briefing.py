"""
briefing.py — Morning startup briefing (Mark-XLVII two-phase pattern).

Phase 1: instant spoken greeting (time, name, weather hint).
Phase 2: async news fetch → content panel + short spoken summary.
"""
from __future__ import annotations

from datetime import datetime

import ambient
import web_search


def today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def greeting_text(user_name: str, memories: list[dict]) -> str:
    """Phase 1 — speak immediately, no network."""
    nm = user_name.strip() or "sir"
    hour = datetime.now().hour
    if hour < 12:
        salute = "Good morning"
    elif hour < 17:
        salute = "Good afternoon"
    else:
        salute = "Good evening"

    time_str = datetime.now().strftime("%I:%M %p").lstrip("0")
    parts = [f"{salute}, {nm}. It's {time_str}."]

    lang = _memory_value(memories, "language")

    try:
        snap = ambient.snapshot()
        wx = snap.get("weather") or {}
        loc = snap.get("location") or {}
        if wx.get("casual"):
            parts.append(f"It's {wx['casual']} out.")
        elif wx.get("label"):
            parts.append(f"It's {wx['label']} out.")
        elif wx.get("temp_c") is not None:
            parts.append(f"Temperature is around {wx['temp_c']:.0f} degrees.")
        elif loc.get("city"):
            parts.append(f"You're in {loc['city']}.")
    except Exception:
        pass

    parts.append("I'm pulling today's headlines — they'll appear on screen in a moment.")
    return " ".join(parts)


def _memory_value(memories: list[dict], key: str) -> str:
    key_l = key.lower()
    for m in memories:
        cat = (m.get("category") or "").lower()
        content = (m.get("content") or "").lower()
        if key_l in cat or key_l in content:
            # Prefer explicit "language: Turkish" style
            raw = m.get("content", "")
            if ":" in raw:
                k, _, v = raw.partition(":")
                if k.strip().lower() == key_l:
                    return v.strip()
            if cat == "personal" and key_l == "language":
                return raw.strip()
    return ""


async def fetch_news_phase() -> dict:
    """Phase 2 — network; returns {titles, panel_title, panel_body, speak}."""
    titles, body = await __import__("asyncio").to_thread(web_search.fetch_headlines, 5)
    if not titles:
        return {
            "titles": [],
            "panel_title": "Briefing — news unavailable",
            "panel_body": None,
            "speak": "I couldn't reach the news feeds right now. Ask me again in a moment if you need headlines.",
        }

    headline_lines = ". ".join(f"{i + 1}: {t}" for i, t in enumerate(titles[:2]))
    speak = (
        f"Headlines are on screen. Top stories: {headline_lines}. "
        "Want me to dig into any of them?"
    )
    return {
        "titles": titles,
        "panel_title": "BRIEFING — latest headlines",
        "panel_body": body,
        "speak": speak,
    }

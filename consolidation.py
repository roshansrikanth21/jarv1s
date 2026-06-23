"""
consolidation.py — memory "sleep" cycle.

While the machine is idle and charging, JARVIS consolidates: it compresses recent
episodic conversation turns into a small set of durable, factual semantic memories,
merging duplicates and letting stale low-importance memories decay. One LLM call
per cycle (ideally a LOCAL model, so reflection stays on-device); all the cheap
arithmetic — the gate, decay, dedup, write-back — is plain code.

Grounded in Generative Agents (importance + reflection), Letta sleep-time compute
(raw→learned context), and ACT-R/FadeMem (importance-modulated, access-reinforced
forgetting).
"""
from __future__ import annotations

import json
import re
import time

CONSOLIDATE_EVERY = 6      # new user+assistant turns (3 exchanges) before a cycle is worthwhile
_NOW = time.time


def should_consolidate(history: list[dict], last_consolidated_len: int) -> bool:
    return (len(history) - last_consolidated_len) >= CONSOLIDATE_EVERY and len(history) >= 4


def decay_and_prune(memories: list[dict]) -> tuple[list[dict], int]:
    """Importance-modulated exponential forgetting with access reinforcement. Returns
    (survivors, dropped_count). Never drops importance>=9 ('pinned') memories."""
    import math
    now = _NOW()
    survivors, dropped = [], 0
    for m in memories:
        imp = m.get("importance", 5)
        if imp >= 9:
            survivors.append(m)
            continue
        last = m.get("last_access") or m.get("ts") or now
        days = max(0.0, (now - last) / 86400.0)
        half_life = 3.0 + 2.0 * imp                       # imp1≈5d … imp8≈19d
        strength = 0.5 ** (days / half_life)
        strength *= 1.0 + 0.15 * math.log1p(m.get("access_count", 0))
        if strength >= 0.05:
            survivors.append(m)
        else:
            dropped += 1
    return survivors, dropped


_PROMPT = """You are the memory consolidator for a personal AI assistant. You run while the device is idle. Compress raw conversation turns into a SMALL set of durable, factual memory statements about the user and their world.

RECENT CONVERSATION (newest last):
{turns}

EXISTING DURABLE MEMORIES (index: text):
{existing}

Rules:
- Each durable memory is ONE standalone declarative sentence, present tense, third person ("User ..."), context-free (resolve pronouns/"this"/"my").
- Keep only durable, reusable facts: identity, preferences, ongoing projects, relationships, decisions, recurring patterns. DISCARD chit-chat, greetings, one-offs, and anything already captured.
- MERGE duplicates/updates: if a new fact supersedes an existing memory, put the existing index in "delete" and add the merged fact to "add".
- Resolve contradictions in favor of the most recent turn.
- Rate importance 1-10 (1=mundane, 10=identity-defining). category one of: personal, preference, project, security, fact.

Return ONLY compact JSON, no prose:
{{"add": [{{"content": "...", "category": "project", "importance": 7}}], "delete": [<existing indices to drop>]}}
If there is nothing worth storing, return {{"add": [], "delete": []}}."""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


async def consolidate(memories: list[dict], history: list[dict], llm_call) -> dict:
    """Run one consolidation cycle. `llm_call` is async: prompt -> text.
    Returns {memories, added, dropped, summary}. Decays first (cheap), then the
    single LLM call merges new episodic turns into durable memory."""
    memories = list(memories)
    memories, decayed = decay_and_prune(memories)

    turns = "\n".join(
        f"- {t.get('role', '?')}: {str(t.get('content', ''))[:300]}"
        for t in history[-CONSOLIDATE_EVERY * 2:]
    )
    existing = "\n".join(f"{i}: {m.get('content', '')}" for i, m in enumerate(memories[-25:])) or "(none)"
    offset = max(0, len(memories) - 25)

    added = 0
    try:
        raw = await llm_call(_PROMPT.format(turns=turns, existing=existing))
    except Exception:
        raw = ""
    parsed = _extract_json(raw)

    if parsed:
        now = _NOW()
        # deletes are indices into the shown window (offset-adjusted)
        to_drop = set()
        for idx in parsed.get("delete", []) or []:
            try:
                to_drop.add(offset + int(idx))
            except (ValueError, TypeError):
                pass
        memories = [m for i, m in enumerate(memories) if i not in to_drop]
        decayed += len(to_drop)

        existing_texts = {m.get("content", "").strip().lower() for m in memories}
        for item in parsed.get("add", []) or []:
            content = (item.get("content") or "").strip()
            if not content or content.lower() in existing_texts:
                continue
            memories.append({
                "id": len(memories) + 1,
                "content": content,
                "category": item.get("category", "fact"),
                "importance": int(item.get("importance", 5)),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "ts": now,
                "last_access": now,
                "access_count": 0,
                "source": "consolidation",
            })
            existing_texts.add(content.lower())
            added += 1

    summary = f"consolidated · +{added} learned · {decayed} faded" if (added or decayed) else "nothing new to learn"
    return {"memories": memories, "added": added, "dropped": decayed, "summary": summary}

"""cortex — JARVIS's cognitive memory layer.

Metaphor: past / present / future — three tables that mirror how humans hold time,
plus a single-row emotion_state. Every model call flows through this before it
reaches the LLM.

Public API (all sync, safe to call from either event loop or plain code):

    cortex.init()                                 — set up SQLite + Chroma + migrate
    cortex.record_turn(user, assistant, ...)      — persist episode + kick extraction
    cortex.build_system_prompt(user_prompt, hooks)— assemble the LLM system message
    cortex.recall(query, k=6, namespace=None)     — semantic search over facts
    cortex.remember(text, ...)                    — write a durable fact directly
    cortex.forget(fact_id)                        — delete + de-index one fact
    cortex.stats()                                — health / observability
    cortex.dreaming.run_once()                    — one nightly consolidation cycle
"""
from __future__ import annotations

import logging
from typing import Optional

from . import (dreaming, embeddings, emotion, extract, migrate, paths,
               prompt as _prompt, router, store, vectors)
from .prompt import PromptHooks, build_system_prompt

log = logging.getLogger("jarvis.cortex")

_INITIALIZED = False


def init() -> dict:
    """Idempotent boot: SQLite schema, Chroma (or in-RAM), legacy migration."""
    global _INITIALIZED
    if _INITIALIZED:
        return stats()
    store.init()
    vectors.init()
    migrate.run_if_needed()
    _INITIALIZED = True
    s = stats()
    log.info("cortex: ready — %s", s)
    return s


def record_turn(user_text: str, assistant_text: str, *,
                source: str = "chat", namespace: str = "personal",
                schedule_extraction: bool = True) -> str:
    """Persist the exchange as one episode; kick extraction fire-and-forget.
    Returns the new episode id."""
    init()
    raw = f"user: {user_text}\nassistant: {assistant_text}".strip()
    eid = store.add_episode(raw, source=source)
    vectors.index_episode({"id": eid, "raw_text": raw, "source": source,
                           "timestamp": store.utcnow()})
    if schedule_extraction:
        extract.schedule(user_text, assistant_text,
                         source_episode_id=eid, namespace=namespace)
    return eid


def recall(query: str, k: int = 6, namespace: Optional[str] = None,
           include_private: bool = True) -> list[dict]:
    """Semantic recall over facts. Each result is a fact dict from cortex.store."""
    init()
    hits = vectors.search_facts(query, k=k, namespace=namespace,
                                include_private=include_private)
    ids = [h["id"] for h in hits]
    if not ids:
        return []
    store.reinforce_facts(ids)
    return store.get_facts(ids)


def remember(text: str, *, category: str = "preference", confidence: float = 0.85,
             namespace: str = "personal", source_model: str = "jarvis",
             private: bool = False, importance: int = 5) -> str:
    """Write a durable fact directly (used by explicit `remember` tool + hub endpoints)."""
    init()
    fid = store.add_fact(text, category=category, confidence=confidence,
                         namespace=namespace, source_model=source_model,
                         private=private, importance=importance)
    if fid:
        vectors.index_fact(store.get_fact(fid) or {})
    return fid


def forget(fact_id: str) -> bool:
    """Delete + de-index one fact by id."""
    init()
    ok = store.forget_fact(fact_id)
    if ok:
        vectors.delete_fact(fact_id)
    return ok


def stats() -> dict:
    s = store.stats()
    s.update({"vectors": vectors.stats()})
    return s


__all__ = [
    "PromptHooks", "build_system_prompt",
    "init", "record_turn", "recall", "remember", "forget", "stats",
    "dreaming", "embeddings", "emotion", "extract", "migrate", "paths",
    "router", "store", "vectors",
]

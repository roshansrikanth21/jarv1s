"""
mem_recall.py — semantic recall over JARVIS's memory store.

Upgrades recall from substring match to embedding cosine similarity, re-ranked by
importance and recency (the same strength curve `consolidation.decay_and_prune` uses,
so what JARVIS keeps and what it surfaces agree). Embeds via the local Ollama
`nomic-embed-text` model to keep recall on-device, matching the rest of JARVIS's
local-compute layer. If Ollama is unavailable it degrades to substring match, so
recall never hard-fails.

Vectors are cached in-memory (keyed by content hash), NOT persisted — this keeps
jarvis_memory.json human-readable, and re-embedding a few hundred memories on startup
costs well under a second.
"""
from __future__ import annotations

import hashlib
import math
import time

EMBED_MODEL = "nomic-embed-text"
MIN_SIM = 0.30                       # below this, a semantic "hit" is noise
_vec_cache: dict[str, list[float]] = {}


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def embed(text: str) -> list[float] | None:
    """Embed `text` via Ollama, cached. Returns None if Ollama is unreachable — callers
    treat None as 'fall back to substring'. Supports both the old (`embeddings`/`prompt`)
    and new (`embed`/`input`) ollama-python signatures."""
    text = (text or "").strip()
    if not text:
        return None
    k = _key(text)
    if k in _vec_cache:
        return _vec_cache[k]
    try:
        import ollama
        v = None
        try:
            r = ollama.embeddings(model=EMBED_MODEL, prompt=text)
            v = r.get("embedding") if isinstance(r, dict) else getattr(r, "embedding", None)
        except Exception:
            r = ollama.embed(model=EMBED_MODEL, input=text)
            embs = r.get("embeddings") if isinstance(r, dict) else getattr(r, "embeddings", None)
            v = embs[0] if embs else None
        if v:
            _vec_cache[k] = list(v)
            return _vec_cache[k]
    except Exception:
        return None
    return None


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _strength(m: dict) -> float:
    """Recency+importance+access strength, identical to consolidation's decay curve."""
    imp = m.get("importance", 5)
    now = time.time()
    last = m.get("last_access") or m.get("ts") or now
    days = max(0.0, (now - last) / 86400.0)
    half_life = 3.0 + 2.0 * imp
    s = 0.5 ** (days / half_life)
    s *= 1.0 + 0.15 * math.log1p(m.get("access_count", 0))
    return s


def _visible(m: dict, namespace: str | None, include_private: bool) -> bool:
    if m.get("private") and not include_private:
        return False
    if namespace and m.get("namespace", "personal") != namespace:
        return False
    return True


def _substring(query: str, memories: list[dict], k: int,
               namespace: str | None, include_private: bool) -> list[dict]:
    q = query.lower()
    hits = [m for m in memories
            if q in m.get("content", "").lower()
            and _visible(m, namespace, include_private)]
    return hits[-k:]


def recall(query: str, memories: list[dict], k: int = 6,
           namespace: str | None = None, include_private: bool = False,
           reinforce: bool = True) -> list[dict]:
    """Semantic top-k over `memories`, most relevant first. Blends cosine similarity
    with the decay strength so a slightly-less-similar but important/recent memory can
    outrank a stale exact match. Reinforces access_count/last_access on the memories it
    returns (the caller is responsible for persisting). Falls back to substring match
    when embeddings are unavailable."""
    qv = embed(query)
    if qv is None:
        return _substring(query, memories, k, namespace, include_private)

    scored: list[tuple[float, dict]] = []
    for m in memories:
        if not _visible(m, namespace, include_private):
            continue
        mv = embed(m.get("content", ""))
        if mv is None:
            continue
        sim = _cos(qv, mv)
        if sim < MIN_SIM:
            continue
        score = sim * (0.6 + 0.4 * _strength(m))
        scored.append((score, m))

    if not scored:
        return _substring(query, memories, k, namespace, include_private)

    scored.sort(key=lambda t: t[0], reverse=True)
    top = [m for _, m in scored[:k]]

    if reinforce:
        now = time.time()
        for m in top:
            m["access_count"] = m.get("access_count", 0) + 1
            m["last_access"] = now
    return top


def warm(memories: list[dict]) -> int:
    """Pre-embed all visible memories (call once at startup). Returns how many embedded."""
    n = 0
    for m in memories:
        if embed(m.get("content", "")) is not None:
            n += 1
    return n

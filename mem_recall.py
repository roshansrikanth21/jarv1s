"""
mem_recall.py — semantic recall over JARVIS's memory store.

Ranks memories by relevance to a query, re-ranked by importance and recency (the same
strength curve `consolidation.decay_and_prune` uses, so what JARVIS keeps and what it
surfaces agree).

Relevance backend, chosen once per process and then locked (so we never mix vectors of
different dimensionality within one recall):
  1. Ollama `nomic-embed-text` embeddings — on-device, matches JARVIS's local layer.
  2. sentence-transformers `all-MiniLM-L6-v2` — only if the package is already installed
     (it is NOT a hard dependency; it would pull in torch).
  3. keyword overlap — a dependency-free fallback that scores shared significant tokens
     (plus an exact-substring boost). Much stronger than plain substring matching, and it
     logs ONCE so a cloud-only install knows semantic recall is off (not silently degraded).

Vectors are cached in-memory (keyed by content hash), NOT persisted — this keeps
jarvis_memory.json human-readable, and re-embedding a few hundred memories is sub-second.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import time

log = logging.getLogger("jarvis")

EMBED_MODEL = "nomic-embed-text"
ST_MODEL = "all-MiniLM-L6-v2"
MIN_SIM = 0.30                       # below this, a semantic "hit" is noise
_vec_cache: dict[str, list[float]] = {}

# Which backend this process locked onto: None (undecided) | "ollama" | "st" | "none".
_backend: str | None = None
_st_model = None


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()


def _ollama_embed(text: str) -> list[float] | None:
    """Embed via Ollama. Supports both the old (`embeddings`/`prompt`) and new
    (`embed`/`input`) ollama-python signatures. None if Ollama isn't reachable."""
    try:
        import ollama
        try:
            r = ollama.embeddings(model=EMBED_MODEL, prompt=text)
            v = r.get("embedding") if isinstance(r, dict) else getattr(r, "embedding", None)
        except Exception:
            r = ollama.embed(model=EMBED_MODEL, input=text)
            embs = r.get("embeddings") if isinstance(r, dict) else getattr(r, "embeddings", None)
            v = embs[0] if embs else None
        return list(v) if v else None
    except Exception:
        return None


def _st_embed(text: str) -> list[float] | None:
    """Best-effort sentence-transformers embedding. Used only if the package is already
    installed — it is NOT a hard dependency (it would pull in torch). None otherwise."""
    global _st_model
    if _st_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _st_model = SentenceTransformer(ST_MODEL)
        except Exception:
            return None
    try:
        return [float(x) for x in _st_model.encode(text)]
    except Exception:
        return None


def embed(text: str) -> list[float] | None:
    """Embed `text`, cached, via a single locked backend. Returns None when no embedder is
    available (callers then fall back to keyword scoring). The first call decides the backend;
    later calls stick to it so a transient miss can't mix incompatible vectors mid-recall."""
    global _backend
    text = (text or "").strip()
    if not text:
        return None
    k = _key(text)
    if k in _vec_cache:
        return _vec_cache[k]
    if _backend == "none":
        return None
    if _backend in (None, "ollama"):
        v = _ollama_embed(text)
        if v:
            _backend = "ollama"
            _vec_cache[k] = v
            return v
        if _backend == "ollama":
            return None                       # locked to ollama; a transient miss ≠ switch
    if _backend is None:
        v = _st_embed(text)
        if v:
            _backend = "st"
            _vec_cache[k] = v
            return v
        _backend = "none"                     # nothing available → keyword mode, announced once
        log.info("mem_recall: no embedding backend (Ollama / sentence-transformers) available — "
                 "recall is using keyword matching. Run `ollama pull nomic-embed-text` for "
                 "semantic recall.")
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


_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 2}


def _keyword(query: str, memories: list[dict], k: int,
             namespace: str | None, include_private: bool) -> list[dict]:
    """Embedding-free ranking: shared significant tokens (normalized overlap) + an exact-
    substring boost, blended with decay strength. Surfaces 'User likes Python' for a query
    like 'what language do I prefer' — which plain substring matching would miss."""
    qtok = _tokens(query)
    ql = (query or "").lower().strip()
    scored: list[tuple[float, dict]] = []
    for m in memories:
        if not _visible(m, namespace, include_private):
            continue
        content = m.get("content", "")
        overlap = len(qtok & _tokens(content))
        sub = 1.0 if ql and ql in content.lower() else 0.0
        if overlap == 0 and not sub:
            continue
        rel = overlap / (len(qtok) or 1) + 0.5 * sub
        scored.append((rel * (0.6 + 0.4 * _strength(m)), m))
    if not scored:
        return []
    scored.sort(key=lambda t: t[0], reverse=True)
    return [m for _, m in scored[:k]]


def recall(query: str, memories: list[dict], k: int = 6,
           namespace: str | None = None, include_private: bool = False,
           reinforce: bool = True) -> list[dict]:
    """Top-k over `memories`, most relevant first. Blends relevance with the decay strength so
    a slightly-less-similar but important/recent memory can outrank a stale exact match.
    Reinforces access_count/last_access on what it returns (caller persists). Uses embeddings
    when available, else keyword overlap."""
    # Snapshot once: the store can be appended to concurrently (tool threads, the sleep
    # cycle), and embedding each candidate is slow — iterating the live list would risk
    # "list changed size during iteration". The dict objects are shared, so reinforcement
    # below still writes through to the real memories.
    snapshot = list(memories)
    qv = embed(query)
    if qv is None:
        top = _keyword(query, snapshot, k, namespace, include_private)
    else:
        scored: list[tuple[float, dict]] = []
        for m in snapshot:
            if not _visible(m, namespace, include_private):
                continue
            mv = embed(m.get("content", ""))
            if mv is None:
                continue
            sim = _cos(qv, mv)
            if sim < MIN_SIM:
                continue
            scored.append((sim * (0.6 + 0.4 * _strength(m)), m))
        if scored:
            scored.sort(key=lambda t: t[0], reverse=True)
            top = [m for _, m in scored[:k]]
        else:
            top = _keyword(query, snapshot, k, namespace, include_private)

    if reinforce and top:
        now = time.time()
        for m in top:
            m["access_count"] = m.get("access_count", 0) + 1
            m["last_access"] = now
    return top


def warm(memories: list[dict]) -> int:
    """Pre-embed all memories (call once at startup) so first-recall latency isn't paid
    mid-conversation. Returns how many embedded (0 if running in keyword mode)."""
    n = 0
    for m in list(memories):
        if embed(m.get("content", "")) is not None:
            n += 1
    return n

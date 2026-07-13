"""cortex.embeddings — embedder for cortex.vectors.

Order of preference, locked per-process on first use so a transient miss can't mix
incompatible vector dimensionalities inside one recall:

  1. Ollama `nomic-embed-text`  (~768-dim, local, free, on-device)
  2. WordHashEmbedder            (128-dim, deterministic, stdlib-only)

The WordHashEmbedder isn't a "good" embedder — it's a hashed-word bag-of-tokens —
but it's deterministic and fast, so the offline self-test can exercise the whole
pipeline without pulling models.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from typing import Callable

log = logging.getLogger("jarvis.cortex")

EMBED_MODEL = os.environ.get("JARVIS_EMBED_MODEL", "nomic-embed-text")
WORDHASH_DIM = 128

_backend: str | None = None
_ollama_dim: int | None = None
_cache: dict[str, list[float]] = {}
_word_re = re.compile(r"[a-z0-9']+")


def _normalize_tok(w: str) -> str:
    """Strip a trailing possessive `'s` (and any stray trailing apostrophe) so "user's"
    and "user" hash to the SAME token. Facts are routinely phrased "User's dog is …" while
    queries say "the user's …" or just "the user …" — without this, those two forms share
    zero tokens and a real fact can score as unrelated to an obviously-relevant query."""
    if w.endswith("'s") and len(w) > 2:
        return w[:-2]
    return w.rstrip("'")


def _wordhash_embed(text: str, dim: int = WORDHASH_DIM) -> list[float]:
    """Deterministic hashed-word bag-of-tokens embedding. Cheap and offline.
    Two texts that share content-words end up with similar vectors under cosine."""
    vec = [0.0] * dim
    tokens = [_normalize_tok(w) for w in _word_re.findall((text or "").lower())]
    tokens = [w for w in tokens if len(w) > 1]
    if not tokens:
        vec[0] = 1e-6
        return vec
    for tok in tokens:
        h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest()[:8], 16)
        idx = h % dim
        sign = 1.0 if (h >> 31) & 1 else -1.0
        vec[idx] += sign
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _ollama_embed(text: str) -> list[float] | None:
    global _ollama_dim
    try:
        import ollama
    except Exception:
        return None
    try:
        try:
            r = ollama.embeddings(model=EMBED_MODEL, prompt=text)
            v = r.get("embedding") if isinstance(r, dict) else getattr(r, "embedding", None)
        except Exception:
            r = ollama.embed(model=EMBED_MODEL, input=text)
            embs = r.get("embeddings") if isinstance(r, dict) else getattr(r, "embeddings", None)
            v = embs[0] if embs else None
        if not v:
            return None
        v = [float(x) for x in v]
        if _ollama_dim is None:
            _ollama_dim = len(v)
        return v
    except Exception:
        return None


def _pick_backend(sample: str) -> None:
    global _backend
    if _backend is not None:
        return
    if os.environ.get("JARVIS_FORCE_WORDHASH") == "1":
        _backend = "wordhash"
        log.info("cortex.embeddings: forced WordHash backend (%d-dim)", WORDHASH_DIM)
        return
    v = _ollama_embed(sample)
    if v is not None:
        _backend = "ollama"
        log.info("cortex.embeddings: using Ollama `%s` (%d-dim)", EMBED_MODEL, len(v))
        return
    _backend = "wordhash"
    log.info("cortex.embeddings: no Ollama embed model available — using WordHash "
             "(%d-dim). Run `ollama pull %s` for semantic recall.",
             WORDHASH_DIM, EMBED_MODEL)


def backend() -> str:
    """The backend this process locked onto. Empty string until first embed()."""
    return _backend or ""


def dim() -> int:
    if _backend == "ollama":
        return _ollama_dim or 0
    return WORDHASH_DIM


def embed(text: str) -> list[float]:
    """Embed `text` with the locked backend. Returns a numeric vector — never None
    (WordHash always works, so the whole pipeline can proceed offline)."""
    text = (text or "").strip()
    if not text:
        return [0.0] * (dim() or WORDHASH_DIM)
    if text in _cache:
        return _cache[text]
    if _backend is None:
        _pick_backend(text)
    if _backend == "ollama":
        v = _ollama_embed(text)
        if v is None:
            # Ollama went away mid-run — do NOT switch backend (would mix dims).
            # Serve a zero vector; caller sees a near-miss rather than a crash.
            return [0.0] * (_ollama_dim or WORDHASH_DIM)
    else:
        v = _wordhash_embed(text)
    _cache[text] = v
    return v


def embed_batch(texts: list[str]) -> list[list[float]]:
    return [embed(t) for t in texts]


def reset_for_tests() -> None:
    """Test hook — clear cache + backend lock so a suite can force WordHash."""
    global _backend, _ollama_dim
    _backend = None
    _ollama_dim = None
    _cache.clear()

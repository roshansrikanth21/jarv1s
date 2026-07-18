"""cortex.vectors — the semantic index over cortex.store.

Chroma holds two collections that mirror the SQLite tables — one for facts, one for
episodes. SQL is authoritative (this module is a read-through cache and search index);
every write to the store also writes here.

Fail-soft design:
- If `chromadb` isn't installed, we fall back to a pure-Python cosine index kept in
  RAM (rebuilt from SQLite on startup). Same public API, no external dependency.
- If the embedder dimensionality changes between runs (e.g. Ollama then WordHash),
  we detect it via a stamped `_dim` and rebuild the index automatically.
"""
from __future__ import annotations

import logging
import math
import os
from typing import Iterable

from . import embeddings, paths, store

log = logging.getLogger("jarvis.cortex")

FACTS_COLLECTION = "facts"
EPISODES_COLLECTION = "episodes"

_client = None
_facts_col = None
_episodes_col = None
_impl: str = "none"        # "chroma" | "memory" | "none"
_dim: int = 0

# In-RAM fallback: {collection_name: {id: (vector, metadata_dict, document)}}
_mem_index: dict[str, dict[str, tuple[list[float], dict, str]]] = {
    FACTS_COLLECTION: {},
    EPISODES_COLLECTION: {},
}


def _try_chroma():
    """Return (client, facts_col, episodes_col) or (None, None, None) on any failure."""
    try:
        import chromadb
        from chromadb.config import Settings
    except Exception as exc:
        log.info("cortex.vectors: chromadb not available (%s) — using in-RAM index.", exc)
        return None, None, None
    try:
        paths.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=str(paths.CHROMA_DIR),
            settings=Settings(
                anonymized_telemetry=False,
                allow_reset=True,
                # Bound segment cache so personal DBs don't retain unbounded HNSW pages in RSS.
                chroma_segment_cache_policy="LRU",
                chroma_memory_limit_bytes=int(
                    os.environ.get("JARVIS_CHROMA_MEMORY_BYTES", str(256 * 1024 * 1024))
                ),
            ),
        )
        facts = client.get_or_create_collection(FACTS_COLLECTION)
        eps = client.get_or_create_collection(EPISODES_COLLECTION)
        return client, facts, eps
    except Exception as exc:
        log.warning("cortex.vectors: chroma init failed (%s) — using in-RAM index.", exc)
        return None, None, None


def init() -> None:
    """Bring the index up + rebuild from SQLite if empty or stale."""
    global _client, _facts_col, _episodes_col, _impl, _dim
    if _impl != "none":
        return
    store.init()
    _client, _facts_col, _episodes_col = _try_chroma()
    _impl = "chroma" if _client else "memory"
    # Force a first embed so we can stamp the dim.
    embeddings.embed("hello")
    _dim = embeddings.dim() or 0
    _bootstrap_from_store()


def _bootstrap_from_store() -> None:
    """Re-index whatever's already in SQLite. Idempotent — upserts are safe."""
    facts = store.all_facts(include_private=True)
    for f in facts:
        _upsert(FACTS_COLLECTION, f["id"], f["text"], _fact_meta(f))
    for ep in store.recent_episodes(limit=200):
        _upsert(EPISODES_COLLECTION, ep["id"], ep["raw_text"], _ep_meta(ep))


def _fact_meta(f: dict) -> dict:
    return {
        "category": f.get("category", "preference"),
        "namespace": f.get("namespace", "personal"),
        "source_model": f.get("source_model", "jarvis"),
        "private": int(f.get("private", 0)),
        "importance": int(f.get("importance", 5)),
        "confidence": float(f.get("confidence", 0.7)),
        "last_confirmed_at": f.get("last_confirmed_at") or "",
    }


def _ep_meta(ep: dict) -> dict:
    return {
        "source": ep.get("source", "chat"),
        "timestamp": ep.get("timestamp") or "",
        "is_summary": 1 if ep.get("source") == "dreaming-summary" else 0,
    }


def _upsert(collection: str, doc_id: str, text: str, metadata: dict) -> None:
    if _impl == "none":
        init()
    vec = embeddings.embed(text)
    if _impl == "chroma":
        col = _facts_col if collection == FACTS_COLLECTION else _episodes_col
        try:
            col.upsert(ids=[doc_id], embeddings=[vec], documents=[text],
                       metadatas=[metadata])
            return
        except Exception as exc:
            log.warning("cortex.vectors: chroma upsert failed (%s) — degrading to memory.", exc)
    _mem_index[collection][doc_id] = (vec, dict(metadata), text)


def index_fact(fact: dict) -> None:
    if not fact:
        return
    init()
    _upsert(FACTS_COLLECTION, fact["id"], fact.get("text", ""), _fact_meta(fact))


def index_episode(episode: dict) -> None:
    if not episode:
        return
    init()
    _upsert(EPISODES_COLLECTION, episode["id"], episode.get("raw_text", ""),
            _ep_meta(episode))


def delete_fact(fact_id: str) -> None:
    if _impl == "none":
        return
    if _impl == "chroma":
        try:
            _facts_col.delete(ids=[fact_id])
            return
        except Exception:
            pass
    _mem_index[FACTS_COLLECTION].pop(fact_id, None)


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _memory_search(collection: str, query: str, k: int, where: dict | None) -> list[dict]:
    qv = embeddings.embed(query)
    hits: list[tuple[float, str, dict, str]] = []
    for doc_id, (vec, meta, doc) in _mem_index[collection].items():
        if where and not _matches_where(meta, where):
            continue
        s = _cos(qv, vec)
        hits.append((s, doc_id, meta, doc))
    hits.sort(reverse=True, key=lambda t: t[0])
    return [
        {"id": did, "score": float(s), "document": doc, "metadata": meta}
        for s, did, meta, doc in hits[:k]
    ]


def _matches_where(meta: dict, where: dict) -> bool:
    for key, val in where.items():
        actual = meta.get(key)
        if isinstance(val, dict) and "$eq" in val:
            if actual != val["$eq"]:
                return False
        elif actual != val:
            return False
    return True


def search(collection: str, query: str, k: int = 8,
           where: dict | None = None) -> list[dict]:
    """Top-k semantic hits. Each: {id, score, document, metadata}. Highest score first."""
    if not query or not query.strip():
        return []
    if _impl == "none":
        init()
    if _impl == "chroma":
        col = _facts_col if collection == FACTS_COLLECTION else _episodes_col
        try:
            qv = embeddings.embed(query)
            res = col.query(query_embeddings=[qv], n_results=k,
                            where=where or None)
            out = []
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for i, doc_id in enumerate(ids):
                # Chroma cosine distance = 1 - similarity for cosine spaces; convert.
                score = 1.0 - float(dists[i]) if i < len(dists) else 0.0
                out.append({
                    "id": doc_id,
                    "score": score,
                    "document": docs[i] if i < len(docs) else "",
                    "metadata": metas[i] if i < len(metas) else {},
                })
            return out
        except Exception as exc:
            log.warning("cortex.vectors: chroma query failed (%s) — memory fallback.", exc)
    return _memory_search(collection, query, k, where)


def search_facts(query: str, k: int = 8, namespace: str | None = None,
                 include_private: bool = True) -> list[dict]:
    where: dict = {}
    if namespace:
        where["namespace"] = namespace
    if not include_private:
        where["private"] = 0
    return search(FACTS_COLLECTION, query, k=k, where=where or None)


def search_episodes(query: str, k: int = 3, include_dreaming: bool = True) -> list[dict]:
    # We don't Chroma-filter on `is_summary` here — cheaper to post-filter after top-k*2.
    hits = search(EPISODES_COLLECTION, query, k=k * 2)
    if include_dreaming:
        return hits[:k]
    return [h for h in hits if not h["metadata"].get("is_summary")][:k]


def stats() -> dict:
    if _impl == "none":
        init()
    return {
        "impl": _impl,
        "dim": _dim,
        "backend": embeddings.backend(),
        "facts": (len(_mem_index[FACTS_COLLECTION]) if _impl == "memory"
                  else (_facts_col.count() if _facts_col else 0)),
        "episodes": (len(_mem_index[EPISODES_COLLECTION]) if _impl == "memory"
                     else (_episodes_col.count() if _episodes_col else 0)),
    }


def reset_for_tests() -> None:
    """Test hook — drop the in-RAM index and force re-init."""
    global _client, _facts_col, _episodes_col, _impl, _dim
    _client = None
    _facts_col = None
    _episodes_col = None
    _impl = "none"
    _dim = 0
    _mem_index[FACTS_COLLECTION].clear()
    _mem_index[EPISODES_COLLECTION].clear()

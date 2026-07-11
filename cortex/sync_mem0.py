"""cortex.sync_mem0 — optional cloud mirror to Mem0's free tier.

The local SQLite store is the source of truth. This module pushes durable facts
and daily "dreaming" summaries to Mem0 so other devices / apps can read the same
memory. It is entirely optional and fail-soft:

- If `MEM0_API_KEY` isn't set → every entry point is a no-op that returns False.
- If the `mem0ai` package isn't installed → same, logged once.
- If any push errors → logged at INFO and swallowed; the local write stays.

**Private facts NEVER leave the machine.** `sync_all_facts()` skips them by
design; `sync_fact()` refuses to push a fact with `private=True`.

CLI:
    python -m cortex.sync_mem0                    # status
    python -m cortex.sync_mem0 --all              # backfill: push every non-private fact
    python -m cortex.sync_mem0 --restore          # pull memories from Mem0 into cortex
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("jarvis.cortex.sync_mem0")

USER_ID = os.environ.get("MEM0_USER_ID", "jarvis")
WRITETHROUGH = os.environ.get("JARVIS_MEM0_WRITETHROUGH", "0") == "1"

_client = None
_checked = False
_enabled = False


def _init_client() -> None:
    """Cached one-time client init. Sets `_enabled` for the rest of the process."""
    global _client, _checked, _enabled
    if _checked:
        return
    _checked = True
    key = os.environ.get("MEM0_API_KEY", "").strip()
    if not key:
        log.info("cortex.sync_mem0: MEM0_API_KEY not set — cloud mirror disabled.")
        return
    try:
        from mem0 import MemoryClient
    except Exception as exc:
        log.info("cortex.sync_mem0: mem0ai not installed (%s) — cloud mirror disabled. "
                 "Run `pip install mem0ai` to enable.", exc)
        return
    try:
        _client = MemoryClient(api_key=key)
        _enabled = True
        log.info("cortex.sync_mem0: connected to Mem0 (user_id=%s, writethrough=%s)",
                 USER_ID, WRITETHROUGH)
    except Exception as exc:
        log.info("cortex.sync_mem0: MemoryClient init failed (%s) — mirror disabled.", exc)


def enabled() -> bool:
    """Is Mem0 sync configured, importable, and connected? Cached."""
    _init_client()
    return _enabled


def reset_for_tests() -> None:
    """Test hook — force re-evaluation of the enabled state after env changes."""
    global _client, _checked, _enabled
    _client = None
    _checked = False
    _enabled = False


def _add(messages: list[dict], metadata: dict) -> bool:
    """One `client.add(...)` call, tolerant of minor API drift between mem0ai versions."""
    if not _enabled:
        return False
    try:
        _client.add(messages, user_id=USER_ID, metadata=metadata)
        return True
    except TypeError:
        # Older mem0ai signatures accept a raw string; try that.
        try:
            _client.add(messages[0]["content"] if messages else "",
                        user_id=USER_ID, metadata=metadata)
            return True
        except Exception as exc:
            log.info("cortex.sync_mem0: add failed (%s)", exc)
            return False
    except Exception as exc:
        log.info("cortex.sync_mem0: add failed (%s)", exc)
        return False


def sync_fact(fact: dict) -> bool:
    """Push one fact to Mem0. Refuses to push private facts. Returns True on success."""
    if not enabled():
        return False
    if fact.get("private"):
        return False
    text = (fact.get("text") or "").strip()
    if not text:
        return False
    return _add(
        [{"role": "user", "content": text}],
        {
            "kind": "fact",
            "category": fact.get("category", "preference"),
            "confidence": float(fact.get("confidence", 0.7)),
            "importance": int(fact.get("importance", 5)),
            "namespace": fact.get("namespace", "personal"),
            "source_model": fact.get("source_model", "jarvis"),
            "cortex_id": fact.get("id", ""),
        },
    )


def sync_episode_summary(episode: dict) -> bool:
    """Push a dreaming-summary episode. Only summaries — not every raw turn."""
    if not enabled():
        return False
    if episode.get("source") != "dreaming-summary":
        return False
    text = (episode.get("raw_text") or "").strip()
    if not text:
        return False
    return _add(
        [{"role": "assistant", "content": f"[Daily summary] {text}"}],
        {
            "kind": "dreaming-summary",
            "timestamp": episode.get("timestamp") or "",
            "cortex_id": episode.get("id", ""),
        },
    )


def sync_dreaming_result(result: dict, added_facts: Optional[list[dict]] = None) -> dict:
    """Called at the end of a dreaming cycle. Pushes the summary + newly-added facts.
    Returns a small report; safe to call from any async/sync context."""
    if not enabled():
        return {"enabled": False, "summary": 0, "facts": 0}
    summary_ok = 0
    facts_ok = 0
    if result.get("summary") and result.get("summary_episode_id"):
        ep = {
            "id": result["summary_episode_id"],
            "raw_text": result["summary"],
            "source": "dreaming-summary",
            "timestamp": (result.get("window") or ["", ""])[1],
        }
        if sync_episode_summary(ep):
            summary_ok = 1
    for f in (added_facts or []):
        if sync_fact(f):
            facts_ok += 1
    log.info("cortex.sync_mem0: pushed %d summary, %d facts", summary_ok, facts_ok)
    return {"enabled": True, "summary": summary_ok, "facts": facts_ok}


def sync_all_facts() -> dict:
    """One-time backfill: push every NON-PRIVATE fact currently in cortex to Mem0."""
    if not enabled():
        return {"enabled": False, "pushed": 0, "total": 0}
    from . import store
    facts = store.all_facts(include_private=False)
    pushed = 0
    for f in facts:
        if sync_fact(f):
            pushed += 1
    log.info("cortex.sync_mem0: backfilled %d/%d facts", pushed, len(facts))
    return {"enabled": True, "pushed": pushed, "total": len(facts)}


def _client_search(query: str, limit: int) -> list:
    """`client.search(...)` tolerant of the v1→v2 signature change. v2 requires
    `filters={'user_id': ...}`; v1 accepted `user_id=` at top level."""
    # Try v2 (filters=) first — that's what current mem0ai (>=2.0) enforces.
    try:
        r = _client.search(query, filters={"user_id": USER_ID}, limit=limit,
                           version="v2")
        return r if isinstance(r, list) else (r or {}).get("results", []) or []
    except TypeError:
        pass
    except Exception:
        # Fall through to v1 pattern below.
        pass
    try:
        r = _client.search(query, user_id=USER_ID, limit=limit)
        return r if isinstance(r, list) else (r or {}).get("results", []) or []
    except Exception as exc:
        log.info("cortex.sync_mem0: search failed (%s)", exc)
        return []


def _client_get_all(limit: int) -> list:
    """Same tolerance for `get_all`."""
    try:
        r = _client.get_all(filters={"user_id": USER_ID}, limit=limit, version="v2")
        return r if isinstance(r, list) else (r or {}).get("results", []) or []
    except TypeError:
        pass
    except Exception:
        pass
    try:
        r = _client.get_all(user_id=USER_ID)
        return r if isinstance(r, list) else (r or {}).get("results", []) or []
    except Exception as exc:
        log.info("cortex.sync_mem0: get_all failed (%s)", exc)
        return []


def search(query: str, k: int = 5) -> list[dict]:
    """Semantic search against Mem0 (cross-device recall). Returns [] if disabled."""
    if not enabled():
        return []
    hits = _client_search(query, k)
    out = []
    for item in hits:
        if not isinstance(item, dict):
            continue
        out.append({
            "text": item.get("memory") or item.get("text") or "",
            "score": item.get("score"),
            "metadata": item.get("metadata") or {},
            "id": item.get("id"),
        })
    return out


def restore_from_mem0(limit: int = 200) -> dict:
    """Pull memories from Mem0 and insert as cortex facts. Idempotent — cortex's
    dedup (exact-text within namespace) bumps confidence rather than duplicating."""
    if not enabled():
        return {"enabled": False, "restored": 0}
    from . import store, vectors
    items = _client_get_all(limit) or _client_search("", limit)
    if not items:
        return {"enabled": True, "restored": 0}
    if isinstance(items, dict):
        items = items.get("results") or items.get("memories") or []

    restored = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        text = (item.get("memory") or item.get("text") or "").strip()
        if not text:
            continue
        meta = item.get("metadata") or {}
        fid = store.add_fact(
            text,
            category=meta.get("category", "preference"),
            confidence=float(meta.get("confidence", 0.7)),
            namespace=meta.get("namespace", "personal"),
            source_model=meta.get("source_model", "mem0"),
            importance=int(meta.get("importance", 5)),
        )
        if fid:
            vectors.index_fact(store.get_fact(fid) or {})
            restored += 1
    log.info("cortex.sync_mem0: restored %d facts from Mem0", restored)
    return {"enabled": True, "restored": restored}


def status() -> dict:
    return {"enabled": enabled(), "user_id": USER_ID, "writethrough": WRITETHROUGH}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Mem0 cloud mirror for JARVIS cortex.")
    parser.add_argument("--all", action="store_true",
                        help="Push every non-private fact in cortex to Mem0.")
    parser.add_argument("--restore", action="store_true",
                        help="Pull memories from Mem0 and insert as cortex facts.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.all:
        print(sync_all_facts())
    elif args.restore:
        print(restore_from_mem0())
    else:
        print(status())


if __name__ == "__main__":
    main()

"""cortex.migrate — one-shot import from the legacy JSON stores.

Runs the first time cortex boots on a machine that already has data in
`memory/jarvis_memory.json` and `memory/jarvis_history.json`. Idempotent — if the
cortex tables already have rows, we don't re-import.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import paths, store, vectors

log = logging.getLogger("jarvis.cortex.migrate")


def _iso(ts_val) -> str:
    """Normalize a legacy timestamp to ISO-8601 UTC."""
    if isinstance(ts_val, (int, float)):
        return datetime.fromtimestamp(ts_val, tz=timezone.utc).isoformat()
    if isinstance(ts_val, str) and ts_val:
        return ts_val
    return store.utcnow()


def _load_json(path) -> list | dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("cortex.migrate: %s unreadable (%s)", path, exc)
        return None


def _map_legacy_category(cat: str) -> str:
    """Legacy tools used {fact|preference|personal|project|security|general|task}.
    Map to the new closed set."""
    if not cat:
        return "preference"
    cat = cat.lower().strip()
    if cat in ("preference", "situation", "person", "identity", "skill"):
        return cat
    if cat in ("personal", "identity"):
        return "identity"
    if cat in ("project", "task"):
        return "situation"
    if cat in ("security", "fact", "general"):
        return "preference"
    return "preference"


def run_if_needed() -> dict:
    """Idempotent legacy import. Returns a summary; never raises."""
    store.init()
    stats = store.stats()
    imported = {"facts": 0, "episodes": 0, "skipped": False}
    if stats["facts"] > 0 or stats["episodes"] > 0:
        imported["skipped"] = True
        return imported

    mem = _load_json(paths.LEGACY_MEM_JSON) or []
    for m in mem:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        fid = store.add_fact(
            text,
            category=_map_legacy_category(m.get("category", "")),
            confidence=0.85,   # legacy manual memories were user-approved → high trust
            namespace=m.get("namespace", "personal"),
            source_model=m.get("source_model", "jarvis"),
            private=bool(m.get("private", False)),
            importance=int(m.get("importance", 5)),
        )
        if fid:
            vectors.index_fact(store.get_fact(fid) or {})
            imported["facts"] += 1

    hist = _load_json(paths.LEGACY_HIST_JSON) or []
    # Legacy history is a flat list alternating user/assistant messages.
    # Pair them up and save each pair as one episode.
    it = iter(hist)
    for msg in it:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        user_text = msg.get("content") or ""
        assistant_text = ""
        for follow in it:
            if isinstance(follow, dict) and follow.get("role") == "assistant":
                assistant_text = follow.get("content") or ""
                break
        raw = f"user: {user_text}\nassistant: {assistant_text}".strip()
        if not raw:
            continue
        eid = store.add_episode(raw, source="chat", timestamp=store.utcnow())
        vectors.index_episode({"id": eid, "raw_text": raw, "source": "chat",
                               "timestamp": store.utcnow()})
        imported["episodes"] += 1

    if imported["facts"] or imported["episodes"]:
        log.info("cortex.migrate: imported %d facts, %d episodes from legacy JSON",
                 imported["facts"], imported["episodes"])
    return imported

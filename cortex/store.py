"""cortex.store — SQLite (WAL) authoritative storage for JARVIS's cognitive memory.

Three tables mirror how humans hold time — past / present / future — plus a single-row
emotion_state. Every model call flows through cortex.prompt.build_system_prompt which
reads from here; every user↔assistant exchange flows through cortex.record_turn which
writes here.

SQL is authoritative. Chroma (cortex.vectors) is an index built from these rows.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import paths

FACT_CATEGORIES = {"preference", "situation", "person", "identity", "skill"}
PROSPECTIVE_STATUSES = {"pending", "done", "snoozed", "cancelled"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id           TEXT PRIMARY KEY,
    timestamp    TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    summary      TEXT,
    source       TEXT NOT NULL DEFAULT 'chat'
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp);
CREATE INDEX IF NOT EXISTS idx_episodes_source ON episodes(source);

CREATE TABLE IF NOT EXISTS facts (
    id                 TEXT PRIMARY KEY,
    text               TEXT NOT NULL,
    category           TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 0.7,
    created_at         TEXT NOT NULL,
    last_confirmed_at  TEXT NOT NULL,
    source_episode_id  TEXT REFERENCES episodes(id) ON DELETE SET NULL,
    namespace          TEXT NOT NULL DEFAULT 'personal',
    source_model       TEXT NOT NULL DEFAULT 'jarvis',
    private            INTEGER NOT NULL DEFAULT 0,
    importance         INTEGER NOT NULL DEFAULT 5,
    access_count       INTEGER NOT NULL DEFAULT 0,
    last_access        TEXT
);
CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);
CREATE INDEX IF NOT EXISTS idx_facts_ns ON facts(namespace);
CREATE INDEX IF NOT EXISTS idx_facts_conf ON facts(confidence);

CREATE TABLE IF NOT EXISTS prospective (
    id                 TEXT PRIMARY KEY,
    description        TEXT NOT NULL,
    due_at             TEXT,
    recurrence         TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    created_at         TEXT NOT NULL,
    source_episode_id  TEXT REFERENCES episodes(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_prospective_due ON prospective(due_at);
CREATE INDEX IF NOT EXISTS idx_prospective_status ON prospective(status);

CREATE TABLE IF NOT EXISTS emotion_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    pleasure    REAL NOT NULL DEFAULT 0.1,
    arousal     REAL NOT NULL DEFAULT 0.0,
    dominance   REAL NOT NULL DEFAULT 0.3,
    attachment  REAL NOT NULL DEFAULT 0.0,
    updated_at  TEXT NOT NULL
);
"""

_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_WRITE_LOCK = threading.RLock()


def utcnow() -> str:
    """ISO-8601 UTC — the one timestamp format the whole cortex speaks."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(paths.DB_PATH), timeout=10.0, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def connect():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    """Create tables + seed the single emotion_state row. Safe to call repeatedly."""
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        with connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO emotion_state (id, updated_at) VALUES (1, ?)",
                (utcnow(),),
            )
        _INITIALIZED = True


# ── episodes (the past) ──────────────────────────────────────────────────────────
def add_episode(raw_text: str, source: str = "chat", summary: str | None = None,
                episode_id: str | None = None, timestamp: str | None = None) -> str:
    """Persist one raw conversation turn (or a dreaming summary). Returns its id."""
    init()
    eid = episode_id or uuid.uuid4().hex
    ts = timestamp or utcnow()
    with _WRITE_LOCK, connect() as conn:
        conn.execute(
            "INSERT INTO episodes (id, timestamp, raw_text, summary, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (eid, ts, raw_text, summary, source),
        )
    return eid


def mark_episodes_consolidated(episode_ids: Iterable[str], summary_id: str) -> None:
    """Mark raw turns as consolidated — their `summary` points at the dreaming episode."""
    init()
    ids = list(episode_ids)
    if not ids:
        return
    with _WRITE_LOCK, connect() as conn:
        conn.executemany(
            "UPDATE episodes SET summary = ? WHERE id = ?",
            [(summary_id, eid) for eid in ids],
        )


def unsummarized_episodes(source: str = "chat") -> list[dict]:
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, raw_text, source FROM episodes "
            "WHERE summary IS NULL AND source = ? ORDER BY timestamp ASC",
            (source,),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_episodes(limit: int = 6, include_dreaming: bool = True) -> list[dict]:
    """Newest first. Dreaming summaries surface if included; raw chat otherwise."""
    init()
    with connect() as conn:
        if include_dreaming:
            rows = conn.execute(
                "SELECT id, timestamp, raw_text, summary, source FROM episodes "
                "ORDER BY timestamp DESC LIMIT ?", (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, timestamp, raw_text, summary, source FROM episodes "
                "WHERE source != 'dreaming-summary' "
                "ORDER BY timestamp DESC LIMIT ?", (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def episodes_between(start_ts: str, end_ts: str, source: str = "chat") -> list[dict]:
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, raw_text, source FROM episodes "
            "WHERE source = ? AND timestamp >= ? AND timestamp < ? "
            "ORDER BY timestamp ASC",
            (source, start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


# ── facts (the present — durable knowledge) ─────────────────────────────────────
def add_fact(text: str, *, category: str = "preference", confidence: float = 0.7,
             source_episode_id: str | None = None, namespace: str = "personal",
             source_model: str = "jarvis", private: bool = False,
             importance: int = 5) -> str:
    """Insert a durable fact. Returns its id. Deduplicates on exact-text match within
    the same namespace by bumping last_confirmed_at + confidence."""
    init()
    text = (text or "").strip()
    if not text:
        return ""
    if category not in FACT_CATEGORIES:
        category = "preference"
    now = utcnow()
    with _WRITE_LOCK, connect() as conn:
        existing = conn.execute(
            "SELECT id, confidence FROM facts WHERE text = ? AND namespace = ?",
            (text, namespace),
        ).fetchone()
        if existing:
            new_conf = min(1.0, max(existing["confidence"], confidence) + 0.05)
            conn.execute(
                "UPDATE facts SET last_confirmed_at = ?, confidence = ?, "
                "access_count = access_count + 1 WHERE id = ?",
                (now, new_conf, existing["id"]),
            )
            return existing["id"]
        fid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO facts (id, text, category, confidence, created_at, "
            "last_confirmed_at, source_episode_id, namespace, source_model, "
            "private, importance) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (fid, text, category, float(max(0.0, min(1.0, confidence))), now, now,
             source_episode_id, namespace, source_model, 1 if private else 0,
             int(max(1, min(10, importance)))),
        )
    return fid


def get_fact(fact_id: str) -> Optional[dict]:
    init()
    with connect() as conn:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return dict(row) if row else None


def get_facts(ids: Iterable[str]) -> list[dict]:
    ids = list(ids)
    if not ids:
        return []
    init()
    q_marks = ",".join("?" * len(ids))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM facts WHERE id IN ({q_marks})", tuple(ids),
        ).fetchall()
    order = {i: n for n, i in enumerate(ids)}
    return sorted((dict(r) for r in rows), key=lambda m: order.get(m["id"], 1e9))


def all_facts(namespace: str | None = None,
              include_private: bool = True) -> list[dict]:
    init()
    q = "SELECT * FROM facts WHERE 1=1"
    args: list = []
    if namespace:
        q += " AND namespace = ?"
        args.append(namespace)
    if not include_private:
        q += " AND private = 0"
    with connect() as conn:
        rows = conn.execute(q, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def forget_fact(fact_id: str) -> bool:
    init()
    with _WRITE_LOCK, connect() as conn:
        cur = conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    return cur.rowcount > 0


def reinforce_facts(fact_ids: Iterable[str]) -> None:
    """Bump access_count + last_access on recall hits (feeds decay reinforcement)."""
    ids = list(fact_ids)
    if not ids:
        return
    init()
    now = utcnow()
    with _WRITE_LOCK, connect() as conn:
        conn.executemany(
            "UPDATE facts SET access_count = access_count + 1, last_access = ? "
            "WHERE id = ?",
            [(now, fid) for fid in ids],
        )


# ── prospective (the future) ────────────────────────────────────────────────────
def add_prospective(description: str, *, due_at: str | None = None,
                    recurrence: str | None = None,
                    source_episode_id: str | None = None) -> str:
    init()
    description = (description or "").strip()
    if not description:
        return ""
    pid = uuid.uuid4().hex
    with _WRITE_LOCK, connect() as conn:
        conn.execute(
            "INSERT INTO prospective (id, description, due_at, recurrence, status, "
            "created_at, source_episode_id) VALUES (?,?,?,?,?,?,?)",
            (pid, description, due_at, recurrence, "pending", utcnow(),
             source_episode_id),
        )
    return pid


def pending_prospective(limit: int = 20) -> list[dict]:
    init()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM prospective WHERE status = 'pending' "
            "ORDER BY due_at IS NULL, due_at ASC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_prospective_status(pid: str, status: str) -> bool:
    if status not in PROSPECTIVE_STATUSES:
        return False
    init()
    with _WRITE_LOCK, connect() as conn:
        cur = conn.execute("UPDATE prospective SET status = ? WHERE id = ?",
                           (status, pid))
    return cur.rowcount > 0


# ── emotion state (single-row PAD + attachment mirror) ──────────────────────────
def get_emotion() -> dict:
    init()
    with connect() as conn:
        row = conn.execute("SELECT * FROM emotion_state WHERE id = 1").fetchone()
    return dict(row) if row else {}


def set_emotion(*, pleasure: float | None = None, arousal: float | None = None,
                dominance: float | None = None, attachment: float | None = None) -> dict:
    init()
    cur = get_emotion()
    p = pleasure if pleasure is not None else cur.get("pleasure", 0.1)
    a = arousal if arousal is not None else cur.get("arousal", 0.0)
    d = dominance if dominance is not None else cur.get("dominance", 0.3)
    at = attachment if attachment is not None else cur.get("attachment", 0.0)
    clamp = lambda x: max(-1.0, min(1.0, float(x)))
    p, a, d, at = clamp(p), clamp(a), clamp(d), clamp(at)
    with _WRITE_LOCK, connect() as conn:
        conn.execute(
            "UPDATE emotion_state SET pleasure = ?, arousal = ?, dominance = ?, "
            "attachment = ?, updated_at = ? WHERE id = 1",
            (p, a, d, at, utcnow()),
        )
    return {"pleasure": p, "arousal": a, "dominance": d, "attachment": at}


def nudge_emotion(*, pleasure: float = 0.0, arousal: float = 0.0,
                  dominance: float = 0.0, attachment: float = 0.0) -> dict:
    cur = get_emotion()
    return set_emotion(
        pleasure=cur.get("pleasure", 0.1) + pleasure,
        arousal=cur.get("arousal", 0.0) + arousal,
        dominance=cur.get("dominance", 0.3) + dominance,
        attachment=cur.get("attachment", 0.0) + attachment,
    )


# ── stats + admin ───────────────────────────────────────────────────────────────
def stats() -> dict:
    init()
    with connect() as conn:
        e = conn.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"]
        f = conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"]
        p = conn.execute("SELECT COUNT(*) c FROM prospective "
                         "WHERE status = 'pending'").fetchone()["c"]
    return {"episodes": e, "facts": f, "pending_prospective": p}


def wipe_all() -> None:
    """Test/utility only — nukes all cortex tables. NOT used at runtime."""
    with _WRITE_LOCK, connect() as conn:
        conn.executescript(
            "DELETE FROM facts; DELETE FROM prospective; DELETE FROM episodes; "
            "DELETE FROM emotion_state; "
            "INSERT INTO emotion_state (id, updated_at) VALUES (1, '');"
        )

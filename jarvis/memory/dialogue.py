"""Short-term conversation window — SQLite authority, optional one-shot JSON import.

Replaces the dual-write of in-memory ``_history`` + ``jarvis_history.json``.
Cortex episodes remain the long-term episodic store; this is only the rolling
context window sent to the LLM each turn.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from jarvis.events.bus import bus

log = logging.getLogger("jarvis.memory.dialogue")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dialogue_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dialogue_messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    role    TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dialogue_id ON dialogue_messages(id);
"""


class DialogueStore:
    """Thread-safe rolling dialogue window with durable SQLite backing."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        window: int = 8,
        legacy_json: Path | str | None = None,
        emit_events: bool = True,
    ) -> None:
        if window < 2 or window % 2 != 0:
            window = max(2, window - (window % 2))
        self.db_path = Path(db_path)
        self.window = window
        self.legacy_json = Path(legacy_json) if legacy_json else None
        self.emit_events = emit_events
        self._lock = threading.RLock()
        self._cache: list[dict[str, str]] = []
        self._turn_seq = 0
        self._ensure()

    def _raw_connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._raw_connect()
        try:
            yield conn
        finally:
            conn.close()

    def _ensure(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                row = conn.execute(
                    "SELECT value FROM dialogue_meta WHERE key='turn_seq'"
                ).fetchone()
                if row:
                    try:
                        self._turn_seq = int(row["value"])
                    except (TypeError, ValueError):
                        self._turn_seq = 0
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM dialogue_messages"
                ).fetchone()["n"]
                if count == 0 and self.legacy_json and self.legacy_json.exists():
                    self._import_legacy(conn)
                self._reload_cache(conn)

    def _import_legacy(self, conn: sqlite3.Connection) -> None:
        try:
            raw = json.loads(self.legacy_json.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("dialogue legacy import failed: %s", exc)
            return
        if not isinstance(raw, list):
            return
        now = time.time()
        n = 0
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role not in ("user", "assistant", "system") or not isinstance(content, str):
                continue
            if not content.strip():
                continue
            conn.execute(
                "INSERT INTO dialogue_messages(role, content, ts) VALUES (?,?,?)",
                (role, content, now),
            )
            n += 1
        pairs = n // 2
        self._turn_seq = max(self._turn_seq, pairs)
        conn.execute(
            "INSERT INTO dialogue_meta(key, value) VALUES('turn_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(self._turn_seq),),
        )
        conn.execute(
            "INSERT INTO dialogue_meta(key, value) VALUES('legacy_imported', '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'",
        )
        log.info("dialogue: imported %d messages from %s", n, self.legacy_json.name)

    def _reload_cache(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT role, content FROM dialogue_messages ORDER BY id DESC LIMIT ?",
            (self.window,),
        ).fetchall()
        self._cache = [
            {"role": r["role"], "content": r["content"]} for r in reversed(rows)
        ]

    def messages(self) -> list[dict[str, str]]:
        """Copy of the rolling window (safe to hand to the LLM layer)."""
        with self._lock:
            return [dict(m) for m in self._cache]

    @property
    def turn_seq(self) -> int:
        with self._lock:
            return self._turn_seq

    def append(self, user: str, assistant: str) -> int:
        """Append one exchange; returns the new monotonic turn_seq."""
        u = (user or "").strip()
        a = (assistant or "").strip()
        if not u and not a:
            return self.turn_seq
        with self._lock:
            now = time.time()
            with self._connect() as conn:
                if u:
                    conn.execute(
                        "INSERT INTO dialogue_messages(role, content, ts) VALUES (?,?,?)",
                        ("user", u, now),
                    )
                if a:
                    conn.execute(
                        "INSERT INTO dialogue_messages(role, content, ts) VALUES (?,?,?)",
                        ("assistant", a, now),
                    )
                self._turn_seq += 1
                conn.execute(
                    "INSERT INTO dialogue_meta(key, value) VALUES('turn_seq', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(self._turn_seq),),
                )
                keep = max(self.window * 20, 40)
                conn.execute(
                    "DELETE FROM dialogue_messages WHERE id NOT IN ("
                    "  SELECT id FROM dialogue_messages ORDER BY id DESC LIMIT ?"
                    ")",
                    (keep,),
                )
                self._reload_cache(conn)
            seq = self._turn_seq
        if self.emit_events:
            try:
                bus.emit("MemoryUpdated", kind="dialogue", turn_seq=seq)
            except Exception:
                pass
        return seq

    def clear(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("DELETE FROM dialogue_messages")
                self._turn_seq = 0
                conn.execute(
                    "INSERT INTO dialogue_meta(key, value) VALUES('turn_seq', '0') "
                    "ON CONFLICT(key) DO UPDATE SET value='0'"
                )
                self._cache = []
        if self.emit_events:
            try:
                bus.emit("MemoryUpdated", kind="dialogue_clear", turn_seq=0)
            except Exception:
                pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            with self._connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM dialogue_messages"
                ).fetchone()["n"]
            return {
                "db": str(self.db_path),
                "stored_messages": n,
                "window": len(self._cache),
                "window_cap": self.window,
                "turn_seq": self._turn_seq,
            }

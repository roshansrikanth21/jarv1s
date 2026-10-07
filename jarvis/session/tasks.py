"""tasks.py — persistent task lifecycle manager.

The task list is backend state, not UI state: it must survive a UI/deck switch, outlive the
turn that created it, and read the same from every view. This manager owns that state. Each
task gets a stable, never-reused id (TASK-001, TASK-002, …) from a persisted counter, and a
real lifecycle:

    created → queued → running → waiting → completed
                          ↘            ↘
                           → failed     → cancelled

It operates in place on the SAME list object api.py exposes as `task_list` (so existing
readers — the /api snapshot, the connect replay, routers — keep working), and it keeps the
legacy `status` field (queued|active|done) in sync with the rich `state` so the current decks
render unchanged while new UIs can use `state`, `tid`, and `error`.

Pure and UI-agnostic: it takes an `on_change(items)` callback (api.py wires that to persist +
broadcast) and never imports api or touches the network itself.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

# Rich lifecycle states.
CREATED, QUEUED, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED = (
    "created", "queued", "running", "waiting", "completed", "failed", "cancelled")
STATES = (CREATED, QUEUED, RUNNING, WAITING, COMPLETED, FAILED, CANCELLED)
_TERMINAL = {COMPLETED, FAILED, CANCELLED}

# Rich state → the coarse bucket the existing decks understand (queued|active|done).
_UI_STATUS = {
    CREATED: "queued", QUEUED: "queued",
    RUNNING: "active", WAITING: "active",
    COMPLETED: "done", FAILED: "done", CANCELLED: "done",
}


def _now_hm() -> str:
    return datetime.now().strftime("%H:%M")


class TaskManager:
    def __init__(self, items: list[dict], seq_path: str | Path,
                 on_change: Callable[[list[dict]], None] | None = None):
        self._items = items
        self._seq_path = Path(seq_path)
        self._on_change = on_change
        self._counter = self._load_counter()
        self._migrate()

    # ── counter (never reuses an id, even across restarts / clears) ──────────────
    def _load_counter(self) -> int:
        try:
            n = int(json.loads(self._seq_path.read_text(encoding="utf-8")).get("counter", 0))
        except Exception:
            n = 0
        # Never hand out an id at or below one already present (survives a lost seq file).
        for it in self._items:
            try:
                n = max(n, int(str(it.get("tid", "TASK-0")).split("-")[-1]))
            except Exception:
                pass
        return n

    def _save_counter(self) -> None:
        try:
            self._seq_path.parent.mkdir(parents=True, exist_ok=True)
            self._seq_path.write_text(json.dumps({"counter": self._counter}), encoding="utf-8")
        except Exception:
            pass

    def _next_tid(self) -> str:
        self._counter += 1
        self._save_counter()
        return f"TASK-{self._counter:03d}"

    # ── bring legacy rows up to the new shape (idempotent) ───────────────────────
    def _migrate(self) -> None:
        changed = False
        _legacy = {"queued": QUEUED, "active": RUNNING, "done": COMPLETED}
        for it in self._items:
            if "state" not in it:
                it["state"] = _legacy.get(it.get("status", "queued"), QUEUED)
                changed = True
            if "tid" not in it:
                it["tid"] = self._next_tid()
                changed = True
            it["status"] = _UI_STATUS.get(it["state"], "queued")
            it.setdefault("created_at", it.get("at") or _now_hm())
            it.setdefault("updated_at", it.get("at") or _now_hm())
        if changed:
            self._emit()

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _emit(self) -> None:
        if self._on_change:
            try:
                self._on_change(self._items)
            except Exception:
                pass

    def _find(self, ident) -> dict | None:
        """Look up by TASK-### (case-insensitive), bare number, or legacy int id."""
        s = str(ident).strip().upper()
        for it in self._items:
            if str(it.get("tid", "")).upper() == s:
                return it
        # bare number → TASK-00N, or legacy numeric id
        digits = "".join(ch for ch in s if ch.isdigit())
        if digits:
            n = int(digits)
            for it in self._items:
                if str(it.get("tid", "")).upper() == f"TASK-{n:03d}":
                    return it
            for it in self._items:
                if int(it.get("id", -1)) == n:
                    return it
        return None

    # ── public API ────────────────────────────────────────────────────────────────
    def create(self, text: str, eta: str = "", state: str = QUEUED) -> dict:
        now = _now_hm()
        legacy_id = max((int(i.get("id", 0)) for i in self._items), default=0) + 1
        task = {
            "id": legacy_id,                 # kept for legacy readers
            "tid": self._next_tid(),         # stable, never-reused public id
            "t": text,
            "eta": eta or "",
            "state": state if state in STATES else QUEUED,
            "status": _UI_STATUS.get(state, "queued"),
            "error": "",
            "at": now,
            "created_at": now,
            "updated_at": now,
        }
        self._items.append(task)
        self._emit()
        return task

    def set_state(self, ident, state: str, *, error: str = "") -> dict | None:
        if state not in STATES:
            return None
        it = self._find(ident)
        if it is None:
            return None
        it["state"] = state
        it["status"] = _UI_STATUS.get(state, "queued")
        it["error"] = error or ""
        it["updated_at"] = _now_hm()
        it["at"] = it["updated_at"]
        self._emit()
        return it

    def start(self, ident) -> dict | None:
        return self.set_state(ident, RUNNING)

    def wait(self, ident) -> dict | None:
        return self.set_state(ident, WAITING)

    def complete(self, ident) -> dict | None:
        return self.set_state(ident, COMPLETED)

    def fail(self, ident, error: str = "") -> dict | None:
        return self.set_state(ident, FAILED, error=error)

    def cancel(self, ident) -> dict | None:
        return self.set_state(ident, CANCELLED)

    def get(self, ident) -> dict | None:
        return self._find(ident)

    def list(self) -> list[dict]:
        return list(self._items)

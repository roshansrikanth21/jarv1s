"""goals.py — persistent goal store.

A goal is a future intention with a deadline ("finish the report by Friday") — distinct from
a task (a unit of work in the queue) and a reminder (a timed nudge). Goals persist across
restarts, carry a deadline/priority/progress, and can be linked to a scheduled reminder. This
mirrors the TaskManager design: stable, never-reused ids (GOAL-001…) from a persisted counter,
in-place mutation of a shared list so existing readers keep working, and an on_change callback
that api.py wires to persist + broadcast. Pure and UI-agnostic — no api/network imports.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

ACTIVE, DONE, CANCELLED = "active", "done", "cancelled"
STATES = (ACTIVE, DONE, CANCELLED)
PRIORITIES = ("low", "normal", "high")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="minutes")


class GoalStore:
    def __init__(self, items: list[dict], seq_path: str | Path,
                 on_change: Callable[[list[dict]], None] | None = None):
        self._items = items
        self._seq_path = Path(seq_path)
        self._on_change = on_change
        self._counter = self._load_counter()
        self._migrate()

    def _load_counter(self) -> int:
        try:
            n = int(json.loads(self._seq_path.read_text(encoding="utf-8")).get("counter", 0))
        except Exception:
            n = 0
        for it in self._items:
            try:
                n = max(n, int(str(it.get("gid", "GOAL-0")).split("-")[-1]))
            except Exception:
                pass
        return n

    def _save_counter(self) -> None:
        try:
            self._seq_path.parent.mkdir(parents=True, exist_ok=True)
            self._seq_path.write_text(json.dumps({"counter": self._counter}), encoding="utf-8")
        except Exception:
            pass

    def _next_gid(self) -> str:
        self._counter += 1
        self._save_counter()
        return f"GOAL-{self._counter:03d}"

    def _migrate(self) -> None:
        changed = False
        for it in self._items:
            if "gid" not in it:
                it["gid"] = self._next_gid()
                changed = True
            it.setdefault("status", ACTIVE)
            it.setdefault("priority", "normal")
            it.setdefault("progress", 0)
            it.setdefault("deadline", "")
            it.setdefault("reminder_id", "")
            it.setdefault("created_at", _now_iso())
            it.setdefault("updated_at", it.get("created_at"))
            it.setdefault("warned", False)   # proactive deadline warning already fired?
        if changed:
            self._emit()

    def _emit(self) -> None:
        if self._on_change:
            try:
                self._on_change(self._items)
            except Exception:
                pass

    def _find(self, ident) -> dict | None:
        s = str(ident).strip().upper()
        for it in self._items:
            if str(it.get("gid", "")).upper() == s:
                return it
        digits = "".join(ch for ch in s if ch.isdigit())
        if digits:
            n = int(digits)
            for it in self._items:
                if str(it.get("gid", "")).upper() == f"GOAL-{n:03d}":
                    return it
        return None

    # ── public API ──────────────────────────────────────────────────────────────
    def create(self, title: str, *, deadline: str = "", priority: str = "normal",
               reminder_id: str = "") -> dict:
        now = _now_iso()
        goal = {
            "gid": self._next_gid(),
            "title": title.strip(),
            "deadline": deadline or "",
            "priority": priority if priority in PRIORITIES else "normal",
            "status": ACTIVE,
            "progress": 0,
            "reminder_id": reminder_id or "",
            "created_at": now,
            "updated_at": now,
            "warned": False,
        }
        self._items.append(goal)
        self._emit()
        return goal

    def set(self, ident, **fields) -> dict | None:
        it = self._find(ident)
        if it is None:
            return None
        for k in ("title", "deadline", "priority", "status", "progress", "reminder_id", "warned"):
            if k in fields and fields[k] is not None:
                it[k] = fields[k]
        it["updated_at"] = _now_iso()
        self._emit()
        return it

    def complete(self, ident) -> dict | None:
        return self.set(ident, status=DONE, progress=100)

    def cancel(self, ident) -> dict | None:
        return self.set(ident, status=CANCELLED)

    def get(self, ident) -> dict | None:
        return self._find(ident)

    def active(self) -> list[dict]:
        return [g for g in self._items if g.get("status") == ACTIVE]

    def list(self) -> list[dict]:
        return list(self._items)

    def due_within(self, hours: float) -> list[dict]:
        """Active goals whose deadline is within `hours` from now and not yet warned."""
        out = []
        now = datetime.now()
        for g in self._items:
            if g.get("status") != ACTIVE or g.get("warned") or not g.get("deadline"):
                continue
            try:
                dl = datetime.fromisoformat(g["deadline"])
            except Exception:
                continue
            delta_h = (dl - now).total_seconds() / 3600.0
            if 0 <= delta_h <= hours:
                out.append(g)
        return out

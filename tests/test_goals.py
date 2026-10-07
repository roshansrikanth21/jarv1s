"""Tests for the goal store (jarvis/session/goals.py): stable GOAL-### ids, lifecycle,
deadline-window detection for proactive warnings, migration, and no-reuse counter.
Run: venv\\Scripts\\python -m unittest tests.test_goals -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.session import goals as G


class GoalStoreBasics(unittest.TestCase):
    def setUp(self):
        self.items: list[dict] = []
        self.seq = os.path.join(tempfile.mkdtemp(), "gseq.json")
        self.m = G.GoalStore(self.items, self.seq)

    def test_create_and_ids(self):
        a = self.m.create("finish report", deadline="2026-10-09T18:00", priority="high")
        b = self.m.create("call vendor")
        self.assertEqual((a["gid"], b["gid"]), ("GOAL-001", "GOAL-002"))
        self.assertEqual(a["priority"], "high")
        self.assertEqual(a["status"], "active")
        self.assertIs(self.items[0], a)

    def test_lifecycle(self):
        g = self.m.create("ship it")
        self.m.set(g["gid"], progress=50)
        self.assertEqual(g["progress"], 50)
        self.m.complete(g["gid"])
        self.assertEqual((g["status"], g["progress"]), ("done", 100))
        self.assertEqual(self.m.active(), [])

    def test_lookup_forms(self):
        g = self.m.create("x")
        self.assertIs(self.m.get("GOAL-001"), g)
        self.assertIs(self.m.get("1"), g)
        self.assertIsNone(self.m.get("GOAL-999"))

    def test_due_within_and_warn_once(self):
        soon = (datetime.now() + timedelta(hours=5)).isoformat(timespec="minutes")
        far = (datetime.now() + timedelta(days=10)).isoformat(timespec="minutes")
        g1 = self.m.create("due soon", deadline=soon)
        self.m.create("due later", deadline=far)
        due = self.m.due_within(24)
        self.assertEqual([g["gid"] for g in due], [g1["gid"]])
        # Once warned, it drops out of the window.
        self.m.set(g1["gid"], warned=True)
        self.assertEqual(self.m.due_within(24), [])

    def test_counter_persists_across_restart(self):
        self.m.create("a")
        m2 = G.GoalStore(self.items, self.seq)
        self.assertEqual(m2.create("b")["gid"], "GOAL-002")


class Migration(unittest.TestCase):
    def test_legacy_goal_rows_get_fields(self):
        items = [{"title": "old goal", "deadline": "2026-10-20T18:00"}]
        seq = os.path.join(tempfile.mkdtemp(), "gseq.json")
        G.GoalStore(items, seq)
        self.assertTrue(items[0]["gid"].startswith("GOAL-"))
        self.assertEqual(items[0]["status"], "active")
        self.assertIn("progress", items[0])


if __name__ == "__main__":
    unittest.main()

"""Tests for the task lifecycle manager (jarvis/session/tasks.py): stable TASK-### ids that
never reuse, the rich state machine, legacy-status mirroring so existing decks keep working,
in-place mutation of the shared list, and migration of old rows. No api/LLM/UI.
Run: venv\\Scripts\\python -m unittest tests.test_tasks -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.session import tasks as T


class TaskManagerBasics(unittest.TestCase):
    def setUp(self):
        self.items: list[dict] = []
        self.seq = os.path.join(tempfile.mkdtemp(), "seq.json")
        self.changes = 0

        def on_change(_items):
            self.changes += 1

        self.m = T.TaskManager(self.items, self.seq, on_change=on_change)

    def test_create_assigns_stable_tid_and_mirrors_status(self):
        a = self.m.create("water the plants")
        b = self.m.create("email roshan", eta="5pm")
        self.assertEqual(a["tid"], "TASK-001")
        self.assertEqual(b["tid"], "TASK-002")
        self.assertEqual(a["state"], "queued")
        self.assertEqual(a["status"], "queued")       # legacy mirror for current decks
        self.assertIs(self.items[0], a)                # mutates the shared list in place
        self.assertEqual(self.changes, 2)

    def test_full_lifecycle_updates_state_and_legacy_status(self):
        t = self.m.create("build the thing")
        self.m.start(t["tid"])
        self.assertEqual(t["state"], "running")
        self.assertEqual(t["status"], "active")        # running → active bucket
        self.m.wait(t["tid"])
        self.assertEqual(t["status"], "active")
        self.m.complete(t["tid"])
        self.assertEqual(t["state"], "completed")
        self.assertEqual(t["status"], "done")

    def test_fail_records_error(self):
        t = self.m.create("risky op")
        self.m.fail(t["tid"], error="disk full")
        self.assertEqual(t["state"], "failed")
        self.assertEqual(t["error"], "disk full")
        self.assertEqual(t["status"], "done")

    def test_lookup_by_tid_number_or_legacy_id(self):
        t = self.m.create("x")
        self.assertIs(self.m.get("TASK-001"), t)
        self.assertIs(self.m.get("task-001"), t)       # case-insensitive
        self.assertIs(self.m.get("1"), t)              # bare number
        self.assertIs(self.m.get(t["id"]), t)          # legacy int id
        self.assertIsNone(self.m.get("TASK-999"))

    def test_ids_never_reuse_even_after_clear(self):
        self.m.create("a")
        self.m.create("b")
        self.items.clear()                              # user cleared the list
        c = self.m.create("c")
        self.assertEqual(c["tid"], "TASK-003")          # counter persisted — no reuse

    def test_counter_survives_a_new_manager_on_same_files(self):
        self.m.create("a")
        m2 = T.TaskManager(self.items, self.seq)        # restart
        n = m2.create("b")
        self.assertEqual(n["tid"], "TASK-002")


class Migration(unittest.TestCase):
    def test_legacy_rows_get_tid_and_state(self):
        items = [
            {"id": 1, "t": "old done", "status": "done", "at": "09:00"},
            {"id": 2, "t": "old queued", "status": "queued", "at": "09:01"},
        ]
        seq = os.path.join(tempfile.mkdtemp(), "seq.json")
        T.TaskManager(items, seq)
        self.assertEqual(items[0]["state"], "completed")
        self.assertEqual(items[1]["state"], "queued")
        self.assertTrue(items[0]["tid"].startswith("TASK-"))
        self.assertNotEqual(items[0]["tid"], items[1]["tid"])   # unique


if __name__ == "__main__":
    unittest.main()

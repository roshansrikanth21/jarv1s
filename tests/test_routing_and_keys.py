"""Regression tests for the routing planner, fallback walk, Groq key rotation, and app launch
resolution — the paths the voice-calibration merge touched. Run: npm run test:kernel
(or: venv\\Scripts\\python -m unittest discover -s tests)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.cognition.router import plan_from_governor  # noqa: E402

ALL = {"local_fast", "cloud_fast", "local_deep", "cloud_deep", "council"}


class PlannerRouting(unittest.TestCase):
    def test_council_autopick_is_rerouted_to_a_fast_brain(self):
        # The council has no tools and no ambient prompt, so it cannot answer "what time is it".
        plan = plan_from_governor({"rung": "council", "id": "d1"}, ALL, tools_needed=False)
        self.assertNotEqual(plan.rung, "council")
        self.assertIn(plan.rung, {"cloud_fast", "cloud_deep", "local_deep", "local_fast"})
        self.assertIn("council", plan.rationale)

    def test_council_stays_when_it_is_the_only_rung(self):
        plan = plan_from_governor({"rung": "council"}, {"council"}, tools_needed=False)
        self.assertEqual(plan.rung, "council")

    def test_tool_request_is_forced_off_a_toolless_rung(self):
        plan = plan_from_governor({"rung": "council"}, ALL, tools_needed=True)
        self.assertNotEqual(plan.rung, "council")
        self.assertTrue(plan.tools_enabled)

    def test_capable_rung_is_left_alone(self):
        plan = plan_from_governor({"rung": "cloud_fast"}, ALL, tools_needed=True)
        self.assertEqual(plan.rung, "cloud_fast")

    def test_fallbacks_never_include_council_or_the_chosen_rung(self):
        plan = plan_from_governor({"rung": "cloud_fast"}, ALL)
        self.assertNotIn("council", plan.fallbacks)
        self.assertNotIn("cloud_fast", plan.fallbacks)
        self.assertTrue(plan.fallbacks)

    def test_unavailable_rung_falls_back_to_something_available(self):
        plan = plan_from_governor({"rung": "cloud_deep"}, {"local_fast", "local_deep"})
        self.assertIn(plan.rung, {"local_fast", "local_deep"})


class ApiHelpers(unittest.TestCase):
    """These import api.py (about 0.6s warm). Module globals are saved/restored per test."""

    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def test_fallback_prefers_small_local_before_large_local(self):
        # A RAM-tight machine OOMs on the big model; the small one that fits must come first.
        fb = self.api._fallback_rung("cloud_fast", {"cloud_fast", "local_fast", "local_deep"},
                                     {"cloud_fast"})
        self.assertEqual(fb, "local_fast")

    def test_fallback_walk_skips_tried_and_terminates(self):
        avail = {"cloud_fast", "local_fast", "local_deep", "council"}
        tried = {"cloud_fast"}
        walked = []
        while True:
            nxt = self.api._fallback_rung("cloud_fast", avail, tried)
            if not nxt:
                break
            walked.append(nxt)
            tried.add(nxt)
        self.assertEqual(walked, ["local_fast", "local_deep"])   # whole lattice, never council

    def test_key_rotation_cycles_and_wraps(self):
        api = self.api
        saved = (api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY)
        try:
            api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY = ["k1", "k2", "k3"], 0, "k1"
            self.assertTrue(api._rotate_groq_key("test"))
            self.assertEqual(api.GROQ_API_KEY, "k2")
            self.assertTrue(api._rotate_groq_key("test"))
            self.assertTrue(api._rotate_groq_key("test"))
            self.assertEqual(api.GROQ_API_KEY, "k1")            # wrapped
        finally:
            api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY = saved

    def test_single_key_never_rotates(self):
        api = self.api
        saved = (api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY)
        try:
            api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY = ["only"], 0, "only"
            self.assertFalse(api._rotate_groq_key("test"))
            self.assertEqual(api.GROQ_API_KEY, "only")
        finally:
            api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY = saved

    def test_launch_resolution_is_honest(self):
        # A nonexistent exe must resolve to None (so the caller reports "not installed" instead
        # of claiming a silent success); a real Windows binary must resolve.
        self.assertIsNone(self.api._resolve_launch_target("definitely_not_a_real_app_xyz.exe"))
        if os.name == "nt":
            self.assertIsNotNone(self.api._resolve_launch_target("notepad.exe"))

    def test_launcher_never_uses_a_shell(self):
        import inspect
        src = inspect.getsource(self.api._launch_resolved)
        self.assertNotIn("shell=True", src)


class GroqKeyHealth(unittest.TestCase):
    """A stale key in .env or the app store must never leave JARVIS without a brain."""

    def setUp(self):
        import api
        self.api = api
        self._saved = (api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY,
                       set(api._groq_bad_keys), api._groq_key_alert, api._probe_groq_key)
        api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY = ["stale", "good"], 0, "stale"
        api._groq_bad_keys.clear()
        api._groq_key_alert = None

    def tearDown(self):
        api = self.api
        (api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY,
         bad, api._groq_key_alert, api._probe_groq_key) = self._saved
        api._groq_bad_keys.clear()
        api._groq_bad_keys.update(bad)

    def test_rotation_skips_known_bad_keys(self):
        api = self.api
        api.GROQ_API_KEYS, api._groq_key_idx, api.GROQ_API_KEY = ["a", "b", "c"], 0, "a"
        api._groq_bad_keys.add("b")
        self.assertTrue(api._rotate_groq_key("t"))
        self.assertEqual(api.GROQ_API_KEY, "c")            # b skipped

    def test_rejected_key_is_remembered_and_next_is_used(self):
        api = self.api
        self.assertTrue(api._groq_key_rejected())
        self.assertIn("stale", api._groq_bad_keys)
        self.assertEqual(api.GROQ_API_KEY, "good")

    def test_rejecting_the_last_usable_key_reports_none_left(self):
        api = self.api
        api._groq_key_rejected()                            # stale -> good
        self.assertFalse(api._groq_key_rejected())          # good rejected too -> nothing usable

    def test_healthcheck_switches_to_a_working_key_before_first_turn(self):
        import asyncio
        api = self.api
        api._probe_groq_key = lambda k: "invalid" if k == "stale" else "valid"
        asyncio.run(api._groq_key_healthcheck())
        self.assertEqual(api.GROQ_API_KEY, "good")
        self.assertIn("stale", api._groq_bad_keys)
        self.assertIn("stale", api._groq_key_alert.lower())

    def test_healthcheck_all_invalid_raises_an_alert(self):
        import asyncio
        api = self.api
        api._probe_groq_key = lambda k: "invalid"
        asyncio.run(api._groq_key_healthcheck())
        self.assertEqual(api._groq_bad_keys, {"stale", "good"})
        self.assertIn("rejected every", api._groq_key_alert)

    def test_offline_probe_never_marks_a_key_bad(self):
        import asyncio
        api = self.api
        api._probe_groq_key = lambda k: "unknown"           # offline / 429 / 5xx
        asyncio.run(api._groq_key_healthcheck())
        self.assertEqual(api._groq_bad_keys, set())
        self.assertEqual(api.GROQ_API_KEY, "stale")
        self.assertIsNone(api._groq_key_alert)


if __name__ == "__main__":
    unittest.main()

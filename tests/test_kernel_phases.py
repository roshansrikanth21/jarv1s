"""Extended kernel tests — router, browse/desktop policy, telemetry."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.cognition.router import HealthMonitor, plan_from_governor, provider_for_rung
from jarvis.policy.browse import check_url
from jarvis.policy.desktop import desktop_risk, requires_confirmation
from jarvis.platform import telemetry


class TestBrowsePolicy(unittest.TestCase):
    def test_block_file_scheme(self) -> None:
        self.assertIsNotNone(check_url("file:///C:/Windows/System32"))

    def test_block_localhost(self) -> None:
        self.assertIsNotNone(check_url("http://127.0.0.1/"))
        self.assertIsNotNone(check_url("http://localhost:8000/"))

    def test_allow_public_https(self) -> None:
        # example.com resolves publicly — may fail offline; then skip soft
        err = check_url("https://example.com/")
        if err and "Could not resolve" in err:
            self.skipTest("DNS unavailable")
        self.assertIsNone(err)

    def test_allowlist(self) -> None:
        err = check_url("https://evil.example/", allowlist=["allowed.test"])
        self.assertIsNotNone(err)


class TestDesktopPolicy(unittest.TestCase):
    def test_uninstall_high(self) -> None:
        self.assertEqual(desktop_risk("uninstall_app"), "high")
        self.assertTrue(requires_confirmation("uninstall_app"))

    def test_open_path_low(self) -> None:
        self.assertEqual(desktop_risk("open_path"), "low")


class TestRouter(unittest.TestCase):
    def test_provider_map(self) -> None:
        self.assertEqual(provider_for_rung("cloud_fast"), "groq")
        self.assertEqual(provider_for_rung("local_deep"), "ollama")

    def test_plan_tools_force(self) -> None:
        decision = {"rung": "council", "id": "t1", "rationale": "x"}
        plan = plan_from_governor(decision, {"council", "cloud_fast"}, tools_needed=True)
        self.assertEqual(plan.rung, "cloud_fast")
        self.assertEqual(plan.provider, "groq")

    def test_health_monitor(self) -> None:
        h = HealthMonitor(alpha=1.0)
        h.mark("groq", ok=True, latency_s=0.2)
        h.mark("groq", ok=False, latency_s=2.0)
        snap = h.snapshot()
        self.assertIn("groq", snap)
        self.assertGreater(snap["groq"]["calls"], 0)


class TestTelemetry(unittest.TestCase):
    def test_record_and_summary(self) -> None:
        telemetry.record_turn(
            decision_id="x", rung="cloud_fast", provider="groq",
            latency_s=0.5, ok=True, answer_chars=10,
        )
        s = telemetry.summary()
        self.assertGreaterEqual(s["count"], 1)
        self.assertTrue(telemetry.recent(5))


if __name__ == "__main__":
    unittest.main()

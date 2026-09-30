"""Unit tests for jarvis kernel packages (dialogue, tokens, shell policy, events).

Run from repo root:
  .\\venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jarvis.cognition.tokens import estimate_tokens, clear_token_cache
from jarvis.events.bus import EventBus
from jarvis.memory.dialogue import DialogueStore
from jarvis.policy.shell import check_command
from jarvis.session.state import SessionState, WorkflowPhase


class TestTokens(unittest.TestCase):
    def setUp(self) -> None:
        clear_token_cache()

    def test_empty(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_short_latin(self) -> None:
        n = estimate_tokens("hello world")
        self.assertGreaterEqual(n, 2)
        self.assertLessEqual(n, 8)

    def test_longer_than_char4_floor(self) -> None:
        text = "word " * 50
        n = estimate_tokens(text)
        self.assertGreater(n, 10)


class TestShellPolicy(unittest.TestCase):
    def test_allow_simple(self) -> None:
        self.assertIsNone(check_command("dir"))
        self.assertIsNone(check_command("echo hello"))

    def test_block_empty(self) -> None:
        self.assertIsNotNone(check_command(""))
        self.assertIsNotNone(check_command("   "))

    def test_block_destructive(self) -> None:
        self.assertIn("blocked", (check_command("rm -rf /") or "").lower())
        self.assertIsNotNone(check_command('python -c "print(1)"'))
        self.assertIsNotNone(check_command("node -e console.log(1)"))
        self.assertIsNotNone(check_command("whoami\nRemove-Item foo"))

    def test_block_chaining(self) -> None:
        self.assertIn("chaining", (check_command("echo a && echo b") or "").lower())

    def test_block_long(self) -> None:
        self.assertIn("too long", (check_command("x" * 501) or "").lower())


class TestEventBus(unittest.TestCase):
    def test_emit_and_unsubscribe(self) -> None:
        bus = EventBus()
        seen: list[int] = []

        def handler(_etype: str, **payload):
            seen.append(payload.get("n", 0))

        off = bus.on("Test", handler)
        self.assertEqual(bus.emit("Test", n=1), 1)
        off()
        self.assertEqual(bus.emit("Test", n=2), 0)
        self.assertEqual(seen, [1])

    def test_handler_exception_isolated(self) -> None:
        bus = EventBus()

        def bad(_etype: str, **_payload):
            raise RuntimeError("boom")

        def good(_etype: str, **payload):
            payload["ok"].append(1)

        ok: list[int] = []
        bus.on("X", bad)
        bus.on("X", good)
        bus.emit("X", ok=ok)
        self.assertEqual(ok, [1])


class TestDialogueStore(unittest.TestCase):
    def test_append_window_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "d.sqlite"
            store = DialogueStore(db, window=4, emit_events=False)
            store.append("u1", "a1")
            store.append("u2", "a2")
            store.append("u3", "a3")
            msgs = store.messages()
            self.assertEqual(len(msgs), 4)  # window=4 → last 2 pairs
            self.assertEqual(msgs[0]["content"], "u2")
            self.assertEqual(msgs[-1]["content"], "a3")
            self.assertEqual(store.turn_seq, 3)

            store2 = DialogueStore(db, window=4, emit_events=False)
            self.assertEqual(store2.turn_seq, 3)
            self.assertEqual(len(store2.messages()), 4)

    def test_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DialogueStore(Path(tmp) / "d.sqlite", window=8, emit_events=False)
            store.append("hi", "hello")
            store.clear()
            self.assertEqual(store.messages(), [])
            self.assertEqual(store.turn_seq, 0)

    def test_legacy_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "jarvis_history.json"
            legacy.write_text(
                json.dumps([
                    {"role": "user", "content": "old u"},
                    {"role": "assistant", "content": "old a"},
                ]),
                encoding="utf-8",
            )
            store = DialogueStore(
                Path(tmp) / "d.sqlite",
                window=8,
                legacy_json=legacy,
                emit_events=False,
            )
            msgs = store.messages()
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["content"], "old u")
            self.assertGreaterEqual(store.turn_seq, 1)


class TestSessionState(unittest.TestCase):
    def test_reset_orchestration(self) -> None:
        s = SessionState(current_goal="x", current_task="y", workflow=WorkflowPhase.THINKING)
        s.queue.append({"id": 1})
        s.reset_orchestration()
        self.assertIsNone(s.current_goal)
        self.assertEqual(s.workflow, WorkflowPhase.IDLE)
        self.assertEqual(s.queue, [])


class TestCortexTokenHook(unittest.TestCase):
    def test_prompt_tok_uses_estimator(self) -> None:
        from cortex.prompt import _tok
        n = _tok("hello there friend")
        self.assertGreaterEqual(n, 2)


if __name__ == "__main__":
    unittest.main()

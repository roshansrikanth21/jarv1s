"""Tests for the OpenClaw bridge: POST /api/ask + api.ask_and_wait (backend) and the
jarvis.agent_mcp MCP tools. No LLM, no network — turns are faked by swapping
dispatch_command, and the MCP shim's HTTP call is stubbed.
Run: venv\\Scripts\\python -m unittest tests.test_openclaw_bridge -v"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_dispatch(api, turn):
    """A stand-in for dispatch_command that starts `turn()` as the current task. Accepts the
    same keyword args as the real one (source/wake_ms/stt_ms) and ignores them."""
    async def dispatch(text, **_kw):
        api._current_task = asyncio.create_task(turn())
    return dispatch


class AskAndWait(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def setUp(self):
        self._saved = (self.api.dispatch_command, self.api._speak, self.api._current_task,
                       self.api._speak_task)

    def tearDown(self):
        (self.api.dispatch_command, self.api._speak, self.api._current_task,
         self.api._speak_task) = self._saved

    def test_reply_and_tools_are_returned(self):
        api = self.api

        async def turn():
            await api.broadcast({"type": "agent_tool", "step": {"action": "launch_app"}})
            await api.broadcast({"type": "llm_response", "text": "Opened Notepad."})

        api.dispatch_command = _fake_dispatch(api, turn)
        res = asyncio.run(api.ask_and_wait("open notepad", speak=False, timeout=5))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["reply"], "Opened Notepad.")
        self.assertEqual(res["tools"], ["launch_app"])
        self.assertEqual(api._reply_listeners, [], "listener must be removed after the turn")

    def test_speak_false_keeps_the_turn_silent_and_true_speaks(self):
        api = self.api
        spoken: list[str] = []

        async def fake_speak(text):
            spoken.append(text)

        async def turn():
            await api._emit_final("hello there")

        api._speak = fake_speak
        api._speak_task = None
        api.dispatch_command = _fake_dispatch(api, turn)

        async def run(speak):
            await api.ask_and_wait("hi", speak=speak, timeout=5)
            await asyncio.sleep(0.05)   # let the scheduled speech task run

        asyncio.run(run(False))
        self.assertEqual(spoken, [], "speak=false must not reach TTS")
        asyncio.run(run(True))
        self.assertEqual(spoken, ["hello there"])
        self.assertTrue(api._SPEAK_REPLY.get(), "silence must not leak out of the request")

    def test_timeout_returns_without_killing_the_turn(self):
        api = self.api

        async def turn():
            await asyncio.sleep(10)

        api.dispatch_command = _fake_dispatch(api, turn)

        async def run():
            res = await api.ask_and_wait("slow", speak=False, timeout=0.2)
            still_running = not api._current_task.done()
            api._current_task.cancel()
            return res, still_running

        res, still_running = asyncio.run(run())
        self.assertEqual(res["status"], "timeout")
        self.assertTrue(still_running, "a timeout stops our wait, not JARVIS's turn")

    def test_barge_in_reports_interrupted(self):
        api = self.api

        async def turn():
            await asyncio.sleep(10)

        api.dispatch_command = _fake_dispatch(api, turn)

        async def run():
            async def barge():
                await asyncio.sleep(0.05)
                api._current_task.cancel()   # what _stop_speaking does on a new command
            asyncio.create_task(barge())
            return await api.ask_and_wait("x", speak=False, timeout=5)

        self.assertEqual(asyncio.run(run())["status"], "interrupted")


class AskEndpointAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import api
        from fastapi.testclient import TestClient
        cls.api = api
        cls.client = TestClient(api.app)

    def setUp(self):
        self._saved_ask = self.api.ask_and_wait
        self._saved_tok = os.environ.pop("JARVIS_AGENT_TOKEN", None)

        async def fake_ask(text, *, speak=True, timeout=120.0):
            return {"status": "ok", "reply": f"echo:{text}:{speak}", "tools": [],
                    "elapsed_s": 0.0}
        self.api.ask_and_wait = fake_ask

    def tearDown(self):
        self.api.ask_and_wait = self._saved_ask
        os.environ.pop("JARVIS_AGENT_TOKEN", None)
        if self._saved_tok is not None:
            os.environ["JARVIS_AGENT_TOKEN"] = self._saved_tok

    def test_loopback_without_token_is_allowed(self):
        r = self.client.post("/api/ask", json={"message": "hi", "speak": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reply"], "echo:hi:False")

    def test_foreign_browser_origin_is_rejected(self):
        r = self.client.post("/api/ask", json={"message": "hi"},
                             headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 401)

    def test_token_is_enforced_when_configured(self):
        os.environ["JARVIS_AGENT_TOKEN"] = "s3cret"
        self.assertEqual(self.client.post("/api/ask", json={"message": "hi"}).status_code, 401)
        r = self.client.post("/api/ask", json={"message": "hi"},
                             headers={"authorization": "Bearer s3cret"})
        self.assertEqual(r.status_code, 200)

    def test_empty_message_is_a_400(self):
        self.assertEqual(self.client.post("/api/ask", json={"message": "  "}).status_code, 400)


class AgentMcpTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jarvis import agent_mcp
        cls.m = agent_mcp

    def setUp(self):
        self._saved = self.m._call
        self.calls: list[tuple] = []

    def tearDown(self):
        self.m._call = self._saved

    def _stub(self, status, payload):
        def fake(method, path, **kw):
            self.calls.append((method, path, kw))
            return status, payload
        self.m._call = fake

    def test_exposes_exactly_the_four_jarvis_tools(self):
        names = sorted(t.name for t in asyncio.run(self.m.mcp.list_tools()))
        self.assertEqual(names, ["jarvis_ask", "jarvis_recall", "jarvis_remember",
                                 "jarvis_status"])

    def test_ask_relays_reply_tools_and_speak_flag(self):
        self._stub(200, {"status": "ok", "reply": "Opened Notepad.",
                         "tools": ["launch_app"]})
        out = self.m.jarvis_ask("open notepad", speak=False)
        self.assertIn("Opened Notepad.", out)
        self.assertIn("tools used: launch_app", out)
        method, path, kw = self.calls[0]
        self.assertEqual((method, path), ("POST", "/api/ask"))
        self.assertFalse(kw["body"]["speak"])

    def test_ask_when_jarvis_is_down_says_so(self):
        self._stub(0, None)
        self.assertEqual(self.m.jarvis_ask("hi"), self.m.OFFLINE)

    def test_ask_refused_names_the_token_to_fix(self):
        self._stub(401, {"error": "unauthorized"})
        self.assertIn("JARVIS_AGENT_TOKEN", self.m.jarvis_ask("hi"))

    def test_ask_flags_partial_and_interrupted_replies(self):
        self._stub(200, {"status": "timeout", "reply": "Working on it", "tools": []})
        self.assertIn("still working", self.m.jarvis_ask("slow"))
        self._stub(200, {"status": "interrupted", "reply": "", "tools": []})
        self.assertIn("interrupted", self.m.jarvis_ask("x"))

    def test_status_online_and_offline(self):
        self._stub(200, {"model": "groq", "memories": 12, "time": "now"})
        self.assertIn("online", self.m.jarvis_status())
        self._stub(0, None)
        self.assertEqual(self.m.jarvis_status(), self.m.OFFLINE)

    def test_recall_formats_memories_and_remember_tags_source(self):
        self._stub(200, {"memories": [{"category": "preference", "content": "Likes tea."}]})
        self.assertEqual(self.m.jarvis_recall("drinks"), "[preference] Likes tea.")
        self._stub(200, {"result": "Stored: x"})
        self.m.jarvis_remember("User works with Roshan.")
        self.assertEqual(self.calls[-1][2]["body"]["source_model"], "openclaw")


class HttpBearerGuard(unittest.TestCase):
    def test_rejects_missing_or_wrong_token_and_passes_the_right_one(self):
        from jarvis.agent_mcp import _BearerGuard
        reached: list[bool] = []

        async def inner(scope, receive, send):
            reached.append(True)

        guard = _BearerGuard(inner, "tok")

        async def call(headers):
            sent: list[dict] = []

            async def send(msg):
                sent.append(msg)
            await guard({"type": "http", "headers": headers}, None, send)
            return sent

        sent = asyncio.run(call([]))
        self.assertEqual(sent[0]["status"], 401)
        sent = asyncio.run(call([(b"authorization", b"Bearer nope")]))
        self.assertEqual(sent[0]["status"], 401)
        self.assertEqual(reached, [])
        asyncio.run(call([(b"authorization", b"Bearer tok")]))
        self.assertEqual(reached, [True])


if __name__ == "__main__":
    unittest.main()

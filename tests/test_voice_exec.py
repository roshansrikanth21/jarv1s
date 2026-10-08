"""Tests for the voice/execution overhaul: the deterministic fast-path intent router, the
process-verification helpers, the desktop volume/brightness direction fix, TTS sentence
chunking, and the fast-path executor's honesty (never claims success a tool didn't confirm).
No LLM, no mic, no real apps — process checks are faked.
Run: venv\\Scripts\\python -m unittest tests.test_voice_exec -v
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FastPathRouter(unittest.TestCase):
    def setUp(self):
        from jarvis.act import fastpath
        self.fp = fastpath

    def _k(self, text):
        r = self.fp.match(text)
        return None if r is None else r.kind

    def test_open_and_close_apps(self):
        self.assertEqual(self.fp.match("open notepad").params["app"], "notepad")
        self.assertEqual(self.fp.match("please open vs code").params["app"], "code")
        self.assertEqual(self.fp.match("launch spotify for me").params["app"], "spotify")
        self.assertEqual(self.fp.match("close chrome").kind, "CLOSE_APP")
        self.assertEqual(self.fp.match("quit the calculator").params["app"], "calc")

    def test_volume_brightness_media(self):
        self.assertEqual(self.fp.match("volume up").params["direction"], "up")
        self.assertEqual(self.fp.match("turn it down").params["direction"], "down")
        self.assertEqual(self.fp.match("louder").params["direction"], "up")
        self.assertEqual(self.fp.match("set volume to 30").params, {"direction": "set", "level": 30})
        self.assertEqual(self.fp.match("mute").params["direction"], "mute")
        self.assertEqual(self.fp.match("brightness down").params["direction"], "down")
        self.assertEqual(self.fp.match("set brightness to 80").params["level"], 80)
        self.assertEqual(self.fp.match("next track").params["key"], "nexttrack")
        self.assertEqual(self.fp.match("pause").params["key"], "playpause")

    def test_wifi_lock_screenshot(self):
        self.assertEqual(self.fp.match("turn wifi off").params["state"], "off")
        self.assertEqual(self.fp.match("turn on the wifi").params["state"], "on")
        self.assertEqual(self._k("lock my pc"), "LOCK")
        self.assertEqual(self._k("take a screenshot"), "SCREENSHOT")
        self.assertEqual(self._k("screenshot"), "SCREENSHOT")

    def test_does_not_hijack_real_conversation(self):
        for t in ["what's the weather", "tell me a joke", "stop", "how are you",
                  "open a ticket about the login bug", "play despacito on spotify",
                  "close the deal with roshan", "set a reminder for 5pm"]:
            self.assertIsNone(self.fp.match(t), f"should NOT fast-path: {t!r}")

    def test_specific_patterns_beat_open_capture(self):
        # "turn it up" must be VOLUME, never OPEN_APP "it up".
        self.assertEqual(self.fp.match("turn it up").kind, "VOLUME")

    def test_demo_browser_url_and_exact_notepad_text(self):
        browser = self.fp.match(
            "Open Chrome and navigate to https://example.com using the browser tool.")
        self.assertEqual(browser.kind, "OPEN_URL")
        self.assertEqual((browser.params["url"], browser.params["browser"]),
                         ("https://example.com", "chrome"))

        note = self.fp.match(
            "Open Notepad and type exactly: ‘The best way out is always through. — Robert Frost.’")
        self.assertEqual(note.kind, "OPEN_AND_TYPE")
        self.assertEqual(note.params["content"],
                         "The best way out is always through. — Robert Frost.")

    def test_task_intents(self):
        self.assertEqual(self.fp.match("add a task to review the PR").params["text"], "review the PR")
        self.assertEqual(self.fp.match("create task buy milk").params["text"], "buy milk")
        self.assertEqual(self.fp.match("complete task 3").params["n"], 3)
        self.assertEqual(self.fp.match("mark task 2 as done").params["n"], 2)
        self.assertEqual(self.fp.match("cancel task 5").params["n"], 5)
        # "remind me to ..." is a scheduled reminder, NOT a queue task.
        self.assertEqual(self.fp.match("remind me to call mom at 6pm").kind, "REMINDER")

    def test_reminder_and_goal_intents(self):
        r = self.fp.match("remind me at 7pm to submit the report")
        self.assertEqual((r.kind, r.params["when"], r.params["message"]),
                         ("REMINDER", "19:00", "submit the report"))
        self.assertEqual(self.fp.match("every monday remind me to update the tracker").params["recurrence"],
                         "weekly:MON")
        g = self.fp.match("I need to finish the project report by Friday")
        self.assertEqual((g.kind, g.params["deadline_text"]), ("GOAL", "Friday"))
        # Not reminders/goals:
        self.assertIsNone(self.fp.match("remind me what the capital of france is"))
        self.assertIsNone(self.fp.match("I need coffee"))

    def test_filesystem_intents(self):
        a = self.fp.match("create a folder called JarvisDemo in my documents")
        self.assertEqual((a.kind, a.params["name"], a.params["location"]),
                         ("FS_FOLDER", "JarvisDemo", "documents"))
        b = self.fp.match("make a file notes.txt in Reports with hello world")
        self.assertEqual((b.kind, b.params["name"], b.params["content"]),
                         ("FS_FILE", "notes.txt", "hello world"))
        c = self.fp.match("create a folder called ProjectX and a file readme.txt in it with hi")
        self.assertEqual((c.kind, c.params["folder"], c.params["file"], c.params["content"]),
                         ("FS_FOLDER_FILE", "ProjectX", "readme.txt", "hi"))
        self.assertEqual(self.fp.match("open my downloads").kind, "FS_OPEN")
        # Non-filesystem "create" must not be hijacked.
        self.assertIsNone(self.fp.match("create a haiku about the sea"))
        self.assertIsNone(self.fp.match("make me laugh"))


class FabricationGuard(unittest.TestCase):
    """The agent must never claim a PC action succeeded when no tool ran. The guard fires only
    on (action request) AND (no tool call) AND (a success claim), and never on questions,
    honest failures, or non-OS 'create' (a haiku)."""

    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    def _fab(self, text, reply, tool_calls):
        return (self.api._is_action_request(text) and tool_calls == 0
                and self.api._claims_done(reply))

    def test_flags_fabricated_completion(self):
        self.assertTrue(self._fab("make a folder called X and add a file", "Done — created both.", 0))
        self.assertTrue(self._fab("delete the file report.txt", "Deleted it.", 0))
        self.assertTrue(self._fab("open notepad", "Opened Notepad for you.", 0))

    def test_does_not_flag_when_a_tool_ran(self):
        self.assertFalse(self._fab("make a folder called X", "Created folder C:/x.", 1))

    def test_does_not_flag_honest_failure_or_questions_or_prose(self):
        self.assertFalse(self._fab("open notepad", "I couldn't open notepad — not installed.", 0))
        self.assertFalse(self._fab("what can you do?", "Lots of things.", 0))
        self.assertFalse(self._fab("write a haiku about the sea", "Here is a haiku ...", 0))


class VerifyHelpers(unittest.TestCase):
    def setUp(self):
        from jarvis.act import verify
        self.v = verify

    def test_candidates_cover_exe_and_stem(self):
        self.assertIn("notepad.exe", self.v._candidates("notepad"))
        self.assertIn("notepad", self.v._candidates("Notepad.exe"))

    def test_wait_until_running_observes_true(self):
        self.v.is_running = lambda exe: True
        r = self.v.wait_until_running("x.exe", timeout=0.2)
        self.assertTrue(r.ok and r.verified is True)

    def test_wait_until_running_reports_absence(self):
        self.v.is_running = lambda exe: False
        r = self.v.wait_until_running("x.exe", timeout=0.2, poll=0.05)
        self.assertFalse(r.ok)
        self.assertIs(r.verified, False)

    def test_unknown_when_cannot_inspect(self):
        self.v.is_running = lambda exe: None
        r = self.v.wait_until_running("x.exe", timeout=0.2)
        self.assertTrue(r.ok)          # launch itself didn't error
        self.assertIsNone(r.verified)  # but we couldn't confirm → caller must hedge


class DesktopDirectionFix(unittest.TestCase):
    """The old code read the direction from `action`, which already held the sub-action name,
    so volume/brightness through the tool always failed. _direction must resolve it."""

    def setUp(self):
        from jarvis.act import desktop
        self.d = desktop

    def test_direction_resolution(self):
        self.assertEqual(self.d._direction({"action": "system_volume", "direction": "up"}, "volume"), "up")
        self.assertEqual(self.d._direction({"action": "system_volume", "volume_action": "down"}, "volume"), "down")
        self.assertEqual(self.d._direction({"action": "system_volume", "level": 30}, "volume"), "set")
        self.assertEqual(self.d._direction({"action": "brightness", "direction": "set", "level": 5}, "brightness"), "set")
        self.assertEqual(self.d._direction({"action": "volume_up"}, "volume"), "up")
        # No usable direction anywhere → empty (helper then returns a clear error, not a crash).
        self.assertEqual(self.d._direction({"action": "system_volume"}, "volume"), "")


class SentenceChunks(unittest.TestCase):
    def setUp(self):
        import api
        self.api = api

    def test_splits_into_sentences_and_merges_tiny(self):
        chunks = self.api._sentence_chunks("Done. Notepad is open and ready for you to use now.")
        self.assertGreaterEqual(len(chunks), 1)
        # "Done." is too short to stand alone → merged with the next sentence.
        self.assertTrue(chunks[0].startswith("Done. Notepad"))

    def test_no_punctuation_is_single_chunk(self):
        self.assertEqual(self.api._sentence_chunks("just one line no period"),
                         ["just one line no period"])

    def test_empty(self):
        self.assertEqual(self.api._sentence_chunks("   "), [])


class FastExecuteHonesty(unittest.TestCase):
    """The fast executor must build its reply from the verified result — never a blanket
    'done'. We fake the OS layer and assert the words JARVIS would say."""

    def setUp(self):
        import api
        from jarvis.act import fastpath
        self.api = api
        self.fp = fastpath
        self._saved = (api.verify.is_running, api.verify.terminate,
                       api.verify.wait_until_running, api._resolve_launch_target,
                       api._open_in_browser)

    def tearDown(self):
        (self.api.verify.is_running, self.api.verify.terminate,
         self.api.verify.wait_until_running, self.api._resolve_launch_target,
         self.api._open_in_browser) = self._saved

    def test_open_reports_failure_when_process_never_appears(self):
        import subprocess
        self.api._resolve_launch_target = lambda cmd: None
        # start via ShellExecute "succeeds" (rc 0) but the process never shows up.
        self.api.verify.is_running = lambda exe: False
        orig_run = subprocess.run
        subprocess.run = lambda *a, **k: type("R", (), {"returncode": 0})()
        try:
            reply, handled = self.api._fast_execute(self.fp.FastIntent("OPEN_APP", {"app": "notepad"}))
        finally:
            subprocess.run = orig_run
        self.assertTrue(handled)
        self.assertNotIn("Opened notepad", reply)
        self.assertRegex(reply.lower(), r"couldn't confirm|failed|couldn't")

    def test_open_claims_success_only_when_verified(self):
        self.api._resolve_launch_target = lambda cmd: "notepad.exe"
        self.api.verify.is_running = lambda exe: False  # wasn't running before
        self.api.verify.wait_until_running = lambda exe, timeout=3.0: self.api.verify.VerifyResult(True, True, "running")
        reply, handled = self.api._fast_execute(self.fp.FastIntent("OPEN_APP", {"app": "notepad"}))
        self.assertTrue(handled)
        self.assertIn("Opened notepad", reply)

    def test_unknown_app_is_answered_honestly_not_fabricated(self):
        # A non-existent, non-website app name must be answered honestly by the fast path —
        # NOT passed to the model (which was observed to fabricate "it's open").
        self.api._resolve_launch_target = lambda cmd: None
        reply, handled = self.api._fast_execute(self.fp.FastIntent("OPEN_APP", {"app": "photoshoppe"}))
        self.assertTrue(handled)
        self.assertIn("couldn't find", reply.lower())
        self.assertNotIn("open", reply.lower().split(".")[0])  # never "Opened ..."

    def test_website_name_opens_the_site_directly(self):
        self.api._resolve_launch_target = lambda cmd: None
        opened = []
        self.api._open_in_browser = lambda url, browser="": opened.append((url, browser))
        reply, handled = self.api._fast_execute(self.fp.FastIntent("OPEN_APP", {"app": "gmail"}))
        self.assertTrue(handled)
        self.assertEqual(opened[-1][0], "https://mail.google.com")
        reply, handled = self.api._fast_execute(self.fp.FastIntent("OPEN_APP", {"app": "nytimes.com"}))
        self.assertTrue(handled)
        self.assertEqual(opened[-1][0], "https://nytimes.com")

    def test_web_search_opens_the_requested_browser(self):
        opened = []
        self.api._open_in_browser = lambda url, browser="": opened.append((url, browser))
        intent = self.fp.match("open chrome and search for what is a perceptron")
        self.assertEqual(intent.kind, "WEB_SEARCH")
        reply, handled = self.api._fast_execute(intent)
        self.assertTrue(handled)
        self.assertEqual(opened[-1], ("https://www.google.com/search?q=what+is+a+perceptron", "chrome"))
        self.assertIn("Searching for what is a perceptron", reply)

    def test_full_url_opens_in_the_requested_browser(self):
        opened = []
        self.api._open_in_browser = lambda url, browser="": opened.append((url, browser))
        intent = self.fp.match(
            "Open Chrome and navigate to https://example.com using the browser tool.")
        reply, handled = self.api._fast_execute(intent)
        self.assertTrue(handled)
        self.assertEqual(opened[-1], ("https://example.com", "chrome"))
        self.assertIn("Opening", reply)

    def test_browser_failure_is_reported_honestly(self):
        self.api._open_in_browser = lambda url, browser="": "no browser is available"
        reply, handled = self.api._fast_execute(self.fp.FastIntent("YOUTUBE", {"query": "lofi"}))
        self.assertTrue(handled)
        self.assertIn("couldn't", reply)

    def test_close_reports_gone_only_when_verified(self):
        self.api.verify.terminate = lambda exe, timeout=5.0: self.api.verify.VerifyResult(True, True, "closed")
        reply, handled = self.api._fast_execute(self.fp.FastIntent("CLOSE_APP", {"app": "chrome"}))
        self.assertTrue(handled)
        self.assertIn("Closed chrome", reply)

    def test_close_reports_failure_honestly(self):
        self.api.verify.terminate = lambda exe, timeout=5.0: self.api.verify.VerifyResult(False, False, "still running after 5.0s")
        reply, handled = self.api._fast_execute(self.fp.FastIntent("CLOSE_APP", {"app": "chrome"}))
        self.assertTrue(handled)
        self.assertIn("couldn't close", reply.lower())


if __name__ == "__main__":
    unittest.main()


class WebAndClockIntents(unittest.TestCase):
    """The everyday spoken commands that must never reach (or be fabricated by) the LLM."""

    def test_matches(self):
        from jarvis.act.fastpath import match
        cases = {
            "open a browser and search for perceptrons": ("WEB_SEARCH", {"query": "perceptrons", "browser": ""}),
            "open edge and google best laptops": ("WEB_SEARCH", {"query": "best laptops", "browser": "edge"}),
            "search the web for python asyncio": ("WEB_SEARCH", {"query": "python asyncio", "browser": ""}),
            "play lofi beats on youtube": ("YOUTUBE", {"query": "lofi beats"}),
            "go to github": ("OPEN_URL", {"url": "https://github.com", "site": "github"}),
            "what time is it": ("TIME", {}),
            "set a 10 minute timer": ("TIMER", {"amount": 10, "unit": "minutes"}),
        }
        for text, (kind, params) in cases.items():
            intent = match(text)
            self.assertIsNotNone(intent, text)
            self.assertEqual((intent.kind, intent.params), (kind, params), text)

    def test_questions_and_file_searches_are_not_web_commands(self):
        from jarvis.act.fastpath import match
        for text in ("what is a perceptron", "search my files for report", "find a good restaurant"):
            self.assertIsNone(match(text), text)

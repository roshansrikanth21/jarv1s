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


class WakeWordAndNoise(unittest.TestCase):
    """The text layer of the voice pipeline (verified separately against real SAPI speech)."""

    @classmethod
    def setUpClass(cls):
        import api
        cls.api = api

    # The acoustic wake word (openWakeWord) is validated on real speech in
    # test_voice_pipeline; these cover the TEXT layer: stripping a wake phrase that rode
    # along in a transcript, and the transcript-matching fallback.
    def test_wake_word_forms_extract_the_command(self):
        from jarvis.voice.pipeline import strip_wake
        self.assertEqual(strip_wake("Hey Jarvis, what time is it?"), "what time is it?")
        self.assertEqual(strip_wake("hello jarvis open calculator"), "open calculator")
        self.assertEqual(strip_wake("hi jarvis play some music"), "play some music")
        self.assertEqual(strip_wake("okay jarvis stop"), "stop")
        self.assertEqual(strip_wake("jarvis open spotify"), "open spotify")
        self.assertEqual(strip_wake("open spotify"), "open spotify")   # nothing to strip

    def test_bare_wake_word_arms_the_listener(self):
        from jarvis.voice.pipeline import mentions_wake
        for s in ("Jarvis", "jarvis.", "hey jarvis", "Hello, Jarvis!"):
            self.assertEqual(mentions_wake(s), (True, ""), s)

    def test_ordinary_speech_is_not_a_wake_word(self):
        from jarvis.voice.pipeline import mentions_wake
        for s in ("we should get lunch and watch the game", "open the door please", ""):
            self.assertFalse(mentions_wake(s)[0], s)

    def test_whisper_hallucinations_are_dropped(self):
        n = self.api._is_stt_noise
        for s in ("thanks for watching", "Thank you.", "you", "",
                  "take a look at how take a look at how take a look at how take a look at how",
                  "see you in the next video see you in the next video see you in the next video"):
            self.assertTrue(n(s), s)

    def test_real_commands_are_kept(self):
        n = self.api._is_stt_noise
        for s in ("open calculator", "what time is it", "stop", "remind me to call mom at five",
                  "play some music"):
            self.assertFalse(n(s), s)


class _FakeSD:
    """Scripted stand-in for `sounddevice`: each device is 'live', 'silent' (flat, but with a loud
    pop on open — the exact trap that fooled a peak-based check), or 'error'."""

    def __init__(self, devices, behavior, default):
        import types
        self._devices = devices
        self.behavior = behavior
        self.default = types.SimpleNamespace(device=(default, -1))
        self.opened = []

    def query_devices(self, idx=None):
        return self._devices if idx is None else self._devices[idx]

    def query_hostapis(self):
        return [{"name": "MME"}, {"name": "Windows WDM-KS"}]

    def sleep(self, ms):
        pass

    def RawInputStream(self, device, samplerate, channels, dtype, blocksize, callback):
        # The capture path uses raw (bytes) streams; feed the same scripted signal as bytes.
        def raw_cb(data, n, t, st):
            callback(data.tobytes(), n, t, st)
        return self.InputStream(device, samplerate, channels, dtype, blocksize, raw_cb)

    def InputStream(self, device, samplerate, channels, dtype, blocksize, callback):
        import numpy as np
        fake, kind = self, self.behavior[device]

        class _Ctx:
            def __enter__(s):
                fake.opened.append(device)
                if kind == "error":
                    raise OSError("PaErrorCode -9999")
                n = 4096
                if kind == "live":
                    data = (np.random.RandomState(1).randint(-40, 41, size=(n, channels))).astype("int16")
                else:                                   # silent: flat zeros + one loud pop on open
                    data = np.zeros((n, channels), dtype="int16")
                    data[0, :] = 20000
                callback(data, n, None, None)
                return s

            def __exit__(s, *a):
                return False
        return _Ctx()


def _dev(name, hostapi=0, ch=1):
    return {"name": name, "max_input_channels": ch, "default_samplerate": 16000, "hostapi": hostapi}


class MicPicker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from jarvis.voice.audio_in import pick_input_device
        cls.pick = staticmethod(pick_input_device)

    def test_live_default_is_used_without_probing_others(self):
        sd = _FakeSD([_dev("Mic A"), _dev("Mic B")], {0: "live", 1: "live"}, default=0)
        self.assertEqual(self.pick(sd)[0], 0)
        self.assertEqual(set(sd.opened), {0})

    def test_silent_default_yields_to_a_live_device_even_with_an_open_pop(self):
        # Windows opens a privacy-blocked/muted mic "successfully" and streams silence. The pop
        # on open must not be mistaken for signal (peak is 20000; the stream is flat).
        sd = _FakeSD([_dev("Mic Array"), _dev("Other Mic", 1)], {0: "silent", 1: "live"}, default=0)
        self.assertEqual(self.pick(sd)[0], 1)

    def test_everything_silent_keeps_the_users_default(self):
        sd = _FakeSD([_dev("Mic Array"), _dev("Other Mic", 1)], {0: "silent", 1: "silent"}, default=0)
        self.assertEqual(self.pick(sd)[0], 0)

    def test_loopback_is_never_chosen_as_a_microphone(self):
        # Stereo Mix carries system audio: picking it makes JARVIS hear (and trigger on) itself.
        sd = _FakeSD([_dev("Mic Array"), _dev("Stereo Mix (Realtek)", 1)],
                     {0: "silent", 1: "live"}, default=0)
        self.assertEqual(self.pick(sd)[0], 0)
        self.assertNotIn(1, sd.opened)

    def test_speaker_monitor_is_never_chosen_either(self):
        sd = _FakeSD([_dev("Mic Array"), _dev("PC Speaker (Realtek output)", 1)],
                     {0: "silent", 1: "live"}, default=0)
        self.assertEqual(self.pick(sd)[0], 0)

    def test_default_that_fails_to_open_falls_back_to_a_live_mic(self):
        sd = _FakeSD([_dev("Mic Array"), _dev("Other Mic", 1)], {0: "error", 1: "live"}, default=0)
        self.assertEqual(self.pick(sd)[0], 1)

    def test_no_working_device_returns_none(self):
        sd = _FakeSD([_dev("Mic Array")], {0: "error"}, default=0)
        self.assertEqual(self.pick(sd), (None, None, None))


if __name__ == "__main__":
    unittest.main()

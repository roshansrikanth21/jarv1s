"""pipeline.py — the voice conversation state machine.

    WAITING ──"hey jarvis"──► LISTENING ──speech──► HEARING ──~0.6 s silence──► TRANSCRIBING
       ▲                          │ (7 s no speech)                                  │
       │                          ▼                                                  ▼
       └──(follow-up timeout)── CONVERSATION ◄──reply finished── SPEAKING ◄── PROCESSING
                                   │ (speech, no wake word needed)
                                   └──────────────► HEARING ...

  * WAITING        only the acoustic wake word can start a turn (no STT runs at all).
  * LISTENING      right after the wake word: capture the request (a one-breath "Hey Jarvis,
                   open notepad" works too — the wake phrase is stripped from the transcript).
  * CONVERSATION   after JARVIS answers a voice request, the mic stays open for a follow-up
                   window: just keep talking, no wake word. Silence ends the session;
                   "thanks / that's all / never mind" ends it immediately.
  * SPEAKING       half-duplex (no echo transcription), but saying "hey jarvis" barges in:
                   speech stops and JARVIS listens.

One capture thread runs the cheap per-frame engines (Silero VAD + wake word, ~20 ms CPU per
second of audio); a separate worker runs Whisper so capture never stalls. Every transition
is reported through `on_state` so the UI always shows what JARVIS is actually doing.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from collections import deque
from difflib import SequenceMatcher
from typing import Callable

import numpy as np

from .audio_in import MicCapture
from .stt import WhisperSTT
from .vad import FRAME, SileroVAD

log = logging.getLogger("jarvis")

OFF, WAITING, LISTENING, HEARING, TRANSCRIBING, PROCESSING, SPEAKING, CONVERSATION = (
    "off", "waiting", "listening", "hearing", "transcribing", "processing", "speaking",
    "conversation")

_DISMISS = re.compile(
    r"^(?:thanks?|thank you|that'?s all|that is all|never ?mind|no thanks|nothing|goodbye|"
    r"bye|we'?re done|i'?m done|all good|that'?s it|stop|cancel)(?: jarvis)?[.!]*$", re.I)
_WAKE_LEAD = re.compile(
    r"^\s*(?:(?:hey|hi|hello|ok|okay|yo)[\s,]+)?(?:jarvis|jervis|jarvus|jarvix|charvis|travis)\b[\s,.!?:-]*",
    re.I)


def strip_wake(text: str) -> str:
    """Remove a leading wake phrase ('Hey Jarvis, …') that rode along in the transcript."""
    return _WAKE_LEAD.sub("", text or "", count=1).strip()


def mentions_wake(text: str) -> tuple[bool, str]:
    """Text-only wake check (fallback when the acoustic model is unavailable)."""
    t = (text or "").strip()
    toks = re.findall(r"[a-zA-Z']+", t.lower())[:3]
    for tok in toks:
        if tok.startswith("jarv") or SequenceMatcher(None, tok, "jarvis").ratio() >= 0.75:
            return True, strip_wake(t)
    return False, t


class VoicePipeline:
    def __init__(self, *,
                 on_state: Callable[[str, str, dict], None],
                 on_command: Callable[[str, dict], None],
                 on_level: Callable[[int, bool], None] | None = None,
                 on_barge_in: Callable[[], None] | None = None,
                 on_wake: Callable[[], None] | None = None,
                 on_system: Callable[[str], None] | None = None,
                 wake_required: bool = True,
                 stt: WhisperSTT | None = None):
        self.on_state = on_state
        self.on_command = on_command
        self.on_level = on_level or (lambda e, h: None)
        self.on_barge_in = on_barge_in or (lambda: None)
        self.on_wake = on_wake or (lambda: None)
        self.on_system = on_system or (lambda t: None)
        self.wake_required = wake_required
        self.stt = stt or WhisperSTT()

        env = os.environ.get
        self.followup_sec = float(env("JARVIS_FOLLOWUP_SEC", "8"))
        self.listen_timeout = float(env("JARVIS_LISTEN_TIMEOUT_SEC", "7"))
        self.end_silence_ms = int(env("JARVIS_END_SILENCE_MS", "650"))
        self.max_utter_sec = float(env("JARVIS_MAX_UTTER_SEC", "30"))
        self.wake_threshold = float(env("JARVIS_WAKE_THRESHOLD", "0.5"))
        self.speech_on = float(env("JARVIS_VAD_ON", "0.5"))
        self.speech_off = float(env("JARVIS_VAD_OFF", "0.35"))

        self.state = OFF
        self.state_text = ""
        self.wake_engine = "none"
        self._wake = None
        self._vad: SileroVAD | None = None
        self._mic: MicCapture | None = None
        self._running = False
        self._thread: threading.Thread | None = None
        self._stt_q: queue.Queue = queue.Queue(maxsize=4)
        self._lock = threading.RLock()

        # turn bookkeeping
        self._session = False          # a voice conversation is open (follow-ups allowed)
        self._turn_active = False      # the agent is working on a voice-originated request
        self._speaking = False
        self._armed_until = 0.0        # deadline for LISTENING / CONVERSATION
        self._ignore_until = 0.0       # echo tail after TTS ends
        self._seg: list[np.ndarray] = []
        self._silence_ms = 0
        self._voiced_run = 0
        self._resume_state = WAITING   # where to go if a segment yields nothing
        self._utt = 0
        self._t_speech_end = 0.0
        self._wake_at = 0.0
        self.last_heard = ""
        self.last_stt_ms: float | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._supervise, name="voice-capture", daemon=True)
        self._thread.start()
        threading.Thread(target=self._stt_loop, name="voice-stt", daemon=True).start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        try:
            self._stt_q.put_nowait(None)
        except queue.Full:
            pass
        self._set(OFF, "Microphone is off.")

    @property
    def running(self) -> bool:
        return self._running

    # ── notifications from the rest of JARVIS ───────────────────────────────────────
    def tts_started(self) -> None:
        with self._lock:
            self._speaking = True
            if self.state != OFF:
                self._set(SPEAKING, "JARVIS is speaking — say “Hey Jarvis” to interrupt.")

    def tts_ended(self) -> None:
        with self._lock:
            self._speaking = False
            self._ignore_until = time.time() + 0.25        # let the room echo die down
            if self.state == OFF:
                return
            if self._turn_active:
                self._set(PROCESSING, "Working on it…")
            else:
                self._after_turn()

    def turn_finished(self) -> None:
        """The agent finished a voice-originated request (reply text emitted)."""
        with self._lock:
            if not self._turn_active:
                return                      # barged-in/cancelled turn: state already moved on
            self._turn_active = False
            if self.state != OFF and not self._speaking:
                self._after_turn()

    def report(self, state: str, text: str) -> None:
        """Agent progress (thinking / executing) while a voice turn is in flight."""
        with self._lock:
            if self._turn_active and not self._speaking and self.state not in (OFF,):
                self.state = PROCESSING
                self.state_text = text
                self.on_state(state, text, {})

    def end_session(self) -> None:
        with self._lock:
            self._session = False
            if self.state in (CONVERSATION, LISTENING):
                self._set(WAITING, self._waiting_text())

    # ── internals ────────────────────────────────────────────────────────────────────
    def _waiting_text(self) -> str:
        if not self.wake_required:
            return "Ready — just talk."
        return "Waiting for “Hey Jarvis”."

    def _set(self, state: str, text: str = "", **meta) -> None:
        self.state = state
        self.state_text = text
        ui = {WAITING: "listening", LISTENING: "armed", CONVERSATION: "conversation",
              PROCESSING: "thinking"}.get(state, state)
        meta.setdefault("utterance_id", self._utt)
        try:
            self.on_state(ui, text, meta)
        except Exception:
            pass

    def _after_turn(self) -> None:
        if self._session or not self.wake_required:
            self._arm(CONVERSATION, self.followup_sec,
                      "Listening — go ahead (no need to say “Jarvis”).")
        else:
            self._set(WAITING, self._waiting_text())

    def _arm(self, state: str, seconds: float, text: str) -> None:
        self._armed_until = time.time() + seconds
        self._resume_state = state
        self._seg = []
        self._silence_ms = 0
        self._voiced_run = 0
        self._set(state, text, window_sec=seconds)

    def _load_engines(self) -> None:
        if self._vad is None:
            self._vad = SileroVAD()
        if self._wake is None and self.wake_required:
            try:
                from .wakeword import WakeWord
                self._wake = WakeWord(threshold=self.wake_threshold)
                self.wake_engine = "openwakeword"
            except Exception as exc:
                self._wake = None
                self.wake_engine = "transcript"
                log.warning("wake: acoustic model unavailable (%s) — using transcript matching", exc)
                self.on_system("Acoustic wake word unavailable — falling back to transcript "
                               f"matching ({exc}).")
        if not self.stt.ready:
            self.stt.load()

    def _supervise(self) -> None:
        """Run capture sessions; recover from a dropped/unplugged mic without a restart."""
        attempts = 0
        try:
            self._load_engines()
        except Exception as exc:
            self.on_system(f"Voice engines failed to load: {exc}")
            self._running = False
            self._set(OFF, "Voice unavailable.")
            return
        while self._running:
            t0 = time.time()
            reason = self._capture_session()
            if not self._running or reason == "stopped":
                break
            if time.time() - t0 > 30:
                attempts = 0
            attempts += 1
            if attempts > 6:
                self.on_system("I couldn't recover the microphone. Toggle the mic to retry.")
                break
            self.on_system(f"Reconnecting the microphone (attempt {attempts})…")
            for _ in range(int(min(6.0, 1.5 * attempts) * 10)):
                if not self._running:
                    break
                time.sleep(0.1)
        self._running = False
        self._set(OFF, "Microphone is off.")

    def _capture_session(self) -> str:
        mic = MicCapture()
        try:
            if not mic.open():
                self.on_system(mic.error or "No usable microphone.")
                return "no_mic"
        except Exception as exc:
            self.on_system(f"Microphone failed to open: {exc}")
            return "error"
        self._mic = mic
        self._vad.reset()
        if self._wake is not None:
            self._wake.reset()
        with self._lock:
            if self._session or not self.wake_required:
                self._after_turn()
            else:
                self._set(WAITING, self._waiting_text(), device=mic.device_name)
        pending = np.zeros(0, dtype=np.int16)
        ring: deque = deque(maxlen=12)          # ~384 ms pre-roll so onsets aren't clipped
        tick = 0
        silent_frames = 0
        warned_silent = False
        try:
            while self._running:
                block = mic.read(timeout=0.6)
                if block is None:
                    if not mic.active:
                        return "error"
                    continue
                pending = np.concatenate([pending, block])
                while pending.size >= FRAME:
                    frame, pending = pending[:FRAME], pending[FRAME:]
                    energy = int(np.abs(frame).mean())
                    silent_frames = silent_frames + 1 if energy <= 1 else 0
                    if silent_frames > 300 and not warned_silent:   # ~10 s of digital zero
                        warned_silent = True
                        self.on_system("The microphone is delivering only silence — check that "
                                       "it isn't muted and Windows allows desktop apps to use it.")
                    self._frame(frame, ring)
                    ring.append(frame)
                    tick += 1
                    if tick % 5 == 0:
                        self.on_level(energy, self.state == HEARING)
        except Exception as exc:
            log.warning("voice capture error: %s", exc)
            self.on_system(f"Microphone stream dropped: {exc}")
            return "error"
        finally:
            mic.close()
            self._mic = None
        return "stopped"

    def _frame(self, frame: np.ndarray, ring: deque) -> None:
        now = time.time()
        prob = self._vad.prob(frame) if self._vad else 0.0
        fired = False
        if self._wake is not None:
            thr = self.wake_threshold + (0.2 if self._speaking else 0.0)
            fired = self._wake.process(frame, threshold=min(0.95, thr))

        with self._lock:
            st = self.state
            if fired and st in (WAITING, CONVERSATION, SPEAKING, PROCESSING, LISTENING):
                if st in (SPEAKING, PROCESSING):
                    self._turn_active = False
                    self.on_barge_in()
                self._session = True
                self._wake_at = now
                self._utt += 1
                self._arm(LISTENING, self.listen_timeout, "Listening…")
                # keep the tail of the wake phrase so a one-breath request isn't clipped
                self._seg = list(ring)[-6:]
                self._voiced_run = 0
                self.on_wake()
                return

            if st in (LISTENING, CONVERSATION):
                if prob >= self.speech_on:
                    self._voiced_run += 1
                else:
                    self._voiced_run = 0
                if now < self._ignore_until:
                    return                                   # echo tail: count, don't start yet
                if self._voiced_run >= 3:                    # ~96 ms of speech → start
                    # ~384 ms pre-roll so onsets are never clipped. LISTENING already keeps its
                    # own rolling buffer (wake tail + recent frames); CONVERSATION uses the ring.
                    self._seg = (self._seg if st == LISTENING else list(ring)) + [frame]
                    self._silence_ms = 0
                    self._set(HEARING, "Hearing you…")
                    return
                if st == LISTENING:
                    self._seg.append(frame)
                    self._seg = self._seg[-20:]
                if now > self._armed_until:
                    if st == CONVERSATION:
                        self._session = False
                        self._set(WAITING, self._waiting_text(), reason="followup_timeout")
                    else:
                        self._set("not_heard", "I didn't hear a request.")
                        self._session = False
                        self._set(WAITING, self._waiting_text())
                return

            if st == WAITING and self._wake is None:
                # Fallback (no acoustic model): VAD-segment speech and check the transcript.
                if prob >= self.speech_on:
                    self._voiced_run += 1
                else:
                    self._voiced_run = 0
                if self._voiced_run >= 3:
                    self._resume_state = WAITING
                    self._seg = list(ring)[-6:] + [frame]
                    self._silence_ms = 0
                    self.state = HEARING                     # quiet: don't flash the UI for chatter
                return

            if st == HEARING:
                self._seg.append(frame)
                self._silence_ms = self._silence_ms + 32 if prob < self.speech_off else 0
                too_long = len(self._seg) * FRAME / 16000.0 >= self.max_utter_sec
                if self._silence_ms >= self.end_silence_ms or too_long:
                    keep = max(1, len(self._seg) - max(0, self._silence_ms - 200) // 32)
                    audio = np.concatenate(self._seg[:keep])
                    self._seg = []
                    self._t_speech_end = now
                    self._utt += 1
                    if self._resume_state != WAITING or self._wake is not None:
                        self._set(TRANSCRIBING, "Transcribing…")
                    else:
                        self.state = TRANSCRIBING
                    try:
                        self._stt_q.put_nowait((audio, self._resume_state, self._utt))
                    except queue.Full:
                        self._back_to(self._resume_state)

    def _back_to(self, state: str) -> None:
        if state == CONVERSATION and time.time() < self._armed_until:
            self._set(CONVERSATION, "Still listening — go ahead.")
            self.state = CONVERSATION
            self._resume_state = CONVERSATION
        elif state == LISTENING and time.time() < self._armed_until:
            self._resume_state = LISTENING
            self._set(LISTENING, "Listening…")
        elif self._session:
            self._arm(CONVERSATION, self.followup_sec, "Listening — go ahead.")
        else:
            self._resume_state = WAITING
            if state == WAITING:
                self.state = WAITING                 # silent return in fallback mode
            else:
                self._set(WAITING, self._waiting_text())

    def _stt_loop(self) -> None:
        while self._running:
            try:
                item = self._stt_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            audio, origin, utt = item
            try:
                text, ms = self.stt.transcribe(audio)
            except Exception as exc:
                log.warning("stt failed: %s", exc)
                text, ms = "", 0.0
            self.last_stt_ms = ms
            with self._lock:
                if self.state != TRANSCRIBING:
                    continue                          # barged in / stopped meanwhile
                self._handle_transcript(text.strip(), origin, utt, ms)

    def _handle_transcript(self, text: str, origin: str, utt: int, stt_ms: float) -> None:
        if origin == WAITING and self._wake is None:
            hit, rest = mentions_wake(text)
            if not hit:
                self._back_to(WAITING)
                return
            self._session = True
            if not rest:
                self._utt += 1
                self.on_wake()
                self._arm(LISTENING, self.listen_timeout, "Listening…")
                return
            text = rest
        else:
            text = strip_wake(text)
        if text:
            self.last_heard = text
            self._set("heard", text, utterance_id=utt)
        if not text:
            if origin == LISTENING:
                self._set("not_heard", "I didn't catch that — say it again.", utterance_id=utt)
            self._back_to(origin)
            return
        if _DISMISS.match(text):
            stop = text.lower().startswith(("stop", "cancel"))
            if stop:
                self.on_barge_in()
            self._session = False
            self._set("accepted", "Okay." if not stop else "Stopped.", utterance_id=utt)
            self._set(WAITING, self._waiting_text())
            return
        self._turn_active = True
        self._session = True
        self._set("accepted", "Got it.", utterance_id=utt)
        self._set(PROCESSING, "Thinking…")
        meta = {"stt_ms": stt_ms, "utterance_id": utt,
                "wake_ms": None,
                "endpoint_ms": self.end_silence_ms}
        try:
            self.on_command(text, meta)
        except Exception as exc:
            log.warning("voice dispatch failed: %s", exc)
            self._turn_active = False
            self._after_turn()

    def snapshot(self) -> dict:
        return {"state": self.state, "text": self.state_text, "wake_engine": self.wake_engine,
                "stt_model": self.stt.model_name, "stt_ready": self.stt.ready,
                "session": self._session, "followup_sec": self.followup_sec,
                "last_heard": self.last_heard, "last_stt_ms": self.last_stt_ms,
                "device": self._mic.device_name if self._mic else ""}

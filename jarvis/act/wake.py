"""wake.py — optional dedicated acoustic wake-word engine (openWakeWord).

The default JARVIS wake path transcribes every utterance with Whisper and string-matches
"jarvis". That's reliable but STT-gated — the wake word is only as fast as a full
transcription (~300–700 ms) and depends on the STT model being warm. A dedicated acoustic
detector runs on the raw 16 kHz audio frames, independent of STT, and fires in tens of
milliseconds — which is the architecture a real always-on assistant wants.

This module wraps openWakeWord, which ships a pretrained "hey_jarvis" model. It is strictly
OPT-IN and fully isolated:

  * off unless JARVIS_WAKE_ENGINE=openwakeword
  * `maybe_create()` returns None (→ caller keeps the text-match path) if the package isn't
    installed or the model can't load — it never raises into the mic loop
  * when active, the text-match wake still works too, so "jarvis <command>" in one breath is
    unaffected

Enable it with:  pip install openwakeword   and   set JARVIS_WAKE_ENGINE=openwakeword

NOTE: the detection threshold is hardware/mic-dependent and must be tuned live
(JARVIS_WAKE_THRESHOLD, default 0.5). This code is wired and defensive but its real-world
accuracy can only be judged with a microphone.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("jarvis")


class WakeDetector:
    """Streaming "hey jarvis" detector. Feed it 16 kHz mono int16 frames; `triggered()`
    returns True once the model's score crosses the threshold, then arms a short refractory
    so a single wake can't fire repeatedly across consecutive frames."""

    def __init__(self, model_name: str, threshold: float):
        import numpy as np  # noqa: F401 — ensure the array type is importable up front
        from openwakeword.model import Model

        # First run needs the shared melspectrogram + embedding ONNX models; best-effort.
        try:
            from openwakeword.utils import download_models
            download_models()
        except Exception:
            pass

        self.threshold = threshold
        self._cooldown_frames = 0
        last_err: Exception | None = None
        # The bundled model has been named both "hey_jarvis" and "hey_jarvis_v0.1" across
        # releases; try the configured name first, then the known aliases.
        for name in dict.fromkeys([model_name, "hey_jarvis", "hey_jarvis_v0.1"]):
            try:
                self.model = Model(wakeword_models=[name], inference_framework="onnx")
                self.model_name = name
                log.info("wake: openWakeWord loaded (model=%s, threshold=%.2f)", name, threshold)
                return
            except Exception as exc:          # try the next alias
                last_err = exc
        raise RuntimeError(f"no usable openWakeWord model ({last_err})")

    def process(self, frame_int16) -> float:
        """Feed one frame; return the current wake score (0..1)."""
        try:
            preds = self.model.predict(frame_int16)
            return max(float(v) for v in preds.values()) if preds else 0.0
        except Exception:
            return 0.0

    def triggered(self, frame_int16) -> bool:
        """True exactly once per wake: score over threshold, then a ~1 s refractory so the
        same utterance doesn't re-fire on every following frame."""
        if self._cooldown_frames > 0:
            self._cooldown_frames -= 1
            self.process(frame_int16)   # keep the model's buffer current, but don't fire
            return False
        if self.process(frame_int16) >= self.threshold:
            self._cooldown_frames = 33   # ~1 s at 30 ms/frame
            return True
        return False


def enabled() -> bool:
    return os.environ.get("JARVIS_WAKE_ENGINE", "text").strip().lower() == "openwakeword"


def maybe_create() -> "WakeDetector | None":
    """Build the acoustic detector if it's enabled AND available, else None. Never raises —
    any problem logs a hint and leaves the caller on the reliable text-match wake path."""
    if not enabled():
        return None
    model_name = os.environ.get("JARVIS_WAKE_MODEL", "hey_jarvis").strip() or "hey_jarvis"
    try:
        threshold = float(os.environ.get("JARVIS_WAKE_THRESHOLD", "0.5"))
    except ValueError:
        threshold = 0.5
    try:
        return WakeDetector(model_name, threshold)
    except Exception as exc:
        log.warning("wake: openWakeWord requested but unavailable (%s) — falling back to the "
                    "text-match wake word. Install it with: pip install openwakeword", exc)
        return None

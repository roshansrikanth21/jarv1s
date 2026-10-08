"""stt.py — speech-to-text for already-endpointed utterances (faster-whisper, CPU int8).

Input is a segment the streaming Silero VAD has already cut, so Whisper runs exactly once
per utterance with its own VAD OFF (running it again deleted short commands and forced a
second pass). Greedy decoding and no timestamps: on this class of CPU that measured
~245 ms (tiny.en) / ~470 ms (base.en) per command versus ~2× that with the old
double-pass, beam-5 path. The model is loaded once and kept warm while the mic is on.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time

import numpy as np

log = logging.getLogger("jarvis")

MODEL_NAME = os.environ.get("JARVIS_STT_MODEL", "base.en")

# Whisper hallucinates these stock phrases on silence / noise.
_NOISE = {
    "", "you", ".", "..", "...", "thank you", "thank you.", "thanks for watching",
    "thanks for watching!", "bye", "bye.", "so", "uh", "um", "hmm", "mm", "mhm",
    "thank you for watching", "please subscribe", "subscribe", "the end", "music",
    "[music]", "(music)", "[silence]", "[blank_audio]",
}


def is_noise(text: str) -> bool:
    t = (text or "").strip().lower()
    if t in _NOISE:
        return True
    if len(re.sub(r"[^a-z0-9]", "", t)) < 2:
        return True
    words = re.findall(r"[a-z0-9']+", t)
    if len(words) >= 12 and len(set(words)) / len(words) < 0.35:
        return True                       # looped hallucination
    for n in range(2, 6):
        for i in range(max(0, len(words) - n * 3 + 1)):
            g = words[i:i + n]
            if words[i + n:i + 2 * n] == g and words[i + 2 * n:i + 3 * n] == g:
                return True
    return False


def _import_faster_whisper():
    """faster-whisper imports PyAV for file decoding we never use (we pass PCM). If Windows
    Smart App Control blocks PyAV's DLL, stub it so the import still succeeds."""
    if "av" not in sys.modules:
        try:
            import av  # noqa: F401
        except ImportError:
            import types
            sys.modules["av"] = types.ModuleType("av")
    import faster_whisper
    return faster_whisper


def _runtime() -> tuple[str, str]:
    """Pick faster-whisper (device, compute_type): CUDA float16 when a GPU is present for
    much lower STT latency, else CPU int8. JARVIS_STT_DEVICE / JARVIS_STT_COMPUTE override."""
    dev = os.environ.get("JARVIS_STT_DEVICE", "").strip().lower()
    comp = os.environ.get("JARVIS_STT_COMPUTE", "").strip()
    if dev != "cpu":
        try:
            import ctranslate2
            if dev == "cuda" or ctranslate2.get_cuda_device_count() > 0:
                return "cuda", (comp or "float16")
        except Exception:
            pass
    return "cpu", (comp or "int8")


class WhisperSTT:
    def __init__(self, model_name: str = MODEL_NAME):
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()
        self.load_error: str | None = None

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            try:
                import psutil
                threads = max(1, min(8, psutil.cpu_count(logical=False) or 4))
                device, compute = _runtime()
                fw = _import_faster_whisper()
                def _build(dev: str, comp: str):
                    m = fw.WhisperModel(self.model_name, device=dev, compute_type=comp,
                                        cpu_threads=threads, num_workers=1)
                    # A GPU model can LOAD yet fail at encode time when the CUDA runtime DLLs
                    # (cuBLAS/cuDNN) are missing — so actually run one tiny transcription to
                    # prove the device works before we commit to it. Doubles as a warmup.
                    if dev != "cpu":
                        list(m.transcribe(np.zeros(1600, np.float32), beam_size=1,
                                          without_timestamps=True)[0])
                    return m
                try:
                    self._model = _build(device, compute)
                except Exception as gpu_exc:
                    # CUDA detected but unusable (missing cuDNN/cuBLAS, driver mismatch, …).
                    # Never let that take STT down — fall back to CPU int8.
                    if device != "cpu":
                        log.warning("stt: %s on %s unusable (%s) — falling back to CPU",
                                    self.model_name, device, gpu_exc)
                        device, compute = "cpu", "int8"
                        self._model = _build(device, compute)
                    else:
                        raise
                self.load_error = None
                log.info("stt: %s loaded (device=%s compute=%s, %d threads)",
                         self.model_name, device, compute, threads)
                return True
            except Exception as exc:
                self.load_error = str(exc)
                log.warning("stt: could not load %s: %s", self.model_name, exc)
                return False

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def transcribe(self, pcm16: np.ndarray) -> tuple[str, float]:
        """Transcribe 16 kHz mono int16 speech. Returns (text, milliseconds)."""
        if self._model is None and not self.load():
            return "", 0.0
        audio = pcm16.astype(np.float32) / 32768.0
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if 0.0 < peak < 0.25:
            audio = audio * min(4.0, 0.5 / peak)     # gentle gain for quiet mics (no clipping)
        t0 = time.perf_counter()
        with self._lock:
            segments, _ = self._model.transcribe(
                audio, language="en", beam_size=1, temperature=0.0, vad_filter=False,
                condition_on_previous_text=False, without_timestamps=True,
                no_speech_threshold=0.6,
            )
            kept = [s.text for s in segments
                    if getattr(s, "no_speech_prob", 0.0) < 0.85
                    and getattr(s, "compression_ratio", 0.0) < 2.4]
        text = " ".join(kept).strip()
        ms = (time.perf_counter() - t0) * 1000.0
        return ("" if is_noise(text) else text), ms

"""wakeword.py — acoustic "hey jarvis" detection with openWakeWord's pretrained model.

A faithful, dependency-light port of openWakeWord's streaming inference (its AudioFeatures
+ Model.predict path), using only onnxruntime — the full package would pull in scipy and
scikit-learn just for its training utilities. Three small ONNX models (melspectrogram →
speech embedding → hey_jarvis classifier, ~3 MB total) score every 80 ms of audio.

This replaces "transcribe every utterance with Whisper and string-match 'jarvis'", which
was slow (a full ASR pass before anything could happen) and unreliable (tiny Whisper
routinely hears 'Travis', 'service', 'Jervis').
"""
from __future__ import annotations

import logging

import numpy as np

from . import models

log = logging.getLogger("jarvis")

CHUNK = 1280                 # openWakeWord processes 80 ms steps at 16 kHz


def _session(path):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = 1
    opts.intra_op_num_threads = 1
    opts.log_severity_level = 4
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)


class WakeWord:
    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        self._mel = _session(models.ensure("oww_melspectrogram.onnx"))
        self._emb = _session(models.ensure("oww_embedding_model.onnx"))
        self._clf = _session(models.ensure("oww_hey_jarvis_v0.1.onnx"))
        self._mel_in = self._mel.get_inputs()[0].name
        self._emb_in = self._emb.get_inputs()[0].name
        self._clf_in = self._clf.get_inputs()[0].name
        self._n_frames = int(self._clf.get_inputs()[0].shape[1] or 16)
        self.reset()

    # ── feature extraction (mirrors openwakeword.utils.AudioFeatures) ───────────────
    def _melspec(self, x_i16: np.ndarray) -> np.ndarray:
        out = self._mel.run(None, {self._mel_in: x_i16.astype(np.float32)[None, :]})
        return np.squeeze(out[0]) / 10.0 + 2.0

    def _embed(self, mel_window: np.ndarray) -> np.ndarray:
        x = mel_window.astype(np.float32)[None, :, :, None]
        return self._emb.run(None, {self._emb_in: x})[0].reshape(-1)

    def reset(self) -> None:
        self._tail = np.zeros(160 * 3, dtype=np.int16)   # mel needs 480 samples of left context
        self._pending = np.zeros(0, dtype=np.int16)
        self._mels = np.ones((76, 32), dtype=np.float32)
        # Seed the embedding history with background noise, exactly as openWakeWord does,
        # so the first real frames are scored against a sane baseline.
        noise = np.random.randint(-1000, 1000, 16000 * 4).astype(np.int16)
        spec = self._melspec(noise)
        windows = [spec[i:i + 76] for i in range(0, spec.shape[0], 8) if spec[i:i + 76].shape[0] == 76]
        self._feats = np.stack([self._embed(w) for w in windows])
        self._cooldown = 0
        self.last_score = 0.0

    def _step(self, chunk: np.ndarray) -> float:
        """Process exactly one 1280-sample chunk; return the hey_jarvis score."""
        tail = np.concatenate([self._tail, chunk])
        self._tail = tail[-(160 * 3):]
        self._mels = np.vstack([self._mels, self._melspec(tail)])[-970:]
        if self._mels.shape[0] >= 76:
            emb = self._embed(self._mels[-76:])
            self._feats = np.vstack([self._feats, emb[None, :]])[-120:]
        x = self._feats[-self._n_frames:][None, :, :].astype(np.float32)
        return float(np.asarray(self._clf.run(None, {self._clf_in: x})[0]).reshape(-1)[0])

    def process(self, frame_i16: np.ndarray, *, threshold: float | None = None) -> bool:
        """Feed any-length int16 audio. Returns True once per wake (with a ~1 s refractory
        so the same utterance can't fire twice)."""
        thr = self.threshold if threshold is None else threshold
        buf = np.concatenate([self._pending, frame_i16.astype(np.int16)])
        fired = False
        while buf.shape[0] >= CHUNK:
            chunk, buf = buf[:CHUNK], buf[CHUNK:]
            score = self._step(chunk)
            self.last_score = score
            if self._cooldown > 0:
                self._cooldown -= 1
            elif score >= thr:
                fired = True
                self._cooldown = 12          # 12 × 80 ms ≈ 1 s refractory
        self._pending = buf
        return fired

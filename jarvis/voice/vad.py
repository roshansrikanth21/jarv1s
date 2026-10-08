"""vad.py — streaming Silero VAD, the single speech detector for the pipeline.

Silero is the VAD used by pipecat, RealtimeSTT, openWakeWord and faster-whisper itself:
spectral-learned, robust to gain and background noise, and ~1 ms per 32 ms frame on one
CPU thread. We reuse the v6 ONNX model that already ships inside faster-whisper, run it
frame-by-frame carrying its recurrent state, and use it ONCE — for endpointing at capture
time. The previous design ran WebRTC VAD at capture and then Silero again inside Whisper,
which deleted short utterances ("VAD filter removed 00:00.900 of audio") and forced a
second full transcription pass.
"""
from __future__ import annotations

import os

import numpy as np

FRAME = 512          # samples per call at 16 kHz (32 ms) — Silero's native window
_CONTEXT = 64        # samples of left context the v6 model expects


def _model_path() -> str:
    from faster_whisper.utils import get_assets_path
    return os.path.join(get_assets_path(), "silero_vad_v6.onnx")


class SileroVAD:
    def __init__(self) -> None:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self._sess = ort.InferenceSession(_model_path(), providers=["CPUExecutionProvider"],
                                          sess_options=opts)
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(_CONTEXT, dtype=np.float32)

    def prob(self, frame_i16: np.ndarray) -> float:
        """Speech probability (0..1) for one 512-sample int16 frame."""
        x = frame_i16.astype(np.float32) / 32768.0
        inp = np.concatenate([self._ctx, x])[None, :]
        out, self._h, self._c = self._sess.run(None, {"input": inp, "h": self._h, "c": self._c})
        self._ctx = x[-_CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])

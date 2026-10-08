"""tts.py — text-to-speech engine: local Piper (default) or Edge neural, sentence-streamed,
played by the backend with instant interruption.

Measured on this machine, time until the first sentence is audible:
    Edge (network)        1,300–3,000 ms
    Kokoro int8 (local)   ~2,200 ms   (RTF ≈ 1.1 on this CPU)
    Piper (local)         22–270 ms   (RTF 0.04–0.15)

Design (the RealtimeTTS / GLaDOS pattern):
  * the reply is split into sentences (long ones at clause boundaries) so the first audio
    starts after ONE short synthesis, not the whole reply;
  * a synth thread produces PCM chunk N+1 while a playback thread plays chunk N, so long
    replies flow without gaps;
  * playback happens HERE (sounddevice RawOutputStream), not in the browser: the backend
    knows exactly when speech starts and ends (the mic/follow-up logic depends on that),
    speech works with the window hidden, and stop() silences audio within ~40 ms.

Voice ids: "piper:<voice>" for local Piper voices, anything else is an Edge voice name.
"""
from __future__ import annotations

import asyncio
import io
import logging
import queue
import re
import threading
import time
from typing import Callable

import numpy as np

from . import models

log = logging.getLogger("jarvis")

DEFAULT_VOICE = "piper:en_US-ryan-high"

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|(?<=[.!?])$|\n+")
_CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")


def clean_for_speech(text: str) -> str:
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)          # code blocks aren't speakable
    t = re.sub(r"https?://\S+", "the link", t)
    t = re.sub(r"[*_`#\[\]()<>|]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def sentence_chunks(text: str, *, min_len: int = 18, max_len: int = 160,
                    max_chunks: int = 40) -> list[str]:
    """Split a reply into speakable chunks — about one sentence each, long sentences broken
    at commas — so synthesis of the FIRST chunk is all that stands between the reply and
    audible speech. Tiny fragments ("Done.") merge forward."""
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    if not parts:
        return [text.strip()] if text.strip() else []
    pieces: list[str] = []
    for p in parts:
        if len(p) <= max_len:
            pieces.append(p)
            continue
        buf = ""
        for clause in _CLAUSE_SPLIT.split(p):
            if buf and len(buf) + len(clause) + 1 > max_len:
                pieces.append(buf)
                buf = clause
            else:
                buf = f"{buf} {clause}".strip()
        if buf:
            pieces.append(buf)
    chunks: list[str] = []
    buf = ""
    for part in pieces:
        buf = f"{buf} {part}".strip() if buf else part
        if len(buf) >= min_len:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks:
            chunks[-1] = f"{chunks[-1]} {buf}".strip()
        else:
            chunks.append(buf)
    if len(chunks) > max_chunks:
        chunks = chunks[:max_chunks - 1] + [" ".join(chunks[max_chunks - 1:])]
    return chunks


def play_chime(volume: float = 0.18) -> None:
    """Soft two-tone 'I'm listening' cue (~120 ms), played on its own short-lived stream so
    it never waits behind speech. Fire-and-forget; failures are silent."""
    def run():
        try:
            import sounddevice as sd
            sr = 24000
            def tone(freq, ms):
                t = np.arange(int(sr * ms / 1000)) / sr
                env = np.minimum(1.0, np.minimum(t / 0.008, (t[-1] - t) / 0.02 + 1e-9))
                return np.sin(2 * np.pi * freq * t) * env
            sig = np.concatenate([tone(880, 55), tone(1320, 70)]) * volume
            pcm = (sig * 32767).astype(np.int16)
            with sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16") as s:
                s.write(pcm.tobytes())
        except Exception:
            pass
    threading.Thread(target=run, name="wake-chime", daemon=True).start()


# ── synthesizers ────────────────────────────────────────────────────────────────────
class _Piper:
    def __init__(self, voice_id: str):
        from piper import PiperVoice
        model = models.ensure(f"piper_{voice_id}.onnx")
        models.ensure(f"piper_{voice_id}.onnx.json")
        self.voice = PiperVoice.load(str(model))
        self.sr = int(self.voice.config.sample_rate)

    def synth(self, text: str, rate: float) -> tuple[np.ndarray, int]:
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=max(0.6, min(1.5, 1.0 / max(0.5, rate))))
        audio = [c.audio_int16_array for c in self.voice.synthesize(text, syn_config=cfg)]
        return (np.concatenate(audio) if audio else np.zeros(0, np.int16)), self.sr


class _Edge:
    def __init__(self, voice: str):
        self.voice = voice

    def synth(self, text: str, rate: float) -> tuple[np.ndarray, int]:
        import av
        import edge_tts
        pct = int(round((rate - 1.0) * 100))

        async def fetch() -> bytes:
            data = b""
            async for ch in edge_tts.Communicate(text, self.voice, rate=f"{pct:+d}%").stream():
                if ch.get("type") == "audio":
                    data += ch["data"]
            return data

        mp3 = asyncio.run(fetch())
        if not mp3:
            raise RuntimeError("Edge TTS returned no audio")
        container = av.open(io.BytesIO(mp3))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
        out = []
        for frame in container.decode(audio=0):
            for f in resampler.resample(frame):
                out.append(f.to_ndarray().reshape(-1))
        for f in resampler.resample(None):
            out.append(f.to_ndarray().reshape(-1))
        return (np.concatenate(out).astype(np.int16) if out else np.zeros(0, np.int16)), 24000


# ── engine ──────────────────────────────────────────────────────────────────────────
class TTSEngine:
    """Non-blocking speech output. `say()` enqueues; events report the real playback
    window via `on_event(kind, info)` with kind in {"start", "end", "error"}."""

    def __init__(self, on_event: Callable[[str, dict], None] | None = None,
                 voice: str = DEFAULT_VOICE, rate: float = 1.0):
        self.on_event = on_event or (lambda k, i: None)
        self.voice = voice
        self.rate = rate
        self._synths: dict[str, object] = {}
        self._gen = 0
        self._lock = threading.Lock()
        self._jobs: queue.Queue = queue.Queue()
        self._pcm: queue.Queue = queue.Queue(maxsize=8)
        self._speaking = False
        self.last_first_audio_ms: float | None = None
        threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()

    # public API
    @property
    def speaking(self) -> bool:
        return self._speaking

    def set_voice(self, voice: str) -> None:
        self.voice = voice or DEFAULT_VOICE

    def warm(self) -> None:
        """Load the current voice off the hot path (first-use model download/load)."""
        threading.Thread(target=lambda: self._synth_for(self.voice), daemon=True).start()

    def say(self, text: str) -> int:
        clean = clean_for_speech(text)
        if not clean:
            return -1
        with self._lock:
            gen = self._gen
        self._jobs.put((gen, clean, time.perf_counter()))
        return gen

    def stop(self) -> None:
        """Cancel everything queued and silence current audio (~one 40 ms block)."""
        with self._lock:
            self._gen += 1
        for q in (self._jobs, self._pcm):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    # internals
    def _synth_for(self, voice: str):
        s = self._synths.get(voice)
        if s is None:
            if voice.startswith("piper:"):
                s = _Piper(voice.split(":", 1)[1])
            else:
                s = _Edge(voice)
            self._synths[voice] = s
        return s

    def _current(self) -> int:
        with self._lock:
            return self._gen

    def _synth_loop(self) -> None:
        while True:
            gen, text, t_req = self._jobs.get()
            if gen != self._current():
                continue
            chunks = sentence_chunks(text)
            for i, piece in enumerate(chunks):
                if gen != self._current():
                    break
                try:
                    try:
                        synth = self._synth_for(self.voice)
                        pcm, sr = synth.synth(piece, self.rate)
                    except Exception as exc:
                        if self.voice == DEFAULT_VOICE:
                            raise
                        log.warning("tts: voice %s failed (%s) — using %s", self.voice, exc, DEFAULT_VOICE)
                        pcm, sr = self._synth_for(DEFAULT_VOICE).synth(piece, self.rate)
                except Exception as exc:
                    log.warning("tts: synthesis failed: %s", exc)
                    self.on_event("error", {"text": f"Speech failed: {exc}"})
                    self._pcm.put((gen, None, 0, True, t_req, i))
                    break
                self._pcm.put((gen, pcm, sr, i == len(chunks) - 1, t_req, i))

    def _play_loop(self) -> None:
        import sounddevice as sd
        stream = None
        stream_sr = 0
        active_gen = None
        while True:
            try:
                gen, pcm, sr, final, t_req, idx = self._pcm.get(timeout=0.5)
            except queue.Empty:
                if stream is not None and not self._speaking:
                    try:
                        stream.close()
                    except Exception:
                        pass
                    stream, stream_sr = None, 0
                continue
            if gen != self._current():
                if active_gen == gen:
                    self._finish(gen, cancelled=True)
                    active_gen = None
                continue
            if pcm is not None and pcm.size:
                if active_gen != gen:
                    active_gen = gen
                    self._speaking = True
                    self.last_first_audio_ms = (time.perf_counter() - t_req) * 1000.0
                    self.on_event("start", {"gen": gen, "first_audio_ms": round(self.last_first_audio_ms)})
                try:
                    if stream is None or stream_sr != sr:
                        if stream is not None:
                            stream.close()
                        stream = sd.RawOutputStream(samplerate=sr, channels=1, dtype="int16",
                                                    blocksize=0, latency="low")
                        stream.start()
                        stream_sr = sr
                    block = max(256, int(sr * 0.04))        # 40 ms writes → fast stop()
                    data = pcm.astype(np.int16)
                    for start in range(0, data.size, block):
                        if gen != self._current():
                            break
                        stream.write(data[start:start + block].tobytes())
                except Exception as exc:
                    log.warning("tts: playback failed: %s", exc)
                    self.on_event("error", {"text": f"Audio output failed: {exc}"})
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
                    stream, stream_sr = None, 0
            if gen != self._current():
                if active_gen == gen:
                    self._finish(gen, cancelled=True)
                    active_gen = None
            elif final:
                if active_gen == gen:
                    # let the device drain the last block before declaring the mic safe
                    time.sleep(0.12)
                    self._finish(gen, cancelled=False)
                    active_gen = None
                else:
                    self._finish(gen, cancelled=False, started=False)

    def _finish(self, gen: int, *, cancelled: bool, started: bool = True) -> None:
        self._speaking = False
        self.on_event("end", {"gen": gen, "cancelled": cancelled, "started": started})

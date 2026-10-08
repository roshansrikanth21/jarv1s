"""audio_in.py — microphone capture: pick a working device, stream 16 kHz mono int16.

Uses sounddevice's RAW streams (bytes buffers). The NumPy-typed InputStream path inside
sounddevice 0.5.x does `data.shape = -1, channels`, which NumPy ≥2.5 deprecates — that's
the DeprecationWarning that flooded the logs on every audio callback. Raw streams never
touch that code; we convert with np.frombuffer ourselves.

Device choice is evidence-based and portable: honor the OS default mic if it delivers
real signal (a privacy-blocked mic streams pure digital silence rather than erroring),
otherwise probe every input (WASAPI first), never picking loopback/"stereo mix" endpoints.
"""
from __future__ import annotations

import logging
import queue

import numpy as np

log = logging.getLogger("jarvis")

TARGET_SR = 16000
_LIVE_LEVEL = 1          # p90 |amplitude| above this = real signal, not streamed silence


def _probe(sd, dev, rate: int, ch: int) -> int | None:
    """Open the device briefly; return p90 |amplitude| (None if it won't open/stream)."""
    acc: list[bytes] = []
    try:
        with sd.RawInputStream(device=dev, samplerate=rate, channels=ch, dtype="int16",
                               blocksize=1024, callback=lambda d, *a: acc.append(bytes(d))):
            sd.sleep(250)
    except Exception:
        return None
    if not acc:
        return None
    a = np.abs(np.frombuffer(b"".join(acc), dtype=np.int16).astype(np.int32))
    a = a[len(a) // 5:]                       # skip open-time transients
    return int(np.percentile(a, 90)) if a.size else 0


def _configs(d: dict):
    maxch = int(d.get("max_input_channels", 0) or 0)
    native = int(d.get("default_samplerate") or TARGET_SR)
    for ch in [c for c in (1, 2) if c <= maxch] or ([maxch] if maxch else [1]):
        for rate in dict.fromkeys([TARGET_SR, native]):
            yield rate, ch


def pick_input_device(sd):
    """Return (device_index, samplerate, channels) or (None, None, None)."""
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return None, None, None
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1
    silent_default = None
    if isinstance(default_in, int) and default_in >= 0:
        try:
            di = sd.query_devices(default_in)
            if int(di.get("max_input_channels", 0) or 0) >= 1:
                for rate, ch in _configs(di):
                    lvl = _probe(sd, default_in, rate, ch)
                    if lvl is None:
                        continue
                    if lvl > _LIVE_LEVEL:
                        return default_in, rate, ch
                    silent_default = (default_in, rate, ch)
                    break
        except Exception:
            pass
    pref = ["wasapi", "wdm-ks", "directsound", "mme", "core audio", "alsa", "jack", "asio"]

    def rank(name: str) -> int:
        low = name.lower()
        return next((i for i, p in enumerate(pref) if p in low), len(pref))

    cands = []
    for i, d in enumerate(devices):
        if i == default_in or int(d.get("max_input_channels", 0) or 0) < 1:
            continue
        low = (d.get("name") or "").lower()
        if any(k in low for k in ("stereo mix", "loopback", "what u hear", "speaker",
                                  "wave out", "monitor of")):
            continue
        cands.append((1 if "sound mapper" in low else 0, rank(hostapis[d["hostapi"]]["name"]), i, d))
    cands.sort(key=lambda t: (t[0], t[1], t[2]))
    fallback = None
    for _dp, _rk, i, d in cands:
        for rate, ch in _configs(d):
            lvl = _probe(sd, i, rate, ch)
            if lvl is None:
                continue
            if lvl > _LIVE_LEVEL:
                return i, rate, ch
            fallback = fallback or (i, rate, ch)
            break
    return silent_default or fallback or (None, None, None)


class MicCapture:
    """Opens the chosen mic and yields 16 kHz mono int16 audio via read()."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._stream = None
        self.device = None
        self.rate = TARGET_SR
        self.channels = 1
        self.device_name = ""
        self.error: str | None = None
        self._carry = np.zeros(0, dtype=np.float32)

    def open(self) -> bool:
        import sounddevice as sd
        dev, rate, ch = pick_input_device(sd)
        if dev is None:
            self.error = ("No usable microphone. Check Windows Settings → Privacy & security → "
                          "Microphone (allow desktop apps), that a mic is enabled, and that no "
                          "other app holds it exclusively.")
            return False
        self.device, self.rate, self.channels = dev, int(rate), int(ch)
        try:
            self.device_name = str(sd.query_devices(dev).get("name", ""))
        except Exception:
            self.device_name = ""

        def cb(indata, frames, time_info, status):
            try:
                self._q.put_nowait(bytes(indata))
            except queue.Full:
                pass                      # consumer stalled — drop rather than block PortAudio

        self._stream = sd.RawInputStream(device=dev, samplerate=self.rate, channels=self.channels,
                                         dtype="int16", blocksize=int(self.rate * 0.032),
                                         callback=cb)
        self._stream.start()
        log.info("mic: %s @ %d Hz × %d ch", self.device_name, self.rate, self.channels)
        return True

    def close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    @property
    def active(self) -> bool:
        return bool(self._stream is not None and self._stream.active)

    def read(self, timeout: float = 0.5) -> np.ndarray | None:
        """Next block of 16 kHz mono int16 (any length), or None on timeout."""
        try:
            raw = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        a = np.frombuffer(raw, dtype=np.int16)
        if self.channels > 1:
            a = a.reshape(-1, self.channels).mean(axis=1)
        a = a.astype(np.float32)
        if self.rate != TARGET_SR:
            a = self._resample(a)
        return np.clip(a, -32768, 32767).astype(np.int16)

    def _resample(self, a: np.ndarray) -> np.ndarray:
        if self.rate % TARGET_SR == 0:
            k = self.rate // TARGET_SR           # 48k→16k: box-filter decimation (anti-aliased)
            a = np.concatenate([self._carry, a])
            n = (a.size // k) * k
            self._carry = a[n:]
            return a[:n].reshape(-1, k).mean(axis=1)
        n = max(1, int(round(a.size * TARGET_SR / self.rate)))
        return np.interp(np.linspace(0, a.size - 1, n), np.arange(a.size), a)

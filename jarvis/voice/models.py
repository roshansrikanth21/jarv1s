"""models.py — local model files for the voice stack, fetched once into a user cache.

Wake-word and TTS models are small ONNX files published by their projects. They are
downloaded on first use into %LOCALAPPDATA%/JARVIS/models (or ~/.jarvis/models), written
atomically (temp file + rename) so an interrupted download never leaves a corrupt model
behind, and reused on every later start.
"""
from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path

log = logging.getLogger("jarvis")

_OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
_PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en"

# name -> download URL
URLS: dict[str, str] = {
    "oww_melspectrogram.onnx": f"{_OWW}/melspectrogram.onnx",
    "oww_embedding_model.onnx": f"{_OWW}/embedding_model.onnx",
    "oww_hey_jarvis_v0.1.onnx": f"{_OWW}/hey_jarvis_v0.1.onnx",
}

# Piper voices offered in Settings: id -> (path under the piper-voices repo, label)
PIPER_VOICES: dict[str, tuple[str, str]] = {
    "en_US-ryan-high": ("en_US/ryan/high/en_US-ryan-high", "Ryan (US, local)"),
    "en_GB-alan-medium": ("en_GB/alan/medium/en_GB-alan-medium", "Alan (British, local)"),
    "en_US-lessac-high": ("en_US/lessac/high/en_US-lessac-high", "Lessac (US, local)"),
    "en_GB-northern_english_male-medium": (
        "en_GB/northern_english_male/medium/en_GB-northern_english_male-medium",
        "Northern English (British, local)"),
}
for _vid, (_path, _label) in PIPER_VOICES.items():
    URLS[f"piper_{_vid}.onnx"] = f"{_PIPER}/{_path}.onnx"
    URLS[f"piper_{_vid}.onnx.json"] = f"{_PIPER}/{_path}.onnx.json"


def models_dir() -> Path:
    base = os.environ.get("JARVIS_MODELS_DIR")
    if base:
        p = Path(base)
    elif os.environ.get("LOCALAPPDATA"):
        p = Path(os.environ["LOCALAPPDATA"]) / "JARVIS" / "models"
    else:
        p = Path.home() / ".jarvis" / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure(name: str, *, timeout: float = 120.0) -> Path:
    """Return the local path of model `name`, downloading it first if needed. Raises on
    failure (the caller decides how to degrade)."""
    path = models_dir() / name
    if path.exists() and path.stat().st_size > 0:
        return path
    url = URLS.get(name)
    if not url:
        raise FileNotFoundError(f"unknown voice model {name!r}")
    tmp = path.with_suffix(path.suffix + ".part")
    log.info("voice: downloading %s", name)
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/voice"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise IOError(f"empty download for {name}")
    os.replace(tmp, path)
    return path

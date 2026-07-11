"""cortex.paths — filesystem locations for the cognitive memory layer.

Everything lives under the same `memory/` data dir the rest of JARVIS uses, so
persona/history/tasks and the new SQLite DB share one place on disk.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "memory"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = Path(os.environ.get("JARVIS_MEMORY_DB", str(DATA_DIR / "cortex.sqlite")))
CHROMA_DIR = Path(os.environ.get("JARVIS_CHROMA_DIR", str(DATA_DIR / "chroma")))

# Legacy files we migrate from on first run.
LEGACY_MEM_JSON = DATA_DIR / "jarvis_memory.json"
LEGACY_HIST_JSON = DATA_DIR / "jarvis_history.json"
LEGACY_PERSONA_JSON = DATA_DIR / "jarvis_persona.json"

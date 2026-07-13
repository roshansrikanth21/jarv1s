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

# Legacy files we migrate from on first run. Derived from DB_PATH's own directory (not the
# hardcoded DATA_DIR) so overriding JARVIS_MEMORY_DB — as the offline self-test does to
# isolate itself — moves the legacy lookup with it. Otherwise a "temp DB" test run still
# reads the real memory/jarvis_memory.json and silently imports the user's actual data.
# In production JARVIS_MEMORY_DB is unset, so DB_PATH.parent == DATA_DIR and this resolves
# to exactly the same paths as before.
LEGACY_MEM_JSON = DB_PATH.parent / "jarvis_memory.json"
LEGACY_HIST_JSON = DB_PATH.parent / "jarvis_history.json"
LEGACY_PERSONA_JSON = DB_PATH.parent / "jarvis_persona.json"

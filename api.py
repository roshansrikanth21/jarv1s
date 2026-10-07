#!/usr/bin/env python3
"""
JARVIS Backend
Brain priority: Groq (free, fast) → Claude (Anthropic) → Ollama (local fallback).
Whichever is configured wins, in that order — see _active_brain(), the seam the
future "Governor" (a resource/difficulty-aware policy) will replace.
Run: python api.py
"""

import asyncio
import atexit
import base64
import contextvars
import glob
import importlib.util
import io
import json
import logging
import os
import random
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# Normalize OLLAMA_HOST BEFORE any local module import that might `import ollama` at
# module top-level (models_advisor does). The Ollama Python client builds its default
# HTTP client at import time from OLLAMA_HOST — if that value is `0.0.0.0:11434`
# (a common Windows misconfig: `0.0.0.0` is a valid BIND address but not a valid
# CONNECT address), the default client gets baked with a broken URL and every later
# `ollama.embeddings/chat/list` fails. Rewrite to loopback for our process.
_oh = (os.environ.get("OLLAMA_HOST") or "").strip()
if _oh.startswith("0.0.0.0"):
    _port = _oh.split(":", 1)[1] if ":" in _oh else "11434"
    os.environ["OLLAMA_HOST"] = f"http://127.0.0.1:{_port}"

# Cap BLAS/OpenMP thread pools BEFORE numpy/ChromaDB/CTranslate2 load. Left unset, each of
# these libraries spawns one worker thread PER CPU CORE — on a modern many-core laptop that's
# dozens of idle threads and their stacks resident for our tiny matrices (LinUCB is 10-dim,
# embeddings are small-batch), pure RSS waste. Cap at 4 (plenty for our workloads); setdefault
# so a power user can still override. This is the global thread bound the perf commit missed.
_tcap = str(max(1, min(4, (os.cpu_count() or 4))))
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, _tcap)

import jarvis.host.device as device
import jarvis.host.system_monitor as system_monitor
import jarvis.host.models_advisor as models_advisor
import jarvis.cognition.governor as governor
import jarvis.playbooks.loader as skills
import jarvis.presence.ambient as ambient
import jarvis.presence.briefing as briefing
import jarvis.presence.perception as perception
import jarvis.presence.persona as persona_mod
import jarvis.agents.subagents as subagents
import jarvis.act.desktop as desktop
import jarvis.act.web_search as websearch_mod
import jarvis.act.fastpath as fastpath
import jarvis.act.verify as verify
import jarvis.act.wake as wake

import cortex
from jarvis.memory.dialogue import DialogueStore
from jarvis.policy.shell import check_command as _policy_check_command
from jarvis.policy.browse import check_url as _policy_check_browse_url
from jarvis.policy.desktop import desktop_risk as _desktop_risk
from jarvis.cognition.router import plan_from_governor, provider_for_rung, health as _router_health
from jarvis.events.bus import bus as _event_bus
from jarvis.platform import telemetry as _telemetry
from jarvis.session.state import SessionState, WorkflowPhase
from jarvis.session.tasks import TaskManager

logging.basicConfig(
    level=os.environ.get("JARVIS_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] jarvis: %(message)s",
)
log = logging.getLogger("jarvis")

BASE_DIR     = Path(__file__).parent
MEMORY_FILE   = BASE_DIR / "memory" / "jarvis_memory.json"
HISTORY_FILE  = BASE_DIR / "memory" / "jarvis_history.json"
OVERHEARD_FILE = BASE_DIR / "memory" / "jarvis_overheard.json"  # rolling ambient speech log
TASKS_FILE    = BASE_DIR / "memory" / "jarvis_tasks.json"
TASK_SEQ_FILE = BASE_DIR / "memory" / "jarvis_task_seq.json"  # monotonic TASK-### counter
SETTINGS_FILE = BASE_DIR / "memory" / "jarvis_settings.json"
GOVERNOR_FILE = BASE_DIR / "memory" / "jarvis_governor.json"
PERSONA_FILE  = BASE_DIR / "memory" / "jarvis_persona.json"
MEMORY_FILE.parent.mkdir(exist_ok=True)
TRADING_ROOT = Path(os.environ.get("C0MR4DES_DIR", str(BASE_DIR.parent / "c0mr4des_terminal")))

# Load .env from repo root if present.
#
# API-KEY PRECEDENCE: the Electron app injects keys from its encrypted store as env vars
# BEFORE this runs, so a plain setdefault would let a STALE stored key shadow a fresh one the
# user just typed into .env — the exact "I put a new key in but it still says limit reached"
# trap. So for the known key vars, an explicitly-set .env value OVERRIDES the injected env
# (editing .env is a deliberate, current action). Everything else keeps setdefault semantics
# (real env still wins) so runtime overrides like JARVIS_PORT behave normally.
_ENV_OVERRIDE = {"GROQ_API_KEY", "ANTHROPIC_API_KEY", "MEM0_API_KEY", "GROQ_MODEL"}
_env_file = BASE_DIR / ".env"
# Security toggles: an explicit .env value must win over ambient process env (e.g. a parent
# shell that exported JARVIS_SHELL_APPROVAL=0 for selftests would otherwise silently disable
# the shell gate forever via setdefault).
_ENV_FORCE = {
    "JARVIS_SHELL_APPROVAL",
    "JARVIS_APPROVAL_TOOLS",
    "JARVIS_WS_ALLOW_ALL",
    "JARVIS_WS_PORTS",
}
# Values displaced by an .env override. An override is only a *preference*: if the .env key turns
# out to be stale/revoked, the displaced (app-injected) key is still a working fallback, so it is
# kept in the rotation pool instead of being thrown away (see GROQ_API_KEYS below).
_OVERRIDDEN_ENV: dict[str, str] = {}
if _env_file.exists():
    # utf-8-sig strips a Windows/PowerShell BOM so "GROQ_API_KEY" isn't read as "\ufeffGROQ_API_KEY".
    for _line in _env_file.read_text(encoding="utf-8-sig").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip().lstrip("\ufeff")
            _v = _v.strip()
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in "\"'":
                _v = _v[1:-1]
            if _k in _ENV_FORCE:
                os.environ[_k] = _v          # security toggles: .env always wins
            elif _k in _ENV_OVERRIDE and _v:
                _prior = os.environ.get(_k, "")
                if _prior and _prior != _v:
                    _OVERRIDDEN_ENV[_k] = _prior      # keep the displaced key as a fallback
                os.environ[_k] = _v          # explicit .env value wins over a stale injected key
            else:
                os.environ.setdefault(_k, _v)

# ── Brain config ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"   # fast + cheap; swap to claude-sonnet-4-6 for more power
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL", "").strip()  # optional pin; else auto-detect from Ollama
OLLAMA_KEEP_ALIVE = os.environ.get("JARVIS_OLLAMA_KEEP_ALIVE", "90")  # seconds in RAM after each local call
OLLAMA_RELEASE_RAM_PCT = int(os.environ.get("JARVIS_OLLAMA_RELEASE_RAM", "82"))  # unload after reply when RAM above this
VISION_MODEL      = "llava:latest"
USE_CLAUDE        = bool(ANTHROPIC_API_KEY)

_AnthropicClient: Any = None
# Deferred: find_spec (~ms) tells us Claude is available; the real ~1.7s `anthropic` import
# only happens in _anthropic() when a Claude call is actually made — off the boot path.
_HAS_ANTHROPIC = importlib.util.find_spec("anthropic") is not None


def _anthropic():
    """Lazy handle to the anthropic AsyncAnthropic client class."""
    global _AnthropicClient
    if _AnthropicClient is None:
        from anthropic import AsyncAnthropic
        _AnthropicClient = AsyncAnthropic
    return _AnthropicClient


# GROQ_API_KEY may hold ONE key or SEVERAL (comma/space/newline-separated). With more than
# one, JARVIS auto-rotates to the next on a rate-limit or auth failure — so when a free-tier
# key exhausts its daily/minute quota, the next key picks up seamlessly instead of the brain
# going dead. Add keys any time by editing .env: GROQ_API_KEY=gsk_aaa,gsk_bbb,gsk_ccc
def _split_keys(raw: str) -> list[str]:
    return [k for k in re.split(r"[,\s]+", (raw or "").strip()) if k]


# Pool order: the preferred (.env / current env) key(s) first, then any key the .env override
# displaced (the app-injected one) — de-duplicated. So a stale key in EITHER place can't leave
# JARVIS without a brain as long as the other one works.
GROQ_API_KEYS   = list(dict.fromkeys(
    _split_keys(os.environ.get("GROQ_API_KEY", "")) + _split_keys(_OVERRIDDEN_ENV.get("GROQ_API_KEY", ""))))
_groq_key_idx   = 0
GROQ_API_KEY    = GROQ_API_KEYS[0] if GROQ_API_KEYS else ""
_groq_bad_keys: set[str] = set()      # keys Groq rejected (401/403) this session — never picked again
_groq_key_alert: str | None = None    # shown to a UI client on connect (startup check may run pre-connect)


def _rotate_groq_key(reason: str = "") -> bool:
    """Advance to the next usable Groq key (wrapping), skipping keys already known to be
    rejected. Mutates the module-global GROQ_API_KEY so every subsequent client picks up the new
    key. Returns True only if it actually switched to a different, not-known-bad key."""
    global _groq_key_idx, GROQ_API_KEY
    n = len(GROQ_API_KEYS)
    if n <= 1:
        return False
    for step in range(1, n):
        cand = (_groq_key_idx + step) % n
        if GROQ_API_KEYS[cand] not in _groq_bad_keys:
            _groq_key_idx = cand
            GROQ_API_KEY = GROQ_API_KEYS[cand]
            log.warning("Groq key rotated to #%d/%d (%s)", cand + 1, n, reason or "failover")
            return True
    return False


def _groq_key_rejected() -> bool:
    """The current key got a 401/403: remember it as bad, then move to the next usable one.
    Returns True if another usable key exists."""
    _groq_bad_keys.add(GROQ_API_KEY)
    return _rotate_groq_key("auth failure")


def _pick_served_groq_model(key: str, *, avoid: str = "") -> str:
    """Chat model this key can call when the configured id 404s.

    Groq retires model ids without notice. The fallback is the account's live
    /models list. Whisper, TTS, and classifier models are skipped.
    """
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "jarvis/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return ""
    ids = [m.get("id", "") for m in payload.get("data", []) if m.get("id") and m["id"] != avoid]
    skip = ("whisper", "orpheus", "prompt-guard", "embed", "tts")
    chat = [mid for mid in ids if not any(s in mid.lower() for s in skip)]
    for pref in ("qwen/qwen3.8-27b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"):
        if pref in chat:
            return pref
    return chat[0] if chat else ""


def _probe_groq_key(key: str) -> str:
    """'valid' | 'invalid' | 'unknown' via Groq's free GET /models (consumes no tokens).
    'unknown' (offline, 429, 5xx) must never mark a key bad."""
    req = urllib.request.Request("https://api.groq.com/openai/v1/models",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": "jarvis/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return "valid" if r.status == 200 else "unknown"
    except urllib.error.HTTPError as exc:
        return "invalid" if exc.code in (401, 403) else "unknown"
    except Exception:
        return "unknown"


# Cloud model tiers (Groq). Everyday chat must NOT default to a 120B *reasoning* model:
# reasoning emits a large hidden token stream that (a) overruns max_tokens (the "max
# completion tokens reached" JSON failures) and (b) exhausts the free tier in a handful of
# messages. So we tier by task difficulty (see _groq_model_for) and keep the heavy reasoning
# model for the explicit council only.
#   fast : greetings / trivial chat  — cheap, high rate limits
#   main : normal + tool-calling work — strong, non-reasoning, no hidden-token blowup
GROQ_FAST_MODEL = os.environ.get("JARVIS_GROQ_FAST_MODEL", "qwen/qwen3.8-27b")
GROQ_MODEL      = os.environ.get("GROQ_MODEL", "qwen/qwen3.8-27b")
# Sub-agents on the same served chat model. The old llama-3.1/3.3 ids 404 on current Groq keys.
SUBAGENT_MODEL  = os.environ.get("JARVIS_SUBAGENT_MODEL", "qwen/qwen3.8-27b")
GROQ_REASONING  = os.environ.get("GROQ_REASONING_EFFORT", "low")       # low | medium | high (gpt-oss only); low = snappier
# Free-tier tokens-per-minute ceiling for the chat model. The whole request (system +
# history + tools schema) PLUS the completion must fit under this or Groq 413s the call —
# which used to surface as an empty answer. We size max_tokens against it per round.
# gpt-oss-120b on-demand = 8000 TPM; bump via env if you're on a paid tier.
GROQ_TPM_CEILING = int(os.environ.get("JARVIS_GROQ_TPM", "8000"))
GROQ_TIMEOUT    = float(os.environ.get("JARVIS_GROQ_TIMEOUT", "45"))   # hard cap so a slow/hung API never stalls the agent
STT_MODEL       = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# ── Browser automation (browser-harness, isolated venv → Chrome via CDP) ─────────
# Isolated so browser-harness's pinned deps (websockets 15) never clash with JARVIS's
# (16). JARVIS launches and OWNS a dedicated-profile debug Chrome; it will NOT silently
# drive a foreign Chrome already on the port (that could be your personal, logged-in
# browser) unless you opt in with JARVIS_BH_ATTACH=1. CDP is reached via `localhost` —
# newer Chrome blocks the /json endpoints when addressed as 127.0.0.1.
BH_CLI      = os.environ.get("JARVIS_BH_CLI", str(BASE_DIR.parent / "bh-venv" / "Scripts" / "browser-harness.exe"))
BH_PORT     = int(os.environ.get("JARVIS_BH_PORT", "9222"))
BH_CDP_URL  = os.environ.get("JARVIS_BH_CDP_URL", f"http://localhost:{BH_PORT}")
BH_CHROME   = os.environ.get("JARVIS_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BH_PROFILE  = os.environ.get("JARVIS_BH_PROFILE", str(BASE_DIR.parent / "bh-chrome-profile"))
BH_HEADLESS = os.environ.get("JARVIS_BH_HEADLESS", "0") != "0"   # default: visible, so you can watch it work
BH_TIMEOUT  = int(os.environ.get("JARVIS_BH_TIMEOUT", "150"))
# Opt-in ONLY: drive a Chrome already listening on the debug port. Off by default so JARVIS
# never silently attaches to (and acts inside) your personal, logged-in browser.
BH_ATTACH   = os.environ.get("JARVIS_BH_ATTACH", "0") != "0"
# Optional comma-separated host allowlist for browse(); when set, navigation is restricted
# to these hosts (and their subdomains). Empty = any public host (private/loopback always blocked).
BH_ALLOWLIST = {h.strip().lower() for h in os.environ.get("JARVIS_BROWSE_ALLOWLIST", "").split(",") if h.strip()}

# Wake word: voice commands only fire when the utterance STARTS with one of these
# (prefix-anchored in _match_wake_word). Includes a few common Whisper mis-hears of
# "jarvis" but NOT collision-prone tokens like "travis"/"jarvi"/"javis" that fire on
# ordinary speech. Set JARVIS_WAKE_REQUIRED=0 to disable.
WAKE_WORDS = [w.strip().lower() for w in os.environ.get(
    "JARVIS_WAKE_WORDS", "jarvis,jervis,jarvus,charvis,jarvix"
).split(",") if w.strip()]
WAKE_REQUIRED = os.environ.get("JARVIS_WAKE_REQUIRED", "1") != "0"
# After a bare "jarvis" (wake word with no command), stay armed this many seconds and
# take the NEXT thing you say as the command — so "Jarvis…" [pause] "what's the weather"
# works like any real assistant, not just "jarvis what's the weather" in one breath.
WAKE_WINDOW = float(os.environ.get("JARVIS_WAKE_WINDOW", "8"))
WAKE_ACKS = ["Yes, sir?", "Yes, sir.", "Go ahead, sir.", "Sir?", "I'm listening, sir."]
# Always-on ears: auto-start the mic when a client connects, so JARVIS is listening
# without a button press. It still only ACTS on utterances addressed with the wake word.
ALWAYS_LISTEN = os.environ.get("JARVIS_ALWAYS_LISTEN", "1") != "0"
# Ambient memory: store EVERYTHING transcribed (wake word or not) as a rolling log JARVIS
# can relate to later. Set JARVIS_STORE_OVERHEARD=0 to only ever transcribe/keep commands.
STORE_OVERHEARD = os.environ.get("JARVIS_STORE_OVERHEARD", "1") != "0"
OVERHEARD_MAX = int(os.environ.get("JARVIS_OVERHEARD_MAX", "200"))
STOP_WORDS = {"stop", "stop talking", "shut up", "be quiet", "quiet", "cancel",
              "enough", "shut up jarvis", "nevermind", "never mind"}
RESET_PHRASES = {"new conversation", "start over", "start fresh", "reset",
                 "forget that", "forget all that", "clear context", "let's start over"}
# Observability: when on, every turn emits a stage-by-stage timing trace (route→exec→verify
# →respond, plus STT/TTS latencies) to the UI ops console as a `debug_trace` event. Timings
# are always logged at INFO regardless; this only gates the extra WS broadcast. Toggle live
# from the UI (set_debug) or with JARVIS_DEBUG=1.
JARVIS_DEBUG = os.environ.get("JARVIS_DEBUG", "0") == "1"
# Fast path: deterministic execution of simple OS commands (open/close app, volume,
# brightness, media keys, wifi, lock, screenshot) WITHOUT the reasoning model. Set
# JARVIS_FASTPATH=0 to force everything through the full agent.
FASTPATH_ENABLED = os.environ.get("JARVIS_FASTPATH", "1") != "0"

# Voice (Microsoft Edge neural TTS). Default is the newest "Multilingual" conversation
# voice — markedly more natural/human than the older neural voices. Andrew is warm and
# confident; the British Ryan/Thomas remain a click away for the classic butler feel.
TTS_VOICE = os.environ.get("JARVIS_TTS_VOICE", "en-US-AndrewMultilingualNeural")
TTS_RATE  = os.environ.get("JARVIS_TTS_RATE", "+3%")   # multilingual voices read best near natural pace
TTS_PITCH = os.environ.get("JARVIS_TTS_PITCH", "+0Hz")

# Voices the user can pick from at runtime (curated subset of Edge neural voices).
# The Multilingual "Conversation" voices are the most natural-sounding — listed first.
VOICE_OPTIONS = [
    {"id": "en-US-AndrewMultilingualNeural", "label": "Andrew · natural, warm (default)"},
    {"id": "en-US-BrianMultilingualNeural",  "label": "Brian · natural, casual"},
    {"id": "en-US-AvaMultilingualNeural",    "label": "Ava · natural female"},
    {"id": "en-GB-RyanNeural",        "label": "Ryan · British male (classic JARVIS)"},
    {"id": "en-GB-ThomasNeural",      "label": "Thomas · British male, warm"},
    {"id": "en-US-GuyNeural",         "label": "Guy · US male, deep"},
    {"id": "en-US-EricNeural",        "label": "Eric · US male, calm"},
    {"id": "en-AU-WilliamNeural",     "label": "William · Australian male"},
    {"id": "en-GB-SoniaNeural",       "label": "Sonia · British female"},
    {"id": "en-US-JennyNeural",       "label": "Jenny · US female, friendly"},
]

# Spoken filler so longer tasks don't sit in dead silence while JARVIS works.
# Only fires for genuinely slow tools — fast ones (system info, tasks) answer
# quickly enough that a filler would just talk over the reply.
FILLERS = ["On it.", "One sec.", "Let me check.", "Looking now.",
           "Give me a moment.", "Checking that."]
SLOW_TOOLS = {"capture_screen", "search_web", "run_command", "ict_scan", "analyze_image", "watch_video", "get_weather", "browse", "recon", "pentest", "bugbounty"}
# Tools that pause for an explicit UI confirm before executing. Off with JARVIS_SHELL_APPROVAL=0
# (selftests / headless). Default on — shell is the highest-blast-radius tool.
APPROVAL_TOOLS = {
    t.strip() for t in os.environ.get("JARVIS_APPROVAL_TOOLS", "run_command").split(",") if t.strip()
}
SHELL_APPROVAL = os.environ.get("JARVIS_SHELL_APPROVAL", "1") != "0"
_APPROVAL_TIMEOUT_SEC = float(os.environ.get("JARVIS_APPROVAL_TIMEOUT", "90"))
# id → (asyncio.Event, result_box) where result_box is a one-element list [bool|None]
_pending_approvals: dict[str, tuple[asyncio.Event, list]] = {}

CONV_TURNS = 8   # how many past messages (user+assistant) to keep as context

# Short-term dialogue window — SQLite is authoritative (one-shot import from legacy JSON).
_dialogue = DialogueStore(
    BASE_DIR / "memory" / "dialogue.sqlite",
    window=CONV_TURNS,
    legacy_json=HISTORY_FILE,
    emit_events=False,  # api emits MemoryUpdated after cortex write
)
_session = SessionState(session_id="local")

# Mixture-of-Agents: a panel of different models answers independently, then an
# aggregator reconciles them into one decision. Triggered on demand (see TRIGGERS).
MOA_PROPOSERS = [m.strip() for m in os.environ.get(
    "JARVIS_MOA_PROPOSERS",
    "qwen/qwen3.8-27b,openai/gpt-oss-20b,openai/gpt-oss-120b",
).split(",") if m.strip()]
MOA_AGGREGATOR = os.environ.get("JARVIS_MOA_AGGREGATOR", "openai/gpt-oss-120b")
MOA_TRIGGERS = ("deliberate", "council", "debate", "think hard about", "convene", "panel")

# ICT watcher: scan a watchlist on a timer and alert on fresh BOS / liquidity sweeps.
WATCHLIST = [s.strip() for s in os.environ.get("JARVIS_WATCHLIST", "nifty,sensex").split(",") if s.strip()]
WATCH_INTERVAL_MIN = int(os.environ.get("JARVIS_WATCH_INTERVAL_MIN", "5"))
WATCH_TF = os.environ.get("JARVIS_WATCH_TF", "15m")
WATCH_SPEAK = os.environ.get("JARVIS_WATCH_SPEAK", "1") != "0"
USE_GROQ        = bool(GROQ_API_KEY)

_openai_mod: Any = None
# Availability is checked WITHOUT importing (find_spec is ~ms); the real ~1.2s import is
# deferred to _openai() on first cloud call, keeping it off the boot path.
_HAS_GROQ = importlib.util.find_spec("openai") is not None
if not _HAS_GROQ:
    USE_GROQ = False


def _openai():
    """Lazy handle to the openai SDK (used for Groq's OpenAI-compatible API). Populates the
    module global on first use so existing `except _openai_mod.X` clauses keep working."""
    global _openai_mod
    if _openai_mod is None:
        import openai as _mod
        _openai_mod = _mod
    return _openai_mod


def _active_model() -> str:
    if USE_GROQ and _HAS_GROQ:
        return GROQ_MODEL
    if USE_CLAUDE and _HAS_ANTHROPIC:
        return CLAUDE_MODEL
    if _LOCAL_OK and LOCAL_FAST:
        return LOCAL_FAST
    return OLLAMA_MODEL or "unconfigured"


def _routing_label() -> str:
    """Honest brain label for status — last Governor pick, not a static config default."""
    if _last_decision:
        return str(_last_decision.get("label") or _last_decision.get("rung") or _active_model())
    avail = _available_rungs()
    if not avail:
        return "unconfigured"
    if _gov.mode == "local":
        return LOCAL_FAST or LOCAL_DEEP or "local (waiting for Ollama)"
    if _gov.mode == "cloud" and not (avail & {"cloud_fast", "cloud_deep", "council"}):
        return "cloud (no API key)"
    return f"{_gov.mode} · {', '.join(sorted(avail)[:3])}"


# ── App ────────────────────────────────────────────────────────────────────────
async def _boot_probe():
    """Hardware + local-model detection, OFF the startup critical path — an Ollama
    probe or device scan can be slow or hang, and must not delay serving requests."""
    global _last_device
    try:
        await asyncio.to_thread(_detect_local_models)
    except Exception:
        pass
    try:
        _last_device = await asyncio.to_thread(device.profile)
    except Exception:
        _last_device = {}
    try:
        print(f"[JARVIS] Governor rungs: {sorted(_available_rungs())} | "
              f"tier: {(_last_device or {}).get('tier')} | local: {LOCAL_FAST if _LOCAL_OK else 'off'}")
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _main_loop, _sleep_task, _ambient_task, _boot_task, _monitor_task
    _main_loop = asyncio.get_event_loop()
    brain = "Groq" if (USE_GROQ and _HAS_GROQ) else "Claude" if (USE_CLAUDE and _HAS_ANTHROPIC) else "Ollama"
    print(f"[JARVIS] Online — cloud: {_active_model()} ({brain})")
    print(f"[JARVIS] Memory: {len(memories)} | Tasks: {len(task_list)} | Tools: {len(TOOLS)}")
    print(f"[JARVIS] Affect: emotion={'on' if persona_mod.ENABLED else 'off'} ({persona_mod.SARCASM}) | ambient awareness on")
    # Cortex: SQLite (WAL) + Chroma-or-RAM vector index. First boot migrates any
    # legacy jarvis_memory.json / jarvis_history.json into the new store.
    try:
        _cortex_stats = await asyncio.to_thread(cortex.init)   # fast: schema + client + dim only
        print(f"[JARVIS] Cortex: {_cortex_stats}")
        cortex.emotion.sync_from_persona(_persona)
        # Re-index/reconcile OFF the boot path — a warm Chroma restart re-embeds nothing, and
        # the in-RAM fallback rebuilds without blocking the server from serving.
        asyncio.create_task(asyncio.to_thread(cortex.warm))
    except Exception as _exc:
        log.warning("cortex init failed: %s", _exc)
    # Background tasks (don't block serving): hardware probe, sleep cycle, ambient,
    # monitor, proactive silence-break.
    _boot_task = asyncio.create_task(_boot_probe())
    asyncio.create_task(_groq_key_healthcheck())          # heal a stale key before the first turn
    _sleep_task = asyncio.create_task(_sleep_loop())
    _ambient_task = asyncio.create_task(_ambient_loop())
    _monitor_task = asyncio.create_task(_monitor_loop())
    _proactive_task = asyncio.create_task(_proactive_loop())
    # (Cortex init above already embedded every fact + episode via vectors._bootstrap_from_store,
    #  so first recall is warm without a separate task.)
    # Serve the built SPA when present (packaged desktop); else Vite serves it in dev.
    spa_dir = BASE_DIR / "dist" / "client"
    if (spa_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(spa_dir), html=True), name="ui")
        print(f"[JARVIS] Serving UI from {spa_dir}")
    else:
        print("[JARVIS] Dev mode — UI served by Vite on :8080")
    yield
    for _t in (_boot_task, _sleep_task, _ambient_task, _monitor_task, _proactive_task):
        if _t:
            _t.cancel()


app = FastAPI(title="JARVIS Backend", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    # Local desktop app only — never the open internet. Allows any localhost port
    # (Vite dev, packaged SPA) + Electron file/app origins; blocks external sites
    # from reaching the backend through the user's browser.
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|file://.*|app://.*)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State ──────────────────────────────────────────────────────────────────────
active_connections: list[WebSocket] = []
# Remote callers (POST /api/ask, used by the OpenClaw MCP bridge) need the reply of the
# turn they started, but replies only go out via broadcast(). A caller registers a queue
# here for the life of its turn; broadcast() copies llm_response + agent_tool events in.
_reply_listeners: list[asyncio.Queue] = []
# False inside a turn started with speak=false. asyncio copies context into tasks it
# creates, so this reaches the turn task and every filler / final reply it schedules,
# while voice, alerts and briefings (other contexts) keep the default and still speak.
_SPEAK_REPLY: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_speak_reply",
                                                                    default=True)
task_list:   list[dict] = []
agent_trace: list[dict] = []
memories:    list[dict] = []
_overheard:  list[dict] = []   # rolling log of everything heard (ambient memory)
_overheard_dirty = 0           # utterances since last persist (throttles disk writes)
_main_loop:  asyncio.AbstractEventLoop | None = None
_listening = False
_awake_until = 0.0             # armed-for-command deadline after a bare wake word
_last_ack_at = 0.0            # last time a wake-ack played — dedupes acoustic + text wake
_listen_thread: threading.Thread | None = None
_voice_lock = threading.Lock() # serializes mic start/stop so they can't spawn two InputStreams
# Whisper rate gate — a trigger-happy mic (noisy room / weak VAD) can fire dozens of
# transcription calls a second, which 429s Groq's Whisper endpoint AND burns the shared
# rate budget the chat model needs (surfacing as empty replies). This hard-caps the call
# rate and backs off on 429 so the flood can never happen, whatever the mic does.
_stt_lock = threading.Lock()
_stt_last_ts = 0.0             # monotonic time of the last Whisper call
_stt_backoff_until = 0.0       # skip transcription until this monotonic time (set on 429)
STT_MIN_GAP = float(os.environ.get("JARVIS_STT_MIN_GAP", "0.6"))   # ≥ this many seconds between calls
STT_BACKOFF = float(os.environ.get("JARVIS_STT_BACKOFF", "4.0"))   # cool-off after a rate-limit hit
_tts_playing = False          # frontend reports the exact playback window
_tts_ended_at = 0.0           # when playback last ended — mic stays muted a beat after, so the
                              # acoustic tail/reverb of JARVIS's own voice can't retrigger the VAD
_tts_gen = 0                  # bumped per TTS clip; lets a stale mute-failsafe know it's superseded
_speaking_text = ""           # current TTS text (lowercased) — used as an echo guard
_current_task = None          # in-flight handle_command task (for barge-in cancel)
_speak_task: asyncio.Task[None] | None = None  # in-flight _speak task (for barge-in cancel)
_turn_generation = 0          # bumped on every new dispatch; lets a barged-in tool
                               # thread (which asyncio.to_thread cannot forcibly stop)
                               # notice it's stale and quiet down instead of surfacing
                               # results/progress for a turn that's no longer current
# Rolling dialogue is owned by `_dialogue` (SQLite). Prefer `_history_messages()` /
# `_dialogue.turn_seq` — module aliases below stay for routers/legacy reads.
_tts_voice = TTS_VOICE        # runtime-selectable voice (changed via set_voice)
_watch_task = None            # background ICT watcher task
_watching = False
_watch_state: dict = {}       # symbol -> last {bos, sweep, bias} signature
_filler_sent = False          # slow-tool spoken-filler gate (reset each command)
_ict_cache: dict = {}         # (symbol, interval) -> (epoch, result) short-TTL cache
_ICT_CACHE_MAX = 32
_ICT_CACHE_TTL_SEC = 30
_last_device: dict = {}       # most-recent device profile (drives homeostasis)
_last_activity = time.time()  # for the idle "sleep" trigger
_last_consolidated_turn = 0     # dialogue.turn_seq at last consolidation
_last_decision: dict | None = None   # last Governor decision (for escalation signal)
_sleep_task = None            # background consolidation ("sleep") loop
_sleeping = False
_LOCAL_OK = False             # Ollama up + a tool-capable local model installed
LOCAL_FAST = ""               # set by _detect_local_models()
LOCAL_DEEP = ""               # set by _detect_local_models()
IDLE_SLEEP_MIN = int(os.environ.get("JARVIS_SLEEP_IDLE_MIN", "3"))

# ── Observability: per-turn latency trace ────────────────────────────────────────
_turn_trace: "TurnTrace | None" = None


def _debug_on() -> bool:
    return JARVIS_DEBUG or bool(_settings.get("debug"))


class TurnTrace:
    """Stage-by-stage stopwatch for one command, start (wake/recv) → spoken reply. Each
    `mark()` records milliseconds since the turn began; `emit()` logs the breakdown and, when
    debug is on, broadcasts it as `debug_trace` for the UI ops console. One per turn; replaced
    on the next dispatch. Keeps the latency profile (spec §13/§16) real instead of guessed."""
    __slots__ = ("text", "source", "t0", "marks", "path", "_wake_ms", "_stt_ms", "_done")

    def __init__(self, text: str, source: str, *, wake_ms: float | None = None,
                 stt_ms: float | None = None):
        self.text = (text or "")[:120]
        self.source = source                    # voice | typed | remote
        self.t0 = time.perf_counter()
        self.marks: list[tuple[str, int]] = []
        self.path: str | None = None            # fast | agent
        self._wake_ms = wake_ms
        self._stt_ms = stt_ms
        self._done = False

    def mark(self, stage: str) -> None:
        self.marks.append((stage, int((time.perf_counter() - self.t0) * 1000)))

    def total_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)

    async def emit(self, status: str, *, extra: dict | None = None) -> None:
        if self._done:
            return
        self._done = True
        payload = {
            "type": "debug_trace",
            "source": self.source,
            "path": self.path,
            "text": self.text,
            "status": status,
            "wake_ms": round(self._wake_ms) if self._wake_ms is not None else None,
            "stt_ms": round(self._stt_ms) if self._stt_ms is not None else None,
            "stages": [{"stage": s, "ms": ms} for s, ms in self.marks],
            "total_ms": self.total_ms(),
        }
        if extra:
            payload.update(extra)
        stage_str = " ".join(f"{s}={ms}ms" for s, ms in self.marks)
        pre = []
        if self._wake_ms is not None:
            pre.append(f"wake={round(self._wake_ms)}ms")
        if self._stt_ms is not None:
            pre.append(f"stt={round(self._stt_ms)}ms")
        log.info("[trace] %s path=%s %s %s total=%dms (%s)",
                 self.source, self.path, " ".join(pre), stage_str, self.total_ms(), status)
        if _debug_on():
            try:
                await broadcast(payload)
            except Exception:
                pass

# ── Affect / ambient state ──────────────────────────────────────────────────────
_audio_arousal: float | None = None   # mic-loudness arousal hint (voice turns only)
_last_read = None                      # last perception.Read (drives prompt + UI)
_ambient_task = None                   # background ambient (weather/location) refresher
_boot_task = None                      # one-shot hardware/local-model probe (off critical path)
_monitor_task = None                   # proactive CPU/RAM/temp/GPU alerts
_proactive_task = None                 # proactive silence-break loop (idle → optional suggestion)
_last_proactive = 0.0                  # timestamp of last proactive utterance (rate-limit)
_sys_monitor = system_monitor.SystemMonitor()
_briefing_running = False
_pending_content_panel: dict | None = None

_MEMORY_CATEGORIES = {
    "identity": "personal",
    "personal": "personal",
    "preferences": "preference",
    "preference": "preference",
    "projects": "project",
    "project": "project",
    "relationships": "personal",
    "wishes": "fact",
    "notes": "fact",
    "fact": "fact",
    "security": "security",
    "task": "task",
    "general": "fact",
}


def _norm_memory_category(cat: str) -> str:
    return _MEMORY_CATEGORIES.get((cat or "fact").lower().strip(), "fact")


# ── Memory + persistence ────────────────────────────────────────────────────────
def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        log.warning("failed to save %s: %s", path.name, exc)


def _migrate_memories(mems: list[dict]) -> list[dict]:
    """Backfill schema on pre-migration memories in the legacy JSON mirror. Derive the
    timestamps from the ISO `timestamp` string when present, and fill the bookkeeping
    fields any legacy path expects. (Cortex owns the authoritative store now.)"""
    for m in mems:
        if not isinstance(m, dict):
            continue
        if "ts" not in m or "last_access" not in m:
            base = None
            iso = m.get("timestamp")
            if isinstance(iso, str):
                try:
                    base = datetime.fromisoformat(iso).timestamp()
                except Exception:
                    base = None
            if base is None:
                base = time.time()
            m.setdefault("ts", base)
            m.setdefault("last_access", base)
        m.setdefault("access_count", 0)
        m.setdefault("importance", 5)
        m.setdefault("namespace", "personal")
        m.setdefault("source_model", "jarvis")
    return mems


def _load_memory() -> list[dict]:
    return _migrate_memories(_load_json(MEMORY_FILE, []))


# One reentrant lock guards every mutate-and-save of the global `memories` list. Writers
# live in three places — the event loop (hub endpoints), tool worker threads (remember/
# recall via asyncio.to_thread), and the sleep-cycle coroutine — so without this they race
# and silently drop each other's writes (last-writer-wins on the whole-file rewrite).
_mem_lock = threading.RLock()


def _save_memory(mems: list[dict]) -> None:
    # Snapshot under the lock so json serialization can't trip over a concurrent append,
    # and so two writers can't interleave partial states onto disk.
    with _mem_lock:
        snapshot = list(mems)
    _save_json(MEMORY_FILE, snapshot)


def _save_history() -> None:
    """No-op: dialogue window persists via DialogueStore (SQLite). Kept for call-site compat."""
    return


def _history_messages() -> list[dict]:
    return _dialogue.messages()


# Back-compat aliases — routers and older code read these; they are refreshed on mutate.
_history: list[dict] = _dialogue.messages()
_turn_seq: int = _dialogue.turn_seq


def _refresh_history_aliases() -> None:
    global _history, _turn_seq
    _history = _dialogue.messages()
    _turn_seq = _dialogue.turn_seq


def _save_tasks() -> None:
    _save_json(TASKS_FILE, task_list)


def _save_settings() -> None:
    _save_json(SETTINGS_FILE, _settings)


def _disk_root() -> str:
    """Root volume for disk-usage probes — follows the project drive, not hardcoded C:."""
    try:
        return str(BASE_DIR.anchor) or (os.environ.get("SystemDrive", "C:") + "\\")
    except Exception:
        return os.environ.get("SystemDrive", "C:") + "\\"


def disk_stats() -> tuple[float, int]:
    """Percent used and free bytes.

    psutil.disk_usage crashes on Windows for a drive anchor like ``C:\\``
    (SystemError: bad format char). shutil talks to the Win32 API directly.
    """
    import shutil
    root = _disk_root() or (os.environ.get("SystemDrive", "C:") + "\\")
    usage = shutil.disk_usage(root)
    pct = (usage.used / usage.total * 100.0) if usage.total else 0.0
    return pct, usage.free


def _user_name() -> str:
    """Operator's name — runtime-settable via onboarding/settings, with a JARVIS_USER
    env override, else empty (the UI prompts for it on first run). Never hardcoded."""
    return (_settings.get("user_name") or os.environ.get("JARVIS_USER") or "").strip()


memories  = _load_memory()
task_list = _load_json(TASKS_FILE, [])
_overheard = _load_json(OVERHEARD_FILE, [])[-OVERHEARD_MAX:]
_refresh_history_aliases()
_settings = _load_json(SETTINGS_FILE, {})
_gov = governor.GovernorState(_load_json(GOVERNOR_FILE, {}))
_persona = persona_mod.Persona.load(PERSONA_FILE)
if _settings.get("voice") in {v["id"] for v in VOICE_OPTIONS}:
    _tts_voice = _settings["voice"]
# Privacy prefs are runtime-settable (Settings panel) and persist across restarts; env is
# only the first-run default. A saved value wins over the env default.
if isinstance(_settings.get("always_listen"), bool):
    ALWAYS_LISTEN = _settings["always_listen"]
if isinstance(_settings.get("store_overheard"), bool):
    STORE_OVERHEARD = _settings["store_overheard"]


def _ollama_chat_options(**extra) -> dict:
    opts = {"keep_alive": OLLAMA_KEEP_ALIVE}
    opts.update(extra)
    return opts


async def _ollama_release(model: str) -> None:
    if model:
        await asyncio.to_thread(models_advisor.unload, model)


def _ict_cache_put(key: tuple, value: dict) -> None:
    now = time.time()
    # Proactively drop TTL-expired entries first, not just at the size cap — a
    # stale-but-unevicted entry otherwise lingers until 32 distinct symbols/
    # intervals have been queried.
    for k in [k for k, (t, _) in _ict_cache.items() if now - t >= _ICT_CACHE_TTL_SEC]:
        _ict_cache.pop(k, None)
    if len(_ict_cache) >= _ICT_CACHE_MAX:
        stale = min(_ict_cache, key=lambda k: _ict_cache[k][0])
        _ict_cache.pop(stale, None)
    _ict_cache[key] = (now, value)


# ── WebSocket broadcast ────────────────────────────────────────────────────────
async def broadcast(data: dict) -> None:
    if _reply_listeners and data.get("type") in ("llm_response", "agent_tool"):
        for q in list(_reply_listeners):
            q.put_nowait(data)
    # Snapshot before iterating: an `await` inside this loop yields control, and a
    # concurrent connect/disconnect mutating the live list mid-iteration could
    # otherwise silently skip a socket (list iteration doesn't raise on resize).
    dead: list[WebSocket] = []
    for ws in list(active_connections):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        try:
            active_connections.remove(ws)
        except ValueError:
            pass


def broadcast_from_thread(data: dict) -> None:
    if _main_loop and not _main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(broadcast(data), _main_loop)


# Task lifecycle manager — owns task state (stable TASK-### ids, real states), operates in
# place on `task_list` so every existing reader/snapshot keeps working, and persists +
# broadcasts on every change so all UIs stay in sync.
def _tasks_changed(items: list[dict]) -> None:
    _save_tasks()
    broadcast_from_thread({"type": "tasks", "tasks": items})


_tasks = TaskManager(task_list, TASK_SEQ_FILE, on_change=_tasks_changed)


async def _emit_content_panel(title: str, body: str) -> None:
    if not body or len(body) < websearch_mod.PANEL_MIN_CHARS:
        return
    await broadcast({
        "type": "content_panel",
        "title": title[:80],
        "body": body[:12000],
        "ts": datetime.now().isoformat(timespec="seconds"),
    })


async def _monitor_loop() -> None:
    """Background hardware watchdog — speaks + UI alert when thresholds breach."""
    while True:
        try:
            await asyncio.sleep(15)
            _maybe_unload_whisper()          # reclaim STT RAM when idle (esp. while disconnected)
            if not active_connections:
                continue
            alert = await asyncio.to_thread(_sys_monitor.check)
            if not alert:
                continue
            await broadcast({
                "type": "system_alert",
                "severity": alert.get("severity", "warn"),
                "metric": alert.get("metric", ""),
                "text": alert.get("detail", ""),
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
            quiet = _tts_playing or (_current_task and not _current_task.done())
            if not quiet and alert.get("speak"):
                asyncio.create_task(_schedule_speak(alert["speak"]))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("monitor loop: %s", exc)


async def _run_startup_briefing(*, force: bool = False) -> None:
    """Two-phase morning briefing — once per calendar day unless forced."""
    global _briefing_running, _settings
    if _briefing_running:
        return
    today = briefing.today_key()
    if not force and _settings.get("last_briefing_date") == today:
        return
    if not active_connections:
        return

    _briefing_running = True
    try:
        await broadcast({"type": "briefing", "phase": "start"})
        # Briefing reads cortex facts (authoritative), not the legacy JSON mirror.
        # Fact rows use `text` (cortex.store); map to briefing's `content` field.
        try:
            _brief_mems = [
                {
                    "category": f.get("category", ""),
                    "content": (f.get("text") or f.get("content") or ""),
                }
                for f in cortex.store.all_facts(include_private=True)
            ]
        except Exception:
            _brief_mems = list(memories)
        greet = briefing.greeting_text(_user_name(), _brief_mems)
        await _emit_content_panel(
            "BRIEFING — status",
            f"{greet}\n\nFetching headlines…",
        )
        await _schedule_speak(greet)

        news = await briefing.fetch_news_phase()
        if news.get("panel_body"):
            await _emit_content_panel(news["panel_title"], news["panel_body"])
        if news.get("speak"):
            await asyncio.sleep(0.5)
            await _schedule_speak(news["speak"])

        _settings["last_briefing_date"] = today
        _save_settings()
        await broadcast({"type": "briefing", "phase": "done"})
    except Exception as exc:
        log.warning("briefing failed: %s", exc)
        await broadcast({"type": "system", "text": f"Briefing unavailable: {exc}"})
    finally:
        _briefing_running = False


def _maybe_run_briefing() -> None:
    asyncio.create_task(_run_startup_briefing())


# ── WebSocket endpoint ─────────────────────────────────────────────────────────
# A browser on ANY website can open a WebSocket to localhost, and our agent can run
# shell commands — so only accept connections whose Origin is a local app (the
# Electron shell or the Vite / packaged SPA). Set JARVIS_WS_ALLOW_ALL=1 to bypass.
# Ports are pinned so a random malicious page on localhost:NNNN cannot drive the agent.
WS_ALLOW_ALL = os.environ.get("JARVIS_WS_ALLOW_ALL", "0") == "1"
_WS_ORIGIN_PORTS = {
    int(p) for p in os.environ.get("JARVIS_WS_PORTS", "8000,8080,5173,4173").split(",")
    if p.strip().isdigit()
}


def _origin_allowed(origin: str | None) -> bool:
    if WS_ALLOW_ALL or not origin:    # no Origin = native client, not a browser
        return True
    try:
        parsed = urllib.parse.urlparse(origin)
        scheme = (parsed.scheme or "").lower()
        # Packaged Electron may load file:// or app:// — match CORS policy.
        if scheme in ("file", "app"):
            return True
        host = (parsed.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return False
        port = parsed.port
        if port is None:
            return True
        return port in _WS_ORIGIN_PORTS
    except Exception:
        return False


def _local_client(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    # "testclient" is Starlette TestClient's synthetic peer — not reachable over real TCP.
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


def _mutating_allowed(request: Request) -> bool:
    """Gate for endpoints that drive the agent, write disk, or change prefs.

    Browser calls must carry a trusted Origin. Origin-less clients (curl, Electron
    main, selftests) must come from loopback — never a LAN peer.
    """
    if WS_ALLOW_ALL:
        return True
    origin = request.headers.get("origin")
    if origin:
        return _origin_allowed(origin)
    return _local_client(request)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    global _tts_playing, _tts_ended_at, _speaking_text, _tts_voice
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008)   # policy violation
        return
    await websocket.accept()
    active_connections.append(websocket)
    # No text: the UI already shows its own greeting on mount, and decks that
    # surface a non-empty "state" text (classic/overhaul) would otherwise
    # duplicate it with server/network language ("uplink established") that
    # has no business being user-facing.
    await websocket.send_json({"type": "state", "status": "connected"})
    # Tell this client the real mic state up front so its UI doesn't guess (a fresh client
    # showing "tap to speak" while the mic is already hot under ALWAYS_LISTEN was the desync).
    await websocket.send_json({"type": "mic", "listening": _listening})
    # Snapshot: the backend is the source of truth, the UI is a view. A client that just
    # mounted (first load OR a deck/preset switch that remounted the socket) gets the live
    # task list and recent conversation replayed so nothing *looks* lost on a UI change —
    # the tasks and the turn that ran while the old view was torn down are both still here.
    try:
        await websocket.send_json({"type": "tasks", "tasks": task_list})
        _recent_turns = _history_messages()[-16:]
        if _recent_turns:
            await websocket.send_json({
                "type": "history",
                "turns": [{"role": ("user" if m.get("role") == "user" else "agent"),
                           "text": m.get("content", "")}
                          for m in _recent_turns if m.get("content")],
            })
    except Exception as _exc:
        log.debug("connect snapshot failed: %s", _exc)
    if _groq_key_alert:
        await websocket.send_json({"type": "system", "text": _groq_key_alert})
    _maybe_run_briefing()
    # Always-on ears: start listening the moment a client is present (no button press).
    # The wake-word gate means it still only responds when addressed as "jarvis".
    if ALWAYS_LISTEN and not _listening:
        asyncio.create_task(_start_voice())
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action", "")
            if action == "command":
                # Typed command — also barges in on anything in flight.
                asyncio.create_task(dispatch_command(data.get("text", ""), source="typed"))
            elif action == "set_debug":
                _settings["debug"] = bool(data.get("on"))
                _save_settings()
                await broadcast({"type": "debug_mode", "on": _settings["debug"]})
            elif action == "start_listening":
                asyncio.create_task(_start_voice())
            elif action == "stop_listening":
                _stop_voice()
            elif action == "stop":
                # Explicit interrupt button.
                asyncio.create_task(_stop_speaking())
            elif action == "tts_start":
                _tts_playing = True
            elif action == "tts_end":
                # Playback finished — clear the echo guard so the mic acts normally.
                _tts_playing = False
                _tts_ended_at = time.time()   # start the acoustic-tail cooldown
                _speaking_text = ""
            elif action == "set_voice":
                vid = data.get("voice", "")
                if vid in {v["id"] for v in VOICE_OPTIONS}:
                    _tts_voice = vid
                    _settings["voice"] = vid
                    _save_settings()
                    await broadcast({"type": "voice_changed", "voice": vid})
                    asyncio.create_task(_schedule_speak("Voice updated. This is how I sound now."))
            elif action == "set_name":
                nm = (data.get("name") or "").strip()[:40]
                if nm:
                    _settings["user_name"] = nm
                    _save_settings()
                    await broadcast({"type": "name_changed", "name": nm})
                    asyncio.create_task(_schedule_speak(f"Noted. I'll call you {nm}."))
            elif action == "set_mode":
                mode = (data.get("mode") or "").strip()
                if mode in governor.MODES:
                    _gov.mode = mode
                    _save_json(GOVERNOR_FILE, _gov.to_dict())
                    await broadcast({"type": "governor_mode", "mode": mode})
            elif action == "pull_model":
                asyncio.create_task(_pull_model((data.get("model") or "").strip()))
            elif action == "benchmark_model":
                asyncio.create_task(_benchmark_model((data.get("model") or "").strip()))
            elif action == "delete_model":
                asyncio.create_task(_delete_model((data.get("model") or "").strip()))
            elif action == "set_local_model":
                asyncio.create_task(_set_local_model((data.get("model") or "").strip()))
            elif action == "trigger_sleep":
                asyncio.create_task(_run_sleep_cycle())
            elif action == "trigger_briefing":
                asyncio.create_task(_run_startup_briefing(force=True))
            elif action == "forget_memory":
                _forget_memory(data.get("id"))
            elif action == "start_watch":
                asyncio.create_task(_start_watch())
            elif action == "stop_watch":
                asyncio.create_task(_stop_watch())
            elif action == "tool_approve":
                # UI response to a tool_approval prompt (shell / privileged tools).
                aid = str(data.get("id") or "")
                pending = _pending_approvals.get(aid)
                if pending:
                    ev, box = pending
                    box[0] = bool(data.get("approved"))
                    ev.set()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("websocket handler error: %s", exc)
    finally:
        try:
            active_connections.remove(websocket)
        except ValueError:
            pass
        # A client that disconnects mid-playback can never send its tts_end. Release the mute
        # here so the mic can't stay wedged (there are no speakers playing once it's gone).
        if not active_connections:
            _tts_playing = False
            _tts_ended_at = time.time()
            # The mic/watch loops are single global resources, not per-connection. Don't stop
            # them the instant the last client drops: switching deck/preset remounts the socket,
            # so there's a sub-second window with zero clients that must NOT kill the hot mic or
            # cancel the running turn (that was the "tasks lost on UI switch" bug). Wait a short
            # grace; only stop if STILL no client has reconnected. A running agent turn
            # (_current_task) is never cancelled here — it finishes and its result is replayed
            # to whichever client is connected by then.
            asyncio.create_task(_cleanup_if_still_idle())


_CLIENT_GRACE_SEC = float(os.environ.get("JARVIS_CLIENT_GRACE_SEC", "3.0"))


async def _cleanup_if_still_idle() -> None:
    """After the last client drops, wait a grace period and stop the mic/watch only if no
    client has reconnected (a deck switch reconnects well within it)."""
    await asyncio.sleep(_CLIENT_GRACE_SEC)
    if active_connections:
        return   # a client (e.g. the new deck) reconnected — keep everything running
    if _listening:
        _stop_voice()
    if _watching:
        await _stop_watch()


# ── Tool definitions (OpenAI / Ollama format) ──────────────────────────────────
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a durable fact about the user. Use category identity for name/city/language, "
                "preferences for likes/dislikes, project for goals, relationships for people, "
                "wishes for future plans, notes for anything else."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content":  {"type": "string", "description": "What to remember"},
                    "category": {
                        "type": "string",
                        "description": (
                            "identity | preferences | project | relationships | wishes | notes | "
                            "personal | preference | fact | security | task"
                        ),
                    },
                    "importance": {"type": "integer", "description": "1 (mundane) to 10 (identity-defining). Default 5."},
                    "namespace": {"type": "string", "description": "Scope, e.g. personal | ctf | work. Default personal."},
                    "private": {"type": "boolean", "description": "If true, this memory stays private to JARVIS and is never surfaced to other models via the memory hub. Use for sensitive/secret facts. Default false."},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search saved memories to recall information about the user. Semantic — matches by meaning, not just keywords.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term or topic"},
                    "k": {"type": "integer", "description": "How many memories to return. Default 6."},
                    "namespace": {"type": "string", "description": "Restrict the search to a namespace, e.g. personal | ctf | work. Optional."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse",
            "description": (
                "Drive a REAL Chrome browser to open sites, read live/JS-rendered content, "
                "click, type, screenshot, and drive web apps (WhatsApp/Slack/Gmail) — for what "
                "a plain web search can't do. Pass an ordered `actions` list of {op,…}:\n"
                "navigate(url) · read(selector?) · read_all(selector) · click(selector) · "
                "type(selector,text) · screenshot · page_info.\n"
                "When you lack a stable CSS selector (usual in modern apps) use find_text, which "
                "matches visible text/aria-label/placeholder case-insensitively: "
                "find_text(action:click,text:'Send') · find_text(action:type,text:'Type a "
                "message',text2:'hey') · find_text(action:read,text:'Roshan'); optional role: "
                "narrows it. Timing: wait_for_text(text,timeout_ms) · wait_ms(ms). Shortcut: "
                "open_app(app: whatsapp/slack/discord/spotify/gmail/youtube/x/github/notion/…).\n"
                "Send a WhatsApp msg: [open_app whatsapp → wait_for_text 'Chats' → find_text "
                "click 'Roshan' → find_text type 'Type a message'/'hey' → find_text click 'Send']."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "description": "Ordered browser actions to perform, each an object with an 'op'.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string",
                                       "enum": ["navigate", "read", "read_all", "click", "type", "screenshot",
                                                "page_info", "open_app", "find_text", "wait_for_text", "wait_ms"]},
                                "url": {"type": "string", "description": "For navigate / open_app: http(s) URL or known app URL."},
                                "selector": {"type": "string", "description": "CSS selector for read/read_all/click/type."},
                                "text": {"type": "string", "description": "For type: text to enter. For find_text/wait_for_text: visible label."},
                                "text2": {"type": "string", "description": "For find_text action=type: the value to type into the matched field."},
                                "action": {"type": "string", "description": "For find_text: click | type | read."},
                                "app": {"type": "string", "description": "For open_app: known app name (whatsapp, gmail, …)."},
                                "ms": {"type": "integer", "description": "For wait_ms: milliseconds to wait."},
                                "timeout_ms": {"type": "integer", "description": "For wait_for_text: max wait in ms."},
                            },
                            "required": ["op"],
                        },
                    },
                },
                "required": ["actions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recon",
            "description": (
                "PASSIVE security reconnaissance on any host or URL — legal on any target "
                "because it only reads public data and the target's own responses (like a "
                "browser): DNS records, cert-transparency subdomains, HTTP headers, and a "
                "one-page tech fingerprint. No port scans, no attacks. Use to map a target's "
                "surface. For active scanning/exploitation use `pentest` (scope-gated)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Host, domain, or URL (e.g. example.com or https://example.com)"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pentest",
            "description": (
                "ACTIVE security testing in an isolated Kali container. REFUSED unless the target "
                "is in authorized scope (labs, CTF/HTB, bug-bounty) — enforced; authorize via the "
                "`scope` tool first.\n"
                "tasks: ports(nmap) · probe(live subdomains + status) · urls(gau/katana harvest) · "
                "candidates(gf vuln-class map: xss/sqli/lfi/ssrf/redirect) · js(endpoints from JS) · "
                "params(arjun) · asn(amass intel) · secrets(leaked keys in JS) · dirs(ffuf) · "
                "nuclei(CVE/misconfig) · takeover(subdomain takeover) · web(nikto) · sqli(sqlmap) · "
                "xss(dalfox, confirms w/ PoC) · scanall(nuclei across all live subs; slow) · full · "
                "report(Markdown assessment from memory — no scope needed).\n"
                "Only ACTIVE tasks need scope; `report`/`recon` don't — call those for any target. "
                "Efficient order, ONE task per call (report findings between): probe → ports → urls "
                "→ candidates → js → nuclei → sqli/params on candidates → report. Actually call it "
                "each step; report exactly what it returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Host/IP/URL/domain to test (must be in authorized scope)"},
                    "task": {"type": "string", "description": "ports | probe | dirs | nuclei | web | sqli | full"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bugbounty",
            "description": (
                "Run the full bug-bounty recon sweep on a domain in one call — recon → probe (live "
                "subdomains) → urls (harvest+crawl) → candidates (vuln-class map) — in the efficient "
                "order, emitting EACH phase live to the ops console and storing every finding in "
                "memory. Active phases need the domain in scope (they refuse otherwise). Use for "
                "'recon/sweep/enumerate <domain>'. Follow with `pentest <d> nuclei`/`scanall` and "
                "`report <d>`."
            ),
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "domain to sweep"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report",
            "description": (
                "Write a Markdown security assessment for a target from everything JARVIS has "
                "REMEMBERED about it (past recon/scan findings in memory) — findings + derived next "
                "steps. Reads memory only: NO authorization or scope needed. Call it for ANY target "
                "the user asks to report on; never refuse it for scope reasons."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "host, domain, or URL to report on"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scope",
            "description": (
                "Manage the authorized-target allowlist that gates the `pentest` tool. "
                "action: list (default) | add | remove. When adding, `target` is a host, "
                "domain, or CIDR, and `source` marks why it's authorized (owned | lab | ctf | "
                "bugbounty). Only add targets the user is genuinely allowed to attack."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "list | add | remove"},
                    "target": {"type": "string", "description": "host, domain, or CIDR (for add/remove)"},
                    "source": {"type": "string", "description": "owned | lab | ctf | bugbounty"},
                    "program": {"type": "string", "description": "bug-bounty program name, if source=bugbounty"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web for current information. Modes: search (default), news, "
                "research (deep), price (product cost), compare (side-by-side — pass items array)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query":  {"type": "string", "description": "Search query or topic"},
                    "mode":   {"type": "string", "description": "search | news | research | price | compare"},
                    "items":  {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Items to compare (compare mode)",
                    },
                    "aspect": {"type": "string", "description": "Comparison aspect: price | specs | reviews"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Get current system stats: CPU usage, RAM, disk, and top processes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_app",
            "description": "Launch an application on the user's Windows PC.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "App name or executable, e.g. chrome, code, spotify, notepad, terminal, discord",
                    },
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": (
                "Close / quit an application on the user's Windows PC by terminating its "
                "process, then verify it actually closed. Use for 'close chrome', 'quit "
                "spotify', 'kill notepad'. Reports honestly whether it closed, wasn't running, "
                "or couldn't be closed. For closing a specific WINDOW by its title (not the "
                "whole app) use desktop window_close instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "App name, e.g. chrome, spotify, notepad, code, discord, calc",
                    },
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop",
            "description": (
                "Control the Windows desktop — use instead of `launch_app` for anything "
                "system-shaped. Pass one `action`; args noted:\n"
                "open_path(path) · open_settings(page: apps/display/network/sound/wifi/"
                "bluetooth/notifications/updates/storage/…) · open_control_panel(applet) · "
                "open_registry(key?) · open_component(component: task_manager/device_manager/"
                "services/event_viewer/cmd/powershell/notepad/calc/…) · list_apps(filter?) · "
                "uninstall_app(app, confirm) — confirm=false is a dry-run; needs confirm=true "
                "to run · system_volume(direction up/down/mute/set, level) · brightness(direction "
                "up/down/set, level) · toggle_wifi(state on/off) · mouse_click(x,y,button,clicks) · "
                "mouse_move(x,y,duration) · mouse_scroll(clicks ±20) · type_text(text, confirm "
                "— confirm=true if >60 chars) · key_press(keys e.g. 'enter'/'ctrl+c'; media "
                "keys playpause/nexttrack/volumeup control any player) · notify(title,message,"
                "timeout) · capture_webcam(path?) · window_focus/minimize/maximize/restore/"
                "close(title) · window_list · remind(sub_action schedule/list/cancel, when, "
                "message, title — when: ISO / 'in 5 minutes' / 'tomorrow 9am' / 'HH:MM', "
                "OS-native so it fires even if JARVIS is closed).\n"
                "Destructive actions (uninstall_app, long type_text) MUST use the confirm gate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action":    {"type": "string",
                                  "enum": ["open_path", "open_settings", "open_control_panel",
                                           "open_registry", "open_component", "list_apps",
                                           "uninstall_app", "system_volume", "brightness",
                                           "toggle_wifi", "mouse_click", "mouse_move",
                                           "mouse_scroll", "type_text", "key_press",
                                           "notify", "capture_webcam",
                                           "window_focus", "window_minimize",
                                           "window_maximize", "window_restore",
                                           "window_close", "window_list", "remind"]},
                    "path":      {"type": "string"},
                    "page":      {"type": "string"},
                    "applet":    {"type": "string"},
                    "key":       {"type": "string"},
                    "component": {"type": "string"},
                    "app":       {"type": "string"},
                    "filter":    {"type": "string"},
                    "confirm":   {"type": "boolean"},
                    "level":     {"type": "integer"},
                    "direction": {"type": "string",
                                  "description": "For system_volume / brightness: up | down | mute | set. With 'set', also pass level (0-100)."},
                    "state":     {"type": "string"},
                    "adapter":   {"type": "string"},
                    "x":         {"type": "integer"},
                    "y":         {"type": "integer"},
                    "button":    {"type": "string"},
                    "clicks":    {"type": "integer"},
                    "duration":  {"type": "number"},
                    "text":      {"type": "string"},
                    "keys":      {"type": "string"},
                    "title":     {"type": "string"},
                    "message":   {"type": "string"},
                    "timeout":   {"type": "integer"},
                    # p2 additions:
                    "when":      {"type": "string",
                                  "description": "For remind: ISO datetime OR 'in 5 minutes' / 'tomorrow 9am' / 'today 15:00' / 'HH:MM'."},
                    "sub_action":{"type": "string",
                                  "description": "For remind: schedule | list | cancel."},
                    "id":        {"type": "string",
                                  "description": "For remind cancel: the reminder id returned by schedule."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agents",
            "description": (
                "Spawn up to 5 focused sub-agents that run IN PARALLEL, each with its "
                "own tool loop (read-only tools only), and get back a combined result "
                "block. Use when the same question has independent sub-parts that can "
                "be answered separately — e.g. 'compare the top 3 laptops', "
                "'research pros vs cons vs pricing', 'summarize what these five links "
                "say'. Sub-agents CANNOT write memory, run shell, drive the desktop, "
                "or spawn nested sub-agents. Each sub-agent is bounded (6 tool "
                "iterations); the whole call is bounded (90s wall-clock). Don't use "
                "spawn_agents for a single question — call the right tool directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "description": "List of sub-agent specs (max 5).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":   {"type": "string",
                                           "description": "Short label for this agent's result (e.g. 'pricing', 'cons')."},
                                "prompt": {"type": "string",
                                           "description": "The focused question this sub-agent should answer."},
                            },
                            "required": ["prompt"],
                        },
                    },
                },
                "required": ["agents"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Add a task or reminder to the JARVIS task queue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Task description"},
                    "eta":  {"type": "string", "description": "Optional ETA or deadline"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a task in the queue as completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id to complete, e.g. TASK-003 (a bare number also works)"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "Cancel a queued or running task (it won't be done).",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id to cancel, e.g. TASK-003 (a bare number also works)"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screen",
            "description": (
                "Take a screenshot and describe what is currently on the screen. "
                "When you call this tool, ALSO speak one short natural line first "
                "('let me look' / 'checking your screen' / 'one sec, taking a look') "
                "so there's no awkward silence while the capture runs. The vision "
                "analysis is your next response after the tool returns."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return its output. Use for file ops, git, scripts, system tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "cwd":     {"type": "string", "description": "Working directory (optional)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ict_scan",
            "description": (
                "Scan an Indian-market instrument (Nifty 50, Sensex, Bank Nifty, or any "
                "NSE stock) for ICT / Smart-Money price-action setups: market structure "
                "(BOS/CHoCH), fair value gaps, order blocks, and liquidity levels. "
                "Returns analysis and a directional bias. Analysis only — never places trades."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":   {"type": "string", "description": "nifty, sensex, banknifty, or an NSE ticker like RELIANCE / TCS"},
                    "interval": {"type": "string", "description": "Candle size: 5m, 15m, 30m, 60m, 1d (default 15m)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_trading",
            "description": (
                "Open the full c0mr4des trading terminal in its own window — the dedicated "
                "trading workspace with live charts, options pricing, backtesting, and broker "
                "tools. Use when the user wants to trade or open the trading terminal/dashboard."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Look at an image file on disk and describe/answer about it. Use when the user points at a picture, screenshot, photo, or diagram file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":     {"type": "string", "description": "Absolute path to the image (.png/.jpg/.jpeg/.webp/.gif)"},
                    "question": {"type": "string", "description": "Optional specific question about the image"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "watch_video",
            "description": "Watch a video (local file path OR a URL like YouTube) and describe/answer about it. Samples frames and transcribes the audio, then reasons over both.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source":   {"type": "string", "description": "Local video path or a video URL"},
                    "question": {"type": "string", "description": "Optional specific question about the video"},
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression exactly (arithmetic, powers, %, parentheses). Use this for any calculation instead of doing mental math.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "e.g. '1850*0.07' or '(3+4)**2/5'"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Current weather and conditions for the user's location (auto-detected) or a named city. Use for weather, temperature, rain, or what-to-wear questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "Optional city name; omit to use the user's current location."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "Load the full step-by-step instructions for one of your named skills (see the [SKILLS] list in your context). Call this FIRST whenever a request matches a skill, then follow the returned instructions. Don't guess a skill's steps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "The skill name/slug from the [SKILLS] list (e.g. 'market-brief')."},
                },
                "required": ["skill"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Save a reusable procedure as a new skill (procedural memory) so it can be loaded later with use_skill. Use this after you work out a repeatable, non-trivial way to do something the user is likely to want again.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short human name, e.g. 'Weekly Portfolio Review'."},
                    "description": {"type": "string", "description": "One line: when to use this skill."},
                    "instructions": {"type": "string", "description": "The full step-by-step procedure, in markdown."},
                },
                "required": ["name", "description", "instructions"],
            },
        },
    },
]

# Name → schema lookup for the executor
_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
_TOOL_REQUIRED: dict[str, list[str]] = {
    t["function"]["name"]: list(t["function"]["parameters"].get("required") or [])
    for t in TOOLS
}

# Shell safety lives in jarvis.policy.shell — see _command_allowed().

# Each desktop sub-action reads ONE primary arg under this name. Used to repair calls where
# the model passed {"<sub-action>": value} instead of {"action":"<sub-action>", "<arg>":value}.
_DESKTOP_PRIMARY_ARG = {
    "open_path": "path", "open_settings": "page", "open_control_panel": "applet",
    "open_registry": "key", "open_component": "component", "list_apps": "filter",
    "uninstall_app": "app", "toggle_wifi": "state", "type_text": "text",
    "key_press": "keys", "notify": "message", "window_focus": "title",
    "window_minimize": "title", "window_maximize": "title", "window_restore": "title",
    "window_close": "title",
}


def _normalize_desktop_args(args: dict) -> dict:
    """Repair the `desktop` arg shape gpt-oss frequently mangles: a sub-action passed as a KEY
    ({"open_path": "C:\\…"}) instead of {"action":"open_path","path":"C:\\…"}. If `action` is
    already set, returns args untouched."""
    if not isinstance(args, dict) or (args.get("action") or "").strip():
        return args
    for k in list(args.keys()):
        if k in _DESKTOP_PRIMARY_ARG:
            v = args[k]
            fixed = {kk: vv for kk, vv in args.items() if kk != k}
            fixed["action"] = k
            if isinstance(v, dict):          # value is itself the arg bundle
                fixed.update(v)
            else:                            # scalar → put under the action's primary arg
                fixed.setdefault(_DESKTOP_PRIMARY_ARG[k], v)
            return fixed
    return args


_LAUNCH_ALLOWLIST = {
    "chrome": "chrome.exe", "firefox": "firefox.exe", "edge": "msedge.exe",
    "code": "code", "vscode": "code", "spotify": "spotify.exe",
    "discord": "discord.exe", "notepad": "notepad.exe",
    "terminal": "wt.exe", "powershell": "powershell.exe",
    "explorer": "explorer.exe", "calc": "calc.exe",
    "calculator": "calc.exe", "paint": "mspaint.exe",
    "obs": "obs64.exe", "steam": "steam.exe",
}

# Process NAME to look for / terminate when closing an app. Mostly the launch exe, but a few
# differ: `code` launches a shim but the process is "Code.exe"; `calc` is a Store app whose
# real process is "CalculatorApp.exe"; `terminal` (wt.exe) hosts "WindowsTerminal.exe".
_CLOSE_PROCESS = {
    "chrome": "chrome.exe", "firefox": "firefox.exe", "edge": "msedge.exe",
    "code": "Code.exe", "vscode": "Code.exe", "spotify": "Spotify.exe",
    "discord": "Discord.exe", "notepad": "notepad.exe",
    "terminal": "WindowsTerminal.exe", "powershell": "powershell.exe",
    "calc": "CalculatorApp.exe", "calculator": "CalculatorApp.exe",
    "paint": "mspaint.exe", "obs": "obs64.exe", "steam": "steam.exe",
    "explorer": "explorer.exe",
}


def _resolve_launch_target(cmd: str) -> str | None:
    """Resolve an allowlisted exe to a launchable full path. A bare `Popen("spotify.exe")` only
    searches PATH, which FAILS for apps that install to per-user dirs (Spotify, Discord). We
    consult, in order: PATH, the Windows 'App Paths' registry (where most apps register their
    exe), and a few known per-user locations. Returns a path/command to launch, or None if the
    app can't be found anywhere (so the caller reports honestly instead of a silent no-op)."""
    import shutil
    if shutil.which(cmd):
        return cmd
    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{cmd}") as k:
                        val, _ = winreg.QueryValueEx(k, None)
                        if val and os.path.exists(val.strip('"')):
                            return val.strip('"')
                except FileNotFoundError:
                    continue
        except Exception:
            pass
        appdata = os.environ.get("APPDATA", "")
        local = os.environ.get("LOCALAPPDATA", "")
        known: dict[str, list[str]] = {
            "spotify.exe": [os.path.join(appdata, "Spotify", "Spotify.exe")],
            "discord.exe": sorted(glob.glob(os.path.join(local, "Discord", "app-*", "Discord.exe")), reverse=True),
            "code": [os.path.join(local, "Programs", "Microsoft VS Code", "Code.exe")],
            "steam.exe": [r"C:\Program Files (x86)\Steam\steam.exe"],
        }
        for p in known.get(cmd, []):
            if p and os.path.exists(p):
                return p
        # Microsoft Store apps (e.g. Spotify from the Store) expose an execution-alias stub here;
        # running the stub launches the packaged app. This one path covers most Store installs.
        alias = os.path.join(local, "Microsoft", "WindowsApps", cmd)
        if os.path.exists(alias):
            return alias
    return None


def _launch_resolved(raw: str, cmd: str) -> str:
    """Launch an allowlisted app, resolving per-user install paths first, then VERIFY it is
    actually running before claiming success. Popen not raising only means a process was
    spawned — not that it survived (a bad path, crash-on-start, or an installer stub that
    exits immediately all 'succeed' at Popen). We poll for the real process and report
    honestly: 'Opened X.' only when observed running; a clear hedge when we can't confirm;
    a plain failure otherwise. Always an argv list with shell=False — `cmd` only ever comes
    from _LAUNCH_ALLOWLIST."""
    target = _resolve_launch_target(cmd)
    proc_name = _CLOSE_PROCESS.get(raw) or os.path.basename(target or cmd)
    # A console app (PowerShell) needs its OWN visible console; GUI apps need no flag. Never
    # CREATE_NO_WINDOW here — it would launch an invisible terminal.
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if cmd.lower() == "powershell.exe" else 0
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 shell=False, creationflags=flags)
    # Already running? Don't spawn a duplicate — report the truth instead. (explorer is the
    # exception: it's always "running" as the shell, so a fresh window is the intent.)
    if raw != "explorer" and verify.is_running(proc_name) is True:
        return f"{raw} is already open."
    try:
        if target:
            subprocess.Popen([target], **quiet)
        elif os.name == "nt":
            # Last resort: `start` uses ShellExecute, which is App-Paths-aware where a bare exec isn't.
            rc = subprocess.run(["cmd", "/c", "start", "", cmd], capture_output=True, text=True)
            if rc.returncode != 0:
                return f"Couldn't find {raw} on this machine — it may not be installed."
        else:
            subprocess.Popen([cmd], **quiet)
    except FileNotFoundError:
        return f"Couldn't find {raw} on this machine — it may not be installed."
    except Exception as exc:
        return f"Failed to launch {raw}: {exc}"

    res = verify.wait_until_running(proc_name, timeout=3.0)
    if res.verified is True:
        return f"Opened {raw}."
    if res.verified is None:
        return f"Launched {raw} (couldn't confirm it's running)."
    # Spawned but the process never appeared — don't lie. Some apps re-parent into an existing
    # broker process (so verification by name legitimately misses); say what we actually know.
    return (f"I started {raw} but couldn't confirm a window opened — it may have failed to "
            f"launch or merged into a running instance.")


def _close_app(raw: str) -> str:
    """Close an app by terminating its process(es), then VERIFY it's gone. Honest by
    construction: it reports 'Closed X.' only after the process is observed gone, 'wasn't
    running' when there was nothing to close, and the real reason on failure."""
    proc_name = _CLOSE_PROCESS.get(raw)
    if not proc_name:
        return ""   # unknown app → caller falls through to the full agent
    res = verify.terminate(proc_name, timeout=5.0)
    if res.verified is True and "was not running" in res.detail:
        return f"{raw} wasn't running."
    if res.ok and res.verified in (True, None):
        return f"Closed {raw}."
    return f"I couldn't close {raw} — {res.detail}."


def _match_running_process(token: str) -> str | None:
    """Best-effort: a running process whose name contains `token` (for closing an app that
    isn't in the close map). Returns the process name, or None. Skips a short list of
    system-critical processes so a careless 'close explorer' or 'close system' can't be used
    to take down the shell by accident through the fuzzy path."""
    token = re.sub(r"[^a-z0-9]", "", (token or "").lower())
    if len(token) < 3:
        return None
    try:
        import psutil
    except Exception:
        return None
    protected = {"system", "svchost", "csrss", "wininit", "winlogon", "services",
                 "lsass", "smss", "dwm", "registry"}
    best = None
    for p in psutil.process_iter(["name"]):
        try:
            nm = (p.info.get("name") or "")
        except Exception:
            continue
        stem = re.sub(r"[^a-z0-9]", "", nm.lower().replace(".exe", ""))
        if not stem or stem in protected:
            continue
        if token in stem:
            # Prefer an exact stem match; otherwise keep the first containing one.
            if stem == token:
                return nm
            best = best or nm
    return best


_KNOWN_SITES = {
    "gmail", "google", "youtube", "maps", "whatsapp", "twitter", "x", "reddit", "amazon",
    "netflix", "github", "chatgpt", "notion", "slack", "instagram", "facebook", "linkedin",
    "outlook", "yahoo", "bing", "wikipedia", "stackoverflow", "twitch", "spotify",
}


def _looks_like_site(token: str) -> bool:
    """True if an 'open X' target is better handled as a WEBSITE (agent `browse`) than as a
    local app — a domain-looking token, or a well-known site name. Keeps 'open gmail' /
    'open nytimes.com' working while still answering honestly for a made-up app name."""
    t = (token or "").strip().lower()
    if "." in t or "/" in t:
        return True
    return t in _KNOWN_SITES


def _resolve_any_app(name: str) -> tuple[str, str] | None:
    """For the fast path's 'open <anything>': try to resolve an arbitrary (non-allowlisted)
    app the user named to a launchable exe via PATH / App Paths. Returns (display_name, exe)
    or None when nothing is found — the caller then falls through to the full agent rather
    than guessing. Only resolves real executables, so it can't be coerced into running junk."""
    nm = (name or "").strip().lower()
    if not nm or not re.fullmatch(r"[a-z0-9 ._+-]{2,40}", nm):
        return None
    cand = nm if nm.endswith(".exe") else nm + ".exe"
    cand = cand.replace(" ", "")
    if _resolve_launch_target(cand):
        return nm, cand
    return None


def _next_id(items: list[dict]) -> int:
    return max((int(i.get("id", 0)) for i in items), default=0) + 1


def _validate_tool_args(name: str, args: dict) -> str | None:
    for key in _TOOL_REQUIRED.get(name, []):
        val = args.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            return f"{name} requires '{key}'."
    return None


def _command_allowed(cmd: str) -> str | None:
    return _policy_check_command(cmd)

# Anthropic tool format (input_schema instead of parameters)
CLAUDE_TOOLS = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOLS
]


# ── Browser automation helpers ───────────────────────────────────────────────────
def _cdp_up() -> bool:
    """True if a Chrome CDP endpoint is answering on the debug port (via localhost)."""
    try:
        with urllib.request.urlopen(f"http://localhost:{BH_PORT}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# Browse SSRF policy lives in jarvis.policy.browse.


def _browse_allowed(url: str) -> str | None:
    """Gate a navigation URL via the kernel browse policy."""
    return _policy_check_browse_url(url, allowlist=BH_ALLOWLIST)


# The dedicated-profile Chrome WE launch (never a foreign one). Tracked so we can kill it.
_bh_chrome_proc = None   # subprocess.Popen | None


# Common web-app shortcuts — `{"op": "open_app", "app": "whatsapp"}` resolves to a full URL
# without the model having to guess it. Add entries here as new apps come up.
_WEB_APPS: dict[str, str] = {
    "whatsapp":  "https://web.whatsapp.com",
    "slack":     "https://app.slack.com/client",
    "discord":   "https://discord.com/app",
    "spotify":   "https://open.spotify.com",
    "gmail":     "https://mail.google.com",
    "calendar":  "https://calendar.google.com",
    "drive":     "https://drive.google.com",
    "docs":      "https://docs.google.com",
    "sheets":    "https://sheets.google.com",
    "youtube":   "https://www.youtube.com",
    "twitter":   "https://x.com",
    "x":         "https://x.com",
    "reddit":    "https://www.reddit.com",
    "github":    "https://github.com",
    "notion":    "https://www.notion.so",
    "linear":    "https://linear.app",
    "figma":     "https://www.figma.com",
    "chatgpt":   "https://chatgpt.com",
    "claude":    "https://claude.ai",
    "instagram": "https://www.instagram.com",
    "linkedin":  "https://www.linkedin.com",
    "netflix":   "https://www.netflix.com",
    "amazon":    "https://www.amazon.com",
    "twitch":    "https://www.twitch.tv",
    "maps":      "https://www.google.com/maps",
    "google":    "https://www.google.com",
    "bing":      "https://www.bing.com",
    "duckduckgo": "https://duckduckgo.com",
    "google maps": "https://www.google.com/maps",
}


# The `find_text` op compiles into this JS blob. It's authored by us (never by the model),
# so the only model-supplied values are the string literals that flow through js_lit() —
# they can't escape their quoted context. Handles the three common typable-element flavours:
# a plain <input>/<textarea> (React-safe native setter), a contenteditable (execCommand),
# and everything else (returns a friendly 'not typable'). Visible-text match is case-insensitive,
# prefers exact match, falls back to substring.
_FIND_TEXT_JS = r"""
(function(){
  var target = __TARGET__;
  var action = __ACTION__;
  var typeText = __TYPE_TEXT__;
  var role = __ROLE__;
  function norm(s){ return (s==null?'':String(s)).trim().toLowerCase(); }
  var want = norm(target);
  var roles = role ? [role] : ['button','a','[role="button"]','[role="link"]',
    '[role="menuitem"]','[role="tab"]','[role="option"]','input','textarea',
    '[contenteditable="true"]','[contenteditable=""]','li','div','span'];
  function visible(el){
    var r = el.getBoundingClientRect();
    if (r.width===0 && r.height===0) return false;
    var s = window.getComputedStyle(el);
    return s && s.visibility!=='hidden' && s.display!=='none';
  }
  function labelOf(el){
    return norm(el.innerText || el.value || el.getAttribute('aria-label') ||
                el.getAttribute('placeholder') || el.getAttribute('title'));
  }
  var el = null;
  outer: for (var pass=0; pass<2 && !el; pass++){
    for (var i=0; i<roles.length; i++){
      var els = document.querySelectorAll(roles[i]);
      for (var j=0; j<els.length; j++){
        if (!visible(els[j])) continue;
        var t = labelOf(els[j]);
        if (!t) continue;
        if ((pass===0 && t===want) || (pass===1 && t.indexOf(want)>=0)) {
          el = els[j]; break outer;
        }
      }
    }
  }
  if (!el) return 'no element matched: ' + target;
  try { el.scrollIntoView({block:'center'}); } catch(e){}
  if (action==='click') { el.click(); return 'clicked: ' + target; }
  if (action==='read')  { return (el.innerText||'').slice(0,600); }
  if (action==='type'){
    if (el.tagName==='INPUT' || el.tagName==='TEXTAREA'){
      var proto = el.tagName==='INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
      var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      el.focus(); setter.call(el, typeText);
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      return 'typed: ' + typeText;
    }
    if (el.isContentEditable){
      el.focus();
      try { document.execCommand('insertText', false, typeText); return 'typed (ce): ' + typeText; }
      catch(e){ el.textContent = typeText;
        el.dispatchEvent(new InputEvent('input',{bubbles:true,data:typeText,inputType:'insertText'}));
        return 'typed (ce-fallback): ' + typeText; }
    }
    return 'element not typable: ' + target;
  }
  return 'unknown action: ' + action;
})()
""".strip()


# `wait_for_text` polls the document for a visible text to appear (or disappear, if invert).
_WAIT_FOR_TEXT_JS = r"""
(function(){
  var want = (__TARGET__||'').toLowerCase();
  var text = (document.body && document.body.innerText || '').toLowerCase();
  return text.indexOf(want) >= 0 ? 'found' : 'missing';
})()
""".strip()


def _bh_build_script(actions: list[dict]) -> tuple[str | None, str | None]:
    """Compile a list of STRUCTURED actions into a browser-harness Python snippet. Every
    model-supplied value is JSON-encoded at both the Python and the JavaScript quoting levels,
    so nothing the model provides can escape its string literal into executable code — this is
    what removes the arbitrary-Python / API-key-exfil RCE of the old free-`code` design.
    Returns (script, error)."""
    if not isinstance(actions, list) or not actions:
        return None, "browse needs a non-empty `actions` list (see the tool description)."
    if len(actions) > 20:
        return None, "Too many actions (20 max per browse call)."

    def js_lit(s) -> str:                 # a safe JS string literal from any model value
        return json.dumps("" if s is None else str(s))

    def py_print_js(js_source: str) -> str:   # `print(js(<js_source as a python str literal>))`
        return f"print(js({json.dumps(js_source)}))"

    lines = ["# auto-generated by JARVIS from structured actions - NOT model-authored code",
             "import time"]
    opened = False
    for i, step in enumerate(actions):
        if not isinstance(step, dict):
            return None, f"action {i} must be an object with an 'op'."
        op = str(step.get("op", "")).strip().lower()
        sel = step.get("selector")
        if op == "navigate":
            err = _browse_allowed(step.get("url", ""))
            if err:
                return None, err
            url = str(step.get("url"))
            lines.append(f"{'new_tab' if not opened else 'goto_url'}({json.dumps(url)})")
            lines.append("wait_for_load()")
            opened = True
        elif op == "read":
            expr = (f"(document.querySelector({js_lit(sel)})||document.body).innerText"
                    if sel else "document.body.innerText")
            lines.append(f'print("[read #{i}]")')
            lines.append(py_print_js(expr))
        elif op == "read_all":
            if not sel:
                return None, f"action {i} (read_all) needs a 'selector'."
            expr = (f"JSON.stringify([...document.querySelectorAll({js_lit(sel)})]"
                    f".map(function(e){{return e.innerText}}))")
            lines.append(f'print("[read_all #{i}]")')
            lines.append(py_print_js(expr))
        elif op == "click":
            if not sel:
                return None, f"action {i} (click) needs a 'selector'."
            expr = (f"(function(){{var e=document.querySelector({js_lit(sel)});"
                    f"if(e){{e.click();return 'clicked';}}return 'no element matched';}})()")
            lines.append(f'print("[click #{i}]")')
            lines.append(py_print_js(expr))
        elif op == "type":
            if not sel:
                return None, f"action {i} (type) needs a 'selector'."
            expr = (f"(function(){{var e=document.querySelector({js_lit(sel)});"
                    f"if(!e)return 'no element matched';e.focus();e.value={js_lit(step.get('text',''))};"
                    f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
                    f"e.dispatchEvent(new Event('change',{{bubbles:true}}));return 'typed';}})()")
            lines.append(f'print("[type #{i}]")')
            lines.append(py_print_js(expr))
        elif op == "screenshot":
            lines.append("capture_screenshot()")
            lines.append(f'print("[screenshot #{i} captured]")')
        elif op == "page_info":
            lines.append(f'print("[page_info #{i}]")')
            lines.append("print(page_info())")
        elif op == "open_app":
            # Open a web app OR any site. Priority: an explicit url, then a known-app
            # shortcut, then treat the given name as a site to visit — a full URL is used
            # as-is, a domain becomes https://<domain>, and anything else becomes a web
            # search. So "open amrita.com", "open youtube", and "open cat videos" all work
            # instead of erroring on an unknown app name.
            app = str(step.get("app", "")).strip()
            raw_url = str(step.get("url", "")).strip()
            url = raw_url or _WEB_APPS.get(app.lower())
            if not url and app:
                if re.match(r"^https?://", app, re.I):
                    url = app
                elif re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", app):        # looks like a domain
                    url = "https://" + app
                else:                                                    # a bare term → search it
                    url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(app)
            if not url:
                return None, f"action {i} (open_app): give an 'app', a domain, or a full 'url'."
            err = _browse_allowed(url)
            if err:
                return None, err
            lines.append(f"{'new_tab' if not opened else 'goto_url'}({json.dumps(url)})")
            lines.append("wait_for_load()")
            # Header is JSON-encoded so a hostile URL (query params with quotes, etc.)
            # can't break the compiled script's Python parsing or leak into a code path.
            lines.append(f"print({json.dumps(f'[open_app #{i}] {app or url}')})")
            opened = True
        elif op == "find_text":
            # Act on the first visible element whose visible text/aria-label/placeholder
            # matches. The compiler owns the JS — the only model-supplied values are
            # string literals routed through js_lit() (safe inside their quoted context).
            text = step.get("text")
            if not text:
                return None, f"action {i} (find_text) needs a 'text'."
            act = str(step.get("action", "click")).strip().lower()
            if act not in {"click", "type", "read"}:
                return None, (f"action {i} (find_text): action must be one of "
                              f"click|type|read, got {act!r}.")
            if act == "type" and step.get("text2") is None and step.get("value") is None:
                return None, f"action {i} (find_text, action=type) needs a 'text2' or 'value'."
            js_source = (_FIND_TEXT_JS
                         .replace("__TARGET__", js_lit(text))
                         .replace("__ACTION__", js_lit(act))
                         .replace("__TYPE_TEXT__", js_lit(step.get("text2") or step.get("value")))
                         .replace("__ROLE__", js_lit(step.get("role"))))
            # Header JSON-encoded — text may contain quotes/newlines that would otherwise
            # break the compiled script's Python parsing. Preview truncated to keep logs small.
            _preview = str(text)[:80]
            lines.append(f"print({json.dumps(f'[find_text #{i}] {act}: {_preview}')})")
            lines.append(py_print_js(js_source))
        elif op == "wait_for_text":
            text = step.get("text")
            if not text:
                return None, f"action {i} (wait_for_text) needs a 'text'."
            try:
                timeout_ms = max(100, min(20000, int(step.get("timeout_ms", 5000))))
            except (TypeError, ValueError):
                return None, f"action {i} (wait_for_text): timeout_ms must be an integer."
            poll_js = _WAIT_FOR_TEXT_JS.replace("__TARGET__", js_lit(text))
            polls = max(1, timeout_ms // 250)
            _preview = str(text)[:80]
            lines.append(f"print({json.dumps(f'[wait_for_text #{i}] up to {timeout_ms}ms: {_preview}')})")
            lines.append("_hit = 'missing'")
            lines.append(f"for _ in range({polls}):")
            lines.append(f"    _hit = js({json.dumps(poll_js)})")
            lines.append("    if _hit == 'found': break")
            lines.append("    time.sleep(0.25)")
            lines.append('print("[wait_for_text result]", _hit)')
        elif op == "wait_ms":
            try:
                ms = max(0, min(15000, int(step.get("ms", 500))))
            except (TypeError, ValueError):
                return None, f"action {i} (wait_ms): ms must be an integer."
            lines.append(f'print("[wait_ms #{i}] {ms}ms")')
            lines.append(f"time.sleep({ms / 1000.0})")
        else:
            return None, f"action {i}: unknown op {op!r}."
    return "\n".join(lines), None


def _ensure_debug_chrome() -> str | None:
    """Ensure a debug Chrome WE control is reachable. Reuses the dedicated-profile instance we
    launched; refuses to silently drive a foreign Chrome already on the port unless the user
    explicitly opts in (JARVIS_BH_ATTACH=1). Returns an error string, or None on success."""
    global _bh_chrome_proc
    if _bh_chrome_proc is not None and _bh_chrome_proc.poll() is None and _cdp_up():
        return None                              # our own instance is up
    if _cdp_up():
        if BH_ATTACH:
            return None                          # user opted in to drive whatever's there
        return ("A Chrome is already using the debug port. Attaching to it is disabled by "
                "default (it may be your personal, logged-in browser). Close it so JARVIS can "
                "launch its own isolated Chrome, or set JARVIS_BH_ATTACH=1 to allow attaching.")
    if not os.path.exists(BH_CHROME):
        return f"Chrome not found at {BH_CHROME}. Set JARVIS_CHROME to chrome.exe."
    args = [BH_CHROME, f"--remote-debugging-port={BH_PORT}",
            f"--user-data-dir={BH_PROFILE}", "--no-first-run", "--no-default-browser-check"]
    if BH_HEADLESS:
        args += ["--headless=new", "--disable-gpu"]
    else:
        args += ["--start-maximized"]          # visible + full-size so the user can watch it work
    try:
        _bh_chrome_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return f"could not launch Chrome: {exc}"
    for _ in range(24):                          # ~12s for CDP to come up
        time.sleep(0.5)
        if _cdp_up():
            return None
    return "Chrome launched but its debug port never responded."


def _shutdown_browser() -> None:
    """Terminate the dedicated-profile Chrome we launched, so it isn't orphaned (leaving a
    standing, unauthenticated debug port) after JARVIS exits."""
    global _bh_chrome_proc
    proc = _bh_chrome_proc
    _bh_chrome_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


atexit.register(_shutdown_browser)


def _foreground_chrome() -> None:
    """Best-effort: raise our debug Chrome to the foreground (Windows) so the user actually
    sees it act, instead of it working behind the JARVIS window. Matches any window owned by
    Chrome's process tree (the visible window often belongs to a child process). Silent no-op
    on failure, when headless, or off-Windows."""
    if BH_HEADLESS or os.name != "nt" or _bh_chrome_proc is None:
        return
    try:
        import ctypes
        from ctypes import wintypes

        pids = {_bh_chrome_proc.pid}
        try:
            pids |= {c.pid for c in psutil.Process(_bh_chrome_proc.pid).children(recursive=True)}
        except Exception:
            pass

        user32 = ctypes.windll.user32
        targets: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                wpid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                if wpid.value in pids:
                    targets.append(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
        for hwnd in targets:
            user32.ShowWindow(hwnd, 9)         # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _browser_run(script: str) -> str:
    """Run a JARVIS-generated browser-harness snippet against the debug Chrome and return its
    output. browser-harness lives in its own venv (isolated deps); we shell out to its CLI.
    The subprocess env is MINIMAL — the decrypted API keys in os.environ are never passed in."""
    if not os.path.exists(BH_CLI):
        venv = BASE_DIR.parent / "bh-venv"
        return ("Browser support isn't installed. Set up the isolated env once:\n"
                f"  python -m venv {venv}\n"
                f"  {venv / 'Scripts' / 'pip'} install browser-harness")
    err = _ensure_debug_chrome()
    if err:
        return f"Browser unavailable — {err}"
    _foreground_chrome()          # bring the window to the front so the user sees it act
    # Minimal env: only what the harness needs. NOT **os.environ — that would hand the
    # decrypted GROQ/ANTHROPIC keys to the subprocess. Force UTF-8 so unicode pages survive.
    # The home/appdata vars are REQUIRED on Windows: browser-harness resolves Path.home() at
    # import (for its ~/.config + tmp dirs), which needs USERPROFILE — omitting it crashes the
    # harness before it runs. These are just the user's home path, not secrets, so passing them
    # doesn't reopen the API-key exfil hole the scrub exists to close.
    env = {
        "BU_CDP_URL": BH_CDP_URL,
        "PYTHONIOENCODING": "utf-8",
        "PATH": os.environ.get("PATH", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
    }
    for _var in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "HOME"):
        if os.environ.get(_var):
            env[_var] = os.environ[_var]
    try:
        proc = subprocess.run([BH_CLI], input=script, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=BH_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return f"Browser task timed out after {BH_TIMEOUT}s."
    except Exception as exc:
        return f"Browser task failed to run: {exc}"
    out = (proc.stdout or "").strip()
    errout = (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return f"Browser task error:\n{errout[:1500]}"
    result = out or "(the actions produced no readable output)"
    if errout and errout not in result:
        result += f"\n[stderr] {errout[:400]}"
    return result[:4000]


def _browse(actions) -> str:
    """The single entry point for the `browse` tool: validate + compile structured actions,
    then run them. The model supplies only structured steps, never executable code."""
    if isinstance(actions, dict):
        actions = [actions]
    script, err = _bh_build_script(actions if isinstance(actions, list) else [])
    if err or not script:
        return err or "Failed to build browse script."
    return _browser_run(script)


# ── Pentest learning — feed findings into cortex so JARVIS accumulates a knowledge base
#    of targets/tech/outcomes and pattern-matches new targets against systems it has seen. ──
def _pentest_signals(text: str) -> str:
    """Pull the structured signal out of raw tool output — the stuff worth remembering."""
    sig: list[str] = []
    ports = re.findall(r"(\d+)/tcp\s+open\s+(\S+)", text)
    if ports:
        sig.append("open ports " + ", ".join(f"{p}/{s}" for p, s in ports[:12]))
    m = re.search(r"Server:\s*([^\r\n]+)", text) or re.search(r"HTTPServer\[([^\]]+)\]", text)
    if m:
        sig.append("server " + m.group(1).strip()[:60])
    tech = re.findall(r"\b(Apache|nginx|PHP|WordPress|Cloudflare|IIS|Tomcat|Express|Node\.js|"
                      r"Django|Laravel|Drupal|Joomla|OpenSSH|MySQL|Jenkins|Grafana)\b", text, re.I)
    if tech:
        sig.append("tech " + ", ".join(sorted({t.lower() for t in tech}))[:80])
    paths = re.findall(r"(/[A-Za-z0-9_\-./]{1,40})\s+\(Status:\s*2\d\d", text)
    if paths:
        sig.append("paths " + ", ".join(sorted(set(paths))[:10]))
    m = re.search(r"harvested\s+(\d+)\s+.*URLs", text)
    if m:
        sig.append(f"{m.group(1)} URLs harvested")
    cands = re.findall(r"^\[(xss|sqli|lfi|ssrf|redirect|rce|ssti|idor)\]", text, re.M)
    if cands:
        sig.append("vuln candidates: " + ", ".join(sorted(set(cands))))
    if re.search(r"OSVDB|CVE-\d|SQL injection|\bvulnerab", text, re.I):
        sig.append("vulns flagged")
    return "; ".join(sig)


def _bugbounty_run(domain: str) -> str:
    """Full-chain bug-bounty sweep — recon → probe → urls → candidates — run in the efficient
    order, with EACH phase emitted as its own OpsConsole step (a real agent_tool event) so it
    stays step-by-step and honest, and every phase feeds cortex. Active phases are scope-gated
    (they refuse if the domain isn't authorized); recon/probe are broad. Ask for a `report`
    after for the write-up. (nuclei/scanall are heavier — run them as a follow-up.)"""
    import jarvis.act.pentest as _pt
    host = _pt._host_of(domain) or (domain or "").strip()
    if not host:
        return "Give a domain to sweep, e.g. bugbounty acme.com."
    phases = [("recon", None), ("probe", "probe"), ("urls", "urls"), ("candidates", "candidates")]
    summary: list[str] = []
    for label, task in phases:
        try:
            out = _pt.recon(host) if task is None else _pt.attack(host, task)
        except Exception as exc:
            out = f"(phase failed: {exc})"
        # learn + emit a per-phase OpsConsole step (like _run_tool does)
        try:
            out = _augment_and_learn("bugbounty", host, task or "recon", out)
        except Exception:
            pass
        entry = {"step": len(agent_trace) + 1, "action": f"bugbounty·{label}",
                 "args": {"target": host, "phase": label}, "observation": out[:2500]}
        agent_trace.append(entry)
        agent_trace[:] = agent_trace[-25:]
        broadcast_from_thread({"type": "agent_tool", "step": entry})
        if out.lstrip().startswith("⛔"):
            summary.append(f"[{label}] refused — {host} not in scope (scope add it first)")
            break
        summary.append(f"[{label}] " + (_pentest_signals(out) or "done"))
    return (f"Bug-bounty sweep of {host} — {len(summary)} phase(s):\n"
            + "\n".join("- " + s for s in summary)
            + "\n\nNext: `pentest {h} nuclei` / `scanall` for vulns, then `report {h}` for the write-up."
              .replace("{h}", host))


def _pentest_report(target: str) -> str:
    """Turn everything cortex has learned about a target into a Markdown assessment report —
    the bug-bounty deliverable (methodology phase 7). Fact-driven, with derived next steps."""
    try:
        import jarvis.act.pentest as _pt
        import cortex
        host = _pt._host_of(target) or (target or "").strip()
        if not host:
            return "Give a target to report on, e.g. report acme.com."
        facts = [f for f in cortex.recall(host, k=20, namespace="security")
                 if host.lower() in f.get("text", "").lower()]
        if not facts:
            return (f"No findings recorded for {host} yet. Run scans first — recon, then "
                    f"pentest probe/ports/urls/candidates/nuclei — and everything gets stored; "
                    f"then ask for the report.")
        from datetime import datetime as _dt
        joined = " ".join(f.get("text", "") for f in facts).lower()
        lines = [f"# Security assessment — {host}",
                 f"_Generated by JARVIS · {_dt.now().strftime('%Y-%m-%d %H:%M')}_", "",
                 f"## Findings ({len(facts)} recorded)"]
        for f in facts:
            imp = f.get("importance", 5)
            lines.append(f"- {f.get('text','').strip()}" + (f"  _(importance {imp})_" if imp >= 7 else ""))
        steps = []
        if "sqli" in joined:
            steps.append("Test the SQLi candidate URLs with sqlmap (`pentest <url> sqli`).")
        if "xss" in joined:
            steps.append("Confirm XSS candidates with dalfox.")
        if "lfi" in joined or "ssrf" in joined or "redirect" in joined:
            steps.append("Manually probe the LFI/SSRF/open-redirect candidates.")
        if "open ports" in joined:
            steps.append("Enumerate the open services by version and check for known CVEs.")
        if "vulns flagged" in joined:
            steps.append("Triage the nuclei findings and validate real-world impact.")
        if "urls harvested" in joined:
            steps.append("Review harvested endpoints for auth bypass, IDOR, and logic flaws.")
        if not steps:
            steps.append("Deepen recon: `pentest probe` for live subdomains, `pentest urls`, then `pentest nuclei`.")
        lines += ["", "## Recommended next steps"] + [f"- {s}" for s in steps]
        lines += ["", "_All findings above are ground truth from real tool runs, held in JARVIS's "
                  "memory (cortex) and used to pattern-match future targets._"]
        return "\n".join(lines)
    except Exception as exc:
        return f"Couldn't build the report ({exc})."


def _augment_and_learn(kind: str, target: str, task: str, output: str) -> str:
    """Store this finding in cortex (episode + compact fact) AND surface similar past
    systems from memory, so JARVIS reasons from what it has seen before — pattern analysis."""
    if not target or not output or output.lstrip().startswith("⛔"):
        return output
    try:
        import cortex
        signals = _pentest_signals(output)
        related = ""
        try:
            hits = cortex.recall(f"{kind} {signals or target}", k=4, namespace="security")
            hits = [h for h in hits if target.lower() not in (h.get("text", "").lower())]
            if hits:
                related = "\n\n◆ From memory — similar systems seen before:\n" + "\n".join(
                    "  · " + (h.get("text", "")[:130]) for h in hits[:3])
        except Exception:
            pass
        try:
            raw = f"[{kind} {target}{(' ' + task) if task else ''}]\n{output[:3000]}"
            eid = cortex.store.add_episode(raw, source="pentest")
            cortex.vectors.index_episode({"id": eid, "raw_text": raw, "source": "pentest",
                                          "timestamp": cortex.store.utcnow()})
            if signals:
                cortex.remember(f"{target} — {signals}", category="situation",
                                namespace="security", importance=6, source_model="pentest")
        except Exception:
            pass
        return output + related
    except Exception:
        return output


# ── Tool executor ──────────────────────────────────────────────────────────────
def execute_tool(name: str, args: dict[str, Any], gen: int | None = None) -> str:
    global memories, task_list, _pending_content_panel
    _pending_content_panel = None
    if name not in _TOOL_NAMES:
        return f"Unknown tool: {name}"
    if not isinstance(args, dict):
        return f"{name}: invalid arguments."
    err = _validate_tool_args(name, args)
    if err:
        return err

    if name == "use_skill":
        slug_in = str(args.get("skill", ""))
        skill = skills.get(slug_in)
        if skill is None:
            avail = ", ".join(s.slug for s in skills.all_skills()) or "none"
            return f"No skill named '{slug_in}'. Available skills: {avail}."
        return skills.format_for_tool(skill.body, skill.slug)

    if name == "create_skill":
        try:
            s = skills.create(args["name"], args.get("description", ""), args["instructions"])
        except Exception as exc:
            return f"Could not create skill: {exc}"
        return f"Saved skill '{s.slug}'. Load it later with use_skill(skill='{s.slug}')."

    if name == "remember":
        # Persist via cortex (SQLite + vector index). Legacy `memories` mirror is kept
        # so anything still reading it (UI counts, health endpoint) keeps working.
        raw_cat = _norm_memory_category(args.get("category", "fact"))
        # Cortex categories: preference | situation | person | identity | skill.
        _CAT_MAP = {"fact": "preference", "general": "preference", "task": "situation",
                    "project": "situation", "security": "identity", "personal": "identity"}
        cortex_cat = _CAT_MAP.get(raw_cat, raw_cat if raw_cat in
                                  {"preference", "situation", "person", "identity", "skill"}
                                  else "preference")
        try:
            cortex.remember(
                args["content"],
                category=cortex_cat,
                confidence=0.9,   # explicit user-driven remembers are high-trust
                namespace=args.get("namespace", "personal"),
                source_model=args.get("source_model", "jarvis"),
                private=bool(args.get("private", False)),
                importance=int(args.get("importance") or
                               (6 if raw_cat == "personal" else 5)),
            )
        except Exception as _exc:
            log.info("cortex.remember failed, falling back to legacy JSON only: %s", _exc)
        # Legacy mirror (drives /api/memory count + UI badge until the panel migrates).
        now = time.time()
        entry = {
            "id": _next_id(memories),
            "content": args["content"],
            "category": raw_cat,
            "importance": int(args.get("importance") or
                              (6 if raw_cat == "personal" else 5)),
            "namespace": args.get("namespace", "personal"),
            "private": bool(args.get("private", False)),
            "source_model": args.get("source_model", "jarvis"),
            "timestamp": datetime.now().isoformat(),
            "ts": now,
            "last_access": now,
            "access_count": 0,
            "source": "manual",
        }
        with _mem_lock:
            entry["id"] = _next_id(memories)
            memories.append(entry)
            _save_memory(memories)
        broadcast_from_thread({"type": "memory_update", "count": len(memories)})
        return f"Stored: {args['content']}"

    if name == "recall_memory":
        # Semantic recall via cortex. Reinforcement is handled inside cortex.recall.
        try:
            hits = cortex.recall(
                args["query"],
                k=int(args.get("k", 6)),
                namespace=args.get("namespace"),
                include_private=True,   # JARVIS sees private; the HTTP hub does not
            )
        except Exception as _exc:
            log.info("cortex.recall failed, falling back to legacy JSON: %s", _exc)
            hits = []
        if hits:
            return "\n".join(
                f"[{m.get('category', 'preference')}] {m.get('text', '')}"
                for m in hits
            )
        return "No memories matching that query."

    if name == "browse":
        return _browse(args.get("actions"))

    if name == "report":
        return _pentest_report(args.get("target", ""))

    if name == "bugbounty":
        return _bugbounty_run(args.get("target", ""))

    if name in ("recon", "pentest", "scope"):
        import jarvis.act.pentest as _pt
        if name == "recon":
            tgt = args.get("target", "")
            return _augment_and_learn("recon", tgt, "", _pt.recon(tgt))
        if name == "pentest":
            tgt, task = args.get("target", ""), args.get("task", "ports")
            # `report` is generated from memory (cortex), not a container tool.
            if task.strip().lower() in ("report", "summary", "writeup"):
                return _pentest_report(tgt)
            return _augment_and_learn("pentest", tgt, task, _pt.attack(tgt, task))
        act = (args.get("action") or "list").strip().lower()
        if act == "add":
            return _pt.add_scope(args.get("target", ""), args.get("source", "manual"), args.get("program", ""))
        if act == "remove":
            return _pt.remove_scope(args.get("target", ""))
        return _pt.list_scope()

    if name == "search_web":
        try:
            items = args.get("items")
            if items and not isinstance(items, list):
                items = [str(items)]
            result = websearch_mod.search(
                args.get("query", ""),
                args.get("mode", "search"),
                items=items,
                aspect=str(args.get("aspect") or ""),
            )
            if result.get("panel_body"):
                _pending_content_panel = {
                    "title": result.get("title", "SEARCH"),
                    "body": result["panel_body"],
                }
            return result.get("text", "No results.")
        except Exception as exc:
            return f"Search error: {exc}"

    if name == "get_system_info":
        snap = system_monitor.snapshot()
        cpu = snap["cpu_percent"]
        ram = snap["ram_percent"]
        disk_pct, disk_free = disk_stats()
        procs = sorted(
            psutil.process_iter(["name", "cpu_percent"]),
            key=lambda p: p.info.get("cpu_percent") or 0,
            reverse=True,
        )
        top = [p.info["name"] for p in procs[:8] if p.info.get("name")]
        vm = psutil.virtual_memory()
        extra = []
        if snap.get("cpu_temp_c") is not None:
            extra.append(f"CPU temp {snap['cpu_temp_c']}°C")
        if snap.get("gpu_percent") is not None:
            extra.append(f"GPU {snap['gpu_percent']}%")
        tail = f" | {' | '.join(extra)}" if extra else ""
        return (
            f"CPU {cpu}% | RAM {ram}% ({vm.used // 2**30}GB/{vm.total // 2**30}GB) | "
            f"Disk {disk_pct:.0f}% | Top procs: {', '.join(top)}{tail}"
        )

    if name == "launch_app":
        raw = args["app"].lower().strip()
        cmd = _LAUNCH_ALLOWLIST.get(raw)
        if not cmd:
            supported = ", ".join(sorted(_LAUNCH_ALLOWLIST))
            return f"Unknown app '{raw}'. Supported: {supported}"
        return _launch_resolved(raw, cmd)

    if name == "close_app":
        raw = str(args.get("app", "")).lower().strip()
        out = _close_app(raw)
        if out:
            return out
        # Unknown to the close map — try any running process whose name matches the token.
        proc = _match_running_process(raw)
        if proc:
            res = verify.terminate(proc, timeout=5.0)
            if res.ok:
                return f"Closed {raw}."
            return f"I couldn't close {raw} — {res.detail}."
        return f"{raw} doesn't appear to be running, so there's nothing to close."

    if name == "desktop":
        # Windows-system-shaped ops: Explorer paths, Settings pages, Control Panel,
        # regedit, system components, and winget list/uninstall (confirm-gated).
        # gpt-oss often mis-structures this call — it puts the sub-action as a KEY
        # (e.g. {"open_path": "C:\\…"}) instead of {"action":"open_path","path":"C:\\…"},
        # which would fail with "unknown action ''". Normalize that shape back.
        args = _normalize_desktop_args(args)
        action = str(args.get("action") or "")
        try:
            _event_bus.emit(
                "ToolCalled",
                tool="desktop",
                action=action,
                risk=_desktop_risk(action),
            )
        except Exception:
            pass
        return desktop.run(action, args)

    if name == "add_task":
        task = _tasks.create(args["task"], args.get("eta", ""))
        return f"Task added ({task['tid']}): {args['task']}"

    if name == "complete_task":
        it = _tasks.complete(args["task_id"])
        if it is None:
            return f"Task {args['task_id']} not found."
        return f"{it['tid']} marked complete."

    if name == "cancel_task":
        it = _tasks.cancel(args["task_id"])
        if it is None:
            return f"Task {args['task_id']} not found."
        return f"{it['tid']} cancelled."

    if name == "capture_screen":
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                monitor = sct.monitors[1]
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                try:
                    _lanczos = Image.Resampling.LANCZOS
                except AttributeError:
                    _lanczos = Image.LANCZOS  # type: ignore[attr-defined]
                img.thumbnail((1280, 720), _lanczos)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=70)
                img_bytes = buf.getvalue()
        except ImportError:
            return "Vision deps missing. Run: pip install mss Pillow."
        except Exception as exc:
            return f"Screen capture failed: {exc}"

        prompt = ("Describe what is on this screen concisely. Focus on the main "
                  "content, active app, and any important details.")

        # Preferred: Groq cloud vision (Llama 4, multimodal) — no GPU, no Ollama.
        if USE_GROQ and _HAS_GROQ:
            try:
                b64 = base64.b64encode(img_bytes).decode()
                client = _openai().OpenAI(
                    api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT,
                )
                resp = client.chat.completions.create(
                    model=GROQ_VISION_MODEL,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    max_tokens=500,
                )
                return resp.choices[0].message.content or ""
            except Exception as exc:
                return f"Vision read failed: {exc}"

        # Fallback: local llava via Ollama (needs Ollama running + llava pulled).
        try:
            import ollama
            resp = ollama.chat(
                model=VISION_MODEL,
                messages=[{"role": "user", "content": prompt, "images": [img_bytes]}],
                options={"keep_alive": 0},
            )
            return resp.message.content or ""
        except Exception as exc:
            return f"Screen capture failed (no Groq key and Ollama unavailable): {exc}"

    if name == "run_command":
        cmd = args.get("command", "").strip()
        blocked = _command_allowed(cmd)
        if blocked:
            return blocked
        cwd_arg = args.get("cwd")
        cwd = Path(cwd_arg).expanduser().resolve() if cwd_arg else BASE_DIR
        if not cwd.is_dir():
            return f"cwd '{cwd_arg}' is not a valid directory."
        proc = subprocess.Popen(
            cmd, shell=True, cwd=str(cwd),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            out, err = proc.communicate(timeout=30)
            combined = "\n".join(filter(None, [(out or "").strip(), (err or "").strip()]))
            return combined[:2000] if combined else "(no output)"
        except subprocess.TimeoutExpired:
            # proc.kill() alone only terminates the direct shell child — a
            # backgrounded/detached grandchild (e.g. `start /b ...`) survives it.
            # Kill the whole tree via psutil (cross-platform).
            try:
                parent = psutil.Process(proc.pid)
                for child in parent.children(recursive=True):
                    child.kill()
                parent.kill()
            except psutil.NoSuchProcess:
                pass
            return "Command timed out after 30s."
        except Exception as exc:
            return f"Command failed: {exc}"

    if name == "ict_scan":
        return _ict_scan(args.get("symbol", "nifty"), args.get("interval", "15m"))

    if name == "open_trading":
        if not TRADING_ROOT.is_dir():
            return (f"Trading terminal not installed — expected folder at {TRADING_ROOT}. "
                    "Set C0MR4DES_DIR or install c0mr4des_terminal alongside JARVIS.")
        broadcast_from_thread({"type": "open_trading"})
        return "Opening the trading terminal in its own window."

    if name == "analyze_image":
        return _analyze_image(args.get("path", ""), args.get("question", ""))

    if name == "watch_video":
        return _watch_video(args.get("source", ""), args.get("question", ""), gen)

    if name == "calculate":
        return _calculate(args.get("expression", ""))

    if name == "get_weather":
        return ambient.weather_report(args.get("city", ""))

    return f"Unknown tool: {name}"


def _calculate(expr: str) -> str:
    """Safe arithmetic eval via AST — numbers and operators only, no names/calls."""
    import ast, operator
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
           ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}

    def ev(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        raise ValueError("unsupported expression")

    try:
        result = ev(ast.parse(expr.strip(), mode="eval").body)
        return f"{expr} = {result}"
    except Exception:
        return f"Couldn't evaluate: {expr!r}"


# ── Vision: image + video understanding (Groq Llama-4 vision + Whisper) ─────────
def _groq_vision(b64_images: list, prompt: str, max_tokens: int = 600) -> str:
    """Send one or more base64 JPEGs + a prompt to Groq's multimodal model."""
    if not (USE_GROQ and _HAS_GROQ):
        return "Vision needs a Groq API key."
    client = _openai().OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
    content: list[Any] = [{"type": "text", "text": prompt}]
    for b in b64_images:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
    resp = client.chat.completions.create(
        model=GROQ_VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def _analyze_image(path: str, question: str = "") -> str:
    """Describe / answer about an image file via cloud vision."""
    path = path.strip().strip('"').strip("'")
    if not os.path.isfile(path):
        return f"No image file at: {path}"
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        try:
            _lanczos = Image.Resampling.LANCZOS
        except AttributeError:
            _lanczos = Image.LANCZOS  # type: ignore[attr-defined]
        img.thumbnail((1280, 1280), _lanczos)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        return "Image deps missing. Run: pip install Pillow."
    except Exception as exc:
        return f"Couldn't read image: {exc}"
    prompt = question.strip() or "Describe this image concisely — the main subject, text, and any notable detail."
    try:
        return _groq_vision([b64], prompt, max_tokens=700)
    except Exception as exc:
        return f"Image analysis failed: {exc}"


def _watch_video(source: str, question: str = "", gen: int | None = None) -> str:
    """Watch a local video or URL: sample frames + transcribe audio, then reason over both.
    `gen` is the turn-generation this call started under; if a barge-in bumps
    _turn_generation while this (uncancellable, thread-pool-bound) call is still
    running, we notice at the next checkpoint and stop doing further wasted work
    and stop broadcasting stale progress for a turn that's no longer current."""
    import tempfile, glob

    def _stale() -> bool:
        return gen is not None and gen != _turn_generation

    def _progress(text: str) -> None:
        if not _stale():
            broadcast_from_thread({"type": "state", "status": "thinking", "text": text})

    source = source.strip().strip('"').strip("'")
    try:
        import cv2
    except ImportError:
        return "Video deps missing. Run: pip install opencv-python-headless yt-dlp."

    # Work + temp files on a drive with space (K: if present, else system temp).
    tmp_root = os.environ.get("JARVIS_TMP") or tempfile.gettempdir()
    os.makedirs(tmp_root, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=tmp_root)
    video_path = source

    # If it's a URL, download a small progressive MP4 with yt-dlp.
    if source.lower().startswith(("http://", "https://", "www.")):
        try:
            import yt_dlp
        except ImportError:
            return "URL video needs yt-dlp. Run: pip install yt-dlp."
        out_tmpl = os.path.join(workdir, "vid.%(ext)s")
        _progress("Downloading video...")
        try:
            opts: Any = {"outtmpl": out_tmpl, "quiet": True, "noplaylist": True,
                    "format": "mp4[height<=480]/best[height<=480]/best", "max_filesize": 80 * 1024 * 1024}
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([source])
            hits = glob.glob(os.path.join(workdir, "vid.*"))
            if not hits:
                return "Couldn't download that video (too large or unsupported)."
            video_path = hits[0]
        except Exception as exc:
            return f"Video download failed: {exc}"

    if _stale():
        return "Cancelled — a newer command superseded this one."

    if not os.path.isfile(video_path):
        return f"No video at: {video_path}"

    # Sample frames with OpenCV (budget by duration, hard cap 12 frames for token cost).
    _progress("Sampling frames...")
    frames_b64 = []
    try:
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = total / fps if fps else 0
        n = 5  # Groq's multimodal models accept up to 5 images per request
        idxs = [int(total * i / (n + 1)) for i in range(1, n + 1)] if total else []
        for fi in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ok, frame = cap.read()
            if not ok:
                continue
            ok, enc = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                frames_b64.append(base64.b64encode(enc.tobytes()).decode())
        cap.release()
    except Exception as exc:
        return f"Frame sampling failed: {exc}"
    if not frames_b64:
        return "Couldn't read any frames from that video."

    if _stale():
        return "Cancelled — a newer command superseded this one."

    # Transcribe audio via Groq Whisper (it accepts mp4/webm; skip if file too big).
    transcript = ""
    try:
        if USE_GROQ and _HAS_GROQ and os.path.getsize(video_path) <= 24 * 1024 * 1024:
            _progress("Transcribing audio...")
            client = _openai().OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
            with open(video_path, "rb") as f:
                r = client.audio.transcriptions.create(model=STT_MODEL, file=f, response_format="text")
            transcript = (r or "").strip()
    except Exception:
        transcript = ""

    q = question.strip() or "What happens in this video? Summarize it concisely."
    prompt = (f"These are {len(frames_b64)} frames sampled across a video, in order.\n"
              + (f"Audio transcript:\n{transcript[:4000]}\n\n" if transcript else "")
              + f"{q}\nAnswer in plain spoken sentences.")
    try:
        out = _groq_vision(frames_b64, prompt, max_tokens=800)
    except Exception as exc:
        out = f"Video analysis failed: {exc}"
    try:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:
        pass
    return out


# ── ICT / Smart-Money scanner (Indian markets) ─────────────────────────────────
_INDIAN_SYMBOLS = {
    "nifty": "^NSEI", "nifty50": "^NSEI", "nifty 50": "^NSEI", "nse": "^NSEI",
    "sensex": "^BSESN", "bse": "^BSESN",
    "banknifty": "^NSEBANK", "bank nifty": "^NSEBANK", "nifty bank": "^NSEBANK",
    "finnifty": "NIFTY_FIN_SERVICE.NS",
}


def _download_candles(ysym: str, period: str, interval: str) -> tuple[Any, str | None]:
    """Fetch OHLCV from Yahoo. Returns (dataframe, error_kind).
    error_kind: 'netblock' | 'empty' | 'fetch:<msg>' | None."""
    import yfinance as yf
    try:
        df = yf.download(ysym, period=period, interval=interval, progress=False, auto_adjust=False)
    except Exception as exc:
        if _is_network_block(exc):
            return None, "netblock"
        return None, f"fetch:{exc}"
    if df is None or len(df) == 0:
        return df, "empty"
    return df, None


def _resolve_symbol(sym: str) -> tuple[str, str]:
    """Map a friendly name to a Yahoo symbol. Indian aliases resolve to indices/NSE;
    bare tickers default to NSE (.NS) with a US-ticker retry in _ict_analyze."""
    s = sym.strip().lower()
    if s in _INDIAN_SYMBOLS:
        return _INDIAN_SYMBOLS[s], sym.strip().upper()
    raw = sym.strip().upper()
    if raw.startswith("^") or "." in raw or "=" in raw or "-" in raw:
        return raw, raw
    return f"{raw}.NS", raw


def _tv_symbol(ysym: str, name: str) -> str:
    """TradingView symbol for the chart widget."""
    table = {"^NSEI": "NSE:NIFTY", "^BSESN": "BSE:SENSEX", "^NSEBANK": "NSE:BANKNIFTY",
             "NIFTY_FIN_SERVICE.NS": "NSE:CNXFINANCE"}
    if ysym in table:
        return table[ysym]
    if ysym.endswith(".NS"):
        return f"NSE:{ysym[:-3]}"
    if ysym.endswith(".BO"):
        return f"BSE:{ysym[:-3]}"
    if ysym.endswith("-USD") or ysym.endswith("=X") or ysym.startswith("^"):
        return ysym
    if "." not in ysym:
        return f"NASDAQ:{ysym}"
    return ysym


def _market_session() -> dict:
    """NSE/BSE session status in IST (09:15–15:30, Mon–Fri)."""
    from datetime import timezone, timedelta
    ist = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
    mins = ist.hour * 60 + ist.minute
    open_m, close_m = 9 * 60 + 15, 15 * 60 + 30
    weekday = ist.weekday() < 5
    if weekday and open_m <= mins <= close_m:
        state = "open"
        if mins <= open_m + 60:
            state = "open (opening hour — prime killzone)"
        note = f"{state}; {close_m - mins} min to close"
    elif weekday and mins < open_m:
        note = f"pre-market; opens in {open_m - mins} min"
    else:
        note = "closed"
    return {"open": weekday and open_m <= mins <= close_m, "note": note,
            "ist": ist.strftime("%a %H:%M IST")}


def _quick_bias(ysym: str, interval: str, period: str) -> str:
    """Lightweight higher-timeframe bias (structure only) for confluence."""
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.download(ysym, period=period, interval=interval, progress=False, auto_adjust=False)
        if df is None or len(df) < 25:
            return "neutral"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna()
        h, l = df["High"].to_numpy(), df["Low"].to_numpy()
        n, w = len(df), 2
        sh = [i for i in range(w, n - w) if h[i] == max(h[i - w:i + w + 1])]
        sl = [i for i in range(w, n - w) if l[i] == min(l[i - w:i + w + 1])]
        if len(sh) >= 2 and len(sl) >= 2:
            if h[sh[-1]] > h[sh[-2]] and l[sl[-1]] > l[sl[-2]]:
                return "bullish"
            if h[sh[-1]] < h[sh[-2]] and l[sl[-1]] < l[sl[-2]]:
                return "bearish"
        return "neutral"
    except Exception:
        return "neutral"


def _trade_plan(bias: str, last: float, fvgs: list, last_sl: float, last_sh: float,
                buyside: list, sellside: list) -> dict:
    """Draft an entry/SL/TP from the structure. Analysis only — user places it."""
    if bias == "bullish":
        zone = next((f for f in reversed(fvgs) if f["dir"] == "bullish" and f["hi"] < last), None)
        entry = round((zone["lo"] + zone["hi"]) / 2, 1) if zone else round(last_sl, 1)
        sl = round(last_sl * 0.998, 1)
        tp = round(buyside[0], 1) if buyside else round(last_sh, 1)
    elif bias == "bearish":
        zone = next((f for f in reversed(fvgs) if f["dir"] == "bearish" and f["lo"] > last), None)
        entry = round((zone["lo"] + zone["hi"]) / 2, 1) if zone else round(last_sh, 1)
        sl = round(last_sh * 1.002, 1)
        tp = round(sellside[0], 1) if sellside else round(last_sl, 1)
    else:
        return {"side": "wait", "text": "No setup — stand aside until structure or a sweep prints."}
    risk, reward = abs(entry - sl), abs(tp - entry)
    rr = round(reward / risk, 2) if risk else 0
    return {"side": "long" if bias == "bullish" else "short",
            "entry": entry, "sl": sl, "tp": tp, "rr": rr,
            "text": f"{'Long' if bias == 'bullish' else 'Short'} idea — entry {entry}, stop {sl}, target {tp} (R:R {rr})."}


_MARKET_NETBLOCK = ("Can't reach live market data right now. This network looks like it's "
                    "blocking the data provider (Yahoo Finance) — finance domains are filtered "
                    "while everything else works. Try a mobile hotspot or VPN and Markets comes alive.")


def _is_network_block(exc: Exception) -> bool:
    """True when an exception looks like a connectivity/TLS block rather than bad input."""
    s = str(exc).lower()
    return any(k in s for k in (
        "reset", "10054", "curl: (35)", "curl: (7)", "curl: (28)", "ssl", "timed out",
        "timeout", "connection", "max retries", "failed to establish", "failed to perform",
        "getaddrinfo", "name resolution", "unreachable", "refused"))


def _ict_analyze(symbol: str, interval: str = "15m") -> dict:
    """Structured ICT read. Returns a dict (ok/error + signals) used by both the
    voice tool and the Markets panel / watcher. Cached ~30s to spare repeat fetches."""
    raw = (symbol or "").strip()
    # Reject junk before we hit yfinance (DoS / path-ish strings from the Markets panel).
    if not raw or len(raw) > 32 or not re.fullmatch(r"[A-Za-z0-9.^_=-]+", raw):
        return {"ok": False, "error": "Invalid market symbol."}
    interval = interval if interval in {"5m", "15m", "30m", "60m", "1d"} else "15m"
    ckey = (raw.lower(), interval)
    hit = _ict_cache.get(ckey)
    if hit and (time.time() - hit[0]) < _ICT_CACHE_TTL_SEC:
        return hit[1]
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return {"ok": False, "error": "Market deps missing (pip install yfinance pandas)."}

    ysym, name = _resolve_symbol(raw)
    period = {"5m": "5d", "15m": "1mo", "30m": "1mo", "60m": "3mo", "1d": "1y"}[interval]
    df, err = _download_candles(ysym, period, interval)
    # Bare tickers auto-map to NSE (.NS); retry without suffix for US symbols like AAPL.
    if (df is None or len(df) < 25) and ysym.endswith(".NS"):
        alt = ysym[:-3]
        if alt and alt.isalpha() and len(alt) <= 5:
            df2, err2 = _download_candles(alt, period, interval)
            if df2 is not None and len(df2) >= 25:
                ysym, name, df, err = alt, alt, df2, None
            elif err != "netblock" and err2 == "netblock":
                err = "netblock"
    if df is None or len(df) < 25:
        if err == "netblock" or (ysym.startswith("^") and (df is None or len(df) == 0)):
            return {"ok": False, "error": _MARKET_NETBLOCK, "netblock": True}
        if df is None or len(df) == 0:
            return {"ok": False,
                    "error": (f"No market data for {name}. "
                              "Indian stocks: use the NSE symbol (e.g. RELIANCE). "
                              "US stocks: use the plain ticker (e.g. AAPL).")}
        return {"ok": False,
                "error": f"Not enough {interval} history for {name} yet — markets may be pre-open; try the 1d timeframe."}

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    highs, lows = df["High"].to_numpy(), df["Low"].to_numpy()
    opens, closes = df["Open"].to_numpy(), df["Close"].to_numpy()
    n = len(df)
    last = float(closes[-1])

    w = 2
    sh = [i for i in range(w, n - w) if highs[i] == max(highs[i - w:i + w + 1])]
    sl = [i for i in range(w, n - w) if lows[i] == min(lows[i - w:i + w + 1])]

    structure, bias = "ranging / unclear", "neutral"
    if len(sh) >= 2 and len(sl) >= 2:
        hh, hl = highs[sh[-1]] > highs[sh[-2]], lows[sl[-1]] > lows[sl[-2]]
        lh, ll = highs[sh[-1]] < highs[sh[-2]], lows[sl[-1]] < lows[sl[-2]]
        if hh and hl:
            structure, bias = "higher highs & higher lows (uptrend)", "bullish"
        elif lh and ll:
            structure, bias = "lower highs & lower lows (downtrend)", "bearish"
        else:
            structure, bias = "mixed / ranging", "neutral"

    last_sh = float(highs[sh[-1]]) if sh else float(max(highs))
    last_sl = float(lows[sl[-1]]) if sl else float(min(lows))
    bos = ""
    if last > last_sh:
        bos = f"bullish BOS above {last_sh:.1f}"
    elif last < last_sl:
        bos = f"bearish BOS below {last_sl:.1f}"

    # Liquidity sweep: last 3 candles wick beyond a prior swing then close back inside.
    sweep = ""
    for i in range(max(2, n - 3), n):
        for j in sl[:-1]:
            if lows[i] < lows[j] and closes[i] > lows[j]:
                sweep = f"sell-side sweep of {lows[j]:.1f} (bullish reversal cue)"
        for j in sh[:-1]:
            if highs[i] > highs[j] and closes[i] < highs[j]:
                sweep = f"buy-side sweep of {highs[j]:.1f} (bearish reversal cue)"

    fvgs = []
    for i in range(2, n):
        if highs[i - 2] < lows[i]:
            if i + 1 >= n or lows[i + 1:].min() > highs[i - 2]:
                fvgs.append({"dir": "bullish", "lo": float(highs[i - 2]), "hi": float(lows[i])})
        elif lows[i - 2] > highs[i]:
            if i + 1 >= n or highs[i + 1:].max() < lows[i - 2]:
                fvgs.append({"dir": "bearish", "lo": float(highs[i]), "hi": float(lows[i - 2])})
    recent_fvgs = fvgs[-3:]

    ob = ""
    if bias == "bullish" and sh:
        for k in range(sh[-1] - 1, max(0, sh[-1] - 12), -1):
            if closes[k] < opens[k]:
                ob = f"bullish OB {lows[k]:.1f}-{highs[k]:.1f}"
                break
    elif bias == "bearish" and sl:
        for k in range(sl[-1] - 1, max(0, sl[-1] - 12), -1):
            if closes[k] > opens[k]:
                ob = f"bearish OB {lows[k]:.1f}-{highs[k]:.1f}"
                break

    buyside = sorted({round(float(highs[i]), 1) for i in sh if highs[i] > last})[:3]
    sellside = sorted({round(float(lows[i]), 1) for i in sl if lows[i] < last}, reverse=True)[:3]

    # Equilibrium of the recent dealing range — ICT premium/discount. Longs are
    # "cheap" in discount (below 50%); shorts are "cheap" in premium (above 50%).
    rng_hi = float(max(highs[-50:]))
    rng_lo = float(min(lows[-50:]))
    eq = (rng_hi + rng_lo) / 2
    zone = "premium" if last > eq else "discount"

    if bias == "bullish":
        read = "Momentum favors longs — best entries are a pullback into a bullish FVG or order block, targeting buy-side liquidity above."
    elif bias == "bearish":
        read = "Momentum favors shorts — look for a retrace into a bearish FVG or order block, targeting sell-side liquidity below."
    else:
        read = "No clean directional edge — wait for a liquidity sweep or a break of structure before committing."

    # Confluence score (0-100): how many smart-money factors line up right now.
    score = 0
    if bias in ("bullish", "bearish"):
        score += 25
        if bos:        score += 20
        if sweep:      score += 15
        if recent_fvgs: score += 15
        if ob:         score += 10
        if (bias == "bullish" and zone == "discount") or \
           (bias == "bearish" and zone == "premium"):
            score += 15
    score = min(100, score)

    # Higher-timeframe confluence: intraday TFs read the daily; daily reads weekly.
    htf_interval, htf_period = ("1d", "6mo") if interval != "1d" else ("1wk", "2y")
    htf_bias = _quick_bias(ysym, htf_interval, htf_period)
    confluence = "aligned" if htf_bias == bias and bias != "neutral" else (
        "conflicting" if bias != "neutral" and htf_bias != "neutral" and htf_bias != bias else "neutral")
    plan = _trade_plan(bias, last, recent_fvgs, last_sl, last_sh, buyside, sellside)
    session = _market_session()

    result = {
        "ok": True, "symbol": name, "yahoo": ysym, "tv": _tv_symbol(ysym, name),
        "interval": interval, "last": round(last, 1), "bias": bias, "structure": structure,
        "bos": bos, "sweep": sweep, "fvgs": recent_fvgs, "order_block": ob,
        "buyside": buyside, "sellside": sellside,
        "equilibrium": round(eq, 1), "zone": zone, "score": score, "read": read,
        "htf_bias": htf_bias, "confluence": confluence, "plan": plan, "session": session,
    }
    _ict_cache_put(ckey, result)
    return result


def _ict_scan(symbol: str, interval: str = "15m") -> str:
    """Text formatting of the structured read, for the voice agent."""
    a = _ict_analyze(symbol, interval)
    if not a.get("ok"):
        return a.get("error", "Scan failed.")
    out = [f"{a['symbol']} on the {a['interval']}: last {a['last']}. Market {a['session']['note']}.",
           f"Structure: {a['structure']}; {a['interval']} bias {a['bias']}, daily {a['htf_bias']} ({a['confluence']}).",
           f"Price in {a['zone']} (equilibrium {a['equilibrium']}); confluence {a['score']} of 100."]
    if a["bos"]:
        out.append(a["bos"].capitalize() + ".")
    if a["sweep"]:
        out.append("Liquidity " + a["sweep"] + ".")
    if a["fvgs"]:
        out.append("Unfilled FVGs: " + "; ".join(f"{f['dir']} {f['lo']:.1f}-{f['hi']:.1f}" for f in a["fvgs"]) + ".")
    if a["order_block"]:
        out.append(a["order_block"].capitalize() + ".")
    if a["buyside"]:
        out.append("Buy-side liquidity: " + ", ".join(f"{x:.1f}" for x in a["buyside"]) + ".")
    if a["sellside"]:
        out.append("Sell-side liquidity: " + ", ".join(f"{x:.1f}" for x in a["sellside"]) + ".")
    out.append(a["read"])
    if a.get("plan", {}).get("text"):
        out.append(a["plan"]["text"])
    out.append("Analysis only — not advice, and I won't place trades.")
    return " ".join(out)


# ── ICT watcher (scheduled scans + fresh-signal alerts) ────────────────────────
async def _watch_loop() -> None:
    while _watching:
        for sym in WATCHLIST:
            if not _watching:
                break
            a = await asyncio.to_thread(_ict_analyze, sym, WATCH_TF)
            if not a.get("ok"):
                continue
            name = a["symbol"]
            prev = _watch_state.get(name)
            alerts = []
            if not prev:                       # first scan — seed baseline, don't alert
                pass
            else:
                if a["bos"] and a["bos"] != prev.get("bos"):
                    alerts.append(a["bos"])
                if a["sweep"] and a["sweep"] != prev.get("sweep"):
                    alerts.append(a["sweep"])
            _watch_state[name] = {"bos": a["bos"], "sweep": a["sweep"], "bias": a["bias"]}

            if alerts:
                msg = f"{name} ({a['interval']}): " + "; ".join(alerts)
                await broadcast({"type": "ict_alert", "symbol": name, "text": msg, "data": a})
                broadcast_log = {"type": "system", "text": f"⚑ {msg}"}
                await broadcast(broadcast_log)
                quiet = _tts_playing or (_current_task and not _current_task.done())
                if WATCH_SPEAK and not quiet:
                    asyncio.create_task(_schedule_speak(f"Heads up — {name}, {alerts[0]}."))
        # sleep in short slices so stop is responsive
        for _ in range(max(1, WATCH_INTERVAL_MIN) * 6):
            if not _watching:
                break
            await asyncio.sleep(10)


async def _start_watch() -> None:
    global _watch_task, _watching, _watch_state
    if _watching:
        return
    _watching = True
    _watch_state = {}
    await broadcast({"type": "watch_state", "watching": True,
                     "watchlist": WATCHLIST, "interval_min": WATCH_INTERVAL_MIN, "tf": WATCH_TF})
    await broadcast({"type": "system", "text": f"ICT watcher on — {', '.join(WATCHLIST)} every {WATCH_INTERVAL_MIN}m ({WATCH_TF})."})
    _watch_task = asyncio.create_task(_watch_loop())


async def _stop_watch() -> None:
    global _watching
    _watching = False
    await broadcast({"type": "watch_state", "watching": False,
                     "watchlist": WATCHLIST, "interval_min": WATCH_INTERVAL_MIN, "tf": WATCH_TF})
    await broadcast({"type": "system", "text": "ICT watcher off."})


# ── System prompt ──────────────────────────────────────────────────────────────
_BASE_PROMPT = """You are JARVIS — Just A Rather Very Intelligent System. Personal AI of __USER__, running on Windows 11 as a desktop app.

You don't assume things about __USER__ you weren't told — what you know about them comes from your saved memory below, nothing else.

Core style: sharp, direct, and never verbose. Answer directly. If it's a simple question, answer it — don't narrate your process. When you use a tool, report the result, not what you're about to do.

Language matching: reply in the same language the user is using. Match their register too — if they address you formally, do the same; if they're casual, be casual. When the language has a natural respectful vocative (English 'sir', Turkish 'efendim', Japanese 'さん'/'様', Hindi 'ji'/'sahab', Spanish 'señor', French 'monsieur', Arabic 'sayyidi', German 'Herr'), you may use it when it feels natural — sparingly, at most once per reply, and never mixed between languages in the same turn.

Grounding: when a tool returns data, your answer MUST be built from that exact data — quote the real numbers/values it gave you. Never invent or hand-wave a result, and never pad with unrelated facts about the user. If a tool failed or returned nothing, say so plainly.

NEVER FABRICATE ACTIONS OR RESULTS. This is absolute. You have not done something unless a tool actually returned the result to you in this conversation. Do not claim to have run a scan, launched an attack, created or read a file, or found ports/vulns/paths unless the matching tool call produced that output. Do not invent progress updates, log files, log contents, or findings. If a task needs a tool, CALL THE TOOL — do not describe what it would output. If you were asked to recon or pentest a target, you MUST call the `recon` or `pentest` tool; narrating scan results you didn't get from the tool is a serious failure. If you haven't run it yet, say "running it now" and actually call the tool — never pretend it's done.

This applies just as hard to opening/closing apps and desktop control. NEVER say "Done", "Opened", "Closed", or "I've launched it" unless the `launch_app` / `close_app` / `desktop` tool returned a success to you. Those tools VERIFY the real OS state and tell you exactly what happened — relay that verbatim in spirit. If a tool says it couldn't confirm, couldn't find the app, or failed, say that plainly ("I couldn't open X — it may not be installed") and do NOT dress it up as success. A wrong "done" is worse than an honest "that didn't work."

Capability honesty: you do NOT have tools to send email, send SMS/texts, make phone calls, or move money. If asked for one of these, say you can't do that yet — don't pretend you did it, and don't claim a capability you weren't given.

Do NOT pre-judge authorization or scope in your head and refuse. Always CALL the security tool — it enforces scope itself and tells you (and you relay) if a target is out of scope. `recon` (passive) and `report` (reads memory) never need scope, so never refuse those for scope reasons; just call them.

Security tool routing — pick the tool, don't just talk about it: "recon/look up <target>" → recon. "bugbounty/sweep/enumerate/recon a domain" → bugbounty (full sweep). "pentest/scan/port scan/nuclei/dirs/xss/sqli/subdomain takeover <target>" → pentest with the matching task. "report/write-up on <target>" → report. "add/list/remove scope" → scope. Any of these is a request to RUN the tool on that target, not to explain the concept.

You are in a live spoken conversation — your replies are read aloud and you remember what was just said. Talk like a person, not a document:
- Use contractions and natural, flowing phrasing. Be warm but concise.
- This is a back-and-forth. Follow the thread — refer to what was just said, and resolve references like "that", "the first one", "tomorrow" from context instead of asking the user to repeat themselves.
- Don't echo the question back or narrate ("You asked about..."). Just respond like you're talking.
- If a request is genuinely ambiguous, ask one short clarifying question instead of guessing.
- One or two sentences for most things; go longer only when asked for detail or code.
- NEVER use markdown, headers, bullets, asterisks, code fences, or math notation — spell math in words ("ninety minus sixty"). It all gets spoken.

You have tools — memory, web search, browser (`browse`), security recon (`recon`, passive), active pentest (`pentest`, scope-gated), scope management (`scope`), system info, app launch, Windows desktop control (`desktop`), parallel sub-agents (`spawn_agents`), tasks, screen capture, shell, market scans, and the trading terminal. Use a tool ONLY when the request genuinely needs real data, an action, or your saved memory. For greetings or small talk, just reply directly — never call a tool for "hi". For anything security-related — recon, scanning, pentesting a site — you call `recon`/`pentest` and report ONLY what they return; you never describe scans you didn't run.

Driving web apps with `browse`: to just OPEN a specific site (e.g. "open amrita.com", "go to nytimes.com"), use a single `open_app` action with that site as the `app` (a domain like "amrita.com") or a full `url` — do NOT open Google and search for it unless the user explicitly asked you to search. For app tasks like "open WhatsApp and message Roshan", "play X on Spotify", "post in #general on Slack" — `open_app` the app first, then `find_text` to click/type by visible label (search box → contact/track/channel → send). Prefer `find_text` over `click`/`type` with CSS selectors — visible-text matching survives redesigns. Use `wait_for_text` after navigation because these apps load asynchronously.

Windows desktop control (`desktop`): for anything system-shaped — "open my downloads folder", "open display settings", "open task manager", "uninstall Zoom" — use `desktop`, not `launch_app` or `run_command`. Actions: `open_path` (files/folders), `open_settings` (apps/display/network/…), `open_control_panel` (programs/network/sound/…), `open_registry` (regedit, optional key), `open_component` (task_manager/device_manager/services/event_viewer/…), `list_apps` + `uninstall_app` (winget). Uninstall pattern: call `uninstall_app` with the name first (dry-run, confirm defaults to false) → the tool returns the exact match with version → repeat back to the user and get their yes → call again with `confirm: true`. If the dry-run says 2+ packages matched, ask the user which one before proceeding. `list_apps` is safe to call whenever.

Parallel sub-agents (`spawn_agents`): use when the same request has INDEPENDENT sub-parts you can answer in parallel — "compare the top 3 laptops on price, battery, keyboard", "summarize what these five links say", "research pros vs cons vs pricing for X". Fire up to 5 sub-agents, each with its own focused prompt; they run at the same time with a read-only tool set and you get a combined result block to reason over. Do NOT use `spawn_agents` for a single question with one part — call the right tool directly. Do NOT use it as a wrapper around a single tool call. Sub-agents can't write memory, run shell, or drive the desktop; if the task needs those, do it yourself.

Confirm before irreversible actions when there's ANY ambiguity. If the user says "message Roshan" and the contact list surfaces one Roshan, send it. If two Roshans surface, STOP and ask which one — do not guess. Same for delete/purchase/publish actions: if you're certain of the target, act; if you're not, one short question first. Reading, searching, opening pages — no confirm needed."""


def _build_system_prompt(query: str = "") -> str:
    """Assemble the system message via cortex.

    Persona, ambient, homeostasis, and the overheard buffer stay owned by api.py
    (they see live device/user state) — cortex takes them via PromptHooks and
    injects semantically-recalled facts + episodes on top, with a token-budgeted
    trim (episodes drop first, then low-confidence facts). Persona/emotion never
    trim — they're what keep JARVIS himself.
    """
    nm = _user_name() or "the user"
    base = _BASE_PROMPT.replace("__USER__", nm)

    # An explicit, authoritative clock line so the model NEVER refuses "what's the time" — it
    # has this; the softer ambient line alone made it hedge ("I can't tell the time").
    _now = datetime.now().strftime("%A, %B %-d %Y, %-I:%M %p") if os.name != "nt" \
        else datetime.now().strftime("%A, %B %d %Y, %I:%M %p")
    time_line = (f"Right now it is {_now} (local, authoritative — you DO have the current date and "
                 f"time). Answer any date/time question directly from this; never say you can't.")
    try:
        ambient_frag = time_line + "\n" + (ambient.prompt_fragment() or "")
    except Exception:
        ambient_frag = time_line

    persona_block = ""
    if persona_mod.ENABLED:
        try:
            us = _last_read.user_state if _last_read else "neutral"
            gd = _last_read.guidance if _last_read else ""
            sup = bool(_last_read.suppress_sarcasm) if _last_read else False
            persona_block = _persona.style_block(nm, user_state=us, guidance=gd,
                                                 suppress_sarcasm=sup) or ""
        except Exception:
            persona_block = ""

    homeo_line = ""
    if _last_device:
        h = _homeostasis(_last_device)
        if h["energy"] <= 0.33:
            homeo_line = ("You're on battery and low on energy — keep replies "
                          "especially short and skip anything non-essential.")

    # Mirror persona's live PAD into the cortex emotion_state so the [Emotion] line
    # cortex writes stays in sync with what persona is doing this very turn.
    try:
        cortex.emotion.sync_from_persona(_persona)
    except Exception:
        pass

    hooks = cortex.PromptHooks(
        base_prompt=base,
        user_name=nm,
        ambient_fragment=ambient_frag,
        persona_block=persona_block,
        homeostasis_line=homeo_line,
        overheard=list(_overheard),
        namespace="personal",
        include_private=True,   # in-process JARVIS sees private facts; the HTTP hub does not
    )
    try:
        prompt = cortex.build_system_prompt(query, hooks)
    except Exception as exc:
        # Never let the memory layer fail a reply — degrade to persona + ambient only.
        log.warning("cortex.build_system_prompt failed: %s", exc)
        parts = [base]
        if ambient_frag:
            parts.append("\n" + ambient_frag)
        if persona_block:
            parts.append("\n" + persona_block)
        if homeo_line:
            parts.append("\n" + homeo_line)
        prompt = "\n".join(parts)

    # Advertise available skills (names + descriptions only). The full instructions
    # for a skill are pulled on demand via the use_skill tool — progressive disclosure.
    try:
        skill_cat = skills.catalog()
        if skill_cat:
            prompt = f"{prompt}\n\n{skill_cat}"
    except Exception as exc:
        log.warning("skills.catalog failed: %s", exc)
    return prompt


# ── Shared tool runner ──────────────────────────────────────────────────────────
async def _subagent_brain(messages: list[dict], tools: list[dict], max_tokens: int) -> dict:
    """Sub-agent brain call. Groq first (fast + real tool-call support), Ollama fallback.
    Skips Claude to avoid triple-implementing the tool-call schema — the parent turn can
    still be a Claude run; sub-agents just don't need the extra plumbing.

    Returns {"content": str, "tool_calls": [{"id", "name", "arguments"}]}."""
    if USE_GROQ and _HAS_GROQ:
        client = _openai().AsyncOpenAI(
            api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
        # Deliberately SUBAGENT_MODEL (small/fast), not GROQ_MODEL — see comment where it's
        # defined. Prevents a spawn_agents call from burning through the parent's quota.
        kwargs: dict = {"model": SUBAGENT_MODEL, "messages": messages, "max_tokens": max_tokens}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        r = await client.chat.completions.create(**kwargs)
        m = r.choices[0].message
        return {
            "content": m.content or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (m.tool_calls or [])
            ],
        }
    if _LOCAL_OK and LOCAL_FAST:
        import ollama
        client = ollama.AsyncClient()
        kwargs = {"model": LOCAL_FAST, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        r = await client.chat(**kwargs)
        m = r.message
        return {
            "content": m.content or "",
            "tool_calls": [
                {"id": f"call_{i}", "name": tc.function.name,
                 "arguments": (json.dumps(tc.function.arguments)
                               if isinstance(tc.function.arguments, dict)
                               else str(tc.function.arguments))}
                for i, tc in enumerate(m.tool_calls or [])
            ],
        }
    raise RuntimeError("no brain available for sub-agents (need GROQ_API_KEY or Ollama)")


async def _request_tool_approval(name: str, args: dict) -> bool:
    """Ask the connected UI to confirm a privileged tool. Returns True if approved.

    No live client → deny (safer than silently running shell). Selftests set
    JARVIS_SHELL_APPROVAL=0 so this path is skipped entirely.
    """
    if not SHELL_APPROVAL or name not in APPROVAL_TOOLS:
        return True
    if not active_connections:
        return False
    aid = uuid.uuid4().hex[:12]
    summary = _approval_summary(name, args)
    # Never ship full argv dumps that could include secrets — truncate + redact.
    safe_args = {k: (str(v)[:200] if not isinstance(v, (dict, list)) else str(v)[:200])
                 for k, v in (args or {}).items()}
    ev = asyncio.Event()
    box: list = [None]
    _pending_approvals[aid] = (ev, box)
    try:
        await broadcast({
            "type": "tool_approval",
            "id": aid,
            "tool": name,
            "summary": summary,
            "args": safe_args,
            "timeout_sec": _APPROVAL_TIMEOUT_SEC,
        })
        try:
            await asyncio.wait_for(ev.wait(), timeout=_APPROVAL_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return False
        return bool(box[0])
    finally:
        _pending_approvals.pop(aid, None)


def _approval_summary(name: str, args: dict) -> str:
    if name == "run_command":
        cmd = str(args.get("command") or "").strip()
        cwd = str(args.get("cwd") or "").strip()
        tail = f" (cwd {cwd})" if cwd else ""
        return f"Run shell command{tail}:\n{cmd[:400]}"
    return f"Allow tool `{name}` with args {json.dumps(args)[:300]}"


async def _run_tool(name: str, args: dict) -> str:
    global _filler_sent, _pending_content_panel
    # A slow tool means a real wait — bridge the dead air with a quick spoken
    # acknowledgment, once per turn. Fast tools answer too quickly to bother.
    if not _filler_sent and name in SLOW_TOOLS:
        _filler_sent = True
        asyncio.create_task(_schedule_speak(random.choice(FILLERS)))
    await broadcast({"type": "state", "status": "thinking", "text": f"Running {name}..."})
    my_gen = _turn_generation
    try:
        if name in APPROVAL_TOOLS and SHELL_APPROVAL:
            approved = await _request_tool_approval(name, args)
            if not approved:
                observation = (
                    f"Tool `{name}` was not approved — the operator declined or the "
                    "approval timed out. Do not retry the same privileged action unless "
                    "they explicitly ask again."
                )
                entry = {
                    "step": len(agent_trace) + 1,
                    "action": name,
                    "args": args,
                    "observation": observation,
                }
                agent_trace.append(entry)
                agent_trace[:] = agent_trace[-25:]
                await broadcast({"type": "agent_tool", "step": entry})
                await broadcast({"type": "tool_approval_resolved", "id": None, "approved": False, "tool": name})
                return observation
        if name == "spawn_agents":
            # Native async path — execute_tool is sync and can't await sub-agent gathering.
            # subagents.run_all is bounded (max 5 agents, 6 tool iterations each, 90s wall
            # clock), and the tool set it sees is the read-only subset only.
            results = await subagents.run_all(
                args.get("agents"), TOOLS, _subagent_brain, execute_tool)
            observation = subagents.format_results(results)
        else:
            observation = await asyncio.to_thread(execute_tool, name, args, my_gen)
    except Exception as exc:
        observation = f"Tool {name} failed: {exc}"
    if _pending_content_panel:
        panel = _pending_content_panel
        _pending_content_panel = None
        await _emit_content_panel(panel["title"], panel["body"])
    entry = {
        "step": len(agent_trace) + 1,
        "action": name,
        "args": args,
        "observation": observation,
    }
    agent_trace.append(entry)
    agent_trace[:] = agent_trace[-25:]
    await broadcast({"type": "agent_tool", "step": entry})
    return observation


async def _emit_final(text: str) -> None:
    await broadcast({"type": "llm_response", "text": text})
    asyncio.create_task(_schedule_speak(text))


# ── Groq agent loop (primary — gpt-oss-120b reasoning model, streaming) ────────
# ── Per-request tool subsetting ──────────────────────────────────────────────────
# Sending all ~25 tool schemas (~5.7k tokens) on every action request overruns Groq's
# free-tier per-request token budget (observed persistent HTTP 413s). So we always offer a
# small everyday CORE set and add a heavy group only when the request's intent matches it —
# this keeps a typical tool call around ~1.5k tool-tokens instead of ~5.7k.
_CORE_TOOLS = {
    "remember", "recall_memory", "search_web", "calculate", "get_weather",
    "get_system_info", "add_task", "complete_task", "cancel_task", "run_command", "launch_app",
    "close_app", "capture_screen", "use_skill", "create_skill",
}
_TOOL_GROUPS: list[tuple[re.Pattern, set[str]]] = [
    (re.compile(r"\b(desktop|click|type this|keyboard|mouse|window|gui|automate|drag|scroll)\b", re.I),
     {"desktop"}),
    (re.compile(r"\b(browse|website|web ?page|url|https?://|navigate|scrape|crawl|open the site|"
                r"deep[- ]?web|research|look up|investigate)\b", re.I),
     {"browse"}),
    (re.compile(r"\b(recon|pentest|pen[- ]?test|bug ?bounty|vuln\w*|exploit|cve|nmap|nikto|sqlmap|"
                r"gobuster|ffuf|nuclei|osint|subdomain|payload|scope|target)\b", re.I),
     {"recon", "pentest", "bugbounty", "report", "scope"}),
    (re.compile(r"\b(market|nifty|sensex|banknifty|stock|price|trading|\bict\b|chart|"
                r"market[- ]?brief)\b", re.I),
     {"ict_scan", "open_trading"}),
    (re.compile(r"\b(image|photo|picture|screenshot|video|watch this|analyze the (image|photo|video))\b", re.I),
     {"analyze_image", "watch_video"}),
    (re.compile(r"\b(deliberate|council|panel|debate|multiple agents|sub-?agents|spawn)\b", re.I),
     {"spawn_agents"}),
    (re.compile(r"\b(triage|diagnose|system (health|status|check)|why (is|am).*(slow|hot|loud))\b", re.I),
     {"get_system_info", "run_command"}),
]


def _relevant_tools(text: str) -> list[dict]:
    """The subset of TOOLS worth sending for this request: everyday core + any heavy group
    the text clearly needs. Falls back to the full set if nothing resolves (never empty)."""
    keep = set(_CORE_TOOLS)
    for rx, names in _TOOL_GROUPS:
        if rx.search(text or ""):
            keep |= names
    subset = [t for t in TOOLS if t["function"]["name"] in keep]
    return subset or TOOLS


async def _groq_round(client, messages: list[dict], allow_tools: bool,
                      model: str | None = None, tools: list[dict] | None = None):
    """One streaming round. Returns (full_text, tool_calls_raw dict).

    Groq's free tier caps *total* tokens/minute. The request itself (system + history + the
    tool schemas actually sent) plus the completion must fit — so the completion is sized to
    whatever budget is left after the input, never blowing the limit (an overflow 413s the call
    and used to surface as an empty answer).
    """
    model = model or GROQ_MODEL
    sent_tools = tools if tools is not None else TOOLS

    def _tok(s: str) -> int:
        return len(s) // 4 + 1
    input_tok = sum(_tok(str(m.get("content") or "")) for m in messages)
    if allow_tools:
        input_tok += sum(_tok(json.dumps(t)) for t in sent_tools)
    # Leave input + completion comfortably under the TPM ceiling (headroom for ~1.3x tokenizer
    # variance vs our 4-chars/token estimate). 1536 is plenty for a concise spoken answer or a
    # code snippet; the 384 floor keeps answers usable when the input is already large.
    max_out = max(384, min(1536, int(GROQ_TPM_CEILING * 0.92) - input_tok))
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_out,
        "stream": True,
    }
    if "gpt-oss" in model:
        kwargs["reasoning_effort"] = GROQ_REASONING
    if allow_tools:
        kwargs["tools"] = tools if tools is not None else TOOLS
        kwargs["tool_choice"] = "auto"

    stream = await client.chat.completions.create(**kwargs)

    full_text = ""
    reasoning = ""
    tool_calls_raw: dict[int, dict] = {}
    thinking_sent = False
    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            continue
        if getattr(delta, "reasoning", None):
            reasoning += delta.reasoning
            if not thinking_sent:
                thinking_sent = True
                await broadcast({"type": "state", "status": "thinking", "text": "Reasoning..."})
        if delta.content:
            full_text += delta.content
            await broadcast({"type": "llm_chunk", "text": delta.content})
        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_raw:
                    tool_calls_raw[idx] = {"id": "", "name": "", "arguments": ""}
                if tc.id:                 tool_calls_raw[idx]["id"]   = tc.id
                if tc.function.name:      tool_calls_raw[idx]["name"] = tc.function.name
                if tc.function.arguments: tool_calls_raw[idx]["arguments"] += tc.function.arguments
    # gpt-oss often leaves `content` empty and puts the answer in `reasoning`.
    # Dropping that makes a successful call look like a mute turn.
    if not full_text.strip() and reasoning.strip():
        full_text = reasoning.strip()
        await broadcast({"type": "llm_chunk", "text": full_text})
    return full_text, tool_calls_raw


def _salvage_text_toolcall(text: str):
    """gpt-oss occasionally emits a tool call as PLAIN TEXT instead of a native tool_call —
    e.g. it streams `{"action":"desktop","parameters":{...}}` into the answer, so the user
    sees raw JSON and nothing runs. Detect that shape and recover (name, args) so we can
    execute the real tool instead. Returns (name, args_dict) or None if it isn't one.

    Tolerant of the several shapes the model improvises: name under action/name/tool/function,
    args under parameters/arguments/args/input; and it may wrap the JSON in a ```json fence."""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("```"):                       # strip a ```json … ``` fence
        t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t).strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        obj = json.loads(t)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("action") or obj.get("tool") or obj.get("function")
    args = obj.get("parameters")
    if args is None: args = obj.get("arguments")
    if args is None: args = obj.get("args")
    if args is None: args = obj.get("input")
    if args is None: args = {}
    if isinstance(name, dict):                     # {"function":{"name":…,"arguments":…}}
        args = name.get("arguments", args)
        name = name.get("name")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(name, str) or name not in _TOOL_NAMES:
        return None
    if not isinstance(args, dict):
        args = {}
    return name, args


def _record_turn(user: str, assistant: str) -> None:
    """Keep a short rolling window of the conversation for multi-turn context, AND
    persist the exchange to cortex (episode + fire-and-forget extraction)."""
    global _last_activity
    _dialogue.append(user, assistant)
    _refresh_history_aliases()
    _last_activity = time.time()
    try:
        _event_bus.emit("MemoryUpdated", kind="dialogue", turn_seq=_dialogue.turn_seq)
    except Exception:
        pass
    # Cortex write: never blocks the reply, never raises. Runs on the event loop as a
    # scheduled task so extraction can await router calls.
    try:
        cortex.record_turn(user, assistant, source="chat", namespace="personal")
    except Exception as _exc:
        log.info("cortex.record_turn: %s", _exc)


def _remember_overheard(text: str) -> None:
    """Log an overheard utterance to the rolling ambient buffer so JARVIS can relate to
    it later, even when it wasn't addressed with the wake word. Persists every few
    utterances (not every one) to spare the disk. Echoes of JARVIS's own voice are
    skipped so it doesn't 'remember' itself."""
    global _overheard, _overheard_dirty
    if not STORE_OVERHEARD:
        return
    t = text.strip()
    if not t or _is_echo(t):
        return
    _overheard.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "text": t})
    if len(_overheard) > OVERHEARD_MAX:
        del _overheard[:-OVERHEARD_MAX]
    broadcast_from_thread({"type": "overheard", "text": t, "count": len(_overheard)})
    _overheard_dirty += 1
    if _overheard_dirty >= 5:
        _overheard_dirty = 0
        _save_json(OVERHEARD_FILE, _overheard)


def _groq_model_for(decision: dict, use_tools: bool) -> str:
    """Pick the Groq model by task complexity — this is the core of 'scale the model to
    the task'. Tool-calling and non-trivial asks get the strong 70B model; greetings and
    trivial chat get the cheap fast model. Neither is a reasoning model, so there's no
    hidden-token blowup and the effective free-tier limits are far higher."""
    if use_tools:
        return GROQ_MODEL                       # tool-calling wants the stronger model
    if float(decision.get("difficulty") or 0.0) < 0.20:
        return GROQ_FAST_MODEL                   # "hi", "thanks", "what time is it" -> cheap
    return GROQ_MODEL


async def _groq_auth_dead_end() -> str:
    """Every configured Groq key has been rejected (401/403). If a local model exists, return ""
    so the caller's escalation falls back to it — a dead cloud key must never leave JARVIS mute
    when Ollama is sitting right there. Otherwise tell the user exactly how to fix it."""
    if _LOCAL_OK and (LOCAL_DEEP or LOCAL_FAST):
        await broadcast({"type": "system", "text":
                         "Groq rejected the API key — using the local model. "
                         "Update the key in Settings or .env to bring cloud back."})
        return ""
    return ("Groq rejected the API key (401 Unauthorized) and no local model is available. "
            "Add a valid key in Settings (or .env) and restart.")


async def _groq_key_healthcheck() -> None:
    """Startup, off the boot path: probe every configured Groq key (free GET /models) and switch
    to a working one BEFORE the user's first message — so a stale key in .env or the app store is
    healed silently instead of failing the first turn. If none work, record an alert that every
    UI client sees on connect. Offline / rate-limited probes are 'unknown' and never mark a key bad."""
    global _groq_key_idx, GROQ_API_KEY, _groq_key_alert
    if not GROQ_API_KEYS:
        return
    statuses = await asyncio.to_thread(lambda: [_probe_groq_key(k) for k in GROQ_API_KEYS])
    for k, st in zip(GROQ_API_KEYS, statuses):
        if st == "invalid":
            _groq_bad_keys.add(k)
    if not _groq_bad_keys:
        return
    good = next((k for k, st in zip(GROQ_API_KEYS, statuses) if st == "valid"), None)
    if good and GROQ_API_KEY in _groq_bad_keys:
        _groq_key_idx, GROQ_API_KEY = GROQ_API_KEYS.index(good), good
        log.warning("Groq: the preferred key was rejected at startup — switched to a working key.")
        _groq_key_alert = ("A stale Groq API key was found (in .env or Settings) — JARVIS switched to "
                           "your working one. Remove the dead key to silence this.")
    elif good is None and all(st != "unknown" for st in statuses):
        log.warning("Groq: every configured API key was rejected at startup.")
        _groq_key_alert = ("Groq rejected every configured API key — the cloud brain is offline"
                           + (" (local model will answer)." if (_LOCAL_OK and (LOCAL_DEEP or LOCAL_FAST)) else ".")
                           + " Add a valid key in Settings or .env.")
    if _groq_key_alert:
        await broadcast({"type": "system", "text": _groq_key_alert})


async def _brain_groq(text: str, history: list[dict], *, decision: dict, device: dict) -> str:
    """Primary brain — streams text, runs tools, returns the final answer. The
    shared wrapper (_run_agent) handles emit, fillers, history, and recording."""
    client = _openai().AsyncOpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
        timeout=GROQ_TIMEOUT,
        max_retries=0,   # a 429 auto-retry blocks ~34s in silence; fail fast so the caller
                         #   below turns it into a spoken "hit the rate limit" instead of a hang
    )
    # System prompt + recent conversation + this turn = multi-turn context.
    messages: list[dict] = (
        [{"role": "system", "content": _build_system_prompt(text)}]
        + history
        + [{"role": "user", "content": text}]
    )

    # Force tools whenever the request clearly wants an action/live data — otherwise the
    # model can't act and (rightly forbidden from fabricating) returns nothing.
    use_tools = governor.agent_needs_tools(decision, device) or _needs_tools(text)
    model = _groq_model_for(decision, use_tools)
    tool_subset = _relevant_tools(text) if use_tools else None
    final_answer = ""
    keys_tried = 1   # rotate through additional keys on rate-limit, each at most once per turn
    swapped_model = False
    for _ in range(8):
        try:
            full_text, tool_calls_raw = await _groq_round(client, messages, allow_tools=use_tools,
                                                          model=model, tools=tool_subset)
        except _openai_mod.AuthenticationError:
            if _groq_key_rejected():          # remembers the bad key, moves to the next usable one
                client = _openai().AsyncOpenAI(
                    api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1",
                    timeout=GROQ_TIMEOUT, max_retries=0)
                continue
            return await _groq_auth_dead_end()
        except _openai_mod.RateLimitError:
            # This key is throttled/exhausted — swap to the next configured key and retry the
            # round. Only after every key has been tried do we give up on Groq.
            if keys_tried < len(GROQ_API_KEYS) and _rotate_groq_key("chat rate limit"):
                keys_tried += 1
                client = _openai().AsyncOpenAI(
                    api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1",
                    timeout=GROQ_TIMEOUT, max_retries=0)
                continue
            # Every Groq key is rate-limited. Return EMPTY (not a dead-end message) so the
            # caller's escalation falls back to the local Ollama brain — Groq being throttled
            # should never leave JARVIS mute when a local model is sitting right there.
            if _LOCAL_OK and (LOCAL_DEEP or LOCAL_FAST):
                await broadcast({"type": "system", "text": "Groq rate-limited — falling back to the local model."})
                return ""
            return ("I've hit Groq's rate limit on all configured keys, and no local model is "
                    "available to fall back to. Give me a few seconds and ask again.")
        except _openai_mod.APIError as exc:
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                if _groq_key_rejected():
                    client = _openai().AsyncOpenAI(
                        api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1",
                        timeout=GROQ_TIMEOUT, max_retries=0)
                    continue
                return await _groq_auth_dead_end()
            if status == 404:
                alt = _pick_served_groq_model(GROQ_API_KEY, avoid=model) if not swapped_model else ""
                if alt:
                    swapped_model = True
                    model = alt
                    await broadcast({"type": "system", "text": f"Configured Groq model is unavailable. Using {alt}."})
                    continue
                return (f"Groq does not serve `{model}` on this key. "
                        "Set GROQ_MODEL to an id from the account's model list. "
                        "The key itself was not rejected.")
            # Groq free tier often returns 413 for TPM (tokens/min), not 429 RateLimitError.
            if status in (413, 429):
                return ("Groq rate/token limit hit (HTTP "
                        f"{status}). Wait about a minute, say 'reset' to shrink context, "
                        "then try again.")
            # Malformed tool call rejected mid-stream — retry forcing a text answer;
            # prior tool results stay in context.
            try:
                full_text, tool_calls_raw = await _groq_round(client, messages, allow_tools=False, model=model)
            except _openai_mod.AuthenticationError:
                return ("Groq rejected the API key (401 Unauthorized). Update GROQ_API_KEY "
                        "and restart the backend.")
            except _openai_mod.APIError as exc2:
                status2 = getattr(exc2, "status_code", None)
                if status2 in (401, 403):
                    return ("Groq auth failed — the API key looks invalid or revoked. "
                            "Update GROQ_API_KEY and restart.")
                if status2 in (413, 429):
                    return ("Groq rate/token limit hit (HTTP "
                            f"{status2}). Wait about a minute, say 'reset' to shrink context, "
                            "then try again.")
                break

        if not tool_calls_raw:
            # gpt-oss sometimes DUMPS a tool call as text instead of calling it. If the "answer"
            # is really a tool-call JSON, execute it instead of showing the user raw JSON.
            salvaged = _salvage_text_toolcall(full_text) if use_tools else None
            if salvaged:
                name, args = salvaged
                await broadcast({"type": "llm_reset"})   # discard the JSON already streamed to the UI
                tc_id = f"salvage_{name}_{len(messages)}"
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [{"id": tc_id, "type": "function",
                                                 "function": {"name": name,
                                                              "arguments": json.dumps(args)}}]})
                obs = await _run_tool(name, args)
                messages.append({"role": "tool", "tool_call_id": tc_id, "content": obs})
                continue
            if full_text.strip():
                final_answer = full_text.strip()
            else:
                # Empty answer, no tools — retry once without tools.
                try:
                    retry_text, _ = await _groq_round(client, messages, allow_tools=False, model=model)
                except _openai_mod.APIError:
                    retry_text = ""
                final_answer = retry_text.strip()
            break

        messages.append({
            "role": "assistant",
            "content": full_text or None,
            "tool_calls": [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls_raw.values()
            ],
        })

        for tc in tool_calls_raw.values():
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):   # Groq sometimes streams 'null'
                args = {}
            obs = await _run_tool(tc["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": obs})

    return final_answer


# ── Claude agent loop ───────────────────────────────────────────────────────────
async def _brain_claude(text: str, history: list[dict], *, decision: dict, device: dict) -> str:
    client   = _anthropic()(api_key=ANTHROPIC_API_KEY)
    messages: list[dict] = list(history) + [{"role": "user", "content": text}]

    # Force tools whenever the request clearly wants an action/live data — otherwise the
    # model can't act and (rightly forbidden from fabricating) returns nothing.
    use_tools = governor.agent_needs_tools(decision, device) or _needs_tools(text)
    final_answer = ""
    for _ in range(8):
        req: dict = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "system": _build_system_prompt(text),
            "messages": messages,
        }
        if use_tools:
            req["tools"] = CLAUDE_TOOLS
        response = await client.messages.create(**req)

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    raw_in = block.input
                    tool_args = dict(raw_in) if isinstance(raw_in, dict) else {}
                    obs = await _run_tool(block.name, tool_args)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": obs,
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn (or any non-tool stop) — collect the text and finish.
        final_answer = " ".join(
            b.text.strip() for b in response.content
            if getattr(b, "type", "") == "text" and b.text.strip()
        ).strip()
        break

    return final_answer


# ── Ollama agent loop (fallback) ────────────────────────────────────────────────
async def _brain_ollama(
    text: str,
    history: list[dict],
    model: str,
    *,
    decision: dict,
    device: dict,
) -> str:
    import ollama
    if not model:
        raise RuntimeError("No local model selected — start Ollama or set OLLAMA_MODEL.")
    client   = ollama.AsyncClient()
    messages: list[dict] = (
        [{"role": "system", "content": _build_system_prompt(text)}]
        + history
        + [{"role": "user", "content": text}]
    )

    # Force tools whenever the request clearly wants an action/live data — otherwise the
    # model can't act and (rightly forbidden from fabricating) returns nothing.
    use_tools = governor.agent_needs_tools(decision, device) or _needs_tools(text)
    opts = _ollama_chat_options()
    if not use_tools:
        response = await client.chat(model=model, messages=messages, options=opts)
        return (response.message.content or "").strip()

    final_answer = ""
    for _ in range(8):
        response = await client.chat(model=model, messages=messages, tools=TOOLS, options=opts)
        msg = response.message

        if not msg.tool_calls:
            final_answer = (msg.content or "").strip()
            break

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            _raw_args = tc.function.arguments or {}
            tool_args: dict = dict(_raw_args) if isinstance(_raw_args, dict) else {}
            if isinstance(_raw_args, str):
                try:
                    parsed = json.loads(_raw_args)
                    tool_args = parsed if isinstance(parsed, dict) else {}
                except Exception:
                    tool_args = {}
            obs = await _run_tool(tc.function.name, tool_args)
            messages.append({"role": "tool", "content": obs})

    # Small local models often "fumble" tools: they emit an empty answer plus a spurious tool
    # call for a question that needed none (llama3.2:3b does this on plain Q&A). If the tool
    # loop yielded nothing usable, ask once more WITHOUT tools so the model just answers — far
    # better than handing the user an empty reply and dead-ending the whole fallback.
    if not final_answer:
        try:
            response = await client.chat(
                model=model,
                messages=[{"role": "system", "content": _build_system_prompt(text)}] + history
                         + [{"role": "user", "content": text}],
                options=opts,
            )
            final_answer = (response.message.content or "").strip()
        except Exception:
            pass

    return final_answer


# ── The Governor — compute-elastic routing across the escalation lattice ─────────
def _detect_local_models() -> None:
    """Pick the smallest & largest *allowed* tool-capable local models for the rungs."""
    global _LOCAL_OK, LOCAL_FAST, LOCAL_DEEP
    up, _ = models_advisor.ollama_up()
    if not up:
        _LOCAL_OK = False
        return
    dev = device.profile()
    inst = models_advisor.installed(with_caps=True)
    lookup = {m["name"]: m for m in inst}
    tool_models = []
    for m in inst:
        if not m.get("tools"):
            continue
        ok, _ = models_advisor.model_allowed(dev, m["name"], lookup, require_live=False)
        if ok:
            tool_models.append(m)
    if not tool_models:
        _LOCAL_OK = False
        return
    tool_models.sort(key=lambda m: m.get("gb") or 0)
    names = [m["name"] for m in tool_models]
    LOCAL_FAST, LOCAL_DEEP = names[0], names[-1]
    pin = (_settings.get("local_model") or OLLAMA_MODEL or "").strip()
    if pin:
        ok, _ = models_advisor.model_allowed(dev, pin, lookup)
        if not ok:
            _settings.pop("local_model", None)
            _save_settings()
            pin = ""
    if pin and pin in names:
        LOCAL_DEEP = pin
        if len(names) == 1:
            LOCAL_FAST = pin
    _LOCAL_OK = True


def _available_rungs() -> set[str]:
    s: set[str] = set()
    if USE_GROQ and _HAS_GROQ:
        s.update({"cloud_fast", "council"})
    if USE_CLAUDE and _HAS_ANTHROPIC:
        s.add("cloud_deep")
    if _LOCAL_OK:
        s.add("local_fast")
        if LOCAL_DEEP != LOCAL_FAST:
            s.add("local_deep")
    return s


def _homeostasis(dev: dict) -> dict:
    """The body's 'energy' state — drives model thrift, TTS pace, and persona."""
    energy = dev.get("headroom", 1.0)
    bat = dev.get("battery")
    if dev.get("power_state") == "battery" and bat:
        energy = min(energy, 0.3 + 0.5 * (bat.get("percent", 100) / 100.0))
    energy = round(max(0.05, min(1.0, energy)), 2)
    if energy > 0.66:   mood, label = "lively", "primed"
    elif energy > 0.33: mood, label = "steady", "conserving"
    else:               mood, label = "drowsy", "low-power"
    return {"energy": energy, "mood": mood, "label": label,
            "on_ac": dev.get("power_state") == "ac",
            "tts_rate": "+10%" if energy > 0.66 else "+6%" if energy > 0.33 else "+0%"}


def _device_brief(dev: dict) -> dict:
    return {"tier": dev.get("tier"), "power_state": dev.get("power_state"),
            "battery": dev.get("battery"), "headroom": dev.get("headroom"),
            "ram_available_gb": dev.get("ram_available_gb"), "cpu_percent": dev.get("cpu_percent")}


def _public_decision(d: dict) -> dict:
    """Decision minus the internal feature vector — for the UI."""
    return {k: d[k] for k in ("id", "rung", "label", "kind", "difficulty",
                              "factors", "lambda_eff", "rationale", "candidates") if k in d}


def _observe(decision: dict, latency: float, accepted: bool, escalated: bool = False) -> None:
    global _last_decision
    try:
        _gov.observe(decision, latency_s=latency, escalated=escalated, accepted=accepted)
        _save_json(GOVERNOR_FILE, _gov.to_dict())
    except Exception:
        pass
    _last_decision = decision


_REASK_RE = re.compile(r"\b(no,|that'?s wrong|try again|not what|rephrase|wrong answer|incorrect|do it again)\b", re.I)


async def _run_rung(rung: str, text: str, dev: dict, decision: dict) -> str:
    """Run one lattice rung and return its answer. `council` is handled separately in
    _run_agent (it emits + records itself)."""
    hist = _history_messages()
    if rung == "cloud_deep":
        return await _brain_claude(text, hist, decision=decision, device=dev)
    if rung == "cloud_fast":
        return await _brain_groq(text, hist, decision=decision, device=dev)
    if rung == "local_deep":
        return await _brain_ollama(text, hist, LOCAL_DEEP, decision=decision, device=dev)
    return await _brain_ollama(text, hist, LOCAL_FAST, decision=decision, device=dev)


def _fallback_rung(failed: str, avail: set[str], tried: set[str] | None = None) -> str | None:
    """The next rung to try when `failed` produced nothing: the best available alternative not
    already tried. Never auto-escalates into council (heavy + self-emitting).

    local_fast is ordered BEFORE local_deep among locals: on a RAM-tight machine the bigger
    model OOMs (Ollama 500), so the small model that actually fits should be the first local
    fallback — a cloud rate-limit shouldn't cascade into loading the largest Ollama model (a RAM
    spike) when the small one can answer."""
    tried = tried or set()
    for r in ("cloud_deep", "cloud_fast", "local_fast", "local_deep"):
        if r != failed and r in avail and r not in tried:
            return r
    return None


# Requests that plainly want an ACTION or live DATA — these must reach a brain that can call
# tools. The council (a toolless panel) and, in practice, local models that fumble tool-calls
# can only fabricate a result for these, which is the #1 source of "JARVIS hallucinated it".
_TOOL_INTENT_RE = re.compile(
    r"\b(recon|pentest|pen[- ]?test|bug ?bounty|sweep|scan|nmap|nikto|sqlmap|gobuster|ffuf|"
    r"nuclei|dalfox|katana|httpx|subfinder|takeover|osint|harvest|crawl|enumerate|"
    r"exploit|vuln\w*|port|subdomain|cve|scope|target|payload|"
    r"remember|recall|forget|memoriz|"
    r"open|launch|start|screenshot|capture|screen|"
    r"search|google|look up|browse|website|url|http|download|"
    r"weather|market|price|stock|nifty|sensex|"
    r"cpu|memory|ram|disk|processes|system info|uptime|"
    r"upload|read the file|pdf|"
    r"remind|reminder)\b",
    re.I,
)


def _needs_tools(text: str) -> bool:
    return bool(_TOOL_INTENT_RE.search(text or ""))


async def _run_agent(text: str) -> None:
    """Route the request through the Governor, then run the chosen rung. The Governor
    picks the cheapest brain that clears the difficulty bar within the current
    energy/latency budget, escalating only when the task is hard or the machine is
    healthy — then observes the outcome to adapt the policy to this machine."""
    global _last_device, _last_consolidated_turn

    if text.strip().lower().rstrip(".!") in RESET_PHRASES:
        _dialogue.clear()
        _refresh_history_aliases()
        _last_consolidated_turn = 0
        _session.reset_orchestration()
        _session.workflow = WorkflowPhase.IDLE
        await _emit_final("Done — clean slate. What's on your mind?")
        return

    # A re-ask is a negative signal on the previous routing choice (online learning).
    if _last_decision and _REASK_RE.search(text):
        _observe(_last_decision, latency=0.0, accepted=False, escalated=True)

    dev = await asyncio.to_thread(device.profile)
    _last_device = dev
    avail = _available_rungs()
    if not avail:
        await _emit_final("No brain is configured yet — add a Groq or Anthropic key in "
                          "Settings, or start Ollama for fully-local mode.")
        return

    cloud_rungs = {"cloud_fast", "cloud_deep", "council"}
    if not (avail & cloud_rungs) and (dev.get("ram_percent") or 0) >= 90:
        await broadcast({
            "type": "system",
            "text": "Memory is nearly full and only local models are available — "
                    "responses will be slow. Add a Groq key in Settings for cloud routing, "
                    "or free RAM / unload unused Ollama models.",
        })

    did = f"d{int(time.time() * 1000)}"
    decision = governor.decide(text, _history_messages(), dev, avail, _gov, did)
    if decision["rung"] not in avail:
        mode_hint = " Switch Governor mode to auto/cloud, or start Ollama for local."
        if _gov.mode == "local":
            mode_hint = " Local mode requires Ollama — start it and pull a model."
        await _emit_final(f"No brain available for {_gov.mode} mode.{mode_hint}")
        return

    tools_needed = _needs_tools(text)
    plan = plan_from_governor(decision, avail, tools_needed=tools_needed)
    decision = {**decision, "rung": plan.rung, "rationale": plan.rationale or decision.get("rationale")}
    _session.workflow = WorkflowPhase.THINKING
    _session.current_goal = (text or "")[:240] or None
    try:
        _event_bus.emit(
            "TaskCreated",
            decision_id=did,
            rung=plan.rung,
            provider=plan.provider,
            tools_needed=tools_needed,
        )
    except Exception:
        pass

    await broadcast({"type": "governor_decision",
                     "decision": _public_decision(decision),
                     "homeostasis": _homeostasis(dev), "device": _device_brief(dev)})

    rung = plan.rung
    t0 = time.time()

    if rung == "council":
        try:
            await _deliberate(text)                 # emits + records itself
            _observe(decision, time.time() - t0, accepted=True)
            _router_health().mark("council", ok=True, latency_s=time.time() - t0)
            _telemetry.record_turn(
                decision_id=did, rung=rung, provider="council",
                latency_s=time.time() - t0, ok=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _emit_final(f"The council couldn't convene ({exc}). Try again, or switch mode in Settings.")
            _observe(decision, time.time() - t0, accepted=False)
            _router_health().mark("council", ok=False, latency_s=time.time() - t0)
            _telemetry.record_turn(
                decision_id=did, rung=rung, provider="council",
                latency_s=time.time() - t0, ok=False,
            )
        finally:
            _session.workflow = WorkflowPhase.IDLE
        return

    _session.workflow = WorkflowPhase.ACTING
    answer = ""
    try:
        answer = (await _run_rung(rung, text, dev, decision) or "").strip()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logging.getLogger("jarvis").warning("rung %s failed: %s: %s", rung, type(exc).__name__, exc)
        answer = ""

    # Escalation-on-failure: prefer health-ordered fallbacks from the router plan.
    escalated = False
    if not answer:
        # Walk DOWN the lattice through every remaining rung until one answers — not just one
        # hop. A single fallback dead-ends when e.g. cloud is rate-limited AND the big local
        # model OOMs; we must keep going to the small local model that actually fits. The
        # planner's health-ordered fallbacks go first, then the static order.
        tried = {rung}
        while not answer:
            fb = (next((r for r in plan.fallbacks if r in avail and r not in tried), None)
                  or _fallback_rung(rung, avail, tried))
            if not fb:
                break
            tried.add(fb)
            escalated = True
            await broadcast({"type": "system",
                             "text": f"Escalating to {governor.RUNG_BY_ID.get(fb, {}).get('label', fb)}…"})
            try:
                answer = (await _run_rung(fb, text, dev, decision) or "").strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger("jarvis").warning(
                    "fallback rung %s failed: %s: %s", fb, type(exc).__name__, exc)
                answer = ""
            if answer:
                rung = fb                            # the rung that actually answered

    latency = time.time() - t0
    provider = provider_for_rung(rung)
    if not answer:
        await _emit_final("I couldn't get a usable response that time — try rephrasing, "
                          "or wait a moment if the model is busy.")
        _observe(decision, latency, accepted=False, escalated=escalated)
        _router_health().mark(provider, ok=False, latency_s=latency)
        _telemetry.record_turn(
            decision_id=did, rung=rung, provider=provider,
            latency_s=latency, ok=False, escalated=escalated,
            prompt_chars=len(text or ""),
        )
        try:
            _event_bus.emit("LLMResponded", ok=False, rung=rung, provider=provider)
        except Exception:
            pass
        _session.workflow = WorkflowPhase.IDLE
        return

    await _emit_final(answer)
    _record_turn(text, answer)
    # Credit the chosen rung only if IT answered; if we had to escalate, mark it escalated so
    # the bandit learns this rung was inadequate for this kind of request.
    _observe(decision, latency, accepted=not escalated, escalated=escalated)
    _router_health().mark(provider, ok=True, latency_s=latency)
    _telemetry.record_turn(
        decision_id=did, rung=rung, provider=provider,
        latency_s=latency, ok=True, escalated=escalated,
        prompt_chars=len(text or ""), answer_chars=len(answer or ""),
    )
    try:
        _event_bus.emit(
            "LLMResponded", ok=True, rung=rung, provider=provider, latency_s=latency,
        )
        _event_bus.emit("TaskCompleted", decision_id=did, rung=rung)
    except Exception:
        pass
    _session.workflow = WorkflowPhase.IDLE

    if rung in ("local_fast", "local_deep") and (dev.get("ram_percent") or 0) >= OLLAMA_RELEASE_RAM_PCT:
        used = LOCAL_DEEP if rung == "local_deep" else LOCAL_FAST
        asyncio.create_task(_ollama_release(used))
        asyncio.create_task(_broadcast_models_loaded())


# ── Sleep / consolidation + model management ─────────────────────────────────────
async def _run_sleep_cycle() -> None:
    """One consolidation cycle — cortex.dreaming compresses today's raw episodes
    into a durable narrative + facts + prospective items. The heavy LLM call goes
    through cortex.router (task_type='consolidation'), which prefers a long-context
    brain (Groq gpt-oss-120b > Claude > local deep)."""
    global _last_consolidated_turn, _sleeping
    if _sleeping:
        return
    _sleeping = True
    await broadcast({"type": "sleep", "state": "start", "text": "Consolidating memory…"})
    try:
        result = await cortex.dreaming.run_once()
        # Only advance the gate on a real success — skipped/empty/failed cycles must
        # remain eligible so the next idle window can retry (not wait another 6 turns).
        status = result.get("status")
        if status == "ok":
            _last_consolidated_turn = _turn_seq
        text = ("nothing new to learn" if status != "ok"
                else f"consolidated · +{result.get('facts_added', 0)} facts · "
                     f"+{result.get('prospective_added', 0)} pending items")
        st = cortex.stats()
        await broadcast({"type": "sleep", "state": "done", "text": text,
                         "memory_count": st.get("facts", 0)})
    except Exception as exc:
        log.info("dreaming failed: %s", exc)
        await broadcast({"type": "sleep", "state": "done", "text": "rest interrupted"})
    finally:
        _sleeping = False


async def _ambient_loop() -> None:
    """Keep the ambient snapshot (location, weather) warm in the background."""
    while True:
        try:
            await asyncio.to_thread(ambient.refresh)
        except Exception as exc:
            log.debug("ambient refresh failed (offline?): %s", exc)
        await asyncio.sleep(int(os.environ.get("JARVIS_AMBIENT_REFRESH_SEC", "900")))


async def _sleep_loop() -> None:
    """Idle + on AC → consolidate. The cheap gate makes a misfire nearly free."""
    while True:
        try:
            await asyncio.sleep(30)
            idle_min = (time.time() - _last_activity) / 60.0
            on_ac = (_last_device or {}).get("power_state", "ac") == "ac"
            busy = _tts_playing or bool(_current_task and not _current_task.done())
            # Cheap gate: at least 6 new turns since the last consolidation, and 4 total.
            enough_new = (_turn_seq - _last_consolidated_turn) >= 6 and _turn_seq >= 4
            if (idle_min >= IDLE_SLEEP_MIN and on_ac and not busy and not _sleeping
                    and enough_new):
                await _run_sleep_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sleep/consolidation cycle error: %s", exc)


# ── proactive silence-break ─────────────────────────────────────────────────────
# After N minutes of user inactivity (env JARVIS_PROACTIVE_IDLE_MIN, default 15),
# JARVIS optionally offers ONE useful line — something to do next given cortex facts
# + any pending prospective items. Off by default (JARVIS_PROACTIVE=1 to enable) so
# nobody accidentally has an assistant talking to itself.
#
# Guardrails: never mid-conversation, never mid-TTS, never more than once per
# JARVIS_PROACTIVE_COOLDOWN_MIN (default 45), never on battery, never when muted.
# The suggestion is generated by the same brain path as a normal reply, so it
# inherits persona/emotion/memory naturally; TTS uses the existing speak path.
PROACTIVE_ENABLED = os.environ.get("JARVIS_PROACTIVE", "0") == "1"
PROACTIVE_IDLE_MIN = int(os.environ.get("JARVIS_PROACTIVE_IDLE_MIN", "15"))
PROACTIVE_COOLDOWN_MIN = int(os.environ.get("JARVIS_PROACTIVE_COOLDOWN_MIN", "45"))


async def _proactive_loop() -> None:
    """Nudge the user with ONE useful line when they've been quiet a while."""
    global _last_proactive
    if not PROACTIVE_ENABLED:
        return
    log.info("proactive silence-break enabled — idle=%dmin, cooldown=%dmin",
             PROACTIVE_IDLE_MIN, PROACTIVE_COOLDOWN_MIN)
    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            idle_min = (now - _last_activity) / 60.0
            since_last = (now - _last_proactive) / 60.0
            on_ac = (_last_device or {}).get("power_state", "ac") == "ac"
            busy = _tts_playing or bool(_current_task and not _current_task.done())
            if not (idle_min >= PROACTIVE_IDLE_MIN and since_last >= PROACTIVE_COOLDOWN_MIN
                    and on_ac and not busy and not _sleeping):
                continue
            # Pull a bit of context so the suggestion isn't disembodied.
            try:
                pending = cortex.store.pending_prospective(limit=3) or []
            except Exception:
                pending = []
            pending_lines = "; ".join(p.get("description", "") for p in pending) or "(none)"
            prompt = (
                "The user hasn't said anything in a while. Speak ONE short line that's "
                "genuinely useful — no small talk, no 'how can I help', no self-reference. "
                "If there's a pending item, mention it. If nothing obvious, silence is "
                "better than filler — reply with the literal text SKIP to say nothing.\n"
                f"pending items: {pending_lines}"
            )
            try:
                # Reuse the cortex router (small/fast model, no tools, JSON off).
                line = (await cortex.router.route("reflection", prompt) or "").strip()
            except Exception as exc:
                log.info("proactive: brain call skipped (%s)", exc)
                continue
            if not line or line.upper().startswith("SKIP"):
                continue
            # One line, cap length.
            line = line.splitlines()[0].strip()[:220]
            _last_proactive = now
            log.info("proactive: %r", line)
            await _emit_final(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("proactive loop error: %s", exc)


async def _pull_model(model: str) -> None:
    if not model:
        return
    dev = await asyncio.to_thread(device.profile)
    ok, reason, kind = await asyncio.to_thread(models_advisor.pull_precheck, dev, model)
    if not ok:
        await broadcast({"type": "model_pull", "model": model, "status": "error",
                         "pct": 0, "error": reason})
        await broadcast({"type": "system", "text": reason})
        return
    if kind == "custom" and reason:
        await broadcast({"type": "system", "text": reason})
    await broadcast({"type": "model_pull", "model": model, "status": "starting", "pct": 0})

    def _cb(p):
        broadcast_from_thread({"type": "model_pull", "model": model,
                               "status": p.get("status"), "pct": p.get("pct")})

    res = await asyncio.to_thread(models_advisor.pull, model, _cb)
    await broadcast({"type": "model_pull", "model": model,
                     "status": "done" if res.get("ok") else "error",
                     "pct": 100, "error": res.get("error")})
    await asyncio.to_thread(_detect_local_models)
    await _broadcast_models_loaded()


async def _benchmark_model(model: str) -> None:
    if not model:
        return
    await broadcast({"type": "model_bench", "model": model, "status": "running"})
    res = await asyncio.to_thread(models_advisor.benchmark, model)
    await broadcast({"type": "model_bench", "status": "done", **res})
    await _broadcast_models_loaded()


async def _delete_model(model: str) -> None:
    """Remove an installed local model from disk, then re-detect the local rungs."""
    if not model:
        return
    # Don't let the user delete the model the Governor is mid-thought on; clear a pin
    # that points at it so detection doesn't try to re-select a now-missing model.
    if _settings.get("local_model") == model:
        _settings.pop("local_model", None)
        _save_settings()
    res = await asyncio.to_thread(models_advisor.remove, model)
    await asyncio.to_thread(_detect_local_models)
    await broadcast({"type": "model_delete", "model": model,
                     "ok": bool(res.get("ok")), "error": res.get("error"),
                     "active": {"fast": LOCAL_FAST, "deep": LOCAL_DEEP, "enabled": _LOCAL_OK}})
    await _broadcast_models_loaded()


async def _set_local_model(model: str) -> None:
    """Pin which installed model JARVIS uses as its local quality rung."""
    if not model:
        return
    dev = await asyncio.to_thread(device.profile)
    inst = await asyncio.to_thread(models_advisor.installed, True)
    lookup = {m["name"]: m for m in inst}
    installed = set(lookup.keys())
    resolved = models_advisor.resolve_installed(model, installed) or model
    ok = resolved in installed
    reason = ""
    if ok:
        ok, reason = await asyncio.to_thread(models_advisor.model_allowed, dev, resolved, lookup)
    else:
        reason = f"{model} isn't installed."
    if ok:
        _settings["local_model"] = resolved
        _save_settings()
        models_advisor.invalidate_install_cache()
        await asyncio.to_thread(_detect_local_models)
    else:
        await broadcast({"type": "system", "text": reason or f"Can't use {model} on this device."})
    await broadcast({"type": "local_model_set", "model": resolved if ok else model, "ok": ok,
                     "error": reason or None,
                     "pinned": _settings.get("local_model"),
                     "active": {"fast": LOCAL_FAST, "deep": LOCAL_DEEP, "enabled": _LOCAL_OK}})


def _forget_memory(mid) -> None:
    """Delete one memory. `mid` is a cortex fact id (UUID) since /api/memory now reads from
    cortex; the legacy JSON mirror is also purged as a harmless fallback (a no-op if the id
    doesn't match anything there, e.g. every id post-migration is a cortex UUID)."""
    global memories
    changed = False
    try:
        changed = cortex.forget(str(mid))
    except Exception as exc:
        log.info("cortex.forget failed for %s: %s", mid, exc)
    with _mem_lock:
        before = len(memories)
        memories[:] = [m for m in memories if str(m.get("id")) != str(mid)]
        if len(memories) != before:
            changed = True
            _save_memory(memories)
    if changed:
        broadcast_from_thread({"type": "memory_update", "count": len(memories)})


# ── Wake word + barge-in ─────────────────────────────────────────────────────────
_WAKE_FILLERS = ("hey there ", "hey ", "hello ", "hi ", "hiya ", "ok ", "okay ", "yo ",
                 "um ", "uh ", "so ", "well ", "a ", "and ", "hmm ", "hm ", "yeah ", "hey, ")


def _wake_fuzzy(token: str) -> bool:
    """True if `token` is a close mis-hearing of the wake word. Tiny Whisper routinely swaps or
    drops a letter in 'jarvis' (jervis, jarvus, jarvix, jaravis…). The 0.72 ratio is tuned to
    accept those while rejecting real words that merely rhyme — 'travis' (~0.67) and 'service'
    (~0.62) do NOT trigger. Universal: it's phonetic-ish string distance, not per-mic tuning."""
    from difflib import SequenceMatcher
    tok = (token or "").strip(" ,.!?:;-'\"").lower()
    if len(tok) < 3:
        return False
    for w in WAKE_WORDS:
        if tok.startswith(w):
            return True
    return SequenceMatcher(None, tok, "jarvis").ratio() >= 0.72


def _match_wake_word(text: str):
    """Return the command after the wake word, '' if only the wake word was said, or None if the
    utterance isn't addressed to Jarvis. Tolerant by design so a real 'hey Jarvis' lands reliably:
    strips leading fillers, then looks for the wake word (exact, prefix, or a fuzzy mis-hear) in
    the FIRST TWO tokens — so a garbled leading word or a Whisper mis-spelling still fires — while
    still ignoring ordinary speech that merely mentions a similar word later on."""
    t = text.lower().strip().lstrip("\"'.,!?;:- ")
    changed = True
    while changed:
        changed = False
        for filler in _WAKE_FILLERS:
            if t.startswith(filler):
                t = t[len(filler):].lstrip()
                changed = True
                break
    if not t:
        return None
    tokens = t.split()
    for idx in range(min(2, len(tokens))):          # wake word in the first one or two tokens
        tok = tokens[idx]
        hit = False
        for w in WAKE_WORDS:
            if tok.startswith(w):
                nxt = tok[len(w):len(w) + 1]
                if nxt == "" or not nxt.isalnum():   # word boundary
                    hit = True
                    break
        if not hit and _wake_fuzzy(tok):
            hit = True
        if hit:
            # Keep '?' — it is the difference between a question and a statement for the model.
            # Other trailing punctuation is Whisper junk and is stripped.
            return " ".join(tokens[idx + 1:]).strip(" ,.!:;-'\"")
    return None


def _is_echo(cmd: str) -> bool:
    """True if cmd is mostly contained in what JARVIS is currently saying — i.e. the
    mic picked up JARVIS's own voice rather than the user."""
    sp = _speaking_text
    if not sp:
        return False
    words = [w for w in re.findall(r"[a-z']+", cmd.lower()) if len(w) > 2]
    if not words:
        return False
    hits = sum(1 for w in words if w in sp)
    return hits / len(words) >= 0.6


_wake_ack_cache: dict = {}   # (phrase, voice) -> base64 mp3, so the ack plays with no TTS delay


async def _prewarm_wake_acks() -> None:
    """Pre-synthesize the wake-ack phrases once so 'Yes, sir.' after 'Jarvis' is instant —
    no edge-tts network round-trip on the critical path. Re-runs cheaply if the voice changes."""
    try:
        import edge_tts
    except Exception:
        return
    rate, pitch = _voice_params(TTS_RATE)
    for phrase in WAKE_ACKS:
        key = (phrase, _tts_voice)
        if key in _wake_ack_cache:
            continue
        try:
            audio = b""
            async for chunk in edge_tts.Communicate(phrase, _tts_voice, rate=rate, pitch=pitch).stream():
                if chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if isinstance(data, (bytes, bytearray)):
                        audio += bytes(data)
            if audio:
                _wake_ack_cache[key] = base64.b64encode(audio).decode()
        except Exception:
            pass


def _acoustic_wake_fire() -> None:
    """Called from the capture thread when the openWakeWord detector hears 'hey jarvis':
    arm the command window and play the ack — WITHOUT Whisper, so the wake is near-instant.
    The following utterance is transcribed and taken as the command via the _awake_until
    window (same path as a text-matched bare wake)."""
    global _awake_until
    if time.time() - _last_ack_at < 1.5:
        return   # already acknowledged a wake a moment ago — don't double-fire
    _awake_until = time.time() + WAKE_WINDOW
    broadcast_from_thread({"type": "voice", "state": "wake"})
    if _main_loop and not _main_loop.is_closed():
        asyncio.run_coroutine_threadsafe(_wake_ack(), _main_loop)


async def _wake_ack() -> None:
    """Heard a bare 'jarvis' — acknowledge and open the command window. Any speech mid-
    reply is barged in on (the ack replaces it), so this doubles as an interrupt."""
    global _speaking_text, _tts_playing, _last_ack_at
    _last_ack_at = time.time()
    await broadcast({"type": "state", "status": "listening", "text": "Yes? I'm listening…"})
    phrase = random.choice(WAKE_ACKS)
    cached = _wake_ack_cache.get((phrase, _tts_voice))
    if cached:
        # Instant: replay the pre-synthesized clip (no edge-tts round-trip).
        _speaking_text = re.sub(r"[*_`#\[\]()]", "", phrase).strip().lower()
        await broadcast({"type": "state", "status": "speaking", "text": "Speaking..."})
        _tts_playing = True                         # mute the mic through the ack (base64 ≈ bytes×0.75)
        _arm_tts_failsafe(len(cached) * 0.75 / 6000.0)
        await broadcast({"type": "tts_audio", "data": cached, "seq": 0, "final": True})
        await broadcast({"type": "state", "status": "idle"})
    else:
        await _schedule_speak(phrase)
        asyncio.create_task(_prewarm_wake_acks())   # warm the cache for next time


async def _stop_speaking() -> None:
    """Cancel any in-flight response + TTS and tell the frontend to stop audio."""
    global _current_task, _speak_task, _speaking_text, _tts_playing, _tts_ended_at
    if _speak_task and not _speak_task.done():
        _speak_task.cancel()
        try:
            await _speak_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    if _current_task and not _current_task.done():
        _current_task.cancel()
    _speaking_text = ""
    # Clear mute immediately — don't wait for a client tts_end that may never arrive
    # (disconnect, barge-in race). Frontend still gets tts_stop to halt local audio.
    _tts_playing = False
    _tts_ended_at = time.time()
    await broadcast({"type": "tts_stop"})
    await broadcast({"type": "state", "status": "idle"})


# ── Fast path: deterministic simple-command execution (no reasoning model) ────────
def _lock_workstation() -> str:
    """Lock the session. No admin needed; nothing to verify afterwards (we can't read the
    lock state), so we report the action honestly as issued, not as confirmed."""
    if os.name != "nt":
        return "Locking the screen is only supported on Windows."
    try:
        rc = subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"]).returncode
        return "Locking now." if rc == 0 else "I tried to lock the screen but the command failed."
    except Exception as exc:
        return f"I couldn't lock the screen: {exc}"


def _save_screenshot() -> str:
    """Capture the primary screen to a PNG and verify the file was written. Deterministic —
    no vision model (that's `capture_screen`); this just saves the image and proves it exists."""
    try:
        import mss
        from PIL import Image
    except ImportError:
        return "Screenshot needs the vision extras — run: pip install mss Pillow."
    try:
        out_dir = Path(os.path.expanduser("~")) / "Pictures" / "JARVIS"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX").save(str(path))
        if path.exists() and path.stat().st_size > 0:
            return f"Screenshot saved to {path}."
        return "I took the screenshot but couldn't confirm the file was written."
    except Exception as exc:
        return f"Screenshot failed: {exc}"


def _fast_execute(intent: "fastpath.FastIntent") -> tuple[str, bool]:
    """Run a fast-path intent and return (spoken_reply, handled). handled=False means we
    couldn't honour it deterministically (e.g. an unknown app) and the caller should fall
    through to the full agent. Runs in a worker thread (every call here is blocking I/O).
    Every reply is built from what actually happened — never an assumed success."""
    kind = intent.kind
    p = intent.params

    if kind == "OPEN_APP":
        app = p["app"]
        cmd = _LAUNCH_ALLOWLIST.get(app)
        if cmd:
            return _launch_resolved(app, cmd), True
        resolved = _resolve_any_app(app)
        if resolved:
            name, exe = resolved
            return _launch_resolved(name, exe), True
        if _looks_like_site(app):
            return "", False   # "open gmail" / "open nytimes.com" → let the agent browse to it
        # Not an installed app and not a site — answer HONESTLY rather than falling through to
        # the model, which may fabricate "it's open" for an app that doesn't exist.
        return (f"I couldn't find an app called {app} on this PC — it may not be installed. "
                f"If it's a website, say \"open {app} dot com\"."), True

    if kind == "CLOSE_APP":
        app = p["app"]
        if app in _CLOSE_PROCESS:
            return _close_app(app), True
        proc = _match_running_process(app)
        if proc:
            res = verify.terminate(proc, timeout=5.0)
            return (f"Closed {app}." if res.ok else f"I couldn't close {app} — {res.detail}."), True
        return f"{app} doesn't appear to be running, so there's nothing to close.", True

    if kind == "VOLUME":
        d = p["direction"]
        r = desktop.run("system_volume", {"action": "system_volume", "direction": d,
                                          "level": p.get("level")})
        if "fail" in r.lower():
            return f"I couldn't change the volume — {r}", True
        if d == "mute":
            return "Muted.", True
        if d == "unmute":
            return "Unmuted.", True
        if d == "set":
            return f"Volume set to {p.get('level')}%.", True
        return (f"Volume {d}."), True

    if kind == "BRIGHTNESS":
        d = p["direction"]
        r = desktop.run("brightness", {"action": "brightness", "direction": d,
                                       "level": p.get("level")})
        if "fail" in r.lower():
            return (f"I couldn't change the brightness — this usually only works on a laptop's "
                    f"built-in display."), True
        if d == "set":
            return f"Brightness set to {p.get('level')}%.", True
        return f"Brightness {d}.", True

    if kind == "MEDIA":
        key = p["key"]
        r = desktop.key_press(key)
        if "fail" in r.lower() or "unknown" in r.lower() or "unavailable" in r.lower():
            return f"I couldn't send the media key — {r}", True
        label = {"playpause": "Play/pause.", "nexttrack": "Next track.",
                 "prevtrack": "Previous track."}.get(key, "Done.")
        return label, True

    if kind == "WIFI":
        state = p["state"]
        r = desktop.run("toggle_wifi", {"action": "toggle_wifi", "state": state})
        if "fail" in r.lower() or "error" in r.lower() or "admin" in r.lower():
            return f"I couldn't turn Wi-Fi {state} — {r}", True
        return f"Wi-Fi {state}.", True

    if kind == "LOCK":
        return _lock_workstation(), True

    if kind == "SCREENSHOT":
        return _save_screenshot(), True

    if kind == "TASK_ADD":
        task = _tasks.create(p["text"])
        return f"Added it — that's {task['tid']}.", True

    if kind == "TASK_DONE":
        it = _tasks.complete(p["n"])
        return (f"{it['tid']} marked done." if it else f"I don't see a task number {p['n']}."), True

    if kind == "TASK_CANCEL":
        it = _tasks.cancel(p["n"])
        return (f"{it['tid']} cancelled." if it else f"I don't see a task number {p['n']}."), True

    return "", False


async def _run_fastpath(user_text: str, intent: "fastpath.FastIntent",
                        trace: "TurnTrace | None") -> bool:
    """Execute a fast-path intent off the reasoning model: verify, then speak a concise,
    honest reply. Returns True if it handled the command, False to fall through to the agent.
    Cancellable like any turn — a barge-in cancels the awaited thread call."""
    if trace:
        trace.path = "fast"
        trace.mark("route")
    await broadcast({"type": "state", "status": "thinking", "text": "On it…"})
    try:
        reply, handled = await asyncio.to_thread(_fast_execute, intent)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("fastpath %s failed: %s", intent.kind, exc)
        return False   # fall through; let the agent try
    if trace:
        trace.mark("exec")
    if not handled:
        return False
    # Announce the tool only now that we know we handled it (so a fall-through to the agent
    # doesn't leave a phantom fastpath step in the turn's tool list).
    await broadcast({"type": "agent_tool", "step": {"action": f"fastpath:{intent.kind.lower()}"}})
    await _emit_final(reply)
    _record_turn(user_text, reply)
    if trace:
        trace.mark("respond")
        await trace.emit("ok", extra={"intent": intent.kind, "reply": reply})
    await broadcast({"type": "state", "status": "idle"})
    return True


async def dispatch_command(text: str, *, source: str = "typed",
                           wake_ms: float | None = None, stt_ms: float | None = None) -> None:
    """Entry point for every command (voice or typed). Barges in on whatever is
    currently running — thinking OR speaking — so a new directive takes over."""
    global _current_task, _turn_generation, _turn_trace
    busy = (
        (_current_task and not _current_task.done())
        or (_speak_task and not _speak_task.done())
        or _tts_playing
    )
    if busy:
        await _stop_speaking()
    _turn_generation += 1
    _turn_trace = TurnTrace(text, source, wake_ms=wake_ms, stt_ms=stt_ms)
    _current_task = asyncio.create_task(handle_command(text))


async def ask_and_wait(text: str, *, speak: bool = True, timeout: float = 120.0) -> dict:
    """Run one turn exactly like a typed/voice command, but wait for it and return what
    JARVIS said — for callers outside the HUD (POST /api/ask → OpenClaw MCP bridge).

    Same path as every other command (dispatch_command: barge-in, turn generation,
    governor, tools, memory writes), so remote turns behave identically and show up in
    the HUD. Returns {status, reply, tools, elapsed_s}, where status is
      ok          the turn finished
      timeout     still running after `timeout` s (left running; reply may be partial)
      interrupted a newer command barged in and cancelled this turn
    """
    q: asyncio.Queue = asyncio.Queue()
    _reply_listeners.append(q)
    token = _SPEAK_REPLY.set(bool(speak))
    t0 = time.time()
    status = "ok"
    try:
        await dispatch_command(text, source="remote")
        task = _current_task      # the turn dispatch_command just started
        if task is not None:
            try:
                # shield: a timeout stops our wait, not JARVIS's turn.
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except asyncio.TimeoutError:
                status = "timeout"
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise         # our own request was cancelled, not the turn
                status = "interrupted"
    finally:
        _SPEAK_REPLY.reset(token)
        _reply_listeners.remove(q)
    replies: list[str] = []
    tools: list[str] = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.get("type") == "llm_response":
            replies.append(str(ev.get("text") or ""))
        elif ev.get("type") == "agent_tool":
            tools.append(str((ev.get("step") or {}).get("action") or ""))
    return {
        "status": status,
        "reply": "\n".join(r for r in replies if r).strip(),
        "tools": [t for t in tools if t],
        "elapsed_s": round(time.time() - t0, 2),
    }


# ── Mixture-of-Agents (multi-agent deliberation) ────────────────────────────────
def _short_model(m: str) -> str:
    return m.split("/")[-1].replace("-instruct", "").replace("-versatile", "")


async def _deliberate(question: str) -> None:
    """A panel of different models each give their take, then an aggregator
    reconciles them into one decision. Streams each voice to the UI."""
    if not (USE_GROQ and _HAS_GROQ):
        await _emit_final("Multi-agent deliberation needs the Groq backend.")
        return

    client = _openai().AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
    await broadcast({"type": "council_start", "question": question,
                     "panel": [_short_model(m) for m in MOA_PROPOSERS]})
    await broadcast({"type": "state", "status": "thinking", "text": "Convening the panel..."})

    advisor_sys = ("You are one advisor on a panel weighing a question. Give YOUR own "
                   "honest, reasoned take — your analysis and a clear recommendation in "
                   "2-4 sentences. Don't hedge; the chair will reconcile disagreements.")

    async def ask(model: str):
        try:
            r = await client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": advisor_sys},
                          {"role": "user", "content": question}],
                max_tokens=600,
            )
            text = (r.choices[0].message.content or "").strip()
            # Some models (qwen3) emit <think>…</think> chain-of-thought — drop it.
            if "</think>" in text:
                text = text.split("</think>")[-1].strip()
            text = re.sub(r"</?think>", "", text).strip()
        except Exception as exc:
            text = f"(stood down — {exc})"
        await broadcast({"type": "council_proposal", "model": _short_model(model), "text": text})
        return model, text

    proposals = await asyncio.gather(*[ask(m) for m in MOA_PROPOSERS])

    panel = "\n\n".join(
        f"Advisor {i + 1} ({_short_model(m)}):\n{t}" for i, (m, t) in enumerate(proposals)
    )
    try:
        agg_kwargs = dict(
            model=MOA_AGGREGATOR,
            messages=[
                {"role": "system", "content":
                    "You chair an advisory panel. Given the question and each advisor's "
                    "take, weigh them, resolve disagreements, and deliver ONE clear final "
                    "decision with a one-line rationale. Plain spoken sentences — it's read aloud."},
                {"role": "user", "content": f"Question: {question}\n\n{panel}\n\nThe panel's final decision:"},
            ],
            max_tokens=800,
        )
        if "gpt-oss" in MOA_AGGREGATOR:
            agg_kwargs["reasoning_effort"] = "medium"
        agg = await client.chat.completions.create(**agg_kwargs)  # type: ignore[arg-type]
        verdict = (agg.choices[0].message.content or "").strip()
    except Exception as exc:
        verdict = f"The panel couldn't reach a verdict: {exc}"

    await broadcast({"type": "council_verdict", "text": verdict})
    await _emit_final(verdict)
    _record_turn(f"[panel] {question}", verdict)


def _deliberation_target(text: str):
    """If text invokes the panel, return the question to deliberate, else None."""
    low = text.strip().lower()
    for trig in MOA_TRIGGERS:
        if low.startswith(trig):
            return text.strip()[len(trig):].lstrip(" :,-")
    return None


# ── Agent dispatch ──────────────────────────────────────────────────────────────
# ── Affect + perception (per-turn) ──────────────────────────────────
def _ambient_brief(snap: dict) -> dict:
    """Trimmed ambient snapshot for the UI / status payload."""
    loc = (snap or {}).get("location") or {}
    wx = (snap or {}).get("weather") or {}
    return {"time": (snap or {}).get("time_str"), "tod": (snap or {}).get("tod_label"),
            "city": loc.get("city"), "country": loc.get("country_code"),
            "temp_c": wx.get("temp_c"), "weather": wx.get("label"),
            "tz": (snap or {}).get("tz")}


def _pop_audio_arousal():
    """Consume the most recent mic-loudness arousal hint (voice turns only)."""
    global _audio_arousal
    v, _audio_arousal = _audio_arousal, None
    return v


async def _update_affect(text: str) -> None:
    """Perceive the user, decay + nudge JARVIS's mood, surface it. Best-effort —
    never breaks the turn if anything here misfires."""
    global _last_read
    if not persona_mod.ENABLED:
        return
    try:
        _persona.tick()
        amb = ambient.snapshot()
        reask = bool(_REASK_RE.search(text))
        read = perception.analyze(text, hour=amb.get("hour"),
                                  acoustic_arousal=_pop_audio_arousal(), reask=reask)
        _persona.apply(read.pad_nudge, read.user_state)
        _last_read = read
        await asyncio.to_thread(_persona.save)
        await broadcast({"type": "emotion", "emotion": _persona.snapshot(),
                         "read": read.summary(), "ambient": _ambient_brief(amb)})
    except Exception:
        pass


async def handle_command(text: str) -> None:
    global _filler_sent, _last_activity

    if not text.strip():
        return

    _last_activity = time.time()
    _filler_sent = False   # reset the slow-tool filler gate for this turn

    # Fast path FIRST — a simple OS command ("open notepad", "volume up") is executed and
    # verified deterministically, with no reasoning model and none of the per-turn affect
    # work below. This is both the latency win and the anti-fabrication win: the reply is
    # built from a verified result, so JARVIS can't "say done" without having done it.
    if FASTPATH_ENABLED:
        try:
            intent = fastpath.match(text)
        except Exception:
            intent = None
        if intent is not None:
            try:
                if await _run_fastpath(text, intent, _turn_trace):
                    await broadcast({"type": "state", "status": "idle"})
                    return
            except asyncio.CancelledError:
                raise
            # fell through (unknown app / execution error) → full agent below

    await _update_affect(text)
    await broadcast({"type": "state", "status": "thinking", "text": "Thinking…"})

    if _turn_trace:
        _turn_trace.path = "agent"
        _turn_trace.mark("route")
    try:
        question = _deliberation_target(text)
        # The council is a toolless panel — never send it a request that needs a tool/action,
        # or it can only fabricate the result. Those go to the tool-capable agent instead.
        if question and USE_GROQ and _HAS_GROQ and not _needs_tools(text):
            await _deliberate(question)
        else:
            await _run_agent(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await broadcast({"type": "llm_response", "text": f"Agent error: {exc}"})

    if _turn_trace:
        _turn_trace.mark("respond")
        await _turn_trace.emit("ok")
    await broadcast({"type": "state", "status": "idle"})


# ── TTS ────────────────────────────────────────────────────────────────────────
def _sum_signed_pct(a: str, b: str) -> str:
    n = int(re.sub(r"[^0-9+-]", "", a) or 0) + int(re.sub(r"[^0-9+-]", "", b) or 0)
    return f"{max(-40, min(40, n)):+d}%"


def _sum_signed_hz(a: str, b: str) -> str:
    n = int(re.sub(r"[^0-9+-]", "", a) or 0) + int(re.sub(r"[^0-9+-]", "", b) or 0)
    return f"{max(-30, min(30, n)):+d}Hz"


def _voice_params(base_rate: str) -> tuple[str, str]:
    """Blend the homeostasis TTS rate with a subtle mood bias from the persona."""
    rate, pitch = base_rate, TTS_PITCH
    if persona_mod.ENABLED:
        try:
            bias = _persona.tts_bias()
            rate = _sum_signed_pct(base_rate, bias["rate"])
            pitch = _sum_signed_hz(TTS_PITCH, bias["pitch"])
        except Exception:
            pass
    return rate, pitch


async def _schedule_speak(text: str) -> None:
    """Cancel any in-flight speech before starting new audio."""
    global _speak_task
    if not _SPEAK_REPLY.get():
        return   # turn was started silently (remote /api/ask with speak=false)
    if _speak_task and not _speak_task.done():
        _speak_task.cancel()
        try:
            await _speak_task
        except asyncio.CancelledError:
            pass
    _speak_task = asyncio.create_task(_speak(text))


def _arm_tts_failsafe(est_seconds: float) -> None:
    """Guarantee the send-time TTS mute is released even if the frontend's tts_end never
    arrives (client disconnect, browser blocked the audio). Without this, a lost tts_end
    would wedge the mic muted forever — worse than the echo we're preventing. Fires only if
    this same clip is still the current one after its expected duration + margin."""
    global _tts_gen
    _tts_gen += 1
    gen = _tts_gen

    async def _clear() -> None:
        global _tts_playing, _tts_ended_at
        await asyncio.sleep(min(max(est_seconds, 0.0) + 2.0, 60.0))
        if _tts_gen == gen and _tts_playing:   # not superseded, and tts_end never cleared it
            _tts_playing = False
            _tts_ended_at = time.time()

    try:
        asyncio.create_task(_clear())
    except RuntimeError:
        pass   # no running loop (shouldn't happen here) — frontend tts_end will still clear it


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|(?<=[.!?])$|\n+")


def _sentence_chunks(text: str, *, min_len: int = 18, max_chunks: int = 24) -> list[str]:
    """Split a reply into speakable chunks — roughly one sentence each — so TTS can start on
    the FIRST sentence instead of waiting for the whole reply to synthesize. Very short
    fragments ("Done.", "Okay.") are merged forward so we don't emit a dozen sub-second clips;
    a reply with no sentence punctuation comes back as a single chunk (unchanged behaviour)."""
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p and p.strip()]
    if not parts:
        return [text.strip()] if text.strip() else []
    chunks: list[str] = []
    buf = ""
    for part in parts:
        buf = (buf + " " + part).strip() if buf else part
        if len(buf) >= min_len:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks:
            chunks[-1] = (chunks[-1] + " " + buf).strip()
        else:
            chunks.append(buf)
    return chunks[:max_chunks] if len(chunks) <= max_chunks else (
        chunks[:max_chunks - 1] + [" ".join(chunks[max_chunks - 1:])])


async def _synth(clean: str, rate: str, pitch: str) -> bytes:
    import edge_tts
    audio_bytes = b""
    communicate = edge_tts.Communicate(clean, _tts_voice, rate=rate, pitch=pitch)
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            data = chunk.get("data")
            if isinstance(data, (bytes, bytearray)):
                audio_bytes += bytes(data)
    return audio_bytes


async def _speak(text: str) -> None:
    global _speaking_text, _tts_playing, _tts_ended_at
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        await broadcast({"type": "tts_error",
                         "text": "Speech unavailable — run: pip install edge-tts"})
        return

    clean = re.sub(r"[*_`#\[\]()]", "", text).strip()
    if not clean:
        return

    _speaking_text = clean.lower()
    await broadcast({"type": "state", "status": "speaking", "text": "Speaking..."})
    base_rate = _homeostasis(_last_device)["tts_rate"] if _last_device else TTS_RATE
    rate, pitch = _voice_params(base_rate)

    chunks = _sentence_chunks(clean)
    # Arm the mic-mute + failsafe once, up front, for the WHOLE utterance — mute begins before
    # the first audio reaches the speakers (so no echo leading-edge leaks) and the frontend's
    # tts_end (queue drained) clears it; the failsafe clears it if that message is ever lost.
    # ~14 chars/sec of speech is a safe lower bound for the duration estimate.
    _tts_playing = True
    _arm_tts_failsafe(max(2.0, len(clean) / 14.0))
    try:
        sent_any = False
        for i, piece in enumerate(chunks):
            audio_bytes = await _synth(piece, rate, pitch)
            if not audio_bytes:
                continue
            b64 = base64.b64encode(audio_bytes).decode()
            # seq lets the frontend QUEUE chunks (seq 0 starts a fresh utterance, later chunks
            # append and play gaplessly); final marks the last so it knows when to send tts_end.
            await broadcast({"type": "tts_audio", "data": b64,
                             "seq": i, "final": i == len(chunks) - 1})
            sent_any = True
        if not sent_any:
            _tts_playing = False
            _tts_ended_at = time.time()
            await broadcast({"type": "tts_error",
                             "text": "Speech failed — Edge TTS returned no audio. Check internet."})
            _speaking_text = ""
            await broadcast({"type": "state", "status": "idle"})
            return
    except asyncio.CancelledError:
        _speaking_text = ""
        raise
    except Exception as exc:
        _speaking_text = ""
        await broadcast({"type": "tts_error", "text": f"Speech failed: {exc}"})
    await broadcast({"type": "state", "status": "idle"})


# ── STT ────────────────────────────────────────────────────────────────────────
async def _start_voice() -> None:
    global _listening, _listen_thread, _tts_playing, _speaking_text
    with _voice_lock:
        if _listening:
            return
        # If a previous worker is still winding down (it exits ~0.3s after _listening went
        # False), wait for it to fully release the mic device before opening a new stream —
        # otherwise a quick stop→start races two InputStreams onto one device ("device busy").
        old = _listen_thread
        if old is not None and old.is_alive():
            old.join(timeout=2.0)
        _tts_playing = False
        _speaking_text = ""
        _listening = True
        _listen_thread = threading.Thread(target=_voice_worker, daemon=True)
        _listen_thread.start()
    hint = f"Listening — say \"{WAKE_WORDS[0]}\" to wake me." if WAKE_REQUIRED else "Listening..."
    await broadcast({"type": "state", "status": "listening", "text": hint})
    await broadcast({"type": "mic", "listening": True})   # authoritative — UI mirrors this
    asyncio.create_task(_prewarm_wake_acks())             # so the "Yes, sir." ack is instant
    _warm_local_whisper()                                  # so the first utterance isn't stuck behind model load


def _stop_voice() -> None:
    global _listening
    with _voice_lock:
        _listening = False
    broadcast_from_thread({"type": "state", "status": "idle", "text": "Mic off."})
    broadcast_from_thread({"type": "mic", "listening": False})


# Whisper hallucinates these stock phrases on silence/noise — drop them.
_STT_NOISE = {
    "", "you", ".", "..", "...", "thank you", "thank you.", "thanks for watching",
    "thanks for watching!", "bye", "bye.", "okay", "ok", "so", "uh", "um", "yeah",
    "thank you for watching", "please subscribe", "subscribe", "the end", "music",
    "[music]", "(music)", "[silence]", "i'm sorry", "hmm", "mm", "mhm",
}


def _is_stt_noise(text: str) -> bool:
    """True if the transcription is almost certainly a hallucination, not a command."""
    t = text.strip().lower()
    if t in _STT_NOISE:
        return True
    # Strip to letters/digits — reject if there's basically no real content.
    alnum = re.sub(r"[^a-z0-9]", "", t)
    if len(alnum) < 2:
        return True
    # A single very short word is almost always a noise artifact.
    if len(t.split()) == 1 and len(alnum) <= 2:
        return True
    # Repetition = hallucination. On noise, tiny.en loops one phrase dozens of times
    # ("take a look at how take a look at how ...", "see you in the next video, see you ...").
    # Real speech has variety; a transcript whose words are mostly the same handful, OR that
    # contains a phrase repeated 3+ times back-to-back, is a hallucination.
    if _is_repetitive(t):
        return True
    return False


def _is_repetitive(t: str) -> bool:
    """Detect Whisper's degenerate looping. Two cheap signals: low lexical diversity over a
    longish transcript, and an n-gram that repeats many times in a row."""
    words = re.findall(r"[a-z0-9']+", t)
    if len(words) >= 12:
        diversity = len(set(words)) / len(words)
        if diversity < 0.35:               # e.g. 8 unique words across 40 → looped phrase
            return True
    # A 2–5 word phrase repeated 3+ times consecutively.
    for n in range(2, 6):
        if len(words) < n * 3:
            continue
        for i in range(len(words) - n * 3 + 1):
            gram = words[i:i + n]
            if words[i + n:i + 2 * n] == gram and words[i + 2 * n:i + 3 * n] == gram:
                return True
    return False


_whisper_model = None
_whisper_lock = threading.Lock()
_whisper_load_failed = False   # sticky — don't retry a 1-3s failed load on every utterance
_whisper_last_used = 0.0       # monotonic-ish wall time of the last transcription
# Free the resident model after this many idle seconds to reclaim ~150-300MB RSS. It reloads
# lazily (~1-3s) on the next utterance. 0 disables. Tunable via JARVIS_STT_IDLE_UNLOAD_SEC.
_WHISPER_IDLE_UNLOAD = int(os.environ.get("JARVIS_STT_IDLE_UNLOAD_SEC", "300"))


def _maybe_unload_whisper() -> None:
    """Drop the resident Whisper model if it's been idle a while (called from _monitor_loop).
    Mirrors the Ollama RAM-release policy — voice is bursty, so holding ~200MB of CTranslate2
    weights resident between conversations is pure waste."""
    global _whisper_model
    if _whisper_model is None or _WHISPER_IDLE_UNLOAD <= 0:
        return
    if _listening:                       # never unload mid-session; the mic is hot
        return
    if (time.time() - _whisper_last_used) < _WHISPER_IDLE_UNLOAD:
        return
    with _whisper_lock:
        if _whisper_model is not None and not _listening:
            _whisper_model = None         # GC frees the CTranslate2 weights; reloads on next use
            log.info("whisper: unloaded idle STT model to reclaim RAM")


def _import_faster_whisper():
    """Import the faster_whisper package, working around Windows Smart App Control blocking
    PyAV's native DLL (a hard, unbypassable reputation-policy block — not a mark-of-web flag
    `Unblock-File` can clear). faster-whisper's package `__init__` unconditionally imports
    PyAV (`av`) to decode audio files/bytes, but we never need that decoder: the mic capture
    already hands us raw PCM, and passing a numpy array straight to `model.transcribe()`
    skips `decode_audio()`/PyAV entirely. So we stub a harmless empty `av` module before
    import — satisfies the import, never actually used. Raises on genuine failure."""
    import sys
    if "av" not in sys.modules:
        try:
            import av  # noqa: F401  — real PyAV works fine, use it if SAC allows
        except ImportError:
            import types
            sys.modules["av"] = types.ModuleType("av")   # unused stand-in, see docstring
    import faster_whisper
    return faster_whisper


def _stt_available() -> bool:
    """True if voice input can transcribe at all — local faster-whisper or Groq cloud.

    Uses find_spec (~ms, no import) rather than actually importing faster_whisper: this is
    called from GET /api/agent/status, which the desktop shell polls during boot. Importing
    faster_whisper here (heavy CTranslate2 DLLs) would block the event loop on the first
    status request and could stall the shell's readiness probe — and it defeats the whole
    point of loading the model lazily."""
    if importlib.util.find_spec("faster_whisper") is not None:
        return True
    return bool(USE_GROQ and _HAS_GROQ)


def _get_local_whisper():
    """Process-level singleton faster-whisper model (CPU, int8, tiny.en) — the default STT
    backend: offline, free, no per-request quota. Loaded lazily on first use so app boot
    isn't delayed; returns None (and stays None) if it can't load, so callers fall back to
    Groq's cloud Whisper."""
    global _whisper_model, _whisper_load_failed
    if _whisper_model is not None or _whisper_load_failed:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None or _whisper_load_failed:
            return _whisper_model
        try:
            WhisperModel = _import_faster_whisper().WhisperModel
            # Cap threads to physical cores (max 4) — over-allocating bloats RSS on CTranslate2.
            _phys = psutil.cpu_count(logical=False) or 2
            _threads = max(1, min(4, int(os.environ.get("JARVIS_STT_THREADS", str(_phys)))))
            _whisper_model = WhisperModel(
                os.environ.get("JARVIS_STT_MODEL", "tiny.en"),
                device="cpu",
                compute_type="int8",
                cpu_threads=_threads,
                num_workers=1,
            )
        except Exception as exc:
            _whisper_load_failed = True
            broadcast_from_thread({"type": "system", "text": f"Local STT unavailable ({exc}); using Groq cloud Whisper."})
    return _whisper_model


def _warm_local_whisper() -> None:
    """Load the local Whisper model off the hot path — call once when voice starts so the
    first real utterance isn't stuck behind a one-time model load/download."""
    threading.Thread(target=_get_local_whisper, daemon=True).start()


def _transcribe_local(pcm16, sample_rate: int):
    """Transcribe raw int16 mono PCM via the local faster-whisper singleton — no WAV/file
    round-trip, no PyAV. Returns text, or None if the local model isn't available (caller
    should fall back to Groq).

    Heavily hardened against Whisper's #1 failure mode: on background noise or near-silence,
    tiny.en HALLUCINATES — usually a short phrase looped dozens of times ("thanks for watching,
    see you in the next video, ..." / "take a look at how take a look at how ..."). Three gates
    kill it: (1) faster-whisper's built-in Silero VAD strips non-speech BEFORE decoding; (2)
    per-segment confidence — drop anything the model itself thinks is silence (high
    no_speech_prob) or low-confidence (low avg_logprob) or degenerate (high compression ratio);
    (3) a repetition detector downstream in `_is_stt_noise`."""
    global _whisper_last_used
    model = _get_local_whisper()
    if model is None:
        return None
    _whisper_last_used = time.time()      # keep the idle-unload timer fresh while in use
    try:
        import numpy as np
        audio_f32 = (pcm16.astype(np.float32) / 32768.0)
        def _decode(use_vad: bool):
            kwargs = dict(
                language="en",
                vad_filter=use_vad,
                temperature=0.0,
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.2,
            )
            if use_vad:
                kwargs["vad_parameters"] = dict(
                    min_silence_duration_ms=300,
                    speech_pad_ms=240,
                    threshold=0.35,
                )
            segments, _info = model.transcribe(audio_f32, **kwargs)
            kept = []
            for seg in segments:
                if getattr(seg, "no_speech_prob", 0.0) > 0.6:
                    continue
                if getattr(seg, "avg_logprob", 0.0) < -1.0:
                    continue
                kept.append(seg.text)
            return " ".join(kept).strip()

        text = _decode(True)
        # Energy-gated clips are already speech. If Silero drops the whole clip, decode once more.
        if not text and len(audio_f32) > 16000 * 0.35:
            text = _decode(False)
        return "" if _is_stt_noise(text) else text
    except Exception as exc:
        broadcast_from_thread({"type": "system", "text": f"Local transcription failed: {exc}"})
        return ""


def _transcribe(pcm16, sample_rate: int = 16000) -> str:
    """Transcribe 16kHz mono int16 PCM — local faster-whisper first (no network round-trip
    and no per-request quota, so wake-word detection is instant and doesn't depend on
    internet/Groq availability), falling back to Groq cloud Whisper only if the local model
    can't load."""
    local = _transcribe_local(pcm16, sample_rate)
    if local is not None:
        return local
    # Cloud fallback (local model unavailable). Rate-gated: at most one call per STT_MIN_GAP,
    # and none during a post-429 back-off — real speech is bounded by the VAD's end-of-utterance
    # pause, so this only sheds a flood, never a genuine turn.
    if not (USE_GROQ and _HAS_GROQ):
        return ""
    global _stt_last_ts, _stt_backoff_until
    now = time.monotonic()
    with _stt_lock:
        if now < _stt_backoff_until or (now - _stt_last_ts) < STT_MIN_GAP:
            return ""            # inside back-off or too soon — shed it, don't flood Whisper
        _stt_last_ts = now
    try:
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm16.tobytes())
        client = _openai().OpenAI(
            api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT,
            max_retries=0,   # our own back-off handles 429s — don't let the SDK block the mic 34s
        )
        result = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=("speech.wav", buf.getvalue(), "audio/wav"),
            response_format="text",
            language="en",
        )
        text = (result or "").strip()
        return "" if _is_stt_noise(text) else text
    except Exception as exc:
        # On a rate-limit, go quiet for a beat so we stop hammering Whisper (and freeing the
        # shared budget for the chat model) instead of retrying into more 429s.
        if "429" in str(exc) or "rate_limit" in str(exc).lower():
            with _stt_lock:
                _stt_backoff_until = time.monotonic() + STT_BACKOFF
        else:
            broadcast_from_thread({"type": "system", "text": f"Transcription failed: {exc}"})
        return ""


def _voice_stopped() -> None:
    """Mark the mic authoritatively OFF (thread context). Called on EVERY _voice_worker exit,
    including the early-return failure paths — so one transient mic/dep/key error can't leave
    _listening stuck True and wedge voice (with the UI still claiming it's on) for the session."""
    global _listening
    _listening = False
    broadcast_from_thread({"type": "mic", "listening": False})
    broadcast_from_thread({"type": "audio_level", "level": 0})


def _pick_input_device(sd):
    """Find a microphone that actually WORKS on this machine → (device_index, rate, channels).

    Fully device-agnostic — no hardcoded devices, rates, or channel counts; everything is
    discovered by probing the machine's own hardware, so it works across laptops/OSes:

      1. FIRST honor the OS default input device (the mic the user picked in Windows/macOS/
         Linux) — but only if it actually delivers SIGNAL. "Opens without error" is not enough:
         Windows returns pure digital silence (not an error) when a mic is privacy-blocked,
         muted, or routed through a dead pin, so a default that streams silence is remembered
         but not trusted while a live device exists.
      2. If the default can't be opened (e.g. the Intel Smart Sound array whose MME/DirectSound
         default fails with a -9999 host error) OR it is silent, probe every input device —
         preferring WASAPI (modern, shared) then WDM-KS, skipping loopbacks/speaker-mixes,
         trying mono then the device's native channel count, and taking the first that delivers
         real signal. If EVERYTHING is silent (a genuinely quiet room / no mic), the user's
         default is kept — we never override their choice without evidence.

    For each device we try 16 kHz first (no resampling for Whisper) then its native rate;
    Groq Whisper resamples on its end. Returns (None, None, None) if nothing works — the
    caller then shows a mic-privacy hint."""
    import numpy as np
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return None, None, None

    LIVE_LEVEL = 1   # p90 |amp| must EXCEED this. A real mic's self-noise does; a blocked/muted
    #                  stream is flat (~0-1) even when it emits a loud pop as it opens.

    def probe(dev, rate, ch):
        """Open + capture ~0.25s. Returns the 90th-percentile |amplitude| — deliberately NOT the
        peak: a dead stream often pops loudly on open (peak in the thousands) while staying
        flat otherwise, and a percentile ignores that transient. <= LIVE_LEVEL means streamed
        silence. Returns None on error / no buffers (phantom devices open but never call back)."""
        acc = []
        try:
            with sd.InputStream(device=dev, samplerate=rate, channels=ch, dtype="int16",
                                blocksize=1024, callback=lambda indata, *a: acc.append(indata.copy())):
                sd.sleep(250)
        except Exception:
            return None
        if not acc:
            return None
        try:
            a = np.abs(np.concatenate(acc).astype(np.int32)).reshape(-1)
            a = a[len(a) // 5:]                       # drop the first 20%: open-time transients
            return int(np.percentile(a, 90)) if len(a) else 0
        except Exception:
            return 0

    def configs(d):
        """Formats to try for a device, cheapest-for-Whisper first."""
        maxch = int(d.get("max_input_channels", 0) or 0)
        native = int(d.get("default_samplerate") or 16000)
        chans = [c for c in (1, 2) if c <= maxch] or ([maxch] if maxch else [1])
        for ch in chans:
            for rate in dict.fromkeys([16000, native]):   # dedupe if native == 16000
                yield rate, ch

    # ── 1) the OS default input device — respect the user's chosen mic ──────────────
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = -1
    silent_default = None      # (idx, rate, ch) of a default that opened but delivered silence
    if isinstance(default_in, int) and default_in >= 0:
        try:
            di = sd.query_devices(default_in)
            if int(di.get("max_input_channels", 0) or 0) >= 1:
                for rate, ch in configs(di):
                    pk = probe(default_in, rate, ch)
                    if pk is None:
                        continue                                  # this format didn't open; try the next
                    if pk > LIVE_LEVEL:
                        return default_in, rate, ch               # default works — respect the user's choice
                    silent_default = (default_in, rate, ch)       # opened, but silent: keep as last resort
                    break
        except Exception:
            pass

    # ── 2) default unusable → probe every input device ─────────────────────────────
    pref = ["wasapi", "wdm-ks", "directsound", "mme", "core audio", "alsa", "jack", "asio"]
    def host_rank(name: str) -> int:
        low = name.lower()
        for i, p in enumerate(pref):
            if p in low:
                return i
        return len(pref)

    candidates = []
    for i, d in enumerate(devices):
        if i == default_in or int(d.get("max_input_channels", 0) or 0) < 1:
            continue
        low = (d.get("name") or "").lower()
        # Loopback / speaker-monitor endpoints capture SYSTEM AUDIO, not a person. Choosing one as
        # the "mic" makes JARVIS hear its own voice and whatever media is playing, then trigger on
        # it — strictly worse than admitting there's no working mic. Never eligible.
        if any(k in low for k in ("stereo mix", "loopback", "what u hear", "speaker", "wave out",
                                  "monitor of")):
            continue
        deprio = 1 if "sound mapper" in low else 0     # legit alias of the default; just last
        candidates.append((deprio, host_rank(hostapis[d["hostapi"]]["name"]), i, d))
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))

    fallback = None  # streams but silent — last resort if nothing has live audio
    for _deprio, _rank, i, d in candidates:
        found = None
        for rate, ch in configs(d):
            amp = probe(i, rate, ch)
            if amp is not None:
                found = (i, rate, ch, amp)
                break
        if not found:
            continue
        if found[3] > LIVE_LEVEL:             # live audio → best; use immediately
            return found[0], found[1], found[2]
        if fallback is None:
            fallback = found                 # keep the first streaming-but-silent device
    if silent_default:                       # everything is silent: honor the user's own default
        return silent_default
    if fallback:
        return fallback[0], fallback[1], fallback[2]
    return None, None, None


def _voice_worker() -> None:
    import queue as Q
    import wave

    try:
        import sounddevice as sd
        import numpy as np
    except ImportError as exc:
        broadcast_from_thread({
            "type": "system",
            "text": f"Voice deps missing: {exc}. Run: pip install sounddevice numpy",
        })
        _voice_stopped()
        return

    if not _stt_available():
        broadcast_from_thread({"type": "system", "text":
            "Voice input needs either the local STT model (pip install faster-whisper) or a "
            "GROQ_API_KEY for cloud Whisper."})
        _voice_stopped()
        return

    CHUNK = 1024            # ~64ms per callback at 16kHz
    # Pick a mic that actually opens here — the default MME device fails on many Windows
    # machines (Intel Smart Sound arrays) with a -9999 host error. RATE is whatever that
    # device accepts (16 kHz if possible, else its native rate; Groq Whisper resamples).
    input_device, RATE, CHANS = _pick_input_device(sd)
    if input_device is None or RATE is None or CHANS is None:
        broadcast_from_thread({"type": "system", "text":
            "No usable microphone. Check Windows mic access (Settings → Privacy & security → "
            "Microphone → let desktop apps use the mic), that a mic is enabled, and that no "
            "other app is holding it exclusively."})
        _voice_stopped()
        return
    sample_rate = int(RATE)
    audio_q: Q.Queue = Q.Queue()

    def _cb(indata, frames, time_info, status):
        # Downmix multi-channel capture (some arrays only open at their native 2ch) to mono.
        if indata.shape[1] > 1:
            audio_q.put(indata.mean(axis=1, keepdims=True).astype(indata.dtype))
        else:
            audio_q.put(indata.copy())

    def _run(coro):
        if _main_loop and not _main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, _main_loop)

    # Short enough to catch a quick one-word "Jarvis" (~0.4 s) without dropping it.
    MIN_UTTER_SAMPLES = int(float(os.environ.get("JARVIS_MIN_UTTER_SEC", "0.40")) * 16000)
    # A LOW absolute floor, just above digital silence — only there to skip dead-air segments.
    # WebRTC VAD (spectral, gain-independent) is the real speech gate, so this must stay low or a
    # quiet / low-gain mic on another machine would drop soft "hey Jarvis" before Whisper (and
    # before the peak-normalize below could rescue it). Universal across mics — not gain-tuned.
    MIN_UTTER_ENERGY = int(os.environ.get("JARVIS_MIN_UTTER_ENERGY", "150"))

    def _flush(pcm16) -> None:
        """Transcribe one utterance. `pcm16` is a 1-D int16 numpy array, 16 kHz mono."""
        global _audio_arousal, _awake_until
        if pcm16 is None or len(pcm16) < MIN_UTTER_SAMPLES:
            return
        if int(np.abs(pcm16).mean()) < MIN_UTTER_ENERGY:
            return
        audio = pcm16.astype(np.float32)
        try:
            _audio_arousal = float(min(1.0, max(0.0, (np.abs(audio).mean() - 300) / 1500.0)))
        except Exception:
            _audio_arousal = None
        # Peak-normalize so a low-gain mic still hands Whisper a clean, loud signal.
        peak = float(np.abs(audio).max())
        if 0.0 < peak < 26000.0:
            audio = np.clip(audio * (26000.0 / peak), -32768.0, 32767.0)
        _t_stt = time.perf_counter()
        text = _transcribe(audio.astype(np.int16), 16000)
        stt_ms = (time.perf_counter() - _t_stt) * 1000.0
        if not text:
            return

        # Ambient memory: log EVERYTHING heard first, so JARVIS can relate to it later.
        # This is independent of the wake word — acting still requires it (below).
        _remember_overheard(text)

        now = time.time()

        def _act(command: str) -> None:
            global _awake_until
            _awake_until = 0.0
            broadcast_from_thread({"type": "transcription", "text": command})
            _run(dispatch_command(command, source="voice", stt_ms=stt_ms))

        if not WAKE_REQUIRED:
            if _is_echo(text) or _is_stt_noise(text):
                return
            if text.strip().lower() in STOP_WORDS:
                _run(_stop_speaking())
                return
            _act(text.strip())
            return

        after = _match_wake_word(text)   # command text after "jarvis", "" if bare, None if absent

        if after is not None:
            # This utterance was addressed to JARVIS ("jarvis ..." or bare "jarvis").
            cmd = after.strip()
            if _is_echo(cmd):
                return
            if cmd.lower() in STOP_WORDS:
                _awake_until = 0.0
                _run(_stop_speaking())
                return
            if not cmd:
                # Bare "jarvis" — arm the window and cue the user to say the command.
                _awake_until = now + WAKE_WINDOW
                if time.time() - _last_ack_at >= 1.5:   # the acoustic engine may have just acked
                    _run(_wake_ack())
                return
            _act(cmd)                    # "jarvis <command>" in one breath
            return

        # No wake word here. If a bare "jarvis" just armed us, take this as the command —
        # this is what makes "Jarvis…" [pause] "<command>" work like a real assistant.
        if now < _awake_until:
            cmd = text.strip()
            if _is_echo(cmd) or _is_stt_noise(cmd):
                return               # keep the window open through echoes/noise
            if cmd.lower() in STOP_WORDS:
                _awake_until = 0.0
                _run(_stop_speaking())
                return
            _act(cmd)
        # else: overheard but not addressed to JARVIS — already logged, nothing to do.

    # One dedicated STT worker (queue-and-worker), NOT a thread per utterance. Two reasons:
    #  • the local CTranslate2/Whisper model isn't safe for concurrent transcribe() calls — a
    #    thread-per-utterance pileup (which is exactly what happened when JARVIS heard its own
    #    reply) contends the model and produces garbled/empty text on the NEXT real input;
    #  • a single serialized worker keeps transcription off the capture loop (mic stays live)
    #    while guaranteeing one decode at a time.
    stt_q: Q.Queue = Q.Queue(maxsize=8)   # bounded: drop the oldest stale segment if it backs up

    def _stt_worker() -> None:
        while _listening:
            try:
                pcm16 = stt_q.get(timeout=0.3)
            except Q.Empty:
                continue
            if pcm16 is None:
                break
            try:
                _flush(pcm16)
            except Exception as exc:
                broadcast_from_thread({"type": "system", "text": f"STT worker error: {exc}"})

    def _flush_async(pcm16) -> None:
        try:
            stt_q.put_nowait(pcm16)
        except Q.Full:
            try:                     # queue full → discard the oldest (stale) segment, keep newest
                stt_q.get_nowait()
                stt_q.put_nowait(pcm16)
            except Exception:
                pass

    def _tts_muted() -> bool:
        # Half-duplex: while JARVIS is speaking (or within a short acoustic-tail cooldown after),
        # the mic must not collect — otherwise it transcribes JARVIS's own voice off the speakers,
        # which both wastes the model and corrupts the next real utterance. No echo cancellation
        # needed; we simply don't listen while we talk, like a real push-to-talk radio.
        return _tts_playing or (time.time() - _tts_ended_at) < TTS_TAIL_COOLDOWN

    TTS_TAIL_COOLDOWN = 0.35   # seconds after playback before the mic is trusted again

    # Real speech detection via WebRTC VAD (spectral, gain-INDEPENDENT) at 16 kHz — replaces the
    # brittle energy-threshold VAD that couldn't separate speech from a noisy low-gain mic. A
    # ratio-window collector (below) + a pre-roll buffer means the onset of "Jarvis" is never
    # clipped and ambient blips don't trigger. Falls back to a plain energy gate only if the
    # package is missing (it's in requirements). Aggressiveness 0..3 via JARVIS_VAD_AGGR;
    # default 2 (see below for why not 3). Raise it to 3 only if background chatter/fans/media
    # keep triggering needless transcriptions in a very noisy room.
    try:
        import webrtcvad
        # Aggressiveness 2 (not 3): 3 is the most aggressive filter and rejects a lot of REAL
        # speech — soft, quick, or low-gain "hey Jarvis" — which was the main reason wake words
        # only landed ~2/10. 2 catches far more genuine speech; the wake-word text gate below
        # still rejects anything that isn't actually addressed to Jarvis, so the cost of the
        # occasional noise segment is just one cheap discarded transcription.
        _vad = webrtcvad.Vad(max(0, min(3, int(os.environ.get("JARVIS_VAD_AGGR", "2")))))
    except Exception:
        _vad = None

    # Optional dedicated acoustic wake engine (openWakeWord). Off unless JARVIS_WAKE_ENGINE=
    # openwakeword and the package is installed; None keeps the reliable text-match wake. When
    # active it fires on the raw frames in ~tens of ms, independent of Whisper — so "hey jarvis"
    # wakes near-instantly and the FOLLOWING utterance is transcribed as the command.
    _wake_det = wake.maybe_create()
    if _wake_det is not None:
        broadcast_from_thread({"type": "system", "text": "Acoustic wake word active (openWakeWord)."})

    # Loudness floor a frame must clear (mean |amplitude| of int16) to count as speech, on top
    # of the spectral VAD. ~250 keeps distant TV / fans / room tone from triggering while normal
    # talking near the mic clears it easily. Raise if background still gets through, lower if it
    # misses you.
    MIN_SPEECH_ENERGY = int(os.environ.get("JARVIS_MIC_MIN_ENERGY", "250"))

    FRAME = 480                          # 30 ms @ 16 kHz — the frame size WebRTC VAD requires
    START_PAD, START_VOICED = 6, 3       # ~3/6 voiced frames (~90–180 ms) opens the segment —
    #                                      sensitive enough to catch a quick/soft "Jarvis"
    END_PAD, END_UNVOICED = 12, 10       # end only on a clean ~360 ms pause (won't cut a sentence)
    RING_MAX = max(START_PAD, END_PAD)   # keep the larger window; doubles as the pre-roll buffer
    MAX_UTTER_FRAMES = int(14000 / 30)   # ~14 s hard cap

    def _resample16(mono_f32):
        if sample_rate == 16000 or len(mono_f32) < 2:
            return mono_f32
        n = max(1, int(round(len(mono_f32) * 16000 / sample_rate)))
        return np.interp(np.linspace(0.0, 1.0, n, endpoint=False),
                         np.linspace(0.0, 1.0, len(mono_f32), endpoint=False), mono_f32)

    stt_thread = threading.Thread(target=_stt_worker, daemon=True)
    stt_thread.start()

    try:
        from collections import deque
        with sd.InputStream(device=input_device, samplerate=sample_rate, channels=CHANS, dtype="int16",
                            blocksize=CHUNK, callback=_cb):
            broadcast_from_thread({"type": "system", "text": "Mic online. Listening..."})

            leftover = np.zeros(0, dtype=np.int16)   # 16 kHz samples spanning callback boundaries
            ring = deque(maxlen=RING_MAX)             # (frame, is_speech) — recent window + pre-roll
            triggered = False
            voiced: list = []
            level_tick = 0
            dead_frames = 0                           # consecutive digital-silence frames (~30 ms each)
            dead_warned = False
            DEAD_MIC_FRAMES = 300                     # ~9 s of nothing at all → say so, once

            while _listening:
                try:
                    chunk = audio_q.get(timeout=0.3)
                except Q.Empty:
                    continue

                # Half-duplex: while JARVIS is speaking (or in the tail cooldown), don't listen.
                # Drain what we captured, reset the collector, and drive the orb to calm — this is
                # what stops the mic from transcribing JARVIS's own reply and corrupting the next
                # turn. The moment playback ends (+cooldown) we pick right back up.
                if _tts_muted():
                    if triggered or voiced:
                        triggered = False
                        voiced = []
                        ring.clear()
                    leftover = np.zeros(0, dtype=np.int16)
                    if level_tick:                    # settle the orb once
                        broadcast_from_thread({"type": "audio_level", "level": 0})
                        level_tick = 0
                    continue

                # Resample this chunk to 16 kHz mono and slice into fixed 30 ms VAD frames.
                mono = _resample16(chunk.reshape(-1).astype(np.float32))
                leftover = np.concatenate([leftover, mono.astype(np.int16)])

                # Note: the mic stays live even while JARVIS speaks, so you can barge in with
                # the wake word. Self-talk is prevented by the wake-word gate + echo guard in
                # _flush(), not by muting.
                while len(leftover) >= FRAME:
                    frame = leftover[:FRAME]
                    leftover = leftover[FRAME:]

                    # Dedicated acoustic wake (optional): runs on every frame, independent of
                    # the VAD/Whisper path below, so "hey jarvis" fires near-instantly. Muted
                    # while JARVIS is speaking so it can't trigger on its own voice.
                    if _wake_det is not None and not _tts_muted():
                        try:
                            if _wake_det.triggered(frame):
                                broadcast_from_thread({"type": "voice", "state": "transcribing"})
                                _acoustic_wake_fire()
                        except Exception:
                            pass

                    energy = int(np.abs(frame).mean())
                    # A working mic always has a noise floor. Nine straight seconds of exact
                    # digital silence means Windows is blocking/muting it (or the wrong device is
                    # open) — say so once instead of failing silently like "voice just doesn't work".
                    if energy <= 1:
                        dead_frames += 1
                        if dead_frames >= DEAD_MIC_FRAMES and not dead_warned:
                            dead_warned = True
                            broadcast_from_thread({"type": "system", "text":
                                "The microphone is delivering only silence. Check Windows Settings → "
                                "Privacy & security → Microphone (allow desktop apps), that it isn't "
                                "muted, and that the right input device is selected."})
                    else:
                        dead_frames = 0
                    level_tick += 1
                    if level_tick % 6 == 0:          # ~every 180 ms: drives the orb + live meter
                        # `hearing` mirrors the VAD trigger so the UI lights up on real speech,
                        # not on ambient hiss (the whole point of the spectral VAD).
                        broadcast_from_thread({"type": "audio_level",
                                               "level": min(energy * 6, 32767),
                                               "energy": energy, "hearing": triggered})

                    if _vad is not None:
                        try:
                            speech = _vad.is_speech(frame.tobytes(), 16000)
                        except Exception:
                            speech = energy > 300
                    else:
                        speech = energy > 300   # dependency-free fallback
                    # Energy floor: webrtcvad is spectral-only and will call quiet room tone /
                    # distant TV "speech". Requiring a loudness minimum too means only sound
                    # actually near the mic (you talking) triggers — the biggest single win for
                    # "stop transcribing background noise". Tune with JARVIS_MIC_MIN_ENERGY.
                    if speech and energy < MIN_SPEECH_ENERGY:
                        speech = False

                    ring.append((frame, speech))
                    if not triggered:
                        recent = list(ring)[-START_PAD:]           # last ~150 ms
                        if len(recent) >= START_PAD and sum(1 for _, s in recent if s) >= START_VOICED:
                            triggered = True
                            voiced = [f for f, _ in ring]          # pre-roll → never clips the onset
                            broadcast_from_thread({"type": "voice", "state": "hearing"})
                    else:
                        voiced.append(frame)
                        recent = list(ring)[-END_PAD:]             # last ~360 ms
                        ended = len(recent) >= END_PAD and sum(1 for _, s in recent if not s) >= END_UNVOICED
                        if ended or len(voiced) >= MAX_UTTER_FRAMES:
                            triggered = False
                            # Don't flush a segment that ended right as JARVIS started talking —
                            # it's almost certainly the leading edge of the echo.
                            if not _tts_muted():
                                broadcast_from_thread({"type": "voice", "state": "transcribing"})
                                _flush_async(np.concatenate(voiced))
                            voiced = []
                            ring.clear()
                            broadcast_from_thread({"type": "voice", "state": "listening"})

            if triggered and voiced and not _tts_muted():   # flush an in-progress utterance on mic-off
                _flush_async(np.concatenate(voiced))

    except Exception as exc:
        broadcast_from_thread({"type": "system", "text": f"Voice error: {exc}"})

    stt_q.put(None)   # release the STT worker so it exits cleanly with the capture loop

    # Any exit — normal stop, mic/stream error, or GROQ hiccup — resets the flag so the mic
    # can always be restarted (and so ALWAYS_LISTEN's auto-restart isn't permanently blocked).
    _voice_stopped()



async def _broadcast_models_loaded() -> None:
    running = await asyncio.to_thread(models_advisor.running)
    await broadcast({"type": "models_loaded", "running": running, "ts": time.time()})


# ── HTTP API (routers/rest.py) ───────────────────────────────────────────────────
from routers.rest import register as _register_rest  # noqa: E402
_register_rest(app)

# ── Entry ──────────────────────────────────────────────────────────────────────
def _bind_port(host: str, preferred: int) -> socket.socket:
    """Return a LISTENING socket on the preferred port, or on an OS-assigned free port
    if the preferred one is taken (e.g. Docker Desktop squats on 8000). Handing the
    already-bound socket to uvicorn avoids a bind race, and makes the app portable to any
    machine where the default port happens to be busy. The chosen port is printed for the
    Electron shell to parse (see electron/main.js)."""
    for candidate in (preferred, 0):        # try the preferred port, then any free one
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # NOTE: no SO_REUSEADDR — on Windows it would let us co-bind a port another process
        # already holds, defeating the whole point. We WANT bind() to fail so we fall back.
        try:
            sock.bind((host, candidate))
            sock.listen()
            return sock
        except OSError:
            sock.close()
    raise SystemExit(f"[JARVIS] Could not bind {host}:{preferred} or any free port.")


if __name__ == "__main__":
    import uvicorn   # imported here (not at module top) so `import api` doesn't pay the uvicorn+watchfiles cost
    host = os.getenv("JARVIS_HOST", "127.0.0.1")
    preferred = int(os.getenv("JARVIS_PORT", "8000"))
    # Vite's proxy is fixed at process start to JARVIS_PORT (default 8000). Silent remapping
    # to a free port broke desktop:dev for anyone whose 8000 was taken (Docker Desktop) —
    # Electron learned the new port, Vite kept proxying to 8000 → dead UI. So in desktop/Vite
    # mode we refuse to remap unless JARVIS_ALLOW_PORT_FALLBACK=1 is set explicitly.
    _desktopish = os.getenv("JARVIS_DESKTOP") == "1" or os.getenv("JARVIS_USE_VITE") == "1"
    _fb = os.getenv("JARVIS_ALLOW_PORT_FALLBACK")
    allow_fallback = (_fb == "1") if _fb is not None else (not _desktopish)
    if allow_fallback:
        _sock = _bind_port(host, preferred)
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, preferred))
            sock.listen()
            _sock = sock
        except OSError:
            sock.close()
            raise SystemExit(
                f"[JARVIS] Port {preferred} is busy (often Docker Desktop). "
                f"Free it, or set JARVIS_PORT to a free port and restart BOTH the backend and "
                f"Vite with the same JARVIS_PORT so /api and /ws still proxy correctly."
            )
    _port = _sock.getsockname()[1]
    # Machine-parseable first so the desktop shell learns the real port even when it differs
    # from the default; flush so Electron sees it immediately (PYTHONUNBUFFERED is also set).
    print(f"[[JARVIS_PORT]] {_port}", flush=True)
    if _port != preferred:
        print(f"[JARVIS] Port {preferred} was busy — using free port {_port} instead.", flush=True)
    print(f"[JARVIS] Starting on http://{host}:{_port}", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[_sock])

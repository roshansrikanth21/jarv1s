#!/usr/bin/env python3
"""
JARVIS Backend
Brain priority: Groq (free, fast) → Claude (Anthropic) → Ollama (local fallback).
Whichever is configured wins, in that order — see _active_brain(), the seam the
future "Governor" (a resource/difficulty-aware policy) will replace.
Run: python api.py
"""

import asyncio
import base64
import io
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import consolidation
import device
import governor
import models_advisor

import ambient
import briefing
import perception
import persona as persona_mod
import system_monitor
import web_search as websearch_mod

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
SETTINGS_FILE = BASE_DIR / "memory" / "jarvis_settings.json"
GOVERNOR_FILE = BASE_DIR / "memory" / "jarvis_governor.json"
PERSONA_FILE  = BASE_DIR / "memory" / "jarvis_persona.json"
MEMORY_FILE.parent.mkdir(exist_ok=True)
TRADING_ROOT = Path(os.environ.get("C0MR4DES_DIR", str(BASE_DIR.parent / "c0mr4des_terminal")))

# Load .env from repo root if present
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _v = _v.strip()
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in "\"'":
                _v = _v[1:-1]
            os.environ.setdefault(_k.strip(), _v)

# ── Brain config ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"   # fast + cheap; swap to claude-sonnet-4-6 for more power
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL", "").strip()  # optional pin; else auto-detect from Ollama
OLLAMA_KEEP_ALIVE = os.environ.get("JARVIS_OLLAMA_KEEP_ALIVE", "90")  # seconds in RAM after each local call
OLLAMA_RELEASE_RAM_PCT = int(os.environ.get("JARVIS_OLLAMA_RELEASE_RAM", "82"))  # unload after reply when RAM above this
VISION_MODEL      = "llava:latest"
USE_CLAUDE        = bool(ANTHROPIC_API_KEY)

try:
    from anthropic import AsyncAnthropic as _AnthropicClient
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False
    USE_CLAUDE = False

GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
# Default: gpt-oss-120b — true reasoning model. Keeps chain-of-thought on a hidden
# channel and returns clean, concise answers (ideal for TTS). 8k TPM is fine for
# personal use; rate limits degrade gracefully. For max throughput / no throttling
# set GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct (30k TPM, but verbose).
GROQ_MODEL      = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_REASONING  = os.environ.get("GROQ_REASONING_EFFORT", "low")       # low | medium | high (gpt-oss only); low = snappier
GROQ_TIMEOUT    = float(os.environ.get("JARVIS_GROQ_TIMEOUT", "45"))   # hard cap so a slow/hung API never stalls the agent
STT_MODEL       = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# ── Browser automation (browser-harness, isolated venv → Chrome via CDP) ─────────
# Isolated so browser-harness's pinned deps (websockets 15) never clash with JARVIS's
# (16). JARVIS talks to a debug Chrome on the CDP port: if one is already there (e.g.
# you enabled remote debugging on your own browser via chrome://inspect) it drives that;
# otherwise it launches a dedicated-profile Chrome. CDP is reached via `localhost` —
# newer Chrome blocks the /json endpoints when addressed as 127.0.0.1.
BH_CLI      = os.environ.get("JARVIS_BH_CLI", str(BASE_DIR.parent / "bh-venv" / "Scripts" / "browser-harness.exe"))
BH_PORT     = int(os.environ.get("JARVIS_BH_PORT", "9222"))
BH_CDP_URL  = os.environ.get("JARVIS_BH_CDP_URL", f"http://localhost:{BH_PORT}")
BH_CHROME   = os.environ.get("JARVIS_CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BH_PROFILE  = os.environ.get("JARVIS_BH_PROFILE", str(BASE_DIR.parent / "bh-chrome-profile"))
BH_HEADLESS = os.environ.get("JARVIS_BH_HEADLESS", "0") != "0"   # default: visible, so you can watch it work
BH_TIMEOUT  = int(os.environ.get("JARVIS_BH_TIMEOUT", "150"))

# Wake word: voice commands only fire when prefixed with one of these. Includes
# common Whisper mis-hears of "jarvis". Set JARVIS_WAKE_REQUIRED=0 to disable.
WAKE_WORDS = [w.strip().lower() for w in os.environ.get(
    "JARVIS_WAKE_WORDS", "jarvis,jarvis.,jervis,javis,jarvus,jarvi,travis,charvis,jarvix"
).split(",") if w.strip()]
WAKE_REQUIRED = os.environ.get("JARVIS_WAKE_REQUIRED", "1") != "0"
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
SLOW_TOOLS = {"capture_screen", "search_web", "run_command", "ict_scan", "analyze_image", "watch_video", "get_weather", "browse"}

CONV_TURNS = 8   # how many past messages (user+assistant) to keep as context

# Mixture-of-Agents: a panel of different models answers independently, then an
# aggregator reconciles them into one decision. Triggered on demand (see TRIGGERS).
MOA_PROPOSERS = [m.strip() for m in os.environ.get(
    "JARVIS_MOA_PROPOSERS",
    "llama-3.3-70b-versatile,qwen/qwen3-32b,meta-llama/llama-4-scout-17b-16e-instruct",
).split(",") if m.strip()]
MOA_AGGREGATOR = os.environ.get("JARVIS_MOA_AGGREGATOR", "openai/gpt-oss-120b")
MOA_TRIGGERS = ("deliberate", "council", "debate", "think hard about", "convene", "panel")

# ICT watcher: scan a watchlist on a timer and alert on fresh BOS / liquidity sweeps.
WATCHLIST = [s.strip() for s in os.environ.get("JARVIS_WATCHLIST", "nifty,sensex").split(",") if s.strip()]
WATCH_INTERVAL_MIN = int(os.environ.get("JARVIS_WATCH_INTERVAL_MIN", "5"))
WATCH_TF = os.environ.get("JARVIS_WATCH_TF", "15m")
WATCH_SPEAK = os.environ.get("JARVIS_WATCH_SPEAK", "1") != "0"
USE_GROQ        = bool(GROQ_API_KEY)

try:
    import openai as _openai_mod
    _HAS_GROQ = True
except ImportError:
    _HAS_GROQ = False
    USE_GROQ  = False


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
    # Background tasks (don't block serving): hardware probe, sleep cycle, ambient.
    _boot_task = asyncio.create_task(_boot_probe())
    _sleep_task = asyncio.create_task(_sleep_loop())
    _ambient_task = asyncio.create_task(_ambient_loop())
    _monitor_task = asyncio.create_task(_monitor_loop())
    # Serve the built SPA when present (packaged desktop); else Vite serves it in dev.
    spa_dir = BASE_DIR / "dist" / "client"
    if (spa_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(spa_dir), html=True), name="ui")
        print(f"[JARVIS] Serving UI from {spa_dir}")
    else:
        print("[JARVIS] Dev mode — UI served by Vite on :8080")
    yield
    for _t in (_boot_task, _sleep_task, _ambient_task, _monitor_task):
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
task_list:   list[dict] = []
agent_trace: list[dict] = []
memories:    list[dict] = []
_overheard:  list[dict] = []   # rolling log of everything heard (ambient memory)
_overheard_dirty = 0           # utterances since last persist (throttles disk writes)
_main_loop:  asyncio.AbstractEventLoop | None = None
_listening = False
_listen_thread: threading.Thread | None = None
_tts_playing = False          # frontend reports the exact playback window
_speaking_text = ""           # current TTS text (lowercased) — used as an echo guard
_current_task = None          # in-flight handle_command task (for barge-in cancel)
_speak_task   = None          # in-flight _speak task (for barge-in cancel)
_turn_generation = 0          # bumped on every new dispatch; lets a barged-in tool
                               # thread (which asyncio.to_thread cannot forcibly stop)
                               # notice it's stale and quiet down instead of surfacing
                               # results/progress for a turn that's no longer current
_history: list[dict] = []     # rolling conversation turns for multi-turn context
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
_turn_seq = 0                   # monotonic completed exchanges (not capped like _history)
_last_consolidated_turn = 0     # _turn_seq at last consolidation
_last_decision: dict | None = None   # last Governor decision (for escalation signal)
_sleep_task = None            # background consolidation ("sleep") loop
_sleeping = False
_LOCAL_OK = False             # Ollama up + a tool-capable local model installed
LOCAL_FAST = ""               # set by _detect_local_models()
LOCAL_DEEP = ""               # set by _detect_local_models()
IDLE_SLEEP_MIN = int(os.environ.get("JARVIS_SLEEP_IDLE_MIN", "3"))

# ── Affect / ambient state ──────────────────────────────────────────────────────
_audio_arousal: float | None = None   # mic-loudness arousal hint (voice turns only)
_last_read = None                      # last perception.Read (drives prompt + UI)
_ambient_task = None                   # background ambient (weather/location) refresher
_boot_task = None                      # one-shot hardware/local-model probe (off critical path)
_monitor_task = None                   # proactive CPU/RAM/temp/GPU alerts
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


def _load_memory() -> list[dict]:
    return _load_json(MEMORY_FILE, [])


def _save_memory(mems: list[dict]) -> None:
    _save_json(MEMORY_FILE, mems)


def _save_history() -> None:
    _save_json(HISTORY_FILE, _history)


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


def _user_name() -> str:
    """Operator's name — runtime-settable via onboarding/settings, with a JARVIS_USER
    env override, else empty (the UI prompts for it on first run). Never hardcoded."""
    return (_settings.get("user_name") or os.environ.get("JARVIS_USER") or "").strip()


memories  = _load_memory()
task_list = _load_json(TASKS_FILE, [])
_overheard = _load_json(OVERHEARD_FILE, [])[-OVERHEARD_MAX:]
_history  = _load_json(HISTORY_FILE, [])[-CONV_TURNS:]
_turn_seq = len(_history) // 2
_settings = _load_json(SETTINGS_FILE, {})
_gov = governor.GovernorState(_load_json(GOVERNOR_FILE, {}))
_persona = persona_mod.Persona.load(PERSONA_FILE)
if _settings.get("voice") in {v["id"] for v in VOICE_OPTIONS}:
    _tts_voice = _settings["voice"]


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
            await asyncio.sleep(10)
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
        greet = briefing.greeting_text(_user_name(), memories)
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
# Electron shell or the Vite dev server). Set JARVIS_WS_ALLOW_ALL=1 to bypass.
WS_ALLOW_ALL = os.environ.get("JARVIS_WS_ALLOW_ALL", "0") == "1"


def _origin_allowed(origin: str | None) -> bool:
    if WS_ALLOW_ALL or not origin:    # no Origin = native client, not a browser
        return True
    try:
        host = urllib.parse.urlparse(origin).hostname or ""
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    global _tts_playing, _speaking_text, _tts_voice
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
                asyncio.create_task(dispatch_command(data.get("text", "")))
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
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("websocket handler error: %s", exc)
    finally:
        try:
            active_connections.remove(websocket)
        except ValueError:
            pass
        # The mic/watch loops are single global resources, not per-connection. If
        # the last client just disconnected without sending stop_listening (e.g.
        # the window was simply closed), stop them — otherwise the daemon thread
        # keeps recording/transcribing indefinitely with nothing to broadcast to.
        if not active_connections:
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
                "Drive a REAL Chrome browser to open sites, click, fill forms, log in, or "
                "extract live page content — for anything a plain web search can't do "
                "(interacting with a page, logged-in sites, multi-step navigation, reading "
                "JS-rendered content). You WRITE a short Python snippet using these "
                "pre-imported helpers, then print() what you want back:\n"
                "  new_tab(url) — open URL in a new tab (use for first navigation)\n"
                "  goto_url(url) — navigate the current tab\n"
                "  wait_for_load() — wait until the page finishes loading\n"
                "  page_info() — returns {url, title, w, h, ...}\n"
                "  js(code) — run JavaScript in the page and RETURN its value; use this to "
                "read text/DOM, e.g. js(\"document.body.innerText\") or "
                "js(\"[...document.querySelectorAll('h3')].map(e=>e.innerText)\")\n"
                "  click_at_xy(x, y) — click at pixel coordinates\n"
                "  capture_screenshot() — screenshot the viewport\n"
                "Always print() results. Example: new_tab('https://news.ycombinator.com'); "
                "wait_for_load(); print(js(\"[...document.querySelectorAll('.titleline a')]"
                ".slice(0,5).map(a=>a.innerText)\"))"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python snippet using the browser helpers; print() what to return."},
                },
                "required": ["code"],
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
                    "task_id": {"type": "integer", "description": "Task ID to complete"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_screen",
            "description": "Take a screenshot and describe what is currently on the screen.",
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
]

# Name → schema lookup for the executor
_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
_TOOL_REQUIRED: dict[str, list[str]] = {
    t["function"]["name"]: list(t["function"]["parameters"].get("required") or [])
    for t in TOOLS
}

# Shell safety: block destructive / chained commands the LLM might suggest.
_CMD_BLOCK_RE = re.compile(
    r"(?:^|\s)(?:rm\s+-rf|del(?:ete)?\s+|erase\s+|remove-item\b|"
    r"format\s+|shutdown|reboot|mkfs|diskpart|"
    r"reg\s+delete|curl\s+.+\|\s*(?:ba)?sh|(?:powershell|pwsh)\s+-(?:e|enc|encodedcommand)\b|"
    r"invoke-expression|iex\s|wget\s+.+\|\s*sh)",
    re.I,
)
_CMD_META_RE = re.compile(r"[;&|`>]|(?:\$\()")

_LAUNCH_ALLOWLIST = {
    "chrome": "chrome.exe", "firefox": "firefox.exe", "edge": "msedge.exe",
    "code": "code", "vscode": "code", "spotify": "spotify.exe",
    "discord": "discord.exe", "notepad": "notepad.exe",
    "terminal": "wt.exe", "powershell": "powershell.exe",
    "explorer": "explorer.exe", "calc": "calc.exe",
    "calculator": "calc.exe", "paint": "mspaint.exe",
    "obs": "obs64.exe", "steam": "steam.exe",
}


def _next_id(items: list[dict]) -> int:
    return max((int(i.get("id", 0)) for i in items), default=0) + 1


def _top_memories(limit: int = 20) -> list[dict]:
    return sorted(
        memories,
        key=lambda m: (
            -(m.get("importance") or 5),
            -(m.get("access_count") or 0),
            str(m.get("last_access") or m.get("timestamp") or ""),
        ),
    )[:limit]


def _validate_tool_args(name: str, args: dict) -> str | None:
    for key in _TOOL_REQUIRED.get(name, []):
        val = args.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            return f"{name} requires '{key}'."
    return None


def _command_allowed(cmd: str) -> str | None:
    c = cmd.strip()
    if not c:
        return "run_command needs a non-empty command string."
    if _CMD_BLOCK_RE.search(c):
        return "Command blocked for safety."
    if _CMD_META_RE.search(c):
        return "Shell chaining and redirection are blocked — one simple command only."
    if len(c) > 500:
        return "Command too long (500 char max)."
    return None

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


def _ensure_debug_chrome() -> str | None:
    """Make sure a Chrome with remote debugging is reachable. Uses an existing one on the
    port if present (e.g. your own browser via chrome://inspect); else launches a
    dedicated-profile instance. Returns an error string, or None on success."""
    if _cdp_up():
        return None
    if not os.path.exists(BH_CHROME):
        return f"Chrome not found at {BH_CHROME}. Set JARVIS_CHROME to chrome.exe."
    args = [BH_CHROME, f"--remote-debugging-port={BH_PORT}",
            f"--user-data-dir={BH_PROFILE}", "--no-first-run", "--no-default-browser-check"]
    if BH_HEADLESS:
        args += ["--headless=new", "--disable-gpu"]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        return f"could not launch Chrome: {exc}"
    for _ in range(24):                       # ~12s for CDP to come up
        time.sleep(0.5)
        if _cdp_up():
            return None
    return "Chrome launched but its debug port never responded."


def _browser_run(code: str) -> str:
    """Run a browser-harness Python snippet against the debug Chrome and return its output.
    browser-harness lives in its own venv (isolated deps); we shell out to its CLI."""
    code = (code or "").strip()
    if not code:
        return "browse needs a `code` snippet using the browser helpers."
    if not os.path.exists(BH_CLI):
        return ("Browser support isn't installed. Set up the isolated env once:\n"
                "  python -m venv C:\\Users\\rosha\\bh-venv\n"
                "  C:\\Users\\rosha\\bh-venv\\Scripts\\pip install browser-harness")
    err = _ensure_debug_chrome()
    if err:
        return f"Browser unavailable — {err}"
    # Force UTF-8 both ways: pages return unicode (emoji, accents) that Windows' default
    # cp1252 can't decode, which would otherwise lose all output.
    env = {**os.environ, "BU_CDP_URL": BH_CDP_URL, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run([BH_CLI], input=code, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=BH_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return f"Browser task timed out after {BH_TIMEOUT}s."
    except Exception as exc:
        return f"Browser task failed to run: {exc}"
    out = (proc.stdout or "").strip()
    errout = (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return f"Browser task error:\n{errout[:1500]}"
    result = out or "(the snippet produced no output — remember to print() what you want back)"
    if errout and errout not in result:
        result += f"\n[stderr] {errout[:400]}"
    return result[:4000]


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

    if name == "remember":
        now = time.time()
        entry = {
            "id": _next_id(memories),
            "content": args["content"],
            "category": _norm_memory_category(args.get("category", "fact")),
            "importance": int(args.get("importance") or
                              (6 if _norm_memory_category(args.get("category", "")) == "personal" else 5)),
            "namespace": args.get("namespace", "personal"),
            "private": bool(args.get("private", False)),
            "source_model": args.get("source_model", "jarvis"),
            "timestamp": datetime.now().isoformat(),
            # consolidation/decay + recall bookkeeping (so explicit memories age correctly)
            "ts": now,
            "last_access": now,
            "access_count": 0,
            "source": "manual",
        }
        memories.append(entry)
        _save_memory(memories)
        broadcast_from_thread({"type": "memory_update", "count": len(memories)})
        return f"Stored: {args['content']}"

    if name == "recall_memory":
        import mem_recall
        hits = mem_recall.recall(
            args["query"], memories,
            k=int(args.get("k", 6)),
            namespace=args.get("namespace"),
        )
        if hits:
            _save_memory(memories)  # persist access reinforcement from recall
            return "\n".join(
                f"[{m.get('category', 'general')}] {m['content']}" for m in hits
            )
        return "No memories matching that query."

    if name == "browse":
        return _browser_run(args.get("code", ""))

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
        disk = psutil.disk_usage(_disk_root())
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
            f"Disk {disk.percent}% | Top procs: {', '.join(top)}{tail}"
        )

    if name == "launch_app":
        raw = args["app"].lower().strip()
        cmd = _LAUNCH_ALLOWLIST.get(raw)
        if not cmd:
            supported = ", ".join(sorted(_LAUNCH_ALLOWLIST))
            return f"Unknown app '{raw}'. Supported: {supported}"
        try:
            subprocess.Popen(cmd, shell=True)
            return f"Launched {raw}."
        except Exception as exc:
            return f"Failed to launch {raw}: {exc}"

    if name == "add_task":
        task = {
            "id": _next_id(task_list),
            "t": args["task"],
            "eta": args.get("eta", ""),
            "status": "queued",
            "at": datetime.now().strftime("%H:%M"),
        }
        task_list.append(task)
        _save_tasks()
        broadcast_from_thread({"type": "tasks", "tasks": task_list})
        return f"Task added: {args['task']}"

    if name == "complete_task":
        tid = args["task_id"]
        for t in task_list:
            if t["id"] == tid:
                t["status"] = "done"
                t["at"] = datetime.now().strftime("%H:%M")
                _save_tasks()
                broadcast_from_thread({"type": "tasks", "tasks": task_list})
                return f"Task {tid} marked complete."
        return f"Task {tid} not found."

    if name == "capture_screen":
        try:
            import mss
            from PIL import Image

            with mss.mss() as sct:
                monitor = sct.monitors[1]
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                img.thumbnail((1280, 720), Image.LANCZOS)
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
                client = _openai_mod.OpenAI(
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
                return resp.choices[0].message.content
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
            return resp.message.content
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
    client = _openai_mod.OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
    content = [{"type": "text", "text": prompt}]
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
        img.thumbnail((1280, 1280), Image.LANCZOS)
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
            opts = {"outtmpl": out_tmpl, "quiet": True, "noplaylist": True,
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
            client = _openai_mod.OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
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
    ckey = (symbol.strip().lower(), interval)
    hit = _ict_cache.get(ckey)
    if hit and (time.time() - hit[0]) < _ICT_CACHE_TTL_SEC:
        return hit[1]
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return {"ok": False, "error": "Market deps missing (pip install yfinance pandas)."}

    ysym, name = _resolve_symbol(symbol)
    interval = interval if interval in {"5m", "15m", "30m", "60m", "1d"} else "15m"
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

Grounding: when a tool returns data, your answer MUST be built from that exact data — quote the real numbers/values it gave you. Never invent or hand-wave a result, and never pad with unrelated facts about the user. If a tool failed or returned nothing, say so plainly.

You are in a live spoken conversation — your replies are read aloud and you remember what was just said. Talk like a person, not a document:
- Use contractions and natural, flowing phrasing. Be warm but concise.
- This is a back-and-forth. Follow the thread — refer to what was just said, and resolve references like "that", "the first one", "tomorrow" from context instead of asking the user to repeat themselves.
- Don't echo the question back or narrate ("You asked about..."). Just respond like you're talking.
- If a request is genuinely ambiguous, ask one short clarifying question instead of guessing.
- One or two sentences for most things; go longer only when asked for detail or code.
- NEVER use markdown, headers, bullets, asterisks, code fences, or math notation — spell math in words ("ninety minus sixty"). It all gets spoken.

You have tools — memory, web search, system info, app launch, tasks, screen capture, shell, market scans, and opening the trading terminal. Use a tool ONLY when the request genuinely needs real data, an action, or your saved memory. For greetings, small talk, or anything you can answer from what you already know, just reply directly — never call a tool for "hi"."""


def _build_system_prompt() -> str:
    nm = _user_name() or "the user"
    base = _BASE_PROMPT.replace("__USER__", nm)
    lines = [base]
    # Surroundings: time of day, location, weather (non-blocking, cached).
    try:
        lines.append("\n" + ambient.prompt_fragment())
    except Exception:
        lines.append(f"\nToday: {datetime.now().strftime('%A, %B %d %Y — %H:%M')}")
    # Affect: persona + live PAD mood + how to read the user this turn.
    if persona_mod.ENABLED:
        try:
            us = _last_read.user_state if _last_read else "neutral"
            gd = _last_read.guidance if _last_read else ""
            sup = bool(_last_read.suppress_sarcasm) if _last_read else False
            blk = _persona.style_block(nm, user_state=us, guidance=gd, suppress_sarcasm=sup)
            if blk:
                lines.append("\n" + blk)
        except Exception:
            pass
    if _last_device:
        h = _homeostasis(_last_device)
        if h["energy"] <= 0.33:
            lines.append("\nYou're on battery and low on energy — keep replies especially "
                         "short and skip anything non-essential.")
    if memories:
        mem_lines = "\n".join(
            f"  - [{m.get('category', 'general')}] {m['content']}"
            for m in _top_memories(20)
        )
        lines.append(f"\nWhat you know about {nm}:\n{mem_lines}")
    # Ambient memory — things recently overheard nearby. May or may not be addressed to
    # you; use as context only if relevant, and don't assume it was said to you.
    if _overheard:
        heard = "\n".join(f"  - {o['text']}" for o in _overheard[-8:])
        lines.append(f"\nRecently overheard nearby (ambient — relate to it only if relevant):\n{heard}")
    return "\n".join(lines)


# ── Shared tool runner ──────────────────────────────────────────────────────────
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
async def _groq_round(client, messages: list[dict], allow_tools: bool):
    """One streaming round. Returns (full_text, tool_calls_raw dict)."""
    kwargs: dict = {
        "model": GROQ_MODEL,
        "messages": messages,
        "max_tokens": 2048,   # room for hidden reasoning + a concise answer, still under TPM
        "stream": True,
    }
    if "gpt-oss" in GROQ_MODEL:
        kwargs["reasoning_effort"] = GROQ_REASONING
    if allow_tools:
        kwargs["tools"] = TOOLS
        kwargs["tool_choice"] = "auto"

    stream = await client.chat.completions.create(**kwargs)

    full_text = ""
    tool_calls_raw: dict[int, dict] = {}
    thinking_sent = False
    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if not delta:
            continue
        # gpt-oss streams chain-of-thought on a separate `reasoning` channel —
        # don't speak/display it, just flag that the model is thinking.
        if getattr(delta, "reasoning", None) and not thinking_sent:
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
    return full_text, tool_calls_raw


def _record_turn(user: str, assistant: str) -> None:
    """Keep a short rolling window of the conversation for multi-turn context."""
    global _history, _turn_seq
    _turn_seq += 1
    _history = (_history + [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ])[-CONV_TURNS:]
    _save_history()


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


async def _brain_groq(text: str, history: list[dict], *, decision: dict, device: dict) -> str:
    """Primary brain — streams text, runs tools, returns the final answer. The
    shared wrapper (_run_agent) handles emit, fillers, history, and recording."""
    client = _openai_mod.AsyncOpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
        timeout=GROQ_TIMEOUT,
    )
    # System prompt + recent conversation + this turn = multi-turn context.
    messages: list[dict] = (
        [{"role": "system", "content": _build_system_prompt()}]
        + history
        + [{"role": "user", "content": text}]
    )

    use_tools = governor.agent_needs_tools(decision, device)
    final_answer = ""
    for _ in range(8):
        try:
            full_text, tool_calls_raw = await _groq_round(client, messages, allow_tools=use_tools)
        except _openai_mod.RateLimitError:
            return "I've hit Groq's per-minute rate limit. Give me a few seconds and ask again."
        except _openai_mod.APIError:
            # Malformed tool call rejected mid-stream — retry forcing a text answer;
            # prior tool results stay in context.
            try:
                full_text, tool_calls_raw = await _groq_round(client, messages, allow_tools=False)
            except _openai_mod.APIError:
                break

        if not tool_calls_raw:
            if full_text.strip():
                final_answer = full_text.strip()
            else:
                # Empty answer, no tools — retry once without tools.
                try:
                    retry_text, _ = await _groq_round(client, messages, allow_tools=False)
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
    client   = _AnthropicClient(api_key=ANTHROPIC_API_KEY)
    messages: list[dict] = list(history) + [{"role": "user", "content": text}]

    use_tools = governor.agent_needs_tools(decision, device)
    final_answer = ""
    for _ in range(8):
        req: dict = {
            "model": CLAUDE_MODEL,
            "max_tokens": 1024,
            "system": _build_system_prompt(),
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
                    obs = await _run_tool(block.name, block.input)
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
        [{"role": "system", "content": _build_system_prompt()}]
        + history
        + [{"role": "user", "content": text}]
    )

    use_tools = governor.agent_needs_tools(decision, device)
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
            obs = await _run_tool(tc.function.name, _raw_args)
            messages.append({"role": "tool", "content": obs})

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


async def _run_agent(text: str) -> None:
    """Route the request through the Governor, then run the chosen rung. The Governor
    picks the cheapest brain that clears the difficulty bar within the current
    energy/latency budget, escalating only when the task is hard or the machine is
    healthy — then observes the outcome to adapt the policy to this machine."""
    global _history, _last_device, _turn_seq, _last_consolidated_turn

    if text.strip().lower().rstrip(".!") in RESET_PHRASES:
        _history = []
        _turn_seq = 0
        _last_consolidated_turn = 0
        _save_history()
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
    decision = governor.decide(text, list(_history), dev, avail, _gov, did)
    if decision["rung"] not in avail:
        mode_hint = " Switch Governor mode to auto/cloud, or start Ollama for local."
        if _gov.mode == "local":
            mode_hint = " Local mode requires Ollama — start it and pull a model."
        await _emit_final(f"No brain available for {_gov.mode} mode.{mode_hint}")
        return
    await broadcast({"type": "governor_decision",
                     "decision": _public_decision(decision),
                     "homeostasis": _homeostasis(dev), "device": _device_brief(dev)})

    rung = decision["rung"]
    t0 = time.time()
    try:
        if rung == "council":
            await _deliberate(text)                 # emits + records itself
            _observe(decision, time.time() - t0, accepted=True)
            return
        elif rung == "cloud_deep":
            answer = await _brain_claude(text, list(_history), decision=decision, device=dev)
        elif rung == "cloud_fast":
            answer = await _brain_groq(text, list(_history), decision=decision, device=dev)
        elif rung == "local_deep":
            answer = await _brain_ollama(text, list(_history), LOCAL_DEEP, decision=decision, device=dev)
        else:
            answer = await _brain_ollama(text, list(_history), LOCAL_FAST, decision=decision, device=dev)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        answer = f"That rung failed ({exc}). Try again, or switch mode in Settings."

    answer = (answer or "").strip()
    latency = time.time() - t0
    if not answer:
        await _emit_final("I didn't get a usable response that time — try rephrasing, "
                          "or wait a moment if the model is busy.")
        _observe(decision, latency, accepted=False)
        return

    await _emit_final(answer)
    _record_turn(text, answer)
    _observe(decision, latency, accepted=True)

    if rung in ("local_fast", "local_deep") and (dev.get("ram_percent") or 0) >= OLLAMA_RELEASE_RAM_PCT:
        used = LOCAL_DEEP if rung == "local_deep" else LOCAL_FAST
        asyncio.create_task(_ollama_release(used))
        asyncio.create_task(_broadcast_models_loaded())


# ── Sleep / consolidation + model management ─────────────────────────────────────
async def _run_sleep_cycle() -> None:
    """One consolidation cycle — compress episodic turns into durable memory. Prefers
    a local model so reflection stays on-device; falls back to Groq."""
    global memories, _last_consolidated_turn, _sleeping
    if _sleeping:
        return
    _sleeping = True
    await broadcast({"type": "sleep", "state": "start", "text": "Consolidating memory…"})

    async def _llm(prompt: str) -> str:
        if _LOCAL_OK:
            try:
                import ollama
                r = await ollama.AsyncClient().chat(
                    model=LOCAL_FAST,
                    messages=[{"role": "user", "content": prompt}],
                    options={"keep_alive": 0},
                )
                return r.message.content or ""
            except Exception:
                pass
        if USE_GROQ and _HAS_GROQ:
            try:
                c = _openai_mod.AsyncOpenAI(api_key=GROQ_API_KEY,
                                            base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
                r = await c.chat.completions.create(
                    model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=800)
                return r.choices[0].message.content or ""
            except Exception:
                pass
        return ""

    try:
        # Fold recently-overheard speech into the reflection input so durable facts the
        # user mentioned aloud (even without addressing JARVIS) can become memories.
        heard_turns = [{"role": "user", "content": o["text"]} for o in _overheard[-20:]]
        reflect_input = (_history + heard_turns)[-40:]
        result = await consolidation.consolidate(memories, reflect_input, _llm)
        memories[:] = result["memories"]
        _save_memory(memories)
        _last_consolidated_turn = _turn_seq
        await broadcast({"type": "sleep", "state": "done", "text": result["summary"],
                         "memory_count": len(memories)})
    except Exception:
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
            if (idle_min >= IDLE_SLEEP_MIN and on_ac and not busy and not _sleeping
                    and consolidation.should_consolidate(_turn_seq, _last_consolidated_turn)):
                await _run_sleep_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("sleep/consolidation cycle error: %s", exc)


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
    global memories
    before = len(memories)
    memories[:] = [m for m in memories if str(m.get("id")) != str(mid)]
    if len(memories) != before:
        _save_memory(memories)
        broadcast_from_thread({"type": "memory_update", "count": len(memories)})


# ── Wake word + barge-in ─────────────────────────────────────────────────────────
def _match_wake_word(text: str):
    """Return the command after the wake word, '' if only the wake word was said,
    or None if no wake word is present."""
    t = text.lower().strip()
    best = None
    for w in WAKE_WORDS:
        idx = t.find(w)
        if idx != -1 and (best is None or idx < best[0]):
            best = (idx, w)
    if best is None:
        return None
    return t[best[0] + len(best[1]):].lstrip(" ,.!?:;-'\"")


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


async def _stop_speaking() -> None:
    """Cancel any in-flight response + TTS and tell the frontend to stop audio."""
    global _current_task, _speak_task, _speaking_text
    if _speak_task and not _speak_task.done():
        _speak_task.cancel()
    if _current_task and not _current_task.done():
        _current_task.cancel()
    _speaking_text = ""
    await broadcast({"type": "tts_stop"})
    await broadcast({"type": "state", "status": "idle"})


async def dispatch_command(text: str) -> None:
    """Entry point for every command (voice or typed). Barges in on whatever is
    currently running — thinking OR speaking — so a new directive takes over."""
    global _current_task, _turn_generation
    busy = (
        (_current_task and not _current_task.done())
        or (_speak_task and not _speak_task.done())
        or _tts_playing
    )
    if busy:
        await _stop_speaking()
    _turn_generation += 1
    _current_task = asyncio.create_task(handle_command(text))


# ── Mixture-of-Agents (multi-agent deliberation) ────────────────────────────────
def _short_model(m: str) -> str:
    return m.split("/")[-1].replace("-instruct", "").replace("-versatile", "")


async def _deliberate(question: str) -> None:
    """A panel of different models each give their take, then an aggregator
    reconciles them into one decision. Streams each voice to the UI."""
    if not (USE_GROQ and _HAS_GROQ):
        await _emit_final("Multi-agent deliberation needs the Groq backend.")
        return

    client = _openai_mod.AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT)
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
        agg = await client.chat.completions.create(**agg_kwargs)
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
    await _update_affect(text)
    await broadcast({"type": "state", "status": "thinking", "text": "Thinking…"})

    try:
        question = _deliberation_target(text)
        if question and USE_GROQ and _HAS_GROQ:
            await _deliberate(question)
        else:
            await _run_agent(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await broadcast({"type": "llm_response", "text": f"Agent error: {exc}"})

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
    if _speak_task and not _speak_task.done():
        _speak_task.cancel()
        try:
            await _speak_task
        except asyncio.CancelledError:
            pass
    _speak_task = asyncio.create_task(_speak(text))


async def _speak(text: str) -> None:
    global _speaking_text
    try:
        import edge_tts
    except ImportError:
        await broadcast({"type": "tts_error",
                         "text": "Speech unavailable — run: pip install edge-tts"})
        return

    clean = re.sub(r"[*_`#\[\]()]", "", text).strip()
    if not clean:
        return

    _speaking_text = clean.lower()
    await broadcast({"type": "state", "status": "speaking", "text": "Speaking..."})
    try:
        audio_bytes = b""
        base_rate = _homeostasis(_last_device)["tts_rate"] if _last_device else TTS_RATE
        rate, pitch = _voice_params(base_rate)
        communicate = edge_tts.Communicate(clean, _tts_voice, rate=rate, pitch=pitch)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
        if not audio_bytes:
            await broadcast({"type": "tts_error",
                             "text": "Speech failed — Edge TTS returned no audio. Check internet."})
            _speaking_text = ""
            await broadcast({"type": "state", "status": "idle"})
            return
        b64 = base64.b64encode(audio_bytes).decode()
        await broadcast({"type": "tts_audio", "data": b64})
    except asyncio.CancelledError:
        _speaking_text = ""
        raise
    except Exception as exc:
        _speaking_text = ""
        await broadcast({"type": "tts_error",
                         "text": f"Speech failed: {exc}"})
    await broadcast({"type": "state", "status": "idle"})


# ── STT ────────────────────────────────────────────────────────────────────────
async def _start_voice() -> None:
    global _listening, _listen_thread, _tts_playing, _speaking_text
    if _listening:
        return
    _tts_playing = False
    _speaking_text = ""
    _listening = True
    hint = f"Listening — say \"{WAKE_WORDS[0]}\" to wake me." if WAKE_REQUIRED else "Listening..."
    await broadcast({"type": "state", "status": "listening", "text": hint})
    _listen_thread = threading.Thread(target=_voice_worker, daemon=True)
    _listen_thread.start()


def _stop_voice() -> None:
    global _listening
    _listening = False
    broadcast_from_thread({"type": "state", "status": "idle", "text": "Mic off."})


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
    return False


def _transcribe(wav_bytes: bytes) -> str:
    """Transcribe WAV audio via Groq Whisper. Returns text (or '' on failure/noise)."""
    if not (USE_GROQ and _HAS_GROQ):
        return ""
    try:
        client = _openai_mod.OpenAI(
            api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1", timeout=GROQ_TIMEOUT,
        )
        result = client.audio.transcriptions.create(
            model=STT_MODEL,
            file=("speech.wav", wav_bytes, "audio/wav"),
            response_format="text",
            language="en",
        )
        text = (result or "").strip()
        return "" if _is_stt_noise(text) else text
    except Exception as exc:
        broadcast_from_thread({"type": "system", "text": f"Transcription failed: {exc}"})
        return ""


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
        return

    if not (USE_GROQ and _HAS_GROQ):
        broadcast_from_thread({"type": "system", "text": "Voice input needs a GROQ_API_KEY for Whisper transcription."})
        return

    RATE = 16000
    CHUNK = 1024            # ~64ms per callback at 16kHz
    SPEECH_THRESH = 650     # mean absolute value → speech onset (raised: ignore ambient noise)
    SILENCE_THRESH = 150    # mean absolute value → silence
    SILENCE_CHUNKS = 12     # ~0.8 s of trailing silence ends the utterance (snappier)
    MIN_UTTER_CHUNKS = 8    # ignore sub-~0.5s blips (claps, key taps, coughs)

    audio_q: Q.Queue = Q.Queue()

    def _cb(indata, frames, time_info, status):
        audio_q.put(indata.copy())

    def _run(coro):
        if _main_loop and not _main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, _main_loop)

    def _flush(utterance: list) -> None:
        global _audio_arousal
        if len(utterance) < MIN_UTTER_CHUNKS:
            return
        all_audio = np.concatenate(utterance, axis=0)
        try:
            _audio_arousal = float(min(1.0, max(0.0, (np.abs(all_audio).mean() - 300) / 1500.0)))
        except Exception:
            _audio_arousal = None
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(RATE)
            wf.writeframes(all_audio.tobytes())
        text = _transcribe(buf.getvalue())
        if not text:
            return

        # Ambient memory: log EVERYTHING heard first, so JARVIS can relate to it later.
        # This is independent of the wake word — acting still requires it (below).
        _remember_overheard(text)

        # Wake-word gate: only ACT on utterances addressed to JARVIS.
        cmd = _match_wake_word(text) if WAKE_REQUIRED else text
        if cmd is None:
            return
        cmd = cmd.strip()

        # Echo guard: drop the case where the mic heard JARVIS's own voice.
        if _is_echo(cmd):
            return

        # Bare wake word, or a "stop" — just interrupt whatever JARVIS is doing.
        if not cmd or cmd.lower() in STOP_WORDS:
            _run(_stop_speaking())
            return

        broadcast_from_thread({"type": "transcription", "text": cmd})
        _run(dispatch_command(cmd))

    try:
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, callback=_cb):
            broadcast_from_thread({"type": "system", "text": "Mic online. Listening..."})

            recording = False
            utterance: list = []
            silence_cnt = 0
            level_tick = 0

            while _listening:
                try:
                    chunk = audio_q.get(timeout=0.3)
                except Q.Empty:
                    continue

                # Note: the mic stays live even while JARVIS speaks, so you can
                # barge in with the wake word. Self-talk is prevented by the
                # wake-word gate + echo guard in _flush(), not by muting.
                energy = int(np.abs(chunk).mean())
                level_tick += 1
                if level_tick % 4 == 0:
                    broadcast_from_thread({"type": "audio_level", "level": min(energy * 5, 32767)})

                if not recording:
                    if energy > SPEECH_THRESH:
                        recording = True
                        utterance = [chunk]
                        silence_cnt = 0
                else:
                    utterance.append(chunk)
                    if energy < SILENCE_THRESH:
                        silence_cnt += 1
                        if silence_cnt >= SILENCE_CHUNKS:
                            recording = False
                            silence_cnt = 0
                            _flush(utterance)
                            utterance = []
                    else:
                        silence_cnt = 0

            # flush a final in-progress utterance when mic is turned off
            if recording:
                _flush(utterance)

    except Exception as exc:
        broadcast_from_thread({"type": "system", "text": f"Voice error: {exc}"})

    broadcast_from_thread({"type": "audio_level", "level": 0})


# ── HTTP API ───────────────────────────────────────────────────────────────────
@app.get("/api/agent/status")
async def agent_status() -> dict:
    cpu  = psutil.cpu_percent(interval=0)
    vm   = psutil.virtual_memory()
    disk = psutil.disk_usage(_disk_root())
    return {
        "brain": {
            "primary_llm":          _routing_label(),
            "configured_default":   _active_model(),
            "last_rung":            (_last_decision or {}).get("rung"),
            "local_model":          (_settings.get("local_model") or LOCAL_DEEP or LOCAL_FAST or None),
            "reasoning":            GROQ_REASONING if "gpt-oss" in GROQ_MODEL else "—",
            "max_agent_steps":      8,
        },
        "conversation": {
            "turns": len(_history) // 2,
        },
        "council": {
            "panel": [_short_model(m) for m in MOA_PROPOSERS],
            "chair": _short_model(MOA_AGGREGATOR),
        },
        "voice": {
            "current": _tts_voice,
            "options": VOICE_OPTIONS,
            "tts": True,
            "stt": bool(USE_GROQ and _HAS_GROQ),
            "stt_hint": None if (USE_GROQ and _HAS_GROQ) else
                        "Mic input needs a free Groq API key (Whisper). Speech output does not.",
        },
        "user": {
            "name":      _user_name(),
            "onboarded": bool(_user_name()),
        },
        "watch": {
            "watching":     _watching,
            "watchlist":    WATCHLIST,
            "interval_min": WATCH_INTERVAL_MIN,
            "tf":           WATCH_TF,
        },
        "memory": {
            "available": True,
            "count":     len(memories),
        },
        "governor": {
            "mode":      _gov.mode,
            "available": sorted(_available_rungs()),
            "metrics":   _gov.metrics(),
        },
        "emotion": _persona.snapshot() if persona_mod.ENABLED else {"enabled": False},
        "ambient": _ambient_brief(ambient.snapshot()),
        "perception": _last_read.summary() if _last_read else None,
        "homeostasis": _homeostasis(_last_device) if _last_device else None,
        "device_tier": (_last_device or {}).get("tier"),
        "local": {"enabled": _LOCAL_OK, "fast": LOCAL_FAST, "deep": LOCAL_DEEP,
                  "pinned": _settings.get("local_model")},
        "tools": [
            {"name": t["function"]["name"], "description": t["function"]["description"]}
            for t in TOOLS
        ],
        "tasks": task_list,
        "trace": agent_trace[-25:],
        "sys": {
            "cpu":  round(cpu),
            "ram":  round(vm.percent),
            "disk": round(disk.percent),
        },
    }


@app.post("/api/command")
async def command_endpoint(body: dict) -> dict:
    text = (body.get("command") or "").strip()
    if not text:
        return JSONResponse({"error": "empty command"}, status_code=400)
    asyncio.create_task(handle_command(text))
    return {"status": "processing"}


@app.get("/api/ict")
async def ict_endpoint(symbol: str = "nifty", interval: str = "15m") -> dict:
    """Structured ICT read for the Markets panel."""
    return await asyncio.to_thread(_ict_analyze, symbol, interval)


# ── Device / Models / Governor / Memory APIs ─────────────────────────────────────
@app.get("/api/device")
async def device_endpoint() -> dict:
    """Hardware profile + live power state + homeostasis."""
    global _last_device
    dev = await asyncio.to_thread(device.profile)
    _last_device = dev
    return {**dev, "homeostasis": _homeostasis(dev)}


@app.get("/api/models")
async def models_endpoint() -> dict:
    """Model Advisor: what's installed, running, and recommended for this machine."""
    def _gather() -> dict:
        up, ver = models_advisor.ollama_up()
        dev = device.profile()
        budget = models_advisor.model_budget(dev)
        ranked = models_advisor.ranked_for_device(dev, set())
        if not up:
            return {"ollama": False, "tier": dev.get("tier"), "budget": budget,
                    "recommended": models_advisor.recommend_for_device(dev, set()),
                    "ranked": ranked,
                    "benchmarks": models_advisor.load_benchmarks(),
                    "allowed_count": len(models_advisor.allowed_models(dev))}
        inst = models_advisor.annotate_installed(dev, models_advisor.installed(with_caps=True))
        names = {m["name"] for m in inst}
        ranked = models_advisor.ranked_for_device(dev, names)
        out = {"ollama": True, "version": ver, "tier": dev.get("tier"),
                "installed": inst, "running": models_advisor.running(),
                "recommended": models_advisor.recommend_for_device(dev, names),
                "ranked": ranked,
                "benchmarks": models_advisor.load_benchmarks(),
                "budget": budget,
                "allowed_count": len(models_advisor.allowed_models(dev)),
                "active": {"fast": LOCAL_FAST, "deep": LOCAL_DEEP, "enabled": _LOCAL_OK},
                "pinned": _settings.get("local_model")}
        return out
    return await asyncio.to_thread(_gather)


@app.get("/api/models/loaded")
async def models_loaded_endpoint() -> dict:
    """Live snapshot of models Ollama currently has in memory (poll-friendly)."""
    running = await asyncio.to_thread(models_advisor.running)
    return {"running": running, "ts": time.time()}


async def _broadcast_models_loaded() -> None:
    running = await asyncio.to_thread(models_advisor.running)
    await broadcast({"type": "models_loaded", "running": running, "ts": time.time()})


@app.get("/api/governor")
async def governor_endpoint() -> dict:
    """The Governor's policy, lattice, learned metrics, and recent decisions."""
    dev = _last_device or await asyncio.to_thread(device.profile)
    avail = _available_rungs()
    rungs = [{**r, "available": r["id"] in avail} for r in governor.RUNGS]
    return {"mode": _gov.mode, "modes": list(governor.MODES), "rungs": rungs,
            "available": sorted(avail), "metrics": _gov.metrics(),
            "recent": _gov.log[-12:], "homeostasis": _homeostasis(dev)}


@app.get("/api/memory")
async def memory_endpoint() -> dict:
    """The inspectable self-model — durable memories, newest first."""
    return {"count": len(memories),
            "memories": [{"id": m.get("id"), "content": m.get("content"),
                          "category": m.get("category", "fact"),
                          "importance": m.get("importance", 5),
                          "source": m.get("source", "manual")}
                         for m in memories[::-1][:60]]}


# ── Attachment uploads — images/docs the UI drops into chat ────────────────────
# The endpoint extracts a plain-text digest at upload time (vision for images,
# text extraction for documents), so by the time the user hits send, the brain
# receives ready-made context and can answer without the user explaining the file.
UPLOADS_DIR = BASE_DIR / "uploads"
_UPLOAD_MAX = 15 * 1024 * 1024          # 15 MB
_DIGEST_CAP = 6000                      # chars of extracted content passed to the brain
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log",
              ".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".cpp", ".h", ".java",
              ".go", ".rs", ".sh", ".ps1", ".html", ".css", ".sql", ".ini", ".toml"}


def _extract_docx(path) -> str:
    """DOCX body text via stdlib only — a .docx is a zip with word/document.xml."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    paras = re.split(r"</w:p>", xml)
    out = [re.sub(r"<[^>]+>", "", p).strip() for p in paras]
    return "\n".join(p for p in out if p)


def _extract_pdf(path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    chunks = []
    total = 0
    for page in reader.pages:
        t = (page.extract_text() or "").strip()
        if t:
            chunks.append(t)
            total += len(t)
        if total > _DIGEST_CAP * 2:
            break
    return "\n\n".join(chunks)


def _digest_upload(path, ext: str) -> tuple[str, str]:
    """(kind, extracted text) for a saved upload. Runs in a worker thread."""
    if ext in _IMAGE_EXTS:
        desc = _analyze_image(str(path),
                              "Describe this image thoroughly. Transcribe ALL visible "
                              "text exactly as written. Note anything unusual or "
                              "noteworthy — the user attached it for a reason.")
        return "image", desc
    if ext == ".pdf":
        return "pdf", _extract_pdf(path)
    if ext == ".docx":
        return "docx", _extract_docx(path)
    if ext in _TEXT_EXTS:
        return "text", path.read_text(encoding="utf-8", errors="ignore")
    return "binary", ""


@app.post("/api/upload")
async def upload_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    name = os.path.basename(str(body.get("name") or "file"))
    try:
        raw = base64.b64decode(body.get("data_b64") or "", validate=True)
    except Exception:
        return JSONResponse({"error": "data_b64 is not valid base64"}, status_code=400)
    if not raw:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(raw) > _UPLOAD_MAX:
        return JSONResponse({"error": f"file too large (max {_UPLOAD_MAX // (1024*1024)} MB)"},
                            status_code=413)

    UPLOADS_DIR.mkdir(exist_ok=True)
    ext = Path(name).suffix.lower()
    safe = f"{int(time.time())}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}"
    dest = UPLOADS_DIR / safe
    dest.write_bytes(raw)

    try:
        kind, text = await asyncio.to_thread(_digest_upload, dest, ext)
    except Exception as exc:
        return JSONResponse({"ok": True, "name": name, "kind": "binary", "path": str(dest),
                             "digest": "", "note": f"saved, but couldn't read content: {exc}"})
    digest = (text or "").strip()
    truncated = len(digest) > _DIGEST_CAP
    if truncated:
        digest = digest[:_DIGEST_CAP] + "\n…[truncated]"
    return JSONResponse({"ok": True, "name": name, "kind": kind, "path": str(dest),
                         "digest": digest, "truncated": truncated})


# ── Memory hub — cross-model remember/recall over HTTP ─────────────────────────
# External models (Claude / ChatGPT / Gemini via the memory_mcp.py shim) read and
# write the SAME store as JARVIS itself. JARVIS's process stays the single writer;
# these endpoints reuse execute_tool so decay bookkeeping, category normalisation,
# and broadcasts happen in exactly one place.
MEMORY_HUB_TOKEN = os.environ.get("JARVIS_MEMORY_TOKEN", "")


def _hub_authed(request: Request) -> bool:
    if not MEMORY_HUB_TOKEN:
        # No token configured → allow loopback only, never remote.
        return (request.client is None) or request.client.host in ("127.0.0.1", "::1")
    auth = request.headers.get("authorization", "")
    return auth == f"Bearer {MEMORY_HUB_TOKEN}"


@app.post("/api/memory/remember")
async def hub_remember(request: Request) -> JSONResponse:
    if not _hub_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    args = {
        "content": content,
        "category": body.get("category", "fact"),
        "importance": body.get("importance", 5),
        "namespace": body.get("namespace", "personal"),
        "private": bool(body.get("private", False)),
        "source_model": body.get("source_model", "external"),
    }
    result = execute_tool("remember", args)
    return JSONResponse({"ok": True, "result": result, "count": len(memories)})


@app.get("/api/memory/recall")
async def hub_recall(request: Request, q: str, k: int = 6,
                     namespace: str | None = None) -> JSONResponse:
    if not _hub_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    import mem_recall
    # External callers NEVER see private memories — that filter is not optional here.
    hits = mem_recall.recall(q, memories, k=max(1, min(k, 20)),
                             namespace=namespace, include_private=False)
    if hits:
        _save_memory(memories)  # persist access reinforcement
    return JSONResponse({"count": len(hits), "memories": [
        {"content": m.get("content"), "category": m.get("category", "fact"),
         "importance": m.get("importance", 5),
         "source_model": m.get("source_model", "jarvis"),
         "namespace": m.get("namespace", "personal")}
        for m in hits
    ]})


@app.get("/health")
async def health() -> dict:
    return {
        "status":   "ok",
        "model":    _active_model(),
        "time":     datetime.now().isoformat(),
        "memories": len(memories),
    }


# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("JARVIS_PORT", 8000))
    host = os.getenv("JARVIS_HOST", "127.0.0.1")
    print(f"[JARVIS] Starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")

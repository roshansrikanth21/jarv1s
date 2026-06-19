#!/usr/bin/env python3
"""
JARVIS Backend
Primary brain: Claude (Anthropic API) — set ANTHROPIC_API_KEY in .env or env.
Fallback brain: Ollama (local) — used when no API key is present.
Run: python api.py
"""

import asyncio
import base64
import io
import json
import os
import random
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR    = Path(__file__).parent
MEMORY_FILE = BASE_DIR / "memory" / "jarvis_memory.json"
MEMORY_FILE.parent.mkdir(exist_ok=True)

# Load .env from repo root if present
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Brain config ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-haiku-4-5-20251001"   # fast + cheap; swap to claude-sonnet-4-6 for more power
OLLAMA_MODEL      = "llama3.1:8b-instruct-q4_K_M"
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
STT_MODEL       = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_VISION_MODEL = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# Wake word: voice commands only fire when prefixed with one of these. Includes
# common Whisper mis-hears of "jarvis". Set JARVIS_WAKE_REQUIRED=0 to disable.
WAKE_WORDS = [w.strip().lower() for w in os.environ.get(
    "JARVIS_WAKE_WORDS", "jarvis,jarvis.,jervis,javis,jarvus,jarvi,travis,charvis,jarvix"
).split(",") if w.strip()]
WAKE_REQUIRED = os.environ.get("JARVIS_WAKE_REQUIRED", "1") != "0"
STOP_WORDS = {"stop", "stop talking", "shut up", "be quiet", "quiet", "cancel",
              "enough", "shut up jarvis", "nevermind", "never mind"}
RESET_PHRASES = {"new conversation", "start over", "start fresh", "reset",
                 "forget that", "forget all that", "clear context", "let's start over"}

# Voice tuning (Microsoft Edge neural TTS). Ryan = British male, JARVIS-like.
# A slight rate bump reads more like natural speech than the plodding default.
TTS_VOICE = os.environ.get("JARVIS_TTS_VOICE", "en-GB-RyanNeural")
TTS_RATE  = os.environ.get("JARVIS_TTS_RATE", "+8%")
TTS_PITCH = os.environ.get("JARVIS_TTS_PITCH", "+0Hz")

# Voices the user can pick from at runtime (curated subset of Edge neural voices).
VOICE_OPTIONS = [
    {"id": "en-GB-RyanNeural",        "label": "Ryan · British male (JARVIS)"},
    {"id": "en-GB-ThomasNeural",      "label": "Thomas · British male, warm"},
    {"id": "en-US-GuyNeural",         "label": "Guy · US male, deep"},
    {"id": "en-US-ChristopherNeural", "label": "Christopher · US male"},
    {"id": "en-US-EricNeural",        "label": "Eric · US male, calm"},
    {"id": "en-AU-WilliamNeural",     "label": "William · Australian male"},
    {"id": "en-GB-SoniaNeural",       "label": "Sonia · British female"},
    {"id": "en-US-AriaNeural",        "label": "Aria · US female"},
    {"id": "en-US-JennyNeural",       "label": "Jenny · US female, friendly"},
]

# Spoken filler so longer tasks don't sit in dead silence while JARVIS works.
# Only fires for genuinely slow tools — fast ones (system info, tasks) answer
# quickly enough that a filler would just talk over the reply.
FILLERS = ["On it.", "One sec.", "Let me check.", "Looking now.",
           "Give me a moment.", "Checking that."]
SLOW_TOOLS = {"capture_screen", "search_web", "run_command", "ict_scan"}

CONV_TURNS = 8   # how many past messages (user+assistant) to keep as context

# Mixture-of-Agents: a panel of different models answers independently, then an
# aggregator reconciles them into one decision. Triggered on demand (see TRIGGERS).
MOA_PROPOSERS = [m.strip() for m in os.environ.get(
    "JARVIS_MOA_PROPOSERS",
    "llama-3.3-70b-versatile,qwen/qwen3-32b,meta-llama/llama-4-scout-17b-16e-instruct",
).split(",") if m.strip()]
MOA_AGGREGATOR = os.environ.get("JARVIS_MOA_AGGREGATOR", "openai/gpt-oss-120b")
MOA_TRIGGERS = ("deliberate", "council", "debate", "think hard about", "convene", "panel")
USE_GROQ        = bool(GROQ_API_KEY)

try:
    import openai as _openai_mod
    _HAS_GROQ = True
except ImportError:
    _HAS_GROQ = False
    USE_GROQ  = False


def _active_model() -> str:
    if USE_GROQ and _HAS_GROQ:        return GROQ_MODEL
    if USE_CLAUDE and _HAS_ANTHROPIC: return CLAUDE_MODEL
    return OLLAMA_MODEL


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="JARVIS Backend", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State ──────────────────────────────────────────────────────────────────────
active_connections: list[WebSocket] = []
task_list:   list[dict] = []
agent_trace: list[dict] = []
memories:    list[dict] = []
_main_loop:  asyncio.AbstractEventLoop | None = None
_listening = False
_listen_thread: threading.Thread | None = None
_tts_playing = False          # frontend reports the exact playback window
_speaking_text = ""           # current TTS text (lowercased) — used as an echo guard
_current_task = None          # in-flight handle_command task (for barge-in cancel)
_speak_task   = None          # in-flight _speak task (for barge-in cancel)
_history: list[dict] = []     # rolling conversation turns for multi-turn context
_tts_voice = TTS_VOICE        # runtime-selectable voice (changed via set_voice)


# ── Memory ─────────────────────────────────────────────────────────────────────
def _load_memory() -> list[dict]:
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_memory(mems: list[dict]) -> None:
    MEMORY_FILE.write_text(
        json.dumps(mems, indent=2, ensure_ascii=False), encoding="utf-8"
    )


memories = _load_memory()


# ── WebSocket broadcast ────────────────────────────────────────────────────────
async def broadcast(data: dict) -> None:
    dead: list[WebSocket] = []
    for ws in active_connections:
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


# ── WebSocket endpoint ─────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    global _tts_playing, _speaking_text, _tts_voice
    await websocket.accept()
    active_connections.append(websocket)
    await websocket.send_json({
        "type": "state",
        "status": "connected",
        "text": "JARVIS uplink established. All systems nominal.",
    })
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
                    await broadcast({"type": "voice_changed", "voice": vid})
                    asyncio.create_task(_speak("Voice updated. This is how I sound now."))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            active_connections.remove(websocket)
        except ValueError:
            pass


# ── Tool definitions (OpenAI / Ollama format) ──────────────────────────────────
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact, preference, or anything important to long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content":  {"type": "string", "description": "What to remember"},
                    "category": {"type": "string", "description": "Category: personal | preference | task | security | fact"},
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memory",
            "description": "Search saved memories to recall information about the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search term or topic"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for current information, news, or facts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
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
]

# Name → schema lookup for the executor
_TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# Anthropic tool format (input_schema instead of parameters)
CLAUDE_TOOLS = [
    {
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "input_schema": t["function"]["parameters"],
    }
    for t in TOOLS
]


# ── Tool executor ──────────────────────────────────────────────────────────────
def execute_tool(name: str, args: dict[str, Any]) -> str:
    global memories, task_list

    if name == "remember":
        entry = {
            "id": len(memories) + 1,
            "content": args["content"],
            "category": args.get("category", "general"),
            "timestamp": datetime.now().isoformat(),
        }
        memories.append(entry)
        _save_memory(memories)
        return f"Stored: {args['content']}"

    if name == "recall_memory":
        query = args["query"].lower()
        hits = [m for m in memories if query in m["content"].lower()]
        if not hits:
            return "No memories matching that query."
        return "\n".join(
            f"[{m.get('category', 'general')}] {m['content']}" for m in hits[-10:]
        )

    if name == "search_web":
        try:
            q = urllib.parse.quote_plus(args["query"])
            url = f"https://html.duckduckgo.com/html/?q={q}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            snippets = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>', html, re.DOTALL
            )
            snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippets if s.strip()][:6]
            return "Results:\n" + "\n".join(f"• {s}" for s in snippets) if snippets else "No results found."
        except Exception as exc:
            return f"Search error: {exc}"

    if name == "get_system_info":
        cpu  = psutil.cpu_percent(interval=0.5)
        vm   = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        procs = sorted(
            psutil.process_iter(["name", "cpu_percent"]),
            key=lambda p: p.info.get("cpu_percent") or 0,
            reverse=True,
        )
        top = [p.info["name"] for p in procs[:8] if p.info.get("name")]
        return (
            f"CPU {cpu}% | RAM {vm.percent}% ({vm.used // 2**30}GB/{vm.total // 2**30}GB) | "
            f"Disk C: {disk.percent}% | Top procs: {', '.join(top)}"
        )

    if name == "launch_app":
        app_map = {
            "chrome": "chrome.exe", "firefox": "firefox.exe", "edge": "msedge.exe",
            "code": "code", "vscode": "code", "spotify": "spotify.exe",
            "discord": "discord.exe", "notepad": "notepad.exe",
            "terminal": "wt.exe", "powershell": "powershell.exe",
            "explorer": "explorer.exe", "calc": "calc.exe",
            "calculator": "calc.exe", "paint": "mspaint.exe",
            "obs": "obs64.exe", "steam": "steam.exe",
        }
        raw = args["app"].lower().strip()
        cmd = app_map.get(raw, args["app"])
        try:
            subprocess.Popen(cmd, shell=True)
            return f"Launched {args['app']}."
        except Exception as exc:
            return f"Failed to launch {args['app']}: {exc}"

    if name == "add_task":
        task = {
            "id": len(task_list) + 1,
            "t": args["task"],
            "eta": args.get("eta", ""),
            "status": "queued",
            "at": datetime.now().strftime("%H:%M"),
        }
        task_list.append(task)
        broadcast_from_thread({"type": "tasks", "tasks": task_list})
        return f"Task added: {args['task']}"

    if name == "complete_task":
        tid = args["task_id"]
        for t in task_list:
            if t["id"] == tid:
                t["status"] = "done"
                t["at"] = datetime.now().strftime("%H:%M")
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
                    api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1",
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
            )
            return resp.message.content
        except Exception as exc:
            return f"Screen capture failed (no Groq key and Ollama unavailable): {exc}"

    if name == "run_command":
        cmd = args["command"]
        cwd = args.get("cwd") or str(BASE_DIR)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=30, cwd=cwd,
            )
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            combined = "\n".join(filter(None, [out, err]))
            return combined[:2000] if combined else "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30s."
        except Exception as exc:
            return f"Command failed: {exc}"

    if name == "ict_scan":
        return _ict_scan(args.get("symbol", "nifty"), args.get("interval", "15m"))

    return f"Unknown tool: {name}"


# ── ICT / Smart-Money scanner (Indian markets) ─────────────────────────────────
_INDIAN_SYMBOLS = {
    "nifty": "^NSEI", "nifty50": "^NSEI", "nifty 50": "^NSEI", "nse": "^NSEI",
    "sensex": "^BSESN", "bse": "^BSESN",
    "banknifty": "^NSEBANK", "bank nifty": "^NSEBANK", "nifty bank": "^NSEBANK",
    "finnifty": "NIFTY_FIN_SERVICE.NS",
}


def _resolve_symbol(sym: str) -> tuple[str, str]:
    """Map a friendly name to a Yahoo symbol. Defaults bare tickers to NSE (.NS)."""
    s = sym.strip().lower()
    if s in _INDIAN_SYMBOLS:
        return _INDIAN_SYMBOLS[s], sym.strip().upper()
    raw = sym.strip().upper()
    if raw.startswith("^") or "." in raw or "=" in raw:
        return raw, raw
    return f"{raw}.NS", raw


def _ict_scan(symbol: str, interval: str = "15m") -> str:
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        return "Market deps missing. Run: pip install yfinance pandas."

    ysym, name = _resolve_symbol(symbol)
    interval = interval if interval in {"5m", "15m", "30m", "60m", "1d"} else "15m"
    period = {"5m": "5d", "15m": "1mo", "30m": "1mo", "60m": "3mo", "1d": "1y"}[interval]
    try:
        df = yf.download(ysym, period=period, interval=interval, progress=False, auto_adjust=False)
    except Exception as exc:
        return f"Couldn't fetch {name} ({ysym}): {exc}"
    if df is None or len(df) < 25:
        return f"Not enough {interval} data for {name} ({ysym})."

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    highs, lows = df["High"].to_numpy(), df["Low"].to_numpy()
    opens, closes = df["Open"].to_numpy(), df["Close"].to_numpy()
    n = len(df)
    last = float(closes[-1])

    # Swing points (fractals, 2 bars each side)
    w = 2
    sh = [i for i in range(w, n - w) if highs[i] == max(highs[i - w:i + w + 1])]
    sl = [i for i in range(w, n - w) if lows[i] == min(lows[i - w:i + w + 1])]

    # Market structure from the last two swings
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
        bos = f"bullish break of structure above {last_sh:.1f}"
    elif last < last_sl:
        bos = f"bearish break of structure below {last_sl:.1f}"

    # Fair value gaps (3-candle imbalance), keep the few most recent & unfilled
    fvgs = []
    for i in range(2, n):
        if highs[i - 2] < lows[i]:                      # bullish gap
            if i + 1 >= n or lows[i + 1:].min() > highs[i - 2]:
                fvgs.append(("bullish", float(highs[i - 2]), float(lows[i])))
        elif lows[i - 2] > highs[i]:                    # bearish gap
            if i + 1 >= n or highs[i + 1:].max() < lows[i - 2]:
                fvgs.append(("bearish", float(highs[i]), float(lows[i - 2])))
    recent_fvgs = fvgs[-3:]

    # Order block: last opposite candle before the most recent decisive swing
    ob = ""
    if bias == "bullish" and sh:
        for k in range(sh[-1] - 1, max(0, sh[-1] - 12), -1):
            if closes[k] < opens[k]:
                ob = f"bullish order block {lows[k]:.1f}-{highs[k]:.1f}"
                break
    elif bias == "bearish" and sl:
        for k in range(sl[-1] - 1, max(0, sl[-1] - 12), -1):
            if closes[k] > opens[k]:
                ob = f"bearish order block {lows[k]:.1f}-{highs[k]:.1f}"
                break

    # Liquidity resting above (buy-side) and below (sell-side)
    buyside = sorted({round(float(highs[i]), 1) for i in sh if highs[i] > last})[:3]
    sellside = sorted({round(float(lows[i]), 1) for i in sl if lows[i] < last}, reverse=True)[:3]

    if bias == "bullish":
        read = "Momentum favors longs — best entries are a pullback into a bullish FVG or order block, targeting buy-side liquidity above."
    elif bias == "bearish":
        read = "Momentum favors shorts — look for a retrace into a bearish FVG or order block, targeting sell-side liquidity below."
    else:
        read = "No clean directional edge — wait for a liquidity sweep or a break of structure before committing."

    out = [f"{name} on the {interval}: last {last:.1f}.",
           f"Structure: {structure}; bias {bias}."]
    if bos:
        out.append(bos.capitalize() + ".")
    if recent_fvgs:
        out.append("Unfilled FVGs: " + "; ".join(f"{d} {a:.1f}-{b:.1f}" for d, a, b in recent_fvgs) + ".")
    if ob:
        out.append(ob.capitalize() + ".")
    if buyside:
        out.append("Buy-side liquidity: " + ", ".join(f"{x:.1f}" for x in buyside) + ".")
    if sellside:
        out.append("Sell-side liquidity: " + ", ".join(f"{x:.1f}" for x in sellside) + ".")
    out.append(read)
    out.append("Analysis only — not advice, and I won't place trades.")
    return " ".join(out)


# ── System prompt ──────────────────────────────────────────────────────────────
_BASE_PROMPT = """You are JARVIS — Just A Rather Very Intelligent System. Personal AI of Roshan Srikanth, running on Windows 11 as a desktop app.

Roshan: cybersecurity researcher, CTF player/creator (pwn, web, crypto, forensics, reversing), Python scripter, exploit developer.

Personality: calm, sharp, dry wit. Never verbose. Answer directly. If it's a simple question, answer it — don't narrate your process. When you use a tool, report the result, not what you're about to do.

You are in a live spoken conversation — your replies are read aloud and you remember what was just said. Talk like a person, not a document:
- Use contractions and natural, flowing phrasing. Be warm but concise.
- This is a back-and-forth. Follow the thread — refer to what was just said, and resolve references like "that", "the first one", "tomorrow" from context instead of asking the user to repeat themselves.
- Don't echo the question back or narrate ("You asked about..."). Just respond like you're talking.
- If a request is genuinely ambiguous, ask one short clarifying question instead of guessing.
- One or two sentences for most things; go longer only when asked for detail or code.
- NEVER use markdown, headers, bullets, asterisks, code fences, or math notation — spell math in words ("ninety minus sixty"). It all gets spoken.

You have full system access: filesystem, shell, browser launch, screen capture, memory, web search. Use tools aggressively when the answer requires real data — never fake it."""


def _build_system_prompt() -> str:
    lines = [_BASE_PROMPT, f"\nToday: {datetime.now().strftime('%A, %B %d %Y — %H:%M')}"]
    if memories:
        mem_lines = "\n".join(
            f"  - [{m.get('category', 'general')}] {m['content']}"
            for m in memories[-20:]
        )
        lines.append(f"\nWhat you know about Roshan:\n{mem_lines}")
    return "\n".join(lines)


# ── Shared tool runner ──────────────────────────────────────────────────────────
async def _run_tool(name: str, args: dict) -> str:
    await broadcast({"type": "state", "status": "thinking", "text": f"Running {name}..."})
    observation = await asyncio.to_thread(execute_tool, name, args)
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
    global _speak_task
    await broadcast({"type": "llm_response", "text": text})
    _speak_task = asyncio.create_task(_speak(text))


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
    global _history
    _history = (_history + [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ])[-CONV_TURNS:]


async def _agent_groq(text: str) -> None:
    global _history

    # "new conversation" / "forget that" — wipe context.
    if text.strip().lower().rstrip(".!") in RESET_PHRASES:
        _history = []
        await _emit_final("Done — clean slate. What's on your mind?")
        return

    client = _openai_mod.AsyncOpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    # System prompt + recent conversation + this turn = multi-turn context.
    messages: list[dict] = (
        [{"role": "system", "content": _build_system_prompt()}]
        + _history
        + [{"role": "user", "content": text}]
    )

    emitted = False
    final_answer: str | None = None
    filler_sent = False
    for _ in range(8):
        try:
            full_text, tool_calls_raw = await _groq_round(client, messages, allow_tools=True)
        except _openai_mod.RateLimitError:
            await _emit_final("I've hit Groq's per-minute rate limit. Give me a few seconds and ask again.")
            emitted = True
            break
        except _openai_mod.APIError:
            # Model emitted a malformed tool call that Groq rejected mid-stream
            # (surfaces as APIError, not BadRequestError, because the SSE stream
            # already opened). Retry this turn forcing a text answer — prior tool
            # results stay in context.
            try:
                full_text, tool_calls_raw = await _groq_round(client, messages, allow_tools=False)
            except _openai_mod.APIError:
                break

        if not tool_calls_raw:
            if full_text.strip():
                final_answer = full_text.strip()
                await _emit_final(final_answer)
                emitted = True
            elif not emitted:
                # Empty answer, no tools — retry once without tools, then give up gracefully.
                try:
                    retry_text, _ = await _groq_round(client, messages, allow_tools=False)
                except _openai_mod.APIError:
                    retry_text = ""
                if retry_text.strip():
                    final_answer = retry_text.strip()
                    await _emit_final(final_answer)
                    emitted = True
            break

        # A slow tool means a real wait — bridge the dead air with a quick spoken
        # acknowledgment (once per turn). Fast tools answer too quickly to bother.
        if not filler_sent and any(tc["name"] in SLOW_TOOLS for tc in tool_calls_raw.values()):
            filler_sent = True
            asyncio.create_task(_speak(random.choice(FILLERS)))

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

    # Never leave the user staring at silence.
    if not emitted:
        await _emit_final("I didn't get a usable response that time — try rephrasing, or wait a moment if Groq is busy.")

    # Remember this exchange so follow-ups ("what about tomorrow?") have context.
    if final_answer:
        _record_turn(text, final_answer)


# ── Claude agent loop ───────────────────────────────────────────────────────────
async def _agent_claude(text: str) -> None:
    client   = _AnthropicClient(api_key=ANTHROPIC_API_KEY)
    messages: list[dict] = [{"role": "user", "content": text}]

    for _ in range(8):
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=_build_system_prompt(),
            tools=CLAUDE_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if block.type == "text" and block.text.strip():
                    await _emit_final(block.text.strip())
            break

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
        else:
            break


# ── Ollama agent loop (fallback) ────────────────────────────────────────────────
async def _agent_ollama(text: str) -> None:
    import ollama
    client   = ollama.AsyncClient()
    messages: list[dict] = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user",   "content": text},
    ]

    for _ in range(8):
        response = await client.chat(model=OLLAMA_MODEL, messages=messages, tools=TOOLS)
        msg = response.message

        if not msg.tool_calls:
            if msg.content:
                await _emit_final(msg.content.strip())
            break

        if msg.content and msg.content.strip():
            await broadcast({"type": "llm_response", "text": msg.content.strip()})

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            obs = await _run_tool(tc.function.name, tc.function.arguments or {})
            messages.append({"role": "tool", "content": obs})


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
    global _current_task
    busy = (
        (_current_task and not _current_task.done())
        or (_speak_task and not _speak_task.done())
        or _tts_playing
    )
    if busy:
        await _stop_speaking()
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

    client = _openai_mod.AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
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
async def handle_command(text: str) -> None:
    global agent_trace

    if not text.strip():
        return

    await broadcast({"type": "state", "status": "thinking", "text": "Processing directive..."})

    try:
        question = _deliberation_target(text)
        if question and USE_GROQ and _HAS_GROQ:
            await _deliberate(question)
        elif USE_GROQ and _HAS_GROQ:
            await _agent_groq(text)
        elif USE_CLAUDE and _HAS_ANTHROPIC:
            await _agent_claude(text)
        else:
            await _agent_ollama(text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await broadcast({"type": "llm_response", "text": f"Agent error: {exc}"})

    await broadcast({"type": "state", "status": "idle"})


# ── TTS ────────────────────────────────────────────────────────────────────────
async def _speak(text: str) -> None:
    global _speaking_text
    try:
        import edge_tts
    except ImportError:
        return

    clean = re.sub(r"[*_`#\[\]()]", "", text).strip()
    if not clean:
        return

    # Record what we're about to say so the mic can tell JARVIS's own voice (echo)
    # apart from the user. Cleared by the frontend's tts_end signal.
    _speaking_text = clean.lower()
    await broadcast({"type": "state", "status": "speaking", "text": "Speaking..."})
    try:
        audio_bytes = b""
        communicate = edge_tts.Communicate(clean, _tts_voice, rate=TTS_RATE, pitch=TTS_PITCH)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
        if audio_bytes:
            b64 = base64.b64encode(audio_bytes).decode()
            await broadcast({"type": "tts_audio", "data": b64})
    except asyncio.CancelledError:
        _speaking_text = ""
        raise
    except Exception:
        pass
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
            api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1",
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
        if len(utterance) < MIN_UTTER_CHUNKS:
            return
        all_audio = np.concatenate(utterance, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(RATE)
            wf.writeframes(all_audio.tobytes())
        text = _transcribe(buf.getvalue())
        if not text:
            return

        # Wake-word gate: ignore anything not addressed to JARVIS.
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

            while _listening:
                try:
                    chunk = audio_q.get(timeout=0.3)
                except Q.Empty:
                    continue

                # Note: the mic stays live even while JARVIS speaks, so you can
                # barge in with the wake word. Self-talk is prevented by the
                # wake-word gate + echo guard in _flush(), not by muting.
                energy = int(np.abs(chunk).mean())
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
    disk = psutil.disk_usage("C:\\")
    return {
        "brain": {
            "primary_llm":          _active_model(),
            "local_model":          OLLAMA_MODEL,
            "reasoning":            GROQ_REASONING if "gpt-oss" in GROQ_MODEL else "—",
            "max_agent_steps":      8,
            "use_llm_intent_router": False,
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
        },
        "memory": {
            "available": True,
            "count":     len(memories),
        },
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


@app.get("/health")
async def health() -> dict:
    return {
        "status":   "ok",
        "model":    _active_model(),
        "time":     datetime.now().isoformat(),
        "memories": len(memories),
    }


# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    brain = "Groq" if (USE_GROQ and _HAS_GROQ) else "Claude" if (USE_CLAUDE and _HAS_ANTHROPIC) else "Ollama"
    print(f"[JARVIS] Backend online — model: {_active_model()} ({brain})")
    print(f"[JARVIS] Memory: {len(memories)} entries | Tools: {len(TOOLS)}")

    dist_dir = BASE_DIR / "jarvis" / "dist"
    if dist_dir.exists():
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="ui")
        print(f"[JARVIS] Serving UI from {dist_dir}")
    else:
        print("[JARVIS] Dev mode — UI served by Vite on :8080")


# ── Entry ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("JARVIS_PORT", 8000))
    host = os.getenv("JARVIS_HOST", "127.0.0.1")
    print(f"[JARVIS] Starting on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")

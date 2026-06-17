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
import re
import subprocess
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import ollama
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

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL   = "llama-3.3-70b-versatile"
USE_GROQ     = bool(GROQ_API_KEY)

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
                asyncio.create_task(handle_command(data.get("text", "")))
            elif action == "start_listening":
                asyncio.create_task(_start_voice())
            elif action == "stop_listening":
                _stop_voice()
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
                img.save(buf, format="PNG", optimize=True)
                img_bytes = buf.getvalue()

            # Use llava (vision model) to describe the screen
            resp = ollama.chat(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": "Describe what is on this screen concisely. Focus on the main content and any important details.",
                    "images": [img_bytes],
                }],
            )
            return resp.message.content
        except ImportError:
            return "mss/Pillow not installed. Run: pip install mss Pillow"
        except Exception as exc:
            return f"Screen capture failed: {exc}"

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

    return f"Unknown tool: {name}"


# ── System prompt ──────────────────────────────────────────────────────────────
_BASE_PROMPT = """You are JARVIS — Just A Rather Very Intelligent System. Personal AI of Roshan Srikanth, running on Windows 11 as a desktop app.

Roshan: cybersecurity researcher, CTF player/creator (pwn, web, crypto, forensics, reversing), Python scripter, exploit developer.

Personality: calm, sharp, dry wit. Never verbose. Answer directly. If it's a simple question, answer it — don't narrate your process. When you use a tool, report the result, not what you're about to do.

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
    await broadcast({"type": "llm_response", "text": text})
    asyncio.create_task(_speak(text))


# ── Groq agent loop (primary — free 70B, ~500 tok/s, streaming) ────────────────
async def _agent_groq(text: str) -> None:
    client = _openai_mod.AsyncOpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1",
    )
    messages: list[dict] = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user",   "content": text},
    ]

    for _ in range(8):
        stream = await client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=4096,
            stream=True,
        )

        full_text = ""
        tool_calls_raw: dict[int, dict] = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            if delta.content:
                full_text += delta.content
                await broadcast({"type": "llm_chunk", "text": delta.content})
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_raw:
                        tool_calls_raw[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:                   tool_calls_raw[idx]["id"]   = tc.id
                    if tc.function.name:        tool_calls_raw[idx]["name"] = tc.function.name
                    if tc.function.arguments:   tool_calls_raw[idx]["arguments"] += tc.function.arguments

        if not tool_calls_raw:
            if full_text.strip():
                await _emit_final(full_text.strip())
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
            obs = await _run_tool(tc["name"], args)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": obs})


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


# ── Agent dispatch ──────────────────────────────────────────────────────────────
async def handle_command(text: str) -> None:
    global agent_trace

    if not text.strip():
        return

    await broadcast({"type": "state", "status": "thinking", "text": "Processing directive..."})

    try:
        if USE_GROQ and _HAS_GROQ:
            await _agent_groq(text)
        elif USE_CLAUDE and _HAS_ANTHROPIC:
            await _agent_claude(text)
        else:
            await _agent_ollama(text)
    except Exception as exc:
        await broadcast({"type": "llm_response", "text": f"Agent error: {exc}"})

    await broadcast({"type": "state", "status": "idle"})


# ── TTS ────────────────────────────────────────────────────────────────────────
async def _speak(text: str) -> None:
    try:
        import edge_tts
    except ImportError:
        return

    clean = re.sub(r"[*_`#\[\]()]", "", text).strip()
    if not clean:
        return

    await broadcast({"type": "state", "status": "speaking", "text": "Speaking..."})
    try:
        audio_bytes = b""
        communicate = edge_tts.Communicate(clean, "en-GB-RyanNeural")
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_bytes += chunk["data"]
        if audio_bytes:
            b64 = base64.b64encode(audio_bytes).decode()
            await broadcast({"type": "tts_audio", "data": b64})
    except Exception:
        pass
    await broadcast({"type": "state", "status": "idle"})


# ── STT ────────────────────────────────────────────────────────────────────────
async def _start_voice() -> None:
    global _listening, _listen_thread
    if _listening:
        return
    _listening = True
    await broadcast({"type": "state", "status": "listening", "text": "Listening..."})
    _listen_thread = threading.Thread(target=_voice_worker, daemon=True)
    _listen_thread.start()


def _stop_voice() -> None:
    global _listening
    _listening = False
    broadcast_from_thread({"type": "state", "status": "idle", "text": "Mic off."})


def _voice_worker() -> None:
    import queue as Q
    import wave

    try:
        import sounddevice as sd
        import numpy as np
        import speech_recognition as sr
    except ImportError as exc:
        broadcast_from_thread({
            "type": "system",
            "text": f"Voice deps missing: {exc}. Run: pip install sounddevice SpeechRecognition",
        })
        return

    RATE = 16000
    CHUNK = 1024          # ~64ms per callback at 16kHz
    SPEECH_THRESH = 500   # mean absolute value → speech onset
    SILENCE_THRESH = 150  # mean absolute value → silence
    SILENCE_CHUNKS = 20   # ~1.3 s of silence ends the utterance

    audio_q: Q.Queue = Q.Queue()

    def _cb(indata, frames, time_info, status):
        audio_q.put(indata.copy())

    r = sr.Recognizer()

    try:
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, callback=_cb):
            broadcast_from_thread({"type": "system", "text": "Mic online."})

            recording = False
            utterance: list = []
            silence_cnt = 0

            while _listening:
                try:
                    chunk = audio_q.get(timeout=0.3)
                except Q.Empty:
                    continue

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
                            all_audio = np.concatenate(utterance, axis=0)
                            utterance = []

                            buf = io.BytesIO()
                            with wave.open(buf, "wb") as wf:
                                wf.setnchannels(1)
                                wf.setsampwidth(2)
                                wf.setframerate(RATE)
                                wf.writeframes(all_audio.tobytes())
                            buf.seek(0)

                            try:
                                with sr.AudioFile(buf) as source:
                                    audio = r.record(source)
                                text = r.recognize_google(audio)
                                if text and text.strip():
                                    broadcast_from_thread({"type": "transcription", "text": text})
                                    if _main_loop and not _main_loop.is_closed():
                                        asyncio.run_coroutine_threadsafe(
                                            handle_command(text), _main_loop
                                        )
                            except sr.UnknownValueError:
                                pass
                    else:
                        silence_cnt = 0

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
            "max_agent_steps":      8,
            "use_llm_intent_router": False,
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

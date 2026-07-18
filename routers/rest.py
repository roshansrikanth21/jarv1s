"""HTTP REST routes for JARVIS — registered from api.py via register(app).

Late-imports api inside register() so agent/WS symbols exist first. Behaviour matches
the former inline routes in api.py. Mutable process state is always read/written via
`core.<name>` so closures never freeze scalars.
"""
from __future__ import annotations

import asyncio
import base64
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register(app: FastAPI) -> None:
    import api as core

    _disk_root = core._disk_root
    _routing_label = core._routing_label
    _active_model = core._active_model
    _short_model = core._short_model
    _stt_available = core._stt_available
    _user_name = core._user_name
    _available_rungs = core._available_rungs
    _ambient_brief = core._ambient_brief
    _homeostasis = core._homeostasis
    _mutating_allowed = core._mutating_allowed
    dispatch_command = core.dispatch_command
    _ict_analyze = core._ict_analyze
    broadcast = core.broadcast
    _save_json = core._save_json
    _stop_voice = core._stop_voice
    _start_voice = core._start_voice
    _save_settings = core._save_settings
    _analyze_image = core._analyze_image
    execute_tool = core.execute_tool

    @app.get("/api/agent/status")
    async def agent_status() -> dict:
        cpu  = core.psutil.cpu_percent(interval=0)
        vm   = core.psutil.virtual_memory()
        disk = core.psutil.disk_usage(_disk_root())
        try:
            _rss_mb = round(core.psutil.Process().memory_info().rss / (1024 * 1024), 1)
        except Exception:
            _rss_mb = None
        return {
            "brain": {
                "primary_llm":          _routing_label(),
                "configured_default":   _active_model(),
                "last_rung":            (core._last_decision or {}).get("rung"),
                "local_model":          (core._settings.get("local_model") or core.LOCAL_DEEP or core.LOCAL_FAST or None),
                "reasoning":            core.GROQ_REASONING if "gpt-oss" in core.GROQ_MODEL else "—",
                "max_agent_steps":      8,
            },
            "conversation": {
                "turns": len(core._history) // 2,
            },
            "council": {
                "panel": [_short_model(m) for m in core.MOA_PROPOSERS],
                "chair": _short_model(core.MOA_AGGREGATOR),
            },
            "voice": {
                "current": core._tts_voice,
                "options": core.VOICE_OPTIONS,
                "tts": True,
                "stt": _stt_available(),
                "stt_hint": None if _stt_available() else
                            "Mic input needs faster-whisper (local, offline) or a free Groq API key. Speech output does not.",
            },
            "user": {
                "name":      _user_name(),
                "onboarded": bool(_user_name()),
            },
            "watch": {
                "watching":     core._watching,
                "watchlist":    core.WATCHLIST,
                "interval_min": core.WATCH_INTERVAL_MIN,
                "tf":           core.WATCH_TF,
            },
            "memory": {
                "available": True,
                "count":     core.cortex.stats().get("facts", 0),
            },
            "governor": {
                "mode":      core._gov.mode,
                "available": sorted(_available_rungs()),
                "metrics":   core._gov.metrics(),
            },
            "emotion": core._persona.snapshot() if core.persona_mod.ENABLED else {"enabled": False},
            "ambient": _ambient_brief(core.ambient.snapshot()),
            "perception": core._last_read.summary() if core._last_read else None,
            "homeostasis": _homeostasis(core._last_device) if core._last_device else None,
            "device_tier": (core._last_device or {}).get("tier"),
            "local": {"enabled": core._LOCAL_OK, "fast": core.LOCAL_FAST, "deep": core.LOCAL_DEEP,
                      "pinned": core._settings.get("local_model")},
            "tools": [
                {"name": t["function"]["name"], "description": t["function"]["description"]}
                for t in core.TOOLS
            ],
            "tasks": core.task_list,
            "trace": core.agent_trace[-25:],
            "sys": {
                "cpu":  round(cpu),
                "ram":  round(vm.percent),
                # Host volume fill (not JARVIS I/O). Free space is the actionable signal.
                "disk": round(disk.percent),
                "disk_free_gb": round(disk.free / (1024 ** 3), 1),
                "jarvis_rss_mb": _rss_mb,
            },
        }


    @app.post("/api/command")
    async def command_endpoint(request: Request) -> JSONResponse:
        # Same origin/loopback gate as settings/upload — this endpoint drives the full agent
        # (shell, desktop, browse). An ungated POST was the highest-impact CSRF surface.
        if not _mutating_allowed(request):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        text = (body.get("command") or "").strip()
        if not text:
            return JSONResponse({"error": "empty command"}, status_code=400)
        # Must go through dispatch_command — same barge-in + turn-generation path as WS/voice.
        # Calling handle_command directly allowed overlapping agents and skipped interrupts.
        asyncio.create_task(dispatch_command(text))
        return JSONResponse({"status": "processing"})


    @app.get("/api/ict")
    async def ict_endpoint(symbol: str = "nifty", interval: str = "15m") -> dict:
        """Structured ICT read for the Markets panel."""
        return await asyncio.to_thread(_ict_analyze, symbol, interval)


    # ── Global settings (the shared Settings panel talks to these; keys are separate,
    #    handled by Electron safeStorage — never sent here) ──────────────────────────
    @app.get("/api/settings")
    async def get_settings() -> dict:
        return {
            "user_name":       _user_name(),
            "voice":           core._tts_voice,
            "voice_options":   core.VOICE_OPTIONS,
            "mode":            core._gov.mode,
            "modes":           list(core.governor.MODES),
            "always_listen":   core.ALWAYS_LISTEN,
            "store_overheard": core.STORE_OVERHEARD,
            "stt":             _stt_available(),
        }


    @app.post("/api/settings")
    async def post_settings(request: Request) -> JSONResponse:
        """Apply a subset of non-secret prefs and broadcast changes so every connected deck
        stays in sync. API keys are NOT accepted here — they go through Electron safeStorage."""
        # Mutate api module globals (settings live on the core process)
        if not _mutating_allowed(request):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        if "name" in body:
            nm = (str(body.get("name") or "")).strip()[:40]
            if nm:
                core._settings["user_name"] = nm
                await broadcast({"type": "name_changed", "name": nm})
        if "voice" in body:
            vid = body.get("voice", "")
            if vid in {v["id"] for v in core.VOICE_OPTIONS}:
                core._tts_voice = vid
                core._settings["voice"] = vid
                await broadcast({"type": "voice_changed", "voice": vid})
        if "mode" in body:
            mode = (str(body.get("mode") or "")).strip()
            if mode in core.governor.MODES:
                core._gov.mode = mode
                _save_json(core.GOVERNOR_FILE, core._gov.to_dict())
                await broadcast({"type": "governor_mode", "mode": mode})
        if "always_listen" in body:
            core.ALWAYS_LISTEN = bool(body["always_listen"])
            core._settings["always_listen"] = core.ALWAYS_LISTEN
            if not core.ALWAYS_LISTEN and core._listening:
                _stop_voice()                      # honor "off" immediately
            elif core.ALWAYS_LISTEN and not core._listening and core.active_connections:
                asyncio.create_task(_start_voice())
        if "store_overheard" in body:
            core.STORE_OVERHEARD = bool(body["store_overheard"])
            core._settings["store_overheard"] = core.STORE_OVERHEARD
        _save_settings()
        return JSONResponse({"ok": True})


    # ── Device / Models / Governor / Memory APIs ─────────────────────────────────────
    @app.get("/api/device")
    async def device_endpoint() -> dict:
        """Hardware profile + live power state + homeostasis."""
        # Mutate api._last_device via core module
        dev = await asyncio.to_thread(core.device.profile)
        core._last_device = dev
        return {**dev, "homeostasis": _homeostasis(dev)}


    @app.get("/api/models")
    async def models_endpoint() -> dict:
        """Model Advisor: what's installed, running, and recommended for this machine."""
        def _gather() -> dict:
            up, ver = core.models_advisor.ollama_up()
            dev = core.device.profile()
            budget = core.models_advisor.model_budget(dev)
            ranked = core.models_advisor.ranked_for_device(dev, set())
            if not up:
                return {"ollama": False, "tier": dev.get("tier"), "budget": budget,
                        "recommended": core.models_advisor.recommend_for_device(dev, set()),
                        "ranked": ranked,
                        "benchmarks": core.models_advisor.load_benchmarks(),
                        "allowed_count": len(core.models_advisor.allowed_models(dev))}
            inst = core.models_advisor.annotate_installed(dev, core.models_advisor.installed(with_caps=True))
            names = {m["name"] for m in inst}
            ranked = core.models_advisor.ranked_for_device(dev, names)
            out = {"ollama": True, "version": ver, "tier": dev.get("tier"),
                    "installed": inst, "running": core.models_advisor.running(),
                    "recommended": core.models_advisor.recommend_for_device(dev, names),
                    "ranked": ranked,
                    "benchmarks": core.models_advisor.load_benchmarks(),
                    "budget": budget,
                    "allowed_count": len(core.models_advisor.allowed_models(dev)),
                    "active": {"fast": core.LOCAL_FAST, "deep": core.LOCAL_DEEP, "enabled": core._LOCAL_OK},
                    "pinned": core._settings.get("local_model")}
            return out
        return await asyncio.to_thread(_gather)


    @app.get("/api/models/loaded")
    async def models_loaded_endpoint() -> dict:
        """Live snapshot of models Ollama currently has in memory (poll-friendly)."""
        running = await asyncio.to_thread(core.models_advisor.running)
        return {"running": running, "ts": time.time()}



    @app.get("/api/governor")
    async def governor_endpoint() -> dict:
        """The Governor's policy, lattice, learned metrics, and recent decisions."""
        dev = core._last_device or await asyncio.to_thread(core.device.profile)
        avail = _available_rungs()
        rungs = [{**r, "available": r["id"] in avail} for r in core.governor.RUNGS]
        return {"mode": core._gov.mode, "modes": list(core.governor.MODES), "rungs": rungs,
                "available": sorted(avail), "metrics": core._gov.metrics(),
                "recent": core._gov.log[-12:], "homeostasis": _homeostasis(dev)}


    @app.get("/api/memory")
    async def memory_endpoint(limit: int = 60) -> dict:
        """The inspectable self-model — durable core.memories, newest first. Reads core.cortex (the real
        store both explicit `remember` calls AND automatic post-turn extraction write to) rather
        than the legacy JSON mirror, which only ever saw explicit `remember`s."""
        # Clamp — UI wants a digest, not an unbounded dump of the whole store.
        lim = max(1, min(int(limit or 60), 200))
        all_f = core.cortex.store.all_facts()
        ordered = sorted(all_f, key=lambda f: f.get("created_at") or "", reverse=True)
        return {"count": len(all_f),
                "memories": [{"id": f.get("id"), "content": f.get("text"),
                              "category": f.get("category", "preference"),
                              "importance": f.get("importance", 5),
                              "private": bool(f.get("private")),
                              "source": "extracted" if f.get("source_episode_id") else "manual"}
                             for f in ordered[:lim]]}


    # ── Attachment uploads — images/docs the UI drops into chat ────────────────────
    # The endpoint extracts a plain-text digest at upload time (vision for images,
    # text extraction for documents), so by the time the user hits send, the brain
    # receives ready-made context and can answer without the user explaining the file.
    UPLOADS_DIR = core.BASE_DIR / "uploads"
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


    def _digest_upload(path, ext: str) -> tuple[str, str, bool]:
        """(kind, extracted text, extracted?) for a saved upload. Runs in a worker thread.
        `extracted` is False when we couldn't get real content — unsupported type, empty file,
        or image vision unavailable — so the caller flags it instead of feeding the brain a stub
        (e.g. the 'Vision needs a Groq API key.' sentence) as if it were the file's content."""
        if ext in _IMAGE_EXTS:
            if not core.USE_GROQ:
                return "image", "", False        # no vision backend — don't pass a stub off as content
            desc = _analyze_image(str(path),
                                  "Describe this image thoroughly. Transcribe ALL visible "
                                  "text exactly as written. Note anything unusual or "
                                  "noteworthy — the user attached it for a reason.")
            return "image", desc, bool((desc or "").strip())
        if ext == ".pdf":
            t = _extract_pdf(path)
            return "pdf", t, bool(t.strip())
        if ext == ".docx":
            t = _extract_docx(path)
            return "docx", t, bool(t.strip())
        if ext in _TEXT_EXTS:
            t = path.read_text(encoding="utf-8", errors="ignore")
            return "text", t, bool(t.strip())
        return "binary", "", False


    @app.post("/api/upload")
    async def upload_endpoint(request: Request) -> JSONResponse:
        # Same origin/loopback gate the websocket uses — this is the one endpoint that writes
        # files to disk + triggers cloud vision, so it must not be the least-protected surface.
        if not _mutating_allowed(request):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        # Reject oversize BEFORE buffering + base64-decoding the whole body into memory (DoS guard).
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > _UPLOAD_MAX * 2:
            return JSONResponse({"error": f"file too large (max {_UPLOAD_MAX // (1024*1024)} MB)"},
                                status_code=413)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        name = os.path.basename(str(body.get("name") or "file"))
        b64 = body.get("data_b64") or ""
        if not isinstance(b64, str) or len(b64) > _UPLOAD_MAX * 4 // 3 + 1024:   # b64 ≈ 4/3 of raw
            return JSONResponse({"error": f"file too large (max {_UPLOAD_MAX // (1024*1024)} MB)"},
                                status_code=413)
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            return JSONResponse({"error": "data_b64 is not valid base64"}, status_code=400)
        if not raw:
            return JSONResponse({"error": "empty file"}, status_code=400)
        if len(raw) > _UPLOAD_MAX:
            return JSONResponse({"error": f"file too large (max {_UPLOAD_MAX // (1024*1024)} MB)"},
                                status_code=413)

        UPLOADS_DIR.mkdir(exist_ok=True)
        ext = Path(name).suffix.lower()
        # uuid prefix: unique per upload so near-simultaneous drops never collide/overwrite.
        safe = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{re.sub(r'[^A-Za-z0-9._-]', '_', name)}"
        dest = UPLOADS_DIR / safe
        dest.write_bytes(raw)

        # Only the extracted digest is ever used again — never the file itself — so delete it
        # after extraction (in a finally) to keep uploads/ from growing without bound.
        try:
            kind, text, extracted = await asyncio.to_thread(_digest_upload, dest, ext)
        except Exception as exc:
            return JSONResponse({"ok": True, "name": name, "kind": "binary", "digest": "",
                                 "extracted": False,
                                 "note": f"attached, but its content couldn't be read: {exc}"})
        finally:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
        digest = (text or "").strip()
        truncated = len(digest) > _DIGEST_CAP
        if truncated:
            digest = digest[:_DIGEST_CAP] + "\n…[truncated]"
        note = None
        if not extracted:
            note = ("image vision unavailable — add a Groq key in Settings"
                    if kind == "image" else "no readable content could be extracted")
        return JSONResponse({"ok": True, "name": name, "kind": kind, "digest": digest,
                             "extracted": extracted, "truncated": truncated, "note": note})


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
        # execute_tool writes to core.cortex (SQLite + vector) and the legacy JSON mirror.
        # It does blocking disk I/O — run it off the event loop.
        result = await asyncio.to_thread(execute_tool, "remember", args)
        return JSONResponse({"ok": True, "result": result, "count": len(core.memories)})


    @app.get("/api/memory/recall")
    async def hub_recall(request: Request, q: str, k: int = 6,
                         namespace: str | None = None) -> JSONResponse:
        if not _hub_authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        def _do_recall():
            # External callers NEVER see private core.memories — that filter is not optional here.
            return core.cortex.recall(q, k=max(1, min(k, 20)),
                                 namespace=namespace, include_private=False)

        hits = await asyncio.to_thread(_do_recall)
        return JSONResponse({"count": len(hits), "memories": [
            {"content": m.get("text"), "category": m.get("category", "preference"),
             "importance": m.get("importance", 5),
             "confidence": m.get("confidence", 0.7),
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
            "memories": core.cortex.stats().get("facts", 0),
        }



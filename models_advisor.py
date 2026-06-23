"""
models_advisor.py — the Model Advisor: profiles which local models the machine can
run, recommends the best ones for its hardware tier, benchmarks real tokens/sec,
and pulls models on demand. Talks to Ollama (default http://localhost:11434).

Pure-ish module: uses the `ollama` python client; api.py drives it off the event
loop via asyncio.to_thread and broadcasts pull progress.
"""
from __future__ import annotations

import time

try:
    import ollama as _ollama
    _HAS_OLLAMA = True
except Exception:
    _HAS_OLLAMA = False


# Conservative, currently-real Ollama tags per capability tier. Qwen2.5 leads for
# reliable tool calling; gpt-oss adds reasoning where the RAM allows.
RECOMMENDED: dict[str, list[dict]] = {
    "potato": [
        {"tag": "llama3.2:3b", "params": "3B", "gb": 2.0, "note": "tiny, runs anywhere"},
        {"tag": "qwen2.5:3b",  "params": "3B", "gb": 1.9, "note": "best small tool-caller"},
    ],
    "light": [
        {"tag": "qwen2.5:3b",  "params": "3B", "gb": 1.9, "note": "best small tool-caller"},
        {"tag": "llama3.2:3b", "params": "3B", "gb": 2.0, "note": "mature, well-supported"},
    ],
    "balanced": [
        {"tag": "qwen2.5:7b",  "params": "7B", "gb": 4.7, "note": "default — strong tools"},
        {"tag": "llama3.1:8b", "params": "8B", "gb": 4.9, "note": "solid generalist"},
        {"tag": "gpt-oss:20b", "params": "20B MoE", "gb": 14.0, "note": "reasoning+tools (needs ~16GB)"},
    ],
    "heavy": [
        {"tag": "qwen2.5:14b", "params": "14B", "gb": 9.0, "note": "great quality/size"},
        {"tag": "qwen2.5:32b", "params": "32B", "gb": 20.0, "note": "top dense at 24GB"},
        {"tag": "gpt-oss:20b", "params": "20B MoE", "gb": 14.0, "note": "reasoning+tools"},
    ],
    "workstation": [
        {"tag": "qwen2.5:32b",  "params": "32B", "gb": 20.0, "note": "best quality/GB"},
        {"tag": "llama3.3:70b", "params": "70B", "gb": 43.0, "note": "stable frontier-ish"},
        {"tag": "gpt-oss:120b", "params": "120B MoE", "gb": 65.0, "note": "needs 80GB+"},
    ],
}


def ollama_up() -> tuple[bool, str | None]:
    """(running, version). Cheap liveness probe; short timeout so the UI never hangs."""
    if not _HAS_OLLAMA:
        return False, None
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/version", timeout=1.0)
        return (r.status_code == 200), r.json().get("version")
    except Exception:
        # Fall back to the client (also confirms reachability).
        try:
            _ollama.list()
            return True, None
        except Exception:
            return False, None


def _details(m) -> dict:
    d = getattr(m, "details", None) or (m.get("details", {}) if isinstance(m, dict) else {})
    get = (lambda k: getattr(d, k, None)) if not isinstance(d, dict) else d.get
    return {"params": get("parameter_size"), "quant": get("quantization_level")}


def _name(m) -> str:
    return getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else "") or ""


def _supports_tools(name: str) -> bool:
    try:
        info = _ollama.show(name)
        caps = getattr(info, "capabilities", None) or (info.get("capabilities") if isinstance(info, dict) else []) or []
        return "tools" in caps
    except Exception:
        return False


def installed(with_caps: bool = True) -> list[dict]:
    """Locally installed models with size + (optionally) tool capability."""
    if not _HAS_OLLAMA:
        return []
    try:
        resp = _ollama.list()
        models = getattr(resp, "models", None) or resp.get("models", [])
    except Exception:
        return []
    out = []
    for m in models:
        name = _name(m)
        if not name:
            continue
        size = getattr(m, "size", None) or (m.get("size") if isinstance(m, dict) else None) or 0
        det = _details(m)
        out.append({
            "name": name,
            "gb": round(size / 1024 ** 3, 1) if size else None,
            "params": det["params"],
            "quant": det["quant"],
            "tools": _supports_tools(name) if with_caps else None,
        })
    return out


def running() -> list[dict]:
    """Models currently loaded in memory (and whether on GPU)."""
    if not _HAS_OLLAMA:
        return []
    try:
        resp = _ollama.ps()
        models = getattr(resp, "models", None) or resp.get("models", [])
    except Exception:
        return []
    out = []
    for m in models:
        name = _name(m)
        size = getattr(m, "size", 0) or (m.get("size", 0) if isinstance(m, dict) else 0)
        vram = getattr(m, "size_vram", 0) or (m.get("size_vram", 0) if isinstance(m, dict) else 0)
        out.append({"name": name,
                    "gb": round(size / 1024 ** 3, 1) if size else None,
                    "on_gpu": bool(vram and size and vram >= size * 0.9)})
    return out


def recommend(tier: str, installed_names: set[str]) -> list[dict]:
    """Recommended models for the hardware tier, marking which are already installed."""
    base = (installed_names or set())
    short = {n.split(":")[0] for n in base}
    out = []
    for rec in RECOMMENDED.get(tier, RECOMMENDED["balanced"]):
        tag = rec["tag"]
        out.append({**rec, "installed": tag in base or tag.split(":")[0] in short})
    return out


def benchmark(model: str) -> dict:
    """Measure real decode tokens/sec with one short generation. Subtracts model
    load time (reported separately) so warm vs cold is visible."""
    if not _HAS_OLLAMA:
        return {"ok": False, "error": "Ollama not available."}
    try:
        r = _ollama.generate(model=model, prompt="In one sentence, what is a fair value gap?",
                             stream=False, options={"num_predict": 64})
        ec = getattr(r, "eval_count", None) or (r.get("eval_count") if isinstance(r, dict) else None)
        ed = getattr(r, "eval_duration", None) or (r.get("eval_duration") if isinstance(r, dict) else None)
        ld = getattr(r, "load_duration", 0) or (r.get("load_duration", 0) if isinstance(r, dict) else 0)
        if not ec or not ed:
            return {"ok": False, "error": "No timing data returned."}
        return {"ok": True, "model": model,
                "tok_per_sec": round(ec / ed * 1e9, 1),
                "tokens": ec, "load_s": round((ld or 0) / 1e9, 2)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def pull(model: str, on_progress) -> dict:
    """Pull a model, calling on_progress({status, pct}) as it streams. Blocking —
    run in a thread."""
    if not _HAS_OLLAMA:
        return {"ok": False, "error": "Ollama not available."}
    try:
        last = 0.0
        for ev in _ollama.pull(model, stream=True):
            status = ev.get("status", "") if isinstance(ev, dict) else getattr(ev, "status", "")
            total = ev.get("total") if isinstance(ev, dict) else getattr(ev, "total", None)
            done = ev.get("completed") if isinstance(ev, dict) else getattr(ev, "completed", None)
            pct = round(done / total * 100, 1) if (total and done) else last
            last = pct
            on_progress({"status": status, "pct": pct})
        on_progress({"status": "success", "pct": 100.0})
        return {"ok": True, "model": model}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

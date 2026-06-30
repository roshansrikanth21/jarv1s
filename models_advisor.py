"""
models_advisor.py — the Model Advisor: profiles which local models the machine can
run, recommends the best ones for its hardware tier, benchmarks real tokens/sec,
and pulls models on demand. Talks to Ollama (default http://localhost:11434).

Pure-ish module: uses the `ollama` python client; api.py drives it off the event
loop via asyncio.to_thread and broadcasts pull progress.
"""
from __future__ import annotations

import re
import time

try:
    import ollama as _ollama
    _HAS_OLLAMA = True
except Exception:
    _HAS_OLLAMA = False


# Currently-real, tool-capable Ollama tags per hardware tier. Every model here does
# native function-calling (JARVIS's local rungs require it). The first entry in each
# tier is the smart default ("best" for that machine); the rest are honest trade-offs.
# Each carries what it's GOOD at (best_for) and what it ISN'T (limits) so the picker
# can tell the user the truth, not just a size. Qwen3 leads small tool-calling in 2026;
# Llama keeps the long-context generalist slot; gpt-oss adds real reasoning up-tier.
RECOMMENDED: dict[str, list[dict]] = {
    "potato": [
        {"tag": "qwen3:1.7b",  "params": "1.7B", "gb": 1.4, "ctx": "32K", "tools": True,
         "best_for": "Runs on almost anything — quick replies, basic tool use.",
         "limits": "Light reasoning; will lean on the cloud for anything hard."},
        {"tag": "llama3.2:1b",  "params": "1B", "gb": 1.3, "ctx": "128K", "tools": True,
         "best_for": "Absolute smallest footprint, long context for its size.",
         "limits": "Weakest reasoner here; best as a fast fallback."},
        {"tag": "qwen2.5:3b",   "params": "3B", "gb": 1.9, "ctx": "32K", "tools": True,
         "best_for": "Proven, stable small tool-caller.",
         "limits": "Older generation than Qwen3."},
    ],
    "light": [
        {"tag": "qwen3:4b",   "params": "4B", "gb": 2.5, "ctx": "32K", "tools": True,
         "best_for": "Best small all-rounder — reliable tools, snappy on CPU.",
         "limits": "Not for deep multi-step reasoning."},
        {"tag": "qwen3:1.7b", "params": "1.7B", "gb": 1.4, "ctx": "32K", "tools": True,
         "best_for": "Lighter and faster if 4B feels sluggish.",
         "limits": "Lower answer quality than 4B."},
        {"tag": "llama3.2:3b", "params": "3B", "gb": 2.0, "ctx": "128K", "tools": True,
         "best_for": "Mature, well-supported, big context window.",
         "limits": "Tool calls slightly less consistent than Qwen3."},
    ],
    "balanced": [
        {"tag": "qwen3:8b",   "params": "8B", "gb": 5.2, "ctx": "40K", "tools": True,
         "best_for": "The sweet spot — strongest small tool-caller, fast and dependable.",
         "limits": "Big reasoning problems still favour a cloud model."},
        {"tag": "qwen2.5:7b", "params": "7B", "gb": 4.7, "ctx": "128K", "tools": True,
         "best_for": "Proven generalist with very long context.",
         "limits": "A step behind Qwen3 8B on tool reliability."},
        {"tag": "llama3.1:8b", "params": "8B", "gb": 4.9, "ctx": "128K", "tools": True,
         "best_for": "Solid all-rounder, huge 128K context.",
         "limits": "Older; can be wordy and miss tool calls."},
        {"tag": "qwen3:14b",  "params": "14B", "gb": 9.3, "ctx": "40K", "tools": True,
         "best_for": "Noticeably smarter if you have the RAM and patience.",
         "limits": "Slower on CPU-only machines."},
    ],
    "heavy": [
        {"tag": "qwen3:14b",  "params": "14B", "gb": 9.3, "ctx": "40K", "tools": True,
         "best_for": "Best quality-per-GB — sharp tools and reasoning.",
         "limits": "Wants a GPU to feel fast."},
        {"tag": "gpt-oss:20b", "params": "20B MoE", "gb": 14.0, "ctx": "128K", "tools": True,
         "best_for": "Real chain-of-thought reasoning plus tools (~16GB).",
         "limits": "Heavier download; overkill for simple chat."},
        {"tag": "qwen3:30b",  "params": "30B-A3B", "gb": 18.0, "ctx": "40K", "tools": True,
         "best_for": "MoE — 30B smarts at ~3B speed when it fits.",
         "limits": "Needs ~20GB free RAM/VRAM."},
        {"tag": "qwen2.5:32b", "params": "32B", "gb": 20.0, "ctx": "128K", "tools": True,
         "best_for": "Top dense model at this tier, long context.",
         "limits": "Large; slow without a strong GPU."},
    ],
    "workstation": [
        {"tag": "qwen3:32b",   "params": "32B", "gb": 20.0, "ctx": "40K", "tools": True,
         "best_for": "Best quality you can run fully local on a workstation.",
         "limits": "Needs a real GPU for snappy replies."},
        {"tag": "gpt-oss:20b",  "params": "20B MoE", "gb": 14.0, "ctx": "128K", "tools": True,
         "best_for": "Lighter reasoning powerhouse if you want speed too.",
         "limits": "Slightly below 32B on raw quality."},
        {"tag": "llama3.3:70b", "params": "70B", "gb": 43.0, "ctx": "128K", "tools": True,
         "best_for": "Frontier-ish local quality, 128K context.",
         "limits": "Needs ~48GB; heavy download."},
        {"tag": "gpt-oss:120b", "params": "120B MoE", "gb": 65.0, "ctx": "128K", "tools": True,
         "best_for": "The ceiling for local — near-cloud quality.",
         "limits": "Needs 80GB+ of memory; not for laptops."},
    ],
}


# How much headroom to leave for Windows, JARVIS, browser, and Ollama runtime overhead.
RAM_RESERVE_MIN_GB = 3.0
RAM_RESERVE_MAX_GB = 5.0
RAM_RESERVE_PCT = 0.15          # % of total RAM held back for the OS + apps
RAM_USABLE_PCT = 0.70           # max share of (total − reserve) offered to a model
MODEL_RAM_OVERHEAD = 1.35       # weights + KV cache / activations beyond on-disk size
MAX_RECOMMENDATIONS = 5


def _catalog() -> list[dict]:
    """All known tool models, de-duplicated by tag (first tier wins for metadata)."""
    seen: dict[str, dict] = {}
    for entries in RECOMMENDED.values():
        for rec in entries:
            seen.setdefault(rec["tag"], {**rec})
    return list(seen.values())


def _param_billions(params: str) -> float:
    m = re.search(r"([\d.]+)", params or "")
    return float(m.group(1)) if m else 0.0


def compute_profile(device: dict) -> dict:
    """Score the machine's actual inference horsepower — CPU, GPU, and how they
    combine — so we don't recommend an 8B model just because RAM allows it on a
    weak processor."""
    cpu = device.get("cpu") or {}
    name = (cpu.get("name") or "").lower()
    physical = int(cpu.get("physical") or 1)
    logical = int(cpu.get("logical") or physical or 1)
    freq_mhz = float(cpu.get("freq_mhz") or 0)
    gpus = list(device.get("gpus") or [])
    vram_mb = int(device.get("vram_mb") or 0)
    ram_gb = float(device.get("ram_gb") or 8)

    cpu_score = 0.0
    apple = any(g.get("vendor") == "apple" for g in gpus) or "apple" in name

    if apple:
        # Apple Silicon: strong per-core perf + unified memory.
        if any(x in name for x in ("m4", "m3 max", "m3 pro", "m2 max", "m2 pro")):
            cpu_score = 9.0
        elif "m3" in name:
            cpu_score = 8.5
        elif "m2" in name:
            cpu_score = 8.0
        elif "m1" in name:
            cpu_score = 7.0
        else:
            cpu_score = 6.5
    else:
        cpu_score += min(physical, 24) * 0.38
        cpu_score += min(max(logical - physical, 0), 16) * 0.08
        if freq_mhz > 0:
            cpu_score += min(4.2, freq_mhz / 1000) * 1.15
        if any(x in name for x in ("ryzen 9", "i9-", " i9 ", "xeon", "threadripper", "epyc")):
            cpu_score += 2.2
        elif any(x in name for x in ("ryzen 7", "i7-", " i7 ")):
            cpu_score += 1.4
        elif any(x in name for x in ("ryzen 5", "i5-", " i5 ")):
            cpu_score += 0.6
        elif any(x in name for x in ("celeron", "pentium", "athlon silver", "n4020", "n4120")):
            cpu_score -= 1.8

    gpu_score = 0.0
    gpu_name = ""
    for g in gpus:
        gn = (g.get("name") or "")
        gl = gn.lower()
        vm = int(g.get("vram_mb") or 0)
        vendor = (g.get("vendor") or "").lower()
        if vendor == "nvidia":
            gpu_name = gn
            if vm >= 20000:
                gpu_score = max(gpu_score, 10.0)
            elif vm >= 12000:
                gpu_score = max(gpu_score, 8.5)
            elif vm >= 8000:
                gpu_score = max(gpu_score, 7.0)
            elif vm >= 6000:
                gpu_score = max(gpu_score, 5.8)
            elif vm >= 4000:
                gpu_score = max(gpu_score, 4.2)
            if any(x in gl for x in ("4090", "4080", "a6000", "a100")):
                gpu_score = max(gpu_score, 9.5)
            elif any(x in gl for x in ("4070 ti", "4070", "3090", "3080")):
                gpu_score = max(gpu_score, 7.8)
            elif any(x in gl for x in ("4060", "3060", "2070")):
                gpu_score = max(gpu_score, 5.5)
        elif vendor == "apple":
            gpu_name = gn
            gpu_score = max(gpu_score, min(9.5, 4.5 + ram_gb / 6))
        elif vendor in ("discrete", "amd") and vm >= 4000:
            gpu_name = gn
            gpu_score = max(gpu_score, 4.5 + vm / 4096)

    gpu_accel = gpu_score >= 5.5 and vram_mb >= 6000
    if gpu_accel:
        compute_score = min(10.0, gpu_score * 0.62 + cpu_score * 0.38)
    else:
        compute_score = min(10.0, max(0.5, cpu_score * 0.92))

    # Largest model size (params, billions) that should feel acceptable on this iron.
    if gpu_accel:
        if gpu_score >= 9:
            max_params = 70.0
        elif gpu_score >= 7.5:
            max_params = 32.0
        elif gpu_score >= 6:
            max_params = 14.0
        else:
            max_params = 8.0
    elif apple:
        if cpu_score >= 8.5:
            max_params = 14.0
        elif cpu_score >= 7:
            max_params = 8.0
        else:
            max_params = 4.0
    else:
        if cpu_score >= 8:
            max_params = 14.0
        elif cpu_score >= 6.5:
            max_params = 8.0
        elif cpu_score >= 4.5:
            max_params = 4.0
        elif cpu_score >= 3:
            max_params = 3.0
        else:
            max_params = 1.7

    if device.get("power_state") == "battery":
        max_params = min(max_params, 4.0)

    if compute_score >= 8:
        label = "high"
    elif compute_score >= 5.5:
        label = "medium"
    elif compute_score >= 3.2:
        label = "modest"
    else:
        label = "light"

    return {
        "cpu_score": round(cpu_score, 1),
        "gpu_score": round(gpu_score, 1),
        "compute_score": round(compute_score, 1),
        "compute_label": label,
        "max_params_b": max_params,
        "gpu_accel": gpu_accel,
        "cpu_cores": physical,
        "cpu_threads": logical,
        "cpu_ghz": round(freq_mhz / 1000, 1) if freq_mhz else None,
        "gpu_name": gpu_name or None,
        "vram_gb": round(vram_mb / 1024, 1) if vram_mb >= 3500 else 0.0,
    }


def _gpu_accel(device: dict) -> bool:
    return compute_profile(device)["gpu_accel"]


def model_budget(device: dict) -> dict:
    """RAM budget + compute profile — both gate which local models make sense."""
    total = float(device.get("ram_gb") or 8)
    avail = float(device.get("ram_available_gb") or total * 0.55)

    reserve = max(RAM_RESERVE_MIN_GB, min(RAM_RESERVE_MAX_GB, total * RAM_RESERVE_PCT))
    sustained = max(0.5, total - reserve)
    budget_gb = round(sustained * RAM_USABLE_PCT, 1)

    compute = compute_profile(device)

    return {
        "ram_total_gb": round(total, 1),
        "ram_available_gb": round(avail, 1),
        "ram_reserve_gb": round(reserve, 1),
        "budget_gb": budget_gb,
        "vram_gb": compute["vram_gb"],
        "gpu_accel": compute["gpu_accel"],
        "instant_tight": avail < max(2.0, total * 0.18),
        "local_viable": budget_gb >= _required_gb(1.3) and compute["max_params_b"] >= 1.5,
        **compute,
    }


def _required_gb(model_gb: float) -> float:
    return model_gb * MODEL_RAM_OVERHEAD


def _fits(rec: dict, budget: dict) -> bool:
    """Model must fit RAM *and* be within what this CPU/GPU can run well."""
    gb = float(rec.get("gb") or 0)
    if _required_gb(gb) > budget["budget_gb"]:
        return False
    params = _param_billions(rec.get("params", ""))
    if params > float(budget.get("max_params_b") or 0):
        return False
    # GPU path: full-GPU inference needs the weights to fit VRAM (with overhead).
    if budget.get("gpu_accel") and budget.get("vram_gb"):
        vram_budget = float(budget["vram_gb"]) * 0.88
        if _required_gb(gb) > vram_budget and params > 8:
            return False
    return True


def _score(rec: dict, budget: dict) -> float:
    """Higher = better default — balances quality, RAM use, and compute headroom."""
    gb = float(rec.get("gb") or 0)
    if not _fits(rec, budget):
        return -1.0
    params = _param_billions(rec.get("params", ""))
    max_p = float(budget.get("max_params_b") or 8)
    compute = float(budget.get("compute_score") or 5)

    score = params
    # Reward using the machine well without overshooting compute comfort.
    comfort = 1.0 - abs(params - max_p * 0.55) / max(max_p, 1)
    score += comfort * 3.0

    util = _required_gb(gb) / max(budget["budget_gb"], 0.1)
    if 0.4 <= util <= 0.8:
        score += 2.0
    elif util > 0.92:
        score -= 1.5

    if not budget.get("gpu_accel"):
        if params > 8:
            score -= 4.0
        elif 3.5 <= params <= 8:
            score += 1.5
        elif params <= 2:
            score += 0.5
    else:
        # On GPU, prefer models that sit comfortably in VRAM.
        vram = float(budget.get("vram_gb") or 0)
        if vram and _required_gb(gb) <= vram * 0.75:
            score += 2.0
        elif vram and _required_gb(gb) > vram:
            score -= 2.5

    if compute < 4 and params > 4:
        score -= 3.0
    if device_power := budget.get("_power"):
        if device_power == "battery" and params > 4:
            score -= 2.0
    return score


def _norm(tag: str) -> str:
    """Normalise a tag for loose matching: drop the registry host and the quant
    suffix so 'qwen3:8b' matches an installed 'qwen3:8b-instruct-q4_K_M'."""
    t = tag.split("/")[-1]
    name, _, ver = t.partition(":")
    ver = ver.split("-")[0]
    return f"{name}:{ver}" if ver else name


def _budget_ctx(device: dict) -> dict:
    budget = model_budget(device)
    budget["_power"] = device.get("power_state")
    return budget


def catalog_entry(model: str) -> dict | None:
    """Look up a supported model by Ollama tag (loose match)."""
    if not model:
        return None
    want = _norm(model)
    for rec in _catalog():
        if rec["tag"] == model or _norm(rec["tag"]) == want:
            return {**rec}
    return None


def _rec_from_installed(im: dict) -> dict:
    gb = float(im.get("gb") or 5)
    params = str(im.get("params") or "").strip()
    if not params or params == "None":
        # Rough guess from on-disk size when Ollama omits parameter_size.
        params = "1.7B" if gb < 2 else "4B" if gb < 3.5 else "8B" if gb < 6 else "14B"
    return {
        "tag": im["name"],
        "params": params,
        "gb": gb,
        "tools": im.get("tools"),
    }


def _block_reason(rec: dict, budget: dict) -> str:
    gb = float(rec.get("gb") or 0)
    need = round(_required_gb(gb), 1)
    params = _param_billions(rec.get("params", ""))
    tag = rec.get("tag") or "This model"
    if need > budget["budget_gb"]:
        return (f"{tag} needs ~{need}GB RAM but this machine only has "
                f"~{budget['budget_gb']}GB for models ({budget['ram_total_gb']}GB total "
                f"minus system reserve).")
    if params > float(budget.get("max_params_b") or 0):
        return (f"{tag} is too heavy for this CPU/GPU (~{params}B parameters; "
                f"your machine handles up to ~{budget['max_params_b']}B).")
    if budget.get("gpu_accel") and budget.get("vram_gb"):
        vram_budget = float(budget["vram_gb"]) * 0.88
        if need > vram_budget and params > 8:
            return (f"{tag} won't fit in your {budget['vram_gb']}GB GPU VRAM for fast inference.")
    return f"{tag} exceeds what this device can run reliably."


def model_allowed(device: dict, model: str,
                  installed_lookup: dict[str, dict] | None = None) -> tuple[bool, str]:
    """Hard gate: may this device pull or pin this model?"""
    budget = _budget_ctx(device)
    rec = catalog_entry(model)
    if not rec and installed_lookup:
        resolved = resolve_installed(model, installed_lookup.keys())
        if resolved and resolved in installed_lookup:
            rec = _rec_from_installed(installed_lookup[resolved])
    if not rec:
        return False, f"{model} isn't a JARVIS-supported local model."
    if not rec.get("tools"):
        return False, f"{model} doesn't support tool calling, which JARVIS requires."
    if not _fits(rec, budget):
        return False, _block_reason(rec, budget)
    return True, ""


def allowed_models(device: dict) -> list[dict]:
    """Every catalog model this device is permitted to pull or pin."""
    budget = _budget_ctx(device)
    out: list[dict] = []
    for rec in _catalog():
        if not rec.get("tools") or not _fits(rec, budget):
            continue
        out.append({
            **rec,
            "fits": True,
            "needs_gb": round(_required_gb(float(rec.get("gb") or 0)), 1),
        })
    return out


def annotate_installed(device: dict, installed: list[dict]) -> list[dict]:
    """Tag each installed model with whether this device may actually use it."""
    lookup = {m["name"]: m for m in installed if m.get("name")}
    out = []
    for m in installed:
        ok, reason = model_allowed(device, m["name"], lookup)
        out.append({**m, "runnable": ok, "block_reason": None if ok else reason})
    return out


def recommend_for_device(device: dict, installed_names: set[str] | None = None) -> list[dict]:
    """Rank the allowed models and return a short picker list — all entries are runnable."""
    base = installed_names or set()
    norm_installed = {_norm(n) for n in base}
    budget = _budget_ctx(device)

    fitting = allowed_models(device)
    if not fitting:
        return []

    ranked = sorted(fitting, key=lambda r: _score(r, budget), reverse=True)
    best_tag = ranked[0]["tag"] if ranked else ""

    # Build the shortlist: best first, then diverse alternatives (smaller + next-best).
    shortlist: list[dict] = []
    seen_tags: set[str] = set()

    def _add(rec: dict) -> None:
        if rec["tag"] in seen_tags:
            return
        seen_tags.add(rec["tag"])
        shortlist.append(rec)

    if ranked:
        _add(ranked[0])
    # Include one efficient option when the best pick is large.
    if ranked and _param_billions(ranked[0].get("params", "")) > 6:
        small = min(ranked, key=lambda r: float(r.get("gb") or 99))
        _add(small)
    for rec in ranked[1:]:
        if len(shortlist) >= MAX_RECOMMENDATIONS:
            break
        _add(rec)

    out = []
    for rec in shortlist[:MAX_RECOMMENDATIONS]:
        tag = rec["tag"]
        req = round(_required_gb(float(rec.get("gb") or 0)), 1)
        out.append({
            **rec,
            "best": tag == best_tag,
            "installed": tag in base or _norm(tag) in norm_installed,
            "fits": True,
            "needs_gb": req,
        })
    return out


def recommend(tier: str, installed_names: set[str], device: dict | None = None) -> list[dict]:
    """Backward-compatible entry — prefers live device profiling when provided."""
    if device:
        return recommend_for_device(device, installed_names)
    # Legacy tier-only path (tests / fallback).
    base = installed_names or set()
    out = []
    for i, rec in enumerate(RECOMMENDED.get(tier, RECOMMENDED["balanced"])):
        tag = rec["tag"]
        out.append({**rec, "best": i == 0,
                    "installed": tag in base or _norm(tag) in {_norm(n) for n in base}})
    return out


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


def ollama_up() -> tuple[bool, str | None]:
    """(running, version). Cheap liveness probe; short timeout so the UI never hangs."""
    if not _HAS_OLLAMA:
        return False, None
    try:
        import httpx
        r = httpx.get("http://localhost:11434/api/version", timeout=1.0)
        return (r.status_code == 200), r.json().get("version")
    except Exception:
        try:
            _ollama.list()
            return True, None
        except Exception:
            return False, None


def resolve_installed(wanted: str, installed_names: set[str] | list[str]) -> str | None:
    """Map a recommended tag or partial name to the exact installed Ollama name."""
    if not wanted:
        return None
    names = list(installed_names or [])
    if wanted in names:
        return wanted
    wn = _norm(wanted)
    for n in names:
        if _norm(n) == wn:
            return n
    return None


def remove(model: str) -> dict:
    """Delete an installed model from disk (Ollama DELETE /api/delete)."""
    if not _HAS_OLLAMA:
        return {"ok": False, "error": "Ollama not available."}
    try:
        _ollama.delete(model)
        return {"ok": True, "model": model}
    except Exception as exc:
        msg = str(exc)
        if "not found" in msg.lower() or "404" in msg:
            return {"ok": False, "error": f"{model} isn't installed."}
        return {"ok": False, "error": msg}


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

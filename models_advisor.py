"""
models_advisor.py — the Model Advisor: profiles which local models the machine can
run, recommends the best ones for its hardware tier, benchmarks real tokens/sec,
and pulls models on demand. Talks to Ollama (default http://localhost:11434).

Pure-ish module: uses the `ollama` python client; api.py drives it off the event
loop via asyncio.to_thread and broadcasts pull progress.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

try:
    import ollama as _ollama
    _HAS_OLLAMA = True
except Exception:
    _HAS_OLLAMA = False


def _ollama_base() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


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
_BENCH_FILE = Path(__file__).resolve().parent / "memory" / "jarvis_model_benchmarks.json"
_INST_CACHE_TTL = 45.0
_inst_cache: dict = {"at": 0.0, "data": []}
_tools_cache: dict[str, tuple[float, bool]] = {}


def invalidate_install_cache() -> None:
    """Drop cached install/tool metadata after pull, delete, or pin changes."""
    _inst_cache["at"] = 0.0
    _inst_cache["data"] = []
    _tools_cache.clear()


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


def _disk_gb_per_b_param() -> float:
    """Average on-disk GB per billion params — derived from the catalog, not guessed."""
    samples = []
    for rec in _catalog():
        params = _param_billions(rec.get("params", ""))
        gb = float(rec.get("gb") or 0)
        if params > 0 and gb > 0:
            samples.append(gb / params)
    return sum(samples) / len(samples) if samples else 0.65


def _max_params_for_gb(budget_gb: float) -> float:
    denom = _disk_gb_per_b_param() * MODEL_RAM_OVERHEAD
    return budget_gb / denom if denom > 0 else 0.0


def _readiness_label(headroom: float, ram_pct: float) -> str:
    if headroom >= 0.65 and ram_pct < 80:
        return "ready"
    if headroom >= 0.4:
        return "busy"
    if headroom >= 0.2:
        return "tight"
    return "critical"


def capacity_profile(device: dict) -> dict:
    """Live capability from measured signals (device.profile), not CPU name guessing."""
    cpu = device.get("cpu") or {}
    gpus = list(device.get("gpus") or [])
    vram_mb = int(device.get("vram_mb") or 0)
    vram_gb = round(vram_mb / 1024, 1) if vram_mb >= 1024 else 0.0
    gpu_name = next((g.get("name") for g in gpus if g.get("name")), None)
    gpu_accel = vram_mb >= 6144
    headroom = float(device.get("headroom") or 0.5)
    ram_pct = float(device.get("ram_percent") or 50)
    freq_mhz = float(cpu.get("freq_mhz") or 0)

    return {
        "headroom": round(headroom, 2),
        "compute_score": round(headroom * 10, 1),
        "compute_label": _readiness_label(headroom, ram_pct),
        "gpu_accel": gpu_accel,
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
        "cpu_cores": int(cpu.get("physical") or 1),
        "cpu_threads": int(cpu.get("logical") or cpu.get("physical") or 1),
        "cpu_ghz": round(freq_mhz / 1000, 1) if freq_mhz else None,
    }


def compute_profile(device: dict) -> dict:
    """Backward-compatible alias — callers get measured capacity, not name heuristics."""
    return capacity_profile(device)


def model_budget(device: dict) -> dict:
    """RAM budget from measured totals + live free memory."""
    total = float(device.get("ram_gb") or 8)
    avail = float(device.get("ram_available_gb") or total * 0.55)

    reserve = max(RAM_RESERVE_MIN_GB, min(RAM_RESERVE_MAX_GB, total * RAM_RESERVE_PCT))
    capacity_gb = round(max(0.5, total - reserve) * RAM_USABLE_PCT, 1)

    prof = capacity_profile(device)
    headroom = float(device.get("headroom") or 0.5)
    live_gb = round(max(0.0, avail * headroom), 1)

    max_params_b = _max_params_for_gb(capacity_gb)
    if prof["gpu_accel"] and prof["vram_gb"]:
        max_params_b = min(max_params_b, _max_params_for_gb(prof["vram_gb"] * 0.88))
    if device.get("power_state") == "battery":
        max_params_b *= max(0.35, headroom)

    disk_free = disk_free_gb()

    return {
        "ram_total_gb": round(total, 1),
        "ram_available_gb": round(avail, 1),
        "ram_reserve_gb": round(reserve, 1),
        "budget_gb": capacity_gb,
        "live_gb": live_gb,
        "disk_free_gb": disk_free,
        "vram_gb": prof["vram_gb"],
        "gpu_accel": prof["gpu_accel"],
        "instant_tight": live_gb < _required_gb(1.3),
        "local_viable": live_gb >= _required_gb(1.3),
        "max_params_b": round(max_params_b, 1),
        **prof,
    }


def _required_gb(model_gb: float) -> float:
    return model_gb * MODEL_RAM_OVERHEAD


def _ollama_store_path() -> Path:
    custom = os.environ.get("OLLAMA_MODELS")
    if custom:
        return Path(custom)
    return Path.home() / ".ollama" / "models"


def disk_free_gb(path: Path | None = None) -> float | None:
    try:
        p = path or _ollama_store_path()
        p.mkdir(parents=True, exist_ok=True)
        return round(shutil.disk_usage(p).free / 1024 ** 3, 1)
    except Exception:
        return None


def disk_ok_for_pull(model_gb: float) -> tuple[bool, str]:
    free = disk_free_gb()
    if free is None:
        return True, ""
    need = round(model_gb * 1.15, 1)
    if free < need:
        return False, f"Only {free:.1f}GB free on disk; need ~{need:.1f}GB for this download."
    return True, ""


def _estimate_pull_gb(model: str, device: dict) -> float:
    rec = catalog_entry(model)
    if rec:
        return float(rec.get("gb") or 2.0)
    avail = float(device.get("ram_available_gb") or device.get("ram_gb") or 4)
    return max(1.5, min(avail * 0.4, 20.0))


def load_benchmarks() -> dict[str, dict]:
    try:
        if _BENCH_FILE.exists():
            data = json.loads(_BENCH_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def record_benchmark(model: str, tok_per_sec: float, load_s: float = 0) -> dict:
    data = load_benchmarks()
    entry = {
        "tok_per_sec": round(tok_per_sec, 1),
        "load_s": round(load_s, 2),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    data[model] = entry
    _BENCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BENCH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return entry


def _bench_lookup(tag: str, benchmarks: dict[str, dict]) -> dict | None:
    if tag in benchmarks:
        return benchmarks[tag]
    want = _norm(tag)
    for name, entry in benchmarks.items():
        if _norm(name) == want:
            return entry
    return None


def _fits_capacity(rec: dict, budget: dict) -> bool:
    """Model fits sustained RAM/VRAM capacity for this hardware."""
    gb = float(rec.get("gb") or 0)
    if _required_gb(gb) > budget["budget_gb"]:
        return False
    params = _param_billions(rec.get("params", ""))
    if params > float(budget.get("max_params_b") or 0):
        return False
    if budget.get("gpu_accel") and budget.get("vram_gb"):
        vram_budget = float(budget["vram_gb"]) * 0.88
        if _required_gb(gb) > vram_budget and params > 8:
            return False
    return True


def _fits_now(rec: dict, budget: dict) -> bool:
    """Model fits *currently free* RAM — avoids swap/thrash right now."""
    gb = float(rec.get("gb") or 0)
    live = float(budget.get("live_gb") or 0)
    if _required_gb(gb) > live:
        return False
    if budget.get("gpu_accel") and budget.get("vram_gb"):
        vram_budget = float(budget["vram_gb"]) * 0.88
        if _required_gb(gb) > vram_budget:
            return False
    return True


def _fits(rec: dict, budget: dict) -> bool:
    return _fits_capacity(rec, budget)


def _score(rec: dict, budget: dict, benchmarks: dict[str, dict] | None = None) -> float:
    """Rank by measured tok/s when available, else quality vs live RAM pressure."""
    if not _fits_capacity(rec, budget):
        return -1.0

    benchmarks = benchmarks or load_benchmarks()
    tag = rec.get("tag") or ""
    bench = _bench_lookup(tag, benchmarks)
    if bench and bench.get("tok_per_sec"):
        score = float(bench["tok_per_sec"]) * 10.0
        if not _fits_now(rec, budget):
            score *= 0.25
        return score

    params = _param_billions(rec.get("params", ""))
    need = _required_gb(float(rec.get("gb") or 0))
    live = float(budget.get("live_gb") or 0)
    score = params

    if live > 0:
        util = need / live
        if util <= 0.65:
            score += 2.0
        elif util <= 0.85:
            score += 0.5
        else:
            score -= 4.0

    cap = float(budget.get("budget_gb") or 1)
    cap_util = need / cap
    if 0.35 <= cap_util <= 0.75:
        score += 1.0
    elif cap_util > 0.92:
        score -= 1.5

    if not _fits_now(rec, budget):
        score -= 12.0

    headroom = float(budget.get("headroom") or 0.5)
    if budget.get("_power") == "battery":
        score -= params * (1.0 - headroom)
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


_OLLAMA_TAG_RE = re.compile(r"^[\w.-]+(:[\w.-]+)?$")


def _fit_note(rec: dict, budget: dict, benchmarks: dict | None = None) -> str:
    """Explain rank from measured signals."""
    need = round(_required_gb(float(rec.get("gb") or 0)), 1)
    live = float(budget.get("live_gb") or 0)
    label = budget.get("compute_label") or "busy"
    benchmarks = benchmarks or load_benchmarks()
    bench = _bench_lookup(rec.get("tag") or "", benchmarks)
    if bench and bench.get("tok_per_sec"):
        return f"Measured {bench['tok_per_sec']} tok/s on this machine"
    if not _fits_now(rec, budget):
        return (f"Needs ~{need}GB RAM now but only ~{live:.1f}GB free ({label}) — "
                f"close apps before using")
    if budget.get("gpu_accel") and budget.get("gpu_name"):
        return (f"Fits now with ~{live:.1f}GB free · {budget['gpu_name']} · "
                f"system {label}")
    cores = budget.get("cpu_cores") or "?"
    ghz = budget.get("cpu_ghz")
    cpu = f"{cores} cores"
    if ghz:
        cpu = f"{cores} cores @ {ghz}GHz"
    return f"Fits ~{live:.1f}GB free RAM on {cpu} · system {label}"


def _block_reason(rec: dict, budget: dict, *, now: bool = False) -> str:
    gb = float(rec.get("gb") or 0)
    need = round(_required_gb(gb), 1)
    params = _param_billions(rec.get("params", ""))
    tag = rec.get("tag") or "This model"
    if now:
        live = float(budget.get("live_gb") or 0)
        if need > live:
            return (f"{tag} needs ~{need}GB RAM right now but only ~{live:.1f}GB is free — "
                    f"close apps to avoid lag.")
        if budget.get("gpu_accel") and budget.get("vram_gb"):
            vram_budget = float(budget["vram_gb"]) * 0.88
            if need > vram_budget:
                return f"{tag} won't fit in {budget['vram_gb']}GB VRAM for GPU inference."
        return f"{tag} can't run without swapping on current free memory."
    if need > budget["budget_gb"]:
        return (f"{tag} needs ~{need}GB RAM but this machine only has "
                f"~{budget['budget_gb']}GB capacity ({budget['ram_total_gb']}GB total "
                f"minus reserve).")
    if params > float(budget.get("max_params_b") or 0):
        return (f"{tag} is too large (~{params}B params; "
                f"capacity tops out at ~{budget['max_params_b']:.0f}B on this hardware).")
    if budget.get("gpu_accel") and budget.get("vram_gb"):
        vram_budget = float(budget["vram_gb"]) * 0.88
        if need > vram_budget and params > 8:
            return (f"{tag} won't fit in your {budget['vram_gb']}GB GPU VRAM for fast inference.")
    return f"{tag} exceeds what this device can run reliably."


def model_allowed(device: dict, model: str,
                  installed_lookup: dict[str, dict] | None = None,
                  *, require_live: bool = True) -> tuple[bool, str]:
    """Gate pull/pin: catalog model must fit capacity and (by default) live free RAM."""
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
    if not _fits_capacity(rec, budget):
        return False, _block_reason(rec, budget, now=False)
    if require_live and not _fits_now(rec, budget):
        return False, _block_reason(rec, budget, now=True)
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
    """Tag each installed model with capacity + live runnable state."""
    lookup = {m["name"]: m for m in installed if m.get("name")}
    budget = _budget_ctx(device)
    out = []
    for m in installed:
        rec = catalog_entry(m["name"]) or _rec_from_installed(m)
        cap_ok = _fits_capacity(rec, budget) and bool(rec.get("tools"))
        now_ok = cap_ok and _fits_now(rec, budget)
        if not cap_ok:
            reason = _block_reason(rec, budget, now=False)
        elif not now_ok:
            reason = _block_reason(rec, budget, now=True)
        else:
            reason = None
        out.append({
            **m,
            "runnable": now_ok,
            "runnable_capacity": cap_ok,
            "block_reason": reason,
        })
    return out


def ranked_for_device(device: dict, installed_names: set[str] | None = None) -> list[dict]:
    """Catalog models that fit this hardware, ranked by measurement then live RAM fit."""
    base = installed_names or set()
    norm_installed = {_norm(n) for n in base}
    budget = _budget_ctx(device)
    benchmarks = load_benchmarks()

    fitting = allowed_models(device)
    if not fitting:
        return []

    ordered = sorted(fitting, key=lambda r: _score(r, budget, benchmarks), reverse=True)
    best_tag = ordered[0]["tag"] if ordered else ""
    out: list[dict] = []
    for i, rec in enumerate(ordered):
        tag = rec["tag"]
        req = round(_required_gb(float(rec.get("gb") or 0)), 1)
        bench = _bench_lookup(tag, benchmarks)
        now_ok = _fits_now(rec, budget)
        out.append({
            **rec,
            "rank": i + 1,
            "score": round(_score(rec, budget, benchmarks), 2),
            "best": tag == best_tag,
            "installed": tag in base or _norm(tag) in norm_installed,
            "fits": True,
            "runnable_now": now_ok,
            "needs_gb": req,
            "tok_per_sec": bench.get("tok_per_sec") if bench else None,
            "fit_note": _fit_note(rec, budget, benchmarks),
            "block_reason_now": None if now_ok else _block_reason(rec, budget, now=True),
        })
    return out


def recommend_for_device(device: dict, installed_names: set[str] | None = None) -> list[dict]:
    """Rank the allowed models and return a short picker list — all entries are runnable."""
    ranked = ranked_for_device(device, installed_names)
    if not ranked:
        return []

    pool = [r for r in ranked if r.get("runnable_now")] or ranked

    shortlist: list[dict] = []
    seen_tags: set[str] = set()

    def _add(rec: dict) -> None:
        if rec["tag"] in seen_tags:
            return
        seen_tags.add(rec["tag"])
        shortlist.append(rec)

    _add(pool[0])
    if _param_billions(pool[0].get("params", "")) > 6:
        small = min(pool, key=lambda r: float(r.get("gb") or 99))
        _add(small)
    for rec in pool[1:]:
        if len(shortlist) >= MAX_RECOMMENDATIONS:
            break
        _add(rec)

    return shortlist[:MAX_RECOMMENDATIONS]


def pull_precheck(device: dict, model: str) -> tuple[bool, str, str]:
    """Gate a pull request. Returns (ok, message, kind) where kind is catalog|custom|error."""
    model = (model or "").strip()
    if not model:
        return False, "Model name is required.", "error"
    tag = model.split("/")[-1]
    if not _OLLAMA_TAG_RE.match(tag):
        return False, "Use name:tag format (e.g. qwen3:8b or library/qwen3:8b).", "error"

    pull_gb = _estimate_pull_gb(model, device)
    ok_disk, disk_reason = disk_ok_for_pull(pull_gb)
    if not ok_disk:
        return False, disk_reason, "error"

    ok, reason = model_allowed(device, model, require_live=True)
    if ok:
        return True, "", "catalog"

    if catalog_entry(model):
        return False, reason, "catalog"

    if not _fits_now({"tag": model, "gb": pull_gb, "params": "4B", "tools": True},
                     _budget_ctx(device)):
        return False, (
            f"Only ~{_budget_ctx(device)['live_gb']:.1f}GB RAM free — close apps before "
            f"pulling ~{round(_required_gb(pull_gb), 1)}GB model."
        ), "custom"

    return True, (
        "Custom model — not in the JARVIS catalog. Tool calling may not work after install."
    ), "custom"


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


def _supports_tools_cached(name: str) -> bool:
    now = time.time()
    hit = _tools_cache.get(name)
    if hit and (now - hit[0]) < _INST_CACHE_TTL:
        return hit[1]
    ok = _supports_tools(name)
    _tools_cache[name] = (now, ok)
    return ok


def unload(model: str) -> None:
    """Ask Ollama to drop a model from RAM/VRAM (keep_alive=0)."""
    if not model or not _HAS_OLLAMA:
        return
    try:
        import httpx
        httpx.post(
            f"{_ollama_base()}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=8.0,
        )
    except Exception:
        pass


def installed(with_caps: bool = True, *, use_cache: bool = True) -> list[dict]:
    """Locally installed models with size + (optionally) tool capability."""
    if not _HAS_OLLAMA:
        return []
    now = time.time()
    if use_cache and with_caps and _inst_cache["data"] and (now - _inst_cache["at"]) < _INST_CACHE_TTL:
        return [dict(m) for m in _inst_cache["data"]]
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
            "tools": _supports_tools_cached(name) if with_caps else None,
        })
    if with_caps:
        _inst_cache["at"] = now
        _inst_cache["data"] = [dict(m) for m in out]
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
        r = httpx.get(f"{_ollama_base()}/api/version", timeout=1.0)
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
        invalidate_install_cache()
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
                             stream=False, options={"num_predict": 64, "keep_alive": 0})
        ec = getattr(r, "eval_count", None) or (r.get("eval_count") if isinstance(r, dict) else None)
        ed = getattr(r, "eval_duration", None) or (r.get("eval_duration") if isinstance(r, dict) else None)
        ld = getattr(r, "load_duration", 0) or (r.get("load_duration", 0) if isinstance(r, dict) else 0)
        if not ec or not ed:
            return {"ok": False, "error": "No timing data returned."}
        tps = round(ec / ed * 1e9, 1)
        load_s = round((ld or 0) / 1e9, 2)
        record_benchmark(model, tps, load_s)
        return {"ok": True, "model": model,
                "tok_per_sec": tps,
                "tokens": ec, "load_s": load_s}
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
        invalidate_install_cache()
        return {"ok": True, "model": model}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

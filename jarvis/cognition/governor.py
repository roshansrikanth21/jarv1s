"""
governor.py — the Governor: a compute-elastic cognitive router.

Per request it picks the *minimum* rung of an escalation lattice that clears a
quality bar within the current latency/energy budget, and escalates only when the
task is hard or the machine has energy to spare. The machine is treated as the
agent's body: battery, thermal and load modulate how freely it spends compute.

Design (grounded in prior art — FrugalGPT cascades, RouteLLM win-probability,
GreenServ's energy-aware LinUCB, TAPAS thermal-aware routing):
  1. cheap per-request difficulty estimate from the prompt (heuristics, sub-ms),
  2. a feasibility mask (drop rungs whose backend/keys/RAM aren't available),
  3. a transparent utility score   quality - λ_eff·energy - μ·latency   where
     λ_eff climbs on battery / under thermal pressure (so it conserves itself),
  4. a LinUCB contextual-bandit bonus learned online, per machine, from observed
     latency + whether the user had to escalate — so it adapts to *this* device.

Pure module: no api.py imports. api.py binds rung ids to actual model calls.
"""
from __future__ import annotations

import math
import re
import time
from typing import Any

import importlib.util as _ilu

# numpy is a hard dep in practice, but eager-importing it here cost ~0.8s at boot (governor is
# imported eagerly by api.py). find_spec confirms availability without importing; the real import
# is deferred to _np() on the first Governor decision that actually needs the bandit math.
_HAS_NP = _ilu.find_spec("numpy") is not None
np = None


def _np():
    """Lazy handle to numpy. Called only inside the LinUCB paths, which are already guarded
    by _HAS_NP, so this never runs when numpy is absent."""
    global np
    if np is None:
        import numpy as _mod
        np = _mod
    return np


# ── The escalation lattice ───────────────────────────────────────────────────────
# Priors are normalized 0..1. quality = expected answer quality; energy = LOCAL
# energy draw (cloud calls barely touch the battery; a big local model hammers it);
# latency = wall-clock prior. `requires` gates feasibility; `local` marks on-device.
RUNGS: list[dict] = [
    {"id": "local_fast", "label": "local · fast",  "kind": "local",   "tier": 0,
     "quality": 0.46, "energy": 0.45, "latency": 0.55, "requires": "ollama", "local": True},
    {"id": "cloud_fast", "label": "cloud · fast",  "kind": "cloud",   "tier": 1,
     "quality": 0.76, "energy": 0.12, "latency": 0.25, "requires": "groq",   "local": False},
    {"id": "local_deep", "label": "local · deep",  "kind": "local",   "tier": 1,
     "quality": 0.63, "energy": 0.82, "latency": 0.82, "requires": "ollama", "local": True},
    {"id": "cloud_deep", "label": "cloud · deep",  "kind": "cloud",   "tier": 2,
     "quality": 0.90, "energy": 0.15, "latency": 0.50, "requires": "claude", "local": False},
    {"id": "council",    "label": "council",       "kind": "council", "tier": 3,
     "quality": 0.95, "energy": 0.35, "latency": 1.00, "requires": "groq",   "local": False},
]
RUNG_BY_ID = {r["id"]: r for r in RUNGS}

MODES = ("auto", "eco", "local", "cloud")   # local = privacy/offline, cloud = max quality

# Below this difficulty score, agent loops run chat-only (no tool schema round-trips).
TOOLING_MIN_DIFFICULTY = 0.22
# Under memory pressure, raise the bar so local CPU doesn't hammer tool loops.
TOOLING_RAM_PRESSURE_PCT = 85

# Context-vector dimension (LinUCB). Order is fixed — see _feature_vector.
_CTX_DIM = 10
_MU = 0.15            # latency weight
_LAMBDA0 = 0.45       # base energy weight
_ALPHA = 0.12         # LinUCB exploration
_LAMBDA_REG = 0.05    # ridge regularization


# ── Difficulty estimation (cheap, prompt-only) ───────────────────────────────────
_CODE_RE = re.compile(r"```|\bdef \b|\bclass \b|\bimport \b|\bfunction\b|[{};]|</?\w+>|\b0x[0-9a-f]+", re.I)
_MATH_RE = re.compile(r"\$.+?\$|\\frac|\\sum|\b\d+\s*[\+\-\*/\^=]\s*\d+|\bintegral\b|\bderivative\b|\bprove\b|\bsolve\b", re.I)
_TOOL_WORDS = {"search", "news", "today", "latest", "file", "run", "scan", "screen",
               "command", "launch", "remember", "recall", "calculate", "weather", "price", "market"}
_MULTISTEP_WORDS = {"then", "after", "first", "step", "plan", "design", "compare",
                    "analyze", "refactor", "debug", "explain why", "trade-off", "architecture"}
_HARD_WORDS = {"exploit", "vulnerability", "reverse", "cryptography", "proof", "prove",
               "optimi", "algorithm", "concurren", "lock-free", "race condition",
               "threat model", "rationale", "distributed", "kernel", "pointer", "throughput"}


def estimate_difficulty(text: str, history: list[dict] | None = None) -> dict:
    """Return {score 0..1, factors:{...}} from cheap prompt features only."""
    t = (text or "").strip()
    low = t.lower()
    words = len(t.split())

    length = min(1.0, words / 90.0)                       # long asks tend to be harder
    code = 1.0 if _CODE_RE.search(t) else 0.0
    mathy = 1.0 if _MATH_RE.search(t) else 0.0
    tool = 1.0 if any(w in low for w in _TOOL_WORDS) else 0.0
    multistep = min(1.0, sum(w in low for w in _MULTISTEP_WORDS) / 2.0)
    hard = min(1.0, sum(w in low for w in _HARD_WORDS) / 2.0)

    score = (0.22 * length + 0.20 * code + 0.18 * mathy
             + 0.10 * tool + 0.15 * multistep + 0.15 * hard)
    if (code or mathy) and multistep > 0 and length > 0.15:
        score += 0.16                       # multi-part technical asks compound
    if hard >= 0.5 and (code or mathy):
        score += 0.12                       # hard domain + formal/code => genuinely deep
    # Very short greetings / acks are trivial regardless.
    if words <= 3 and not (code or mathy):
        score = min(score, 0.12)
    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "factors": {"length": round(length, 2), "code": code, "math": mathy,
                    "tool": tool, "multistep": round(multistep, 2), "hard": round(hard, 2)},
    }


# ── Device-state modulation ──────────────────────────────────────────────────────
def _battery_frac(device: dict) -> float:
    bat = (device or {}).get("battery")
    if not bat or bat.get("percent") is None:
        return 1.0
    return max(0.0, min(1.0, bat["percent"] / 100.0))


def _on_ac(device: dict) -> bool:
    return (device or {}).get("power_state", "ac") == "ac"


def _thermal_pressure(device: dict) -> float:
    """0..1 thermal pressure. Uses CPU temp if available (Win often lacks it), else
    falls back to CPU load as a proxy for sustained heat."""
    t = (device or {}).get("cpu_temp_c")
    if isinstance(t, (int, float)):
        return max(0.0, min(1.0, (t - 70.0) / 20.0))      # 70°C warn → 90°C max
    load = (device or {}).get("cpu_percent", 0) / 100.0
    return max(0.0, min(1.0, (load - 0.7) / 0.3))          # >70% load → rising pressure


def lambda_eff(device: dict, mode: str = "auto") -> float:
    """Energy weight, modulated by the body. On AC + cool it drops to a floor so the
    Governor spends compute freely; on battery / hot it climbs so it conserves."""
    if mode == "cloud":
        return 0.04                                        # quality-first: barely penalize energy
    base = _LAMBDA0 * (2.2 if mode == "eco" else 1.0)
    if _on_ac(device) and _thermal_pressure(device) < 0.3 and mode != "eco":
        return round(base * 0.22, 3)                       # plugged in & cool → floor
    lam = base * (1.0 + 1.3 * (1.0 - _battery_frac(device)) + 1.1 * _thermal_pressure(device))
    return round(min(1.4, lam), 3)


def _feature_vector(diff_score: float, device: dict) -> list[float]:
    """10-dim context for LinUCB: [bias, difficulty, on_ac, battery, thermal,
    load, ram_head, vram_head, is_eco_placeholder, free]. Kept small + interpretable."""
    vm_head = min(1.0, (device or {}).get("ram_available_gb", 0) / 16.0)
    vram = max((g.get("vram_mb") or 0 for g in (device or {}).get("gpus", [])), default=0)
    vram_head = min(1.0, vram / 16000.0)
    return [
        1.0,
        diff_score,
        1.0 if _on_ac(device) else 0.0,
        _battery_frac(device),
        _thermal_pressure(device),
        (device or {}).get("cpu_percent", 0) / 100.0,
        vm_head,
        vram_head,
        0.0,
        0.0,
    ]


# ── Feasibility ──────────────────────────────────────────────────────────────────
def feasible_rungs(available: set[str], device: dict, mode: str) -> list[dict]:
    """Rungs whose backend is available, honoring mode and a coarse RAM mask for the
    deep local rung (a big local model needs headroom)."""
    out = []
    for r in RUNGS:
        if r["id"] not in available:
            continue
        if mode == "local" and not r["local"]:
            continue
        if r["id"] == "council" and mode in ("eco", "local"):
            continue
        if r["id"] == "local_deep":
            # need real headroom to run the larger local model without thrashing
            if (device or {}).get("ram_available_gb", 0) < 5.5 and not _on_ac(device):
                continue
        out.append(r)
    return out


# ── Governor state (LinUCB bandit + decision log + metrics) ──────────────────────
class GovernorState:
    """Online-learned, per-machine policy state. Serializes to a plain dict so api.py
    can persist it to memory/jarvis_governor.json."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.mode: str = data.get("mode", "auto")
        self.log: list[dict] = data.get("log", [])[-60:]
        self.counts: dict[str, int] = data.get("counts", {})
        self.lat_ewma: dict[str, float] = data.get("lat_ewma", {})   # learned latency per rung
        self._A: dict[str, Any] = {}
        self._b: dict[str, Any] = {}
        if _HAS_NP:
            saved = data.get("bandit", {})
            for r in RUNGS:
                a = saved.get(r["id"], {}).get("A")
                b = saved.get(r["id"], {}).get("b")
                self._A[r["id"]] = _np().array(a) if a else _np().identity(_CTX_DIM) * _LAMBDA_REG
                self._b[r["id"]] = _np().array(b) if b else _np().zeros(_CTX_DIM)

    def to_dict(self) -> dict:
        bandit = {}
        if _HAS_NP:
            for rid in self._A:
                bandit[rid] = {"A": self._A[rid].tolist(), "b": self._b[rid].tolist()}
        return {"mode": self.mode, "log": self.log[-60:], "counts": self.counts,
                "lat_ewma": self.lat_ewma, "bandit": bandit}

    def _bonus(self, rung_id: str, x: list[float]) -> float:
        """LinUCB upper-confidence bonus: θ̂ᵀx + α·√(xᵀA⁻¹x), centered so an
        untrained policy contributes ~0 and never overrides the transparent score."""
        if not _HAS_NP:
            return 0.0
        try:
            A, b = self._A[rung_id], self._b[rung_id]
            xv = _np().array(x)
            A_inv = _np().linalg.inv(A)
            theta = A_inv @ b
            ucb = float(theta @ xv) + _ALPHA * math.sqrt(max(0.0, float(xv @ A_inv @ xv)))
            return max(-0.25, min(0.25, ucb))     # clamp: a gentle nudge, not a takeover
        except Exception:
            return 0.0

    def observe(self, decision: dict, *, latency_s: float, escalated: bool, accepted: bool) -> None:
        """Update the bandit + metrics after a turn. Reward favors quality &
        acceptance, penalizes energy, latency, and having to escalate. A latency<=0
        call is a retroactive re-ask penalty — it nudges the policy but is NOT counted
        as a fresh run, so usage + latency stats stay honest."""
        rid = decision.get("rung")
        if not rid or rid not in RUNG_BY_ID:
            return
        r = RUNG_BY_ID[rid]
        if latency_s > 0:
            self.counts[rid] = self.counts.get(rid, 0) + 1
            lat_norm = min(1.0, latency_s / 60.0)
            self.lat_ewma[rid] = round(0.7 * self.lat_ewma.get(rid, lat_norm) + 0.3 * lat_norm, 3)
        else:
            lat_norm = self.lat_ewma.get(rid, 0.5)
        reward = (r["quality"] + (0.15 if accepted else -0.25)
                  - decision.get("lambda_eff", _LAMBDA0) * r["energy"]
                  - _MU * lat_norm - (0.3 if escalated else 0.0))
        if _HAS_NP and "x" in decision:
            try:
                xv = _np().array(decision["x"])
                self._A[rid] = self._A[rid] + _np().outer(xv, xv)
                self._b[rid] = self._b[rid] + reward * xv
            except Exception:
                pass
        for entry in self.log:
            if entry.get("id") == decision.get("id"):
                entry["latency_s"] = round(latency_s, 2)
                entry["escalated"] = escalated
                entry["reward"] = round(reward, 3)
                break

    def metrics(self) -> dict:
        recent = self.log[-30:]
        lats = [e["latency_s"] for e in recent if isinstance(e.get("latency_s"), (int, float))]
        return {
            "mode": self.mode,
            "distribution": dict(self.counts),
            "avg_latency_s": round(sum(lats) / len(lats), 2) if lats else None,
            "decisions": len(self.log),
            "learned_latency": self.lat_ewma,
        }


def decide(text: str, history: list[dict] | None, device: dict,
           available: set[str], state: GovernorState, decision_id: str) -> dict:
    """Choose a rung. Returns a Decision dict (also appended to state.log)."""
    mode = state.mode
    diff = estimate_difficulty(text, history)
    feas = feasible_rungs(available, device, mode)
    if not feas:
        pool = RUNGS
        if mode == "local":
            pool = [r for r in RUNGS if r["local"]]
        elif mode == "cloud":
            pool = [r for r in RUNGS if not r["local"]]
        feas = [RUNG_BY_ID[r["id"]] for r in pool if r["id"] in available]
        if not feas and mode == "auto":
            feas = [RUNG_BY_ID[r["id"]] for r in RUNGS if r["id"] in available]
        if not feas and mode != "local":
            feas = [RUNG_BY_ID[r["id"]] for r in RUNGS if r["id"] in available]
        if not feas:
            if mode == "local":
                pick = next((r["id"] for r in RUNGS if r["local"] and r["id"] in available),
                            next((r["id"] for r in RUNGS if r["local"]), "local_fast"))
                feas = [RUNG_BY_ID[pick]]
            elif mode == "cloud":
                pick = next((r["id"] for r in RUNGS if not r["local"] and r["id"] in available),
                            next((r["id"] for r in RUNGS if not r["local"]), "cloud_fast"))
                feas = [RUNG_BY_ID[pick]]
            else:
                rid = next(iter(available), "local_fast")
                feas = [RUNG_BY_ID.get(rid, RUNGS[0])]

    lam = lambda_eff(device, mode)
    x = _feature_vector(diff["score"], device)
    min_q = 0.35 + 0.60 * diff["score"]       # quality bar implied by difficulty

    def _cost(r: dict) -> float:
        # Energy + learned latency (EWMA from this machine), minus bandit bonus.
        learned = state.lat_ewma.get(r["id"])
        lat = learned if learned is not None else r["latency"]
        return lam * r["energy"] + _MU * lat - state._bonus(r["id"], x)

    adequate = [r for r in feas if r["quality"] >= min_q]
    if mode == "cloud":
        best = max(feas, key=lambda r: r["quality"] + 0.3 * state._bonus(r["id"], x))
    elif (diff["score"] >= 0.85 and _on_ac(device) and mode == "auto"
          and any(r["id"] == "council" for r in feas)):
        best = RUNG_BY_ID["council"]          # hardest asks on a healthy machine convene the panel
    elif adequate:
        best = min(adequate, key=_cost)        # cheapest rung that clears the bar
    else:
        best = max(feas, key=lambda r: r["quality"] + 0.3 * state._bonus(r["id"], x))

    # Scored list for the UI: net = quality - cost (higher is better).
    scored = sorted(((round(r["quality"] - _cost(r), 3), r) for r in feas),
                    key=lambda s: s[0], reverse=True)

    why = []
    why.append(f"difficulty {diff['score']:.2f}")
    if mode != "auto":
        why.append(f"{mode} mode")
    if not _on_ac(device):
        why.append(f"on battery {round(_battery_frac(device) * 100)}%")
    if _thermal_pressure(device) > 0.3:
        why.append("thermal pressure")
    if best["local"]:
        why.append("kept local")
    rationale = f"{best['label']} — " + ", ".join(why)

    decision = {
        "id": decision_id,
        "rung": best["id"],
        "label": best["label"],
        "kind": best["kind"],
        "difficulty": diff["score"],
        "factors": diff["factors"],
        "lambda_eff": lam,
        "rationale": rationale,
        "candidates": [{"id": r["id"], "util": u} for u, r in scored],
        "x": x,
        "ts": time.time(),
    }
    state.log.append({k: decision[k] for k in ("id", "rung", "difficulty", "lambda_eff", "ts")})
    state.log[:] = state.log[-60:]
    return decision


def agent_needs_tools(decision: dict, device: dict | None = None) -> bool:
    """Whether this turn should run the tool-calling agent loop.

    Derived from the same difficulty estimator the Governor already uses — not
    hardcoded phrase lists. Tool intent in the message, multistep/code/math
    work, or high difficulty all enable tools; casual chat does not."""
    factors = decision.get("factors") or {}
    if factors.get("tool", 0) >= 1.0:
        return True
    if factors.get("multistep", 0) >= 0.5:
        return True
    if factors.get("code", 0) >= 1.0 or factors.get("math", 0) >= 1.0:
        return True
    if factors.get("hard", 0) >= 0.5:
        return True

    threshold = TOOLING_MIN_DIFFICULTY
    ram_pct = (device or {}).get("ram_percent")
    if isinstance(ram_pct, (int, float)) and ram_pct >= TOOLING_RAM_PRESSURE_PCT:
        threshold = max(threshold, 0.35)

    return float(decision.get("difficulty") or 0) >= threshold

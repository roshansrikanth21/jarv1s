"""Provider-agnostic routing layer — health, fallbacks, capability tags.

Governor still picks the *rung*; this module turns that into a provider plan,
tracks health, and orders fallbacks. Actual model SDK calls stay in api.py
adapters until brains are fully extracted.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


# rung id → logical provider family
RUNG_PROVIDER = {
    "cloud_fast": "groq",
    "cloud_deep": "claude",
    "local_fast": "ollama",
    "local_deep": "ollama",
    "council": "council",
}

# Preferred fallback order when a rung fails (never auto-enter council).
FALLBACK_ORDER = ("cloud_deep", "cloud_fast", "local_fast", "local_deep")

CAPABILITIES = {
    "groq": frozenset({"chat", "tools", "stream", "vision", "json"}),
    "claude": frozenset({"chat", "tools", "stream", "json"}),
    "ollama": frozenset({"chat", "tools", "stream"}),
    "council": frozenset({"chat"}),
}


@dataclass
class RoutePlan:
    rung: str
    provider: str
    tools_enabled: bool
    fallbacks: list[str] = field(default_factory=list)
    rationale: str = ""
    decision_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """EMA latency + error rate per provider. Thread-safe."""

    def __init__(self, *, alpha: float = 0.3) -> None:
        self._alpha = alpha
        self._lock = threading.RLock()
        self._latency: dict[str, float] = {}
        self._errors: dict[str, float] = {}  # EMA of 0/1 fail
        self._calls: dict[str, int] = {}

    def mark(self, provider: str, *, ok: bool, latency_s: float) -> None:
        p = (provider or "unknown").strip() or "unknown"
        with self._lock:
            self._calls[p] = self._calls.get(p, 0) + 1
            prev_l = self._latency.get(p, latency_s)
            self._latency[p] = self._alpha * latency_s + (1 - self._alpha) * prev_l
            fail = 0.0 if ok else 1.0
            prev_e = self._errors.get(p, fail)
            self._errors[p] = self._alpha * fail + (1 - self._alpha) * prev_e

    def score(self, provider: str) -> float:
        """Higher is healthier (0..1-ish)."""
        with self._lock:
            err = self._errors.get(provider, 0.0)
            lat = self._latency.get(provider, 0.5)
        # Penalize errors hard; mild latency penalty.
        return max(0.0, 1.0 - err) * max(0.15, 1.0 / (1.0 + lat))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            providers = sorted(set(self._calls) | set(self._latency) | set(self._errors))
            return {
                p: {
                    "calls": self._calls.get(p, 0),
                    "latency_ema_s": round(self._latency.get(p, 0.0), 3),
                    "error_ema": round(self._errors.get(p, 0.0), 3),
                    "score": round(self.score(p), 3),
                }
                for p in providers
            }


_health = HealthMonitor()


def health() -> HealthMonitor:
    return _health


def provider_for_rung(rung: str) -> str:
    return RUNG_PROVIDER.get(rung, "unknown")


def plan_from_governor(
    decision: dict,
    avail: set[str],
    *,
    tools_needed: bool = False,
) -> RoutePlan:
    """Build a RoutePlan from a Governor decision + availability mask."""
    rung = str(decision.get("rung") or "")
    if tools_needed:
        tool_rungs = [r for r in ("cloud_fast", "cloud_deep", "local_deep", "local_fast") if r in avail]
        if tool_rungs and rung not in tool_rungs:
            rung = tool_rungs[0]
            decision = {
                **decision,
                "rung": rung,
                "rationale": "forced to a tool-capable brain — request needs a tool",
            }
    if rung not in avail and avail:
        # Prefer healthiest available non-council rung.
        ranked = sorted(
            (r for r in avail if r != "council"),
            key=lambda r: health().score(provider_for_rung(r)),
            reverse=True,
        )
        rung = ranked[0] if ranked else next(iter(avail))

    fallbacks = [
        r for r in FALLBACK_ORDER
        if r != rung and r in avail
    ]
    # Re-order fallbacks by health (best first).
    fallbacks.sort(key=lambda r: health().score(provider_for_rung(r)), reverse=True)

    return RoutePlan(
        rung=rung,
        provider=provider_for_rung(rung),
        tools_enabled=bool(tools_needed or decision.get("tools_enabled")),
        fallbacks=fallbacks,
        rationale=str(decision.get("rationale") or ""),
        decision_id=str(decision.get("id") or ""),
        raw=dict(decision),
    )


def needs_capability(provider: str, cap: str) -> bool:
    return cap in CAPABILITIES.get(provider, frozenset())

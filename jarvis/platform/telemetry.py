"""In-process turn metrics — cost/latency/tool analytics foundation."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class TurnMetric:
    ts: float
    decision_id: str = ""
    rung: str = ""
    provider: str = ""
    latency_s: float = 0.0
    ok: bool = True
    escalated: bool = False
    tools_used: int = 0
    prompt_chars: int = 0
    answer_chars: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class MetricsRing:
    def __init__(self, maxlen: int = 256) -> None:
        self._lock = threading.RLock()
        self._items: deque[TurnMetric] = deque(maxlen=maxlen)

    def record(self, metric: TurnMetric) -> None:
        with self._lock:
            self._items.append(metric)

    def recent(self, n: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)[-max(1, n):]
        return [asdict(m) for m in items]

    def summary(self) -> dict[str, Any]:
        with self._lock:
            items = list(self._items)
        if not items:
            return {"count": 0, "ok_rate": 1.0, "latency_avg_s": 0.0}
        ok = sum(1 for m in items if m.ok)
        lat = sum(m.latency_s for m in items) / len(items)
        by_provider: dict[str, int] = {}
        for m in items:
            by_provider[m.provider or "?"] = by_provider.get(m.provider or "?", 0) + 1
        return {
            "count": len(items),
            "ok_rate": round(ok / len(items), 3),
            "latency_avg_s": round(lat, 3),
            "by_provider": by_provider,
        }


_ring = MetricsRing()


def record_turn(
    *,
    decision_id: str = "",
    rung: str = "",
    provider: str = "",
    latency_s: float = 0.0,
    ok: bool = True,
    escalated: bool = False,
    tools_used: int = 0,
    prompt_chars: int = 0,
    answer_chars: int = 0,
    **extra: Any,
) -> None:
    _ring.record(TurnMetric(
        ts=time.time(),
        decision_id=decision_id,
        rung=rung,
        provider=provider,
        latency_s=latency_s,
        ok=ok,
        escalated=escalated,
        tools_used=tools_used,
        prompt_chars=prompt_chars,
        answer_chars=answer_chars,
        extra=dict(extra) if extra else {},
    ))


def recent(n: int = 50) -> list[dict[str, Any]]:
    return _ring.recent(n)


def summary() -> dict[str, Any]:
    return _ring.summary()

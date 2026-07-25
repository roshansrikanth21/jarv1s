"""Minimal sync event bus — no third-party deps, safe for desktop single-process.

Handlers that raise are logged and skipped so one bad subscriber cannot kill a turn.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

log = logging.getLogger("jarvis.events")

Handler = Callable[..., Any]


class EventBus:
    """Thread-safe pub/sub. Emit is sync; keep handlers fast or schedule work yourself."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """Subscribe. Returns an unsubscribe callable."""
        if not event_type or not callable(handler):
            raise ValueError("event_type and callable handler required")
        with self._lock:
            self._handlers[event_type].append(handler)

        def _off() -> None:
            with self._lock:
                lst = self._handlers.get(event_type) or []
                if handler in lst:
                    lst.remove(handler)

        return _off

    def off(self, event_type: str, handler: Handler) -> None:
        with self._lock:
            lst = self._handlers.get(event_type) or []
            if handler in lst:
                lst.remove(handler)

    def emit(self, event_type: str, **payload: Any) -> int:
        """Fan out to current subscribers. Returns number of handlers invoked."""
        with self._lock:
            handlers = list(self._handlers.get(event_type) or [])
        for h in handlers:
            try:
                h(event_type, **payload)
            except Exception as exc:
                log.warning("event handler failed (%s): %s", event_type, exc)
        return len(handlers)

    def clear(self, event_type: str | None = None) -> None:
        with self._lock:
            if event_type is None:
                self._handlers.clear()
            else:
                self._handlers.pop(event_type, None)


# Process-wide bus. Tests can construct their own EventBus().
bus = EventBus()

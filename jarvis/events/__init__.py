"""In-process typed-ish event bus for Jarv1s kernel components."""
from __future__ import annotations

from .bus import EventBus, bus

__all__ = ["EventBus", "bus"]

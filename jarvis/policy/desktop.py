"""Desktop action risk classification for approval / logging."""
from __future__ import annotations

# Actions that change system state or can uninstall software.
HIGH_RISK = frozenset({
    "uninstall_app",
})
MEDIUM_RISK = frozenset({
    "open_registry",
    "open_control_panel",
    "key_press",
    "type_text",
    "window_close",
})


def desktop_risk(action: str) -> str:
    """Return 'high' | 'medium' | 'low' for a desktop tool sub-action."""
    a = (action or "").strip().lower()
    if a in HIGH_RISK:
        return "high"
    if a in MEDIUM_RISK:
        return "medium"
    return "low"


def requires_confirmation(action: str) -> bool:
    return desktop_risk(action) == "high"

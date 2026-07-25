"""Jarv1s kernel packages — cognition, memory, policy, session, events.

The FastAPI surface (`api.py`) remains the process entrypoint and gradually
shrinks to an adapter over these modules. Import leaf packages directly:

    from jarvis.memory.dialogue import DialogueStore
    from jarvis.policy.shell import check_command
    from jarvis.cognition.tokens import estimate_tokens
    from jarvis.events.bus import bus
"""
from __future__ import annotations

__version__ = "0.1.0"

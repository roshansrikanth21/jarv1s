"""Explicit SessionState — never rely on the LLM to remember orchestration facts."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class WorkflowPhase(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"
    AWAITING_APPROVAL = "awaiting_approval"


@dataclass
class SessionState:
    """Mutable per-process session. api.py owns the singleton; kernel modules read it."""

    session_id: str = "local"
    current_goal: Optional[str] = None
    current_project: Optional[str] = None
    current_task: Optional[str] = None
    subtask: Optional[str] = None
    workflow: WorkflowPhase = WorkflowPhase.IDLE
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    interrupted: Optional[dict[str, Any]] = None
    queue: list[dict[str, Any]] = field(default_factory=list)
    tool_state: dict[str, Any] = field(default_factory=dict)

    def reset_orchestration(self) -> None:
        """Clear goal/task graph without wiping dialogue (caller clears dialogue separately)."""
        self.current_goal = None
        self.current_project = None
        self.current_task = None
        self.subtask = None
        self.pending_approvals.clear()
        self.interrupted = None
        self.queue.clear()
        self.tool_state.clear()
        self.workflow = WorkflowPhase.IDLE

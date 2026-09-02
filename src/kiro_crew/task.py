"""Task state machine for tracking message lifecycle."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class TaskState(enum.Enum):
    """Lifecycle states for a task (message)."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Terminal states — no further transitions allowed
_TERMINAL = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}

# Valid transitions: from_state -> set of allowed to_states
_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING: {TaskState.IN_PROGRESS, TaskState.CANCELLED},
    TaskState.IN_PROGRESS: {
        TaskState.AWAITING_APPROVAL,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.AWAITING_APPROVAL: {
        TaskState.IN_PROGRESS,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
}


class InvalidTransition(Exception):  # noqa: N818
    """Raised when a state transition is not allowed."""


@dataclass
class Task:
    """Tracks a single message through its lifecycle."""

    id: str
    state: TaskState = TaskState.PENDING
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    def transition(self, to: TaskState) -> None:
        """Move to *to* state, raising InvalidTransition if not allowed."""
        allowed = _TRANSITIONS.get(self.state)
        if allowed is None or to not in allowed:
            raise InvalidTransition(f"{self.state.value} -> {to.value}")
        self.state = to
        now = time.monotonic()
        if to == TaskState.IN_PROGRESS and self.started_at is None:
            self.started_at = now
        if to in _TERMINAL:
            self.finished_at = now

    def start(self) -> None:
        self.transition(TaskState.IN_PROGRESS)

    def complete(self) -> None:
        self.transition(TaskState.COMPLETED)

    def fail(self, error: str = "") -> None:
        self.transition(TaskState.FAILED)
        self.error = error

    def cancel(self) -> None:
        self.transition(TaskState.CANCELLED)

    def await_approval(self) -> None:
        self.transition(TaskState.AWAITING_APPROVAL)

    def resume(self) -> None:
        """Resume from awaiting approval back to in-progress."""
        self.transition(TaskState.IN_PROGRESS)

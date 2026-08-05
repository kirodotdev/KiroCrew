"""SideState: sidecar buffer attached to a parent ChatSlot.

Side messages live only on ``slot._side``; they are never persisted to
JSONL, memory.db, lessons, or preferences. Lifecycle: open → turn(s) → close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SideState:
    """One side conversation attached to a parent slot.

    ``is_complete`` is False while a turn is in flight; flipped True in the
    ``_run_side_turn`` finally block.
    """

    open: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_run_id: str = ""
    is_complete: bool = True
    created_at: str = field(default_factory=_now_iso)

    def append_user(self, content: str, ts: str = "") -> None:
        self.messages.append(
            {
                "role": "user",
                "content": content,
                "ts": ts or _now_iso(),
            }
        )

    def append_assistant(self, content: str, ts: str = "") -> None:
        self.messages.append(
            {
                "role": "assistant",
                "content": content,
                "ts": ts or _now_iso(),
            }
        )

    def clear(self) -> None:
        self.messages.clear()
        self.last_run_id = ""
        self.is_complete = True

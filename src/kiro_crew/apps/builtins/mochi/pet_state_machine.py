"""Pet behaviour state machine for Mochi — the flow table, nothing else.

Ported from ``src/shared/petStateMachine.ts``. Controls behaviour flow only;
visual expression (mood) is separate, driven by the ``set_pet_mood`` MCP tool.

::

    idle → thinking → working → idle
    idle → walking → idle
    any → error → idle (auto 3s)
    any → offline → idle (reconnect)

The transition table was verified against the TypeScript exhaustively — all
96 (state, event) pairs — at port time, and is pinned by
``test/test_mochi_small_modules.py`` (the original's own table test carried
over). Unknown states and unmapped events return the current state unchanged,
matching the ``?? current`` fallback.
"""

from __future__ import annotations

# Auto-recover from the error state after this long.
ERROR_TIMEOUT_MS = 3000

ALL_STATES = ("idle", "thinking", "working", "walking", "error", "offline")

ALL_EVENTS = (
    "user_input",
    "voice_start",
    "voice_end",
    "voice_error",
    "task_start",
    "tool_call",
    "task_complete",
    "approval_required",
    "approval_granted",
    "approval_rejected",
    "walk_start",
    "walk_done",
    "error",
    "connect",
    "disconnect",
    "timeout",
)

TRANSITIONS: dict[str, dict[str, str]] = {
    "idle": {
        "user_input": "thinking",
        "voice_start": "thinking",
        "task_start": "thinking",
        "walk_start": "walking",
        "error": "error",
        "disconnect": "offline",
    },
    "thinking": {
        "tool_call": "working",
        "task_complete": "idle",
        "voice_end": "idle",
        "error": "error",
        "disconnect": "offline",
    },
    "working": {
        "task_complete": "idle",
        "approval_required": "working",
        "approval_granted": "working",
        "approval_rejected": "idle",
        "error": "error",
        "disconnect": "offline",
    },
    "walking": {
        "walk_done": "idle",
        "user_input": "thinking",
        "error": "error",
        "disconnect": "offline",
    },
    "error": {
        "timeout": "idle",
        "user_input": "thinking",
        "connect": "idle",
        "disconnect": "offline",
    },
    "offline": {
        "connect": "idle",
        "error": "error",
    },
}


def transition(current: str, event: str) -> str:
    """Next state for ``event`` in ``current``; unchanged when unmapped."""
    state_map = TRANSITIONS.get(current)
    if state_map is None:
        return current
    return state_map.get(event, current)


def is_legal_transition(from_state: str, event: str) -> bool:
    return event in TRANSITIONS.get(from_state, {})


def legal_events(state: str) -> list[str]:
    return list(TRANSITIONS.get(state, {}))

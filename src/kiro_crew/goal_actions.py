"""Read and pause the goal on the existing auto-nudge service."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from kiro_crew import autonudge
from kiro_crew.goal import GOAL_PAUSE_UNSAVED_MESSAGE, GOAL_PAUSE_UNSAVED_REASON
from kiro_crew.messaging.link import canonical_key


def goal_snapshot(loop: autonudge.NudgeLoop) -> dict[str, Any]:
    return {
        "goal_id": loop.id,
        "generation": loop.config_generation,
        "active": loop.active,
        "stopped_reason": loop.stopped_reason,
        "goal": asdict(loop.goal) if loop.goal else None,
    }


def _goal_loops_for_session(
    state: Any, service: autonudge.AutoNudgeService, binding: str
) -> list[autonudge.NudgeLoop]:
    """Collect loops using only the session's explicit dashboard links."""
    bindings = {binding}
    bindings.update(
        slot.key
        for slot in getattr(state, "_slots", {}).values()
        if getattr(slot, "linked_session_key", "") == binding
    )
    # Every binding must match its stored key exactly; a name-fold fallback
    # cannot establish authority, even for an unlinked dashboard's primary key.
    loops = {
        loop.id: loop
        for key in bindings
        if (loop := service.get_by_slot(key)) is not None and loop.slot_key == key
    }
    return list(loops.values())


def goal_loop_for_session(
    state: Any, service: autonudge.AutoNudgeService, binding: str
) -> autonudge.NudgeLoop | None:
    """Require a single owner before inspecting or changing goal contents."""
    loops = _goal_loops_for_session(state, service, binding)
    if len(loops) > 1:
        raise ValueError(
            "this session has multiple automation records; inspect and clear the intended loop by id"
        )
    return next(iter(loops), None)


async def pause_session_goal(session_key: str, *, state: Any = None) -> bool:
    """Stop future goal turns while retaining progress for inspection/resume."""
    service = autonudge.get_instance()
    # Stop can follow an existing explicit link even when this channel cannot
    # arm new goals. Preserve the session layer's legacy Slack timestamp shim.
    binding = autonudge.binding_key_for(session_key) or canonical_key(session_key)
    if service is None or not binding:
        return False
    # Reconciliation can join two independent loops. Stop pauses
    # every explicitly owned goal without choosing one to revise or replace.
    paused = [
        await service.pause_goal(loop.id)
        for loop in _goal_loops_for_session(state, service, binding)
        if loop.goal is not None
    ]
    return bool(paused) and all(paused)


def goal_pause_warning(session_key: str, *, state: Any = None) -> str:
    """Name a pause that stopped this process but has not been saved."""
    service = autonudge.get_instance()
    binding = autonudge.binding_key_for(session_key) or canonical_key(session_key)
    if service is None or not binding:
        return ""
    if any(
        loop.goal is not None and loop.stopped_reason == GOAL_PAUSE_UNSAVED_REASON
        for loop in _goal_loops_for_session(state, service, binding)
    ):
        return GOAL_PAUSE_UNSAVED_MESSAGE
    return ""

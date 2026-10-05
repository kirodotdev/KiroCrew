"""Read and pause the goal on the existing auto-nudge service."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from kiro_crew import autonudge
from kiro_crew.goal import (
    GOAL_BLOCKED_REASON,
    GOAL_PAUSE_UNSAVED_MESSAGE,
    GOAL_PAUSE_UNSAVED_REASON,
    GoalState,
    continuation_message,
)
from kiro_crew.messaging.link import canonical_key


def goal_snapshot(loop: autonudge.NudgeLoop) -> dict[str, Any]:
    return {
        "goal_id": loop.id,
        "generation": loop.config_generation,
        "active": loop.active,
        "stopped_reason": loop.stopped_reason,
        "goal": asdict(loop.goal) if loop.goal else None,
    }


def _goal_bindings_for_session(state: Any, binding: str) -> set[str]:
    """Collect exact ownership keys from the session's explicit dashboard links."""
    bindings = {binding}
    bindings.update(
        slot.key
        for slot in getattr(state, "_slots", {}).values()
        if getattr(slot, "linked_session_key", "") == binding
    )
    return bindings


def _goal_loops_for_session(
    state: Any, service: autonudge.AutoNudgeService, binding: str
) -> list[autonudge.NudgeLoop]:
    """Collect loops using only the session's explicit dashboard links."""
    # Every binding must match its stored key exactly; a name-fold fallback
    # cannot establish authority, even for an unlinked dashboard's primary key.
    loops = {
        loop.id: loop
        for key in _goal_bindings_for_session(state, binding)
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
    return await service.pause_goals_by_slot(_goal_bindings_for_session(state, binding))


CHANNEL_DENIED_PROGRESS = (
    "This channel's inbound messages are denied by the channels governance policy, "
    "so no goal turn can run here. Allow the channel, then resume the goal."
)


async def block_goal_on_channel_denial(
    service: autonudge.AutoNudgeService,
    loop: autonudge.NudgeLoop,
    *,
    fired_goal: GoalState | None,
    fired_generation: int,
) -> bool:
    """Mark a running goal blocked when its channel refuses every wake.

    Only the revision that fired is blocked: a pause, edit or resume that landed
    since then keeps its own state. ``loop`` is the live row, which an update
    changes in place while the dispatch awaits, so the caller passes the goal and
    generation it read before dispatching.
    """
    if fired_goal is None:
        return False
    generation = fired_generation
    blocked = fired_goal.revised({"status": "blocked", "progress": CHANNEL_DENIED_PROGRESS})

    def _still_running(live: autonudge.NudgeLoop) -> bool:
        return (
            live.active
            and live.config_generation == generation
            and live.goal is not None
            and live.goal.status in {"working", "waiting"}
        )

    updated = await service.update(
        loop.id,
        active=False,
        goal=blocked,
        message=continuation_message(blocked),
        stopped_reason=GOAL_BLOCKED_REASON,
        precondition=_still_running,
    )
    return updated is not None


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

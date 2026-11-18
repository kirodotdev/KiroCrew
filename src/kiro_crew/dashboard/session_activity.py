"""One snapshot of work a dashboard session still owns.

A slot's ``running`` flag covers only its model turn. Session-control callers and
Conductor mode also need the work that survives that turn: attached sub-agents,
a dynamic workflow, pending synthesis, background preparation, and compaction.
The active automation and queued sub-agents are reported too, but remain waits
between execution rather than current execution.
"""

from __future__ import annotations

from typing import Any, Literal

from kiro_crew.dashboard.chat_utils_slots import effective_session_key, session_key_for
from kiro_crew.dashboard.slot_preparing import is_preparing

SessionActivity = Literal[
    "turn",
    "admission",
    "subagents",
    "subagents_queued",
    "workflow",
    "synthesis",
    "preparing",
    "compaction",
    "automation",
    "unknown",
]

_KNOWN_EXECUTION = frozenset(
    {"turn", "admission", "subagents", "workflow", "synthesis", "preparing", "compaction"}
)
_CURRENT_EXECUTION = _KNOWN_EXECUTION | {"unknown"}


def _add(activity: list[SessionActivity], kind: SessionActivity) -> None:
    if kind not in activity:
        activity.append(kind)


def _subagent_activity(state: Any, session_key: str) -> tuple[bool, bool] | None:
    """``(active, queued)`` or unknown from the manager's memory snapshots."""
    manager = vars(state).get("subagents")
    if manager is None:
        return False, False
    try:
        active = False
        running_for = getattr(manager, "running_agents_for", None)
        if callable(running_for):
            running = running_for(session_key)
            if running is None:
                return None
            active = any(not row.get("awaitingSpawnApproval", False) for row in running)
        current_for = getattr(manager, "has_current_execution_for", None)
        if callable(current_for):
            active = active or current_for(session_key) is True
        queued = False
        published = getattr(manager, "published_queued_depths", None)
        if callable(published):
            depths = published()
            queued = queued or (
                isinstance(depths, dict) and int(depths.get(session_key, 0) or 0) > 0
            )
        return active, queued
    except Exception:
        return None


def session_activity(state: Any, slot: Any, *, slot_key: str = "") -> tuple[SessionActivity, ...]:
    """Return the bounded activity kinds currently owned by ``slot``.

    ``slot_key`` is for a narrow test or adapter whose slot double carries no
    key; real dashboard slots leave it empty. This is a read-only, in-memory
    snapshot. An unreadable background-work probe yields ``unknown`` so an agent
    never mistakes missing status for idle.
    """
    activity: list[SessionActivity] = []
    if bool(getattr(slot, "turn_running", False)):
        _add(activity, "turn")
    elif bool(getattr(slot, "running", False)):
        _add(activity, "admission")
    if bool(getattr(slot, "_compacting", False)):
        _add(activity, "compaction")
    if is_preparing(slot):
        _add(activity, "preparing")
    if bool(getattr(slot, "_pending_synthesis", False)):
        _add(activity, "synthesis")

    key = (
        effective_session_key(slot)
        if getattr(slot, "key", "")
        else session_key_for(slot_key, getattr(slot, "linked_session_key", "") or "")
    )
    subagents = _subagent_activity(state, key)
    if subagents is None:
        _add(activity, "unknown")
    else:
        active, queued = subagents
        if active:
            _add(activity, "subagents")
        if queued:
            _add(activity, "subagents_queued")

    workflows = vars(state).get("workflow_service")
    registry = getattr(workflows, "registry", None) if workflows is not None else None
    pending_workflow = getattr(registry, "has_pending_work_for", None)
    if callable(pending_workflow):
        try:
            if pending_workflow(key):
                _add(activity, "workflow")
        except Exception:
            _add(activity, "unknown")

    try:
        from kiro_crew import ledger_wake
        from kiro_crew.autonudge import get_instance

        if ledger_wake.worker_looping(get_instance(), str(getattr(slot, "key", "") or "")):
            _add(activity, "automation")
    except Exception:
        _add(activity, "unknown")
    return tuple(activity)


def session_activity_for_key(state: Any, session_key: str) -> tuple[SessionActivity, ...]:
    """Read one session through the same slot spellings as the wake gate."""
    getter = getattr(state, "get_slot", None)
    if not callable(getter) or not session_key:
        return ()
    for candidate in (session_key, f"dashboard_{session_key}"):
        try:
            slot = getter(candidate)
        except Exception:
            continue
        if slot is not None:
            return session_activity(state, slot, slot_key=session_key)
    return ()


def session_known_execution_active(activity: tuple[SessionActivity, ...]) -> bool:
    """Whether positive evidence says this session executes work now."""
    return any(kind in _KNOWN_EXECUTION for kind in activity)


def session_execution_active(activity: tuple[SessionActivity, ...]) -> bool:
    """Status view of execution; an unreadable probe fails closed to busy."""
    return any(kind in _CURRENT_EXECUTION for kind in activity)


def session_work_pending(activity: tuple[SessionActivity, ...]) -> bool:
    """Whether proven current or queued work prevents a stale verdict."""
    return session_known_execution_active(activity) or "subagents_queued" in activity

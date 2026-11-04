"""The dashboard's answers to "which workflow run does this session work for".

``WorkflowService`` caps runs started from inside runs (``workflows/nesting.py``).
Its ledger knows a run's own worker keys; the two lookups here answer for the
sessions only the dashboard can see: a sub-agent's spawning session, a task
step's shared run, and a dashboard session a run opened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

_Lookup = Callable[[str], Optional[str]]

_SUBAGENT_PREFIX = "subagent:"


def subagent_parent_session(state: "DashboardState", session_key: str) -> str | None:
    """The session that spawned sub-agent *session_key*, or ``None``."""
    manager = getattr(state, "subagents", None)
    if manager is None or not session_key.startswith(_SUBAGENT_PREFIX):
        return None
    info = manager.get(session_key[len(_SUBAGENT_PREFIX) :])
    return getattr(info, "parent_session_key", "") or None


def workflow_run_of_session(state: "DashboardState", session_key: str) -> str | None:
    """The run a task step session or a run-opened dashboard session works for."""
    parts = session_key.split(":")
    if len(parts) >= 3 and parts[0] == "taskrunner":
        runner = getattr(state, "task_runner", None)
        run = runner._runs.get(parts[1]) if runner is not None else None
        return getattr(run, "workflow_run_id", "") or None
    workflows = getattr(state, "workflow_service", None)
    if workflows is None or not workflows.nesting.tracks_opened_sessions:
        return None
    # A session's MCP calls carry its history key, which the slot lookup maps.
    from kiro_crew.dashboard.session_control_parts.caller_access import caller_slot_key

    slot_key = caller_slot_key(state, session_key)
    return (workflows.nesting.opened_by(slot_key) or None) if slot_key else None


def lineage_lookups(state: "DashboardState") -> tuple[_Lookup, _Lookup]:
    """The two lookups ``WorkflowService`` takes, bound to *state*."""

    def parent_session_of(session_key: str) -> str | None:
        return subagent_parent_session(state, session_key)

    def run_of_session(session_key: str) -> str | None:
        return workflow_run_of_session(state, session_key)

    return parent_session_of, run_of_session

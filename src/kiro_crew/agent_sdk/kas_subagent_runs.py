"""What application code reads of KAS sub-agent runs and engine children.

The agent-SDK boundary in front of ``acp.kas_parent_limits``,
``acp.session_handle_native_children`` and ``acp._dispatch``: the sub-agent manager
notes a run's parent session, the session pool asks whether a run is held to its
parents' denies, and the dashboard's engine cards bound their roster and key a
child's calls. Each function looks the ACP owner up when called, so a patch on the
owner reaches every caller.
"""

from __future__ import annotations

from kiro_crew.acp.session_handle_native_children import NATIVE_CHILD_ROSTER_CAP


def note_parent(run_key: str, parent_key: str, parent_dir: str = "") -> None:
    """Record *parent_key* as the session that started the run on *run_key*."""
    from kiro_crew.acp import kas_parent_limits

    kas_parent_limits.note_parent(run_key, parent_key, parent_dir)


def run_is_held(run_key: str, agent: str, run_dir: str = "") -> bool:
    """Whether the run on *run_key*, on *agent* in *run_dir*, is held to a parent
    agent's denies."""
    from kiro_crew.acp import kas_parent_limits

    return bool(kas_parent_limits.run_is_held(run_key, agent, run_dir))


def liveness_key(tool_call_id: object) -> str:
    """The bounded key the transport keeps a tool call's liveness under."""
    from kiro_crew.acp import _dispatch

    return _dispatch.liveness_key(tool_call_id)


__all__ = ["NATIVE_CHILD_ROSTER_CAP", "liveness_key", "note_parent", "run_is_held"]

"""The reads a cold start makes about the session it starts.

``get_or_create`` imports each of these and stays the path callers and tests import
them from. None is a ``SessionAllocationService`` member: each takes its inputs as
arguments and imports what it reads at call time. The three marked "Blocking" run
off the event loop at their call sites.
"""

from __future__ import annotations

from typing import Any


def _worker_tool_limited_servers(
    key: str, mcp_carried: bool = False, native_carried: bool = False
) -> tuple[str, ...]:
    """The servers whose single tools worker *key*'s crewmate file turns off.

    With *mcp_carried*, only the limits no MCP deny carries, less the native tools
    the session's built-in list withholds with *native_carried*. Blocking.
    """
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker import crewmate_tool_limited_servers

    execution = read_session_execution(key)
    member_id = str(getattr(execution, "member_id", "") or "") if execution is not None else ""
    return crewmate_tool_limited_servers(
        member_id, mcp_tool_limits_carried=mcp_carried, native_tool_limits_carried=native_carried
    )


def _crew_oauth_bridges_chat() -> bool:
    """Whether a chat session must project a Crew-owned Connection bridge. Blocking."""
    from kiro_crew.connections import crew_oauth

    return crew_oauth.bridges_chat_sessions()


def _held_to_parent_denies(owner: Any, key: str, agent: str | None) -> bool:
    """Whether a KAS sub-agent run on *key* carries a parent agent's denies. Blocking."""
    from kiro_crew.acp_backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk.kas_subagent_runs import run_is_held

    on_kas = owner._cfg.agent.acp_backend == ACP_BACKEND_KAS
    return on_kas and run_is_held(key, agent or owner._pool_agent or "", owner._pool_cwd or "")


def _is_crewmate_worker_key(key: str) -> bool:
    """Whether *key* is a crewmate's own worker session (``members.is_crewmate_worker_session``)."""
    from kiro_crew.members import is_crewmate_worker_session

    return is_crewmate_worker_session(key)

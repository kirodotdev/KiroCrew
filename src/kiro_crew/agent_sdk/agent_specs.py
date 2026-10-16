"""Read how a Crew agent's sessions are shaped from application code, without the ACP layer.

Application code may not import ``kiro_crew.acp``
(``scripts/check_agent_sdk_boundary.py``). A crewmate's heartbeat
(``dashboard.goal_reminder.member_has_session_tools``) still needs the answer a
session would get to "is this MCP server switched off for this agent", so this module
is the seam it reaches that read through. The ACP import is made at call time, both
because ``kiro_crew.acp`` imports back into ``agent_sdk`` at load time and so that a
test patching the ACP function on its own module is seen here.
"""

from __future__ import annotations


def session_mcp_server_is_disabled(name: str, agent: str | None) -> bool:
    """Whether MCP server *name* is switched off whole for a session as *agent*.

    ``acp.session_mcp.session_mcp_server_is_disabled``: blocking, and an unreadable
    source switches nothing off.
    """
    from kiro_crew.acp import session_mcp

    return session_mcp.session_mcp_server_is_disabled(name, agent)

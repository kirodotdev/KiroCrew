"""Give a Bot's own KAS chat the crewmate's record tools when its agent lacks them.

A Bot on its own file chats on the conductor, which carries ``kirocrew-mate``. A
Bot bound to an agent of the user's own (``kirocrew agent update --kiro-agent``,
or the dashboard's agent field) chats on THAT agent
(:func:`kiro_crew.mate_worker.member_chat_agent`), and a user agent seldom
declares ``kirocrew-mate``. Goals, milestones and check-ins are set only by
talking to the Bot (the decision record that a new goal starts in the
crewmate's chat), so without the server that chat cannot set one at all.

:func:`with_member_goal_tools` adds the server to the PROJECTED spec of that one
session, never to the file on disk, so every other session on the same agent
keeps its own tool set. The entry is the managed one, so the KAS hoist
(``kas_agents_session_hoist``) gives it this session's identity as it does the
conductor's; the grants are :data:`kiro_crew.agent._MATE_AUTO_GRANTS`, which
still cross the governance ceiling in the projection. An entry the spec
declares muted is the user's own switch-off and is left alone, as is a spec
that already mounts the server.

Blocking (the managed entry resolves the ``kirocrew`` launcher on disk); called
from the KAS harness's off-loop build.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.member_mate_tools import MATE_SERVER, _referenced

_MATE_REF = f"@{MATE_SERVER}"


def needs_member_goal_tools(spec: Any, agent: str, session_key: str) -> bool:
    """Whether *spec*, projected for *session_key* on *agent*, must gain the mate tools.

    Only a Bot's own DM session on an agent that is not the conductor, whose spec
    neither mounts ``kirocrew-mate`` nor switches it off.
    """
    # circular import: members' module graph is heavy; resolved at call time.
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted
    from kiro_crew.member_mate_tools import chat_lacks_goal_tools
    from kiro_crew.members import is_member_session_key

    if not isinstance(spec, dict) or not chat_lacks_goal_tools(agent, None):
        return False
    if not is_member_session_key(session_key):
        return False
    servers = spec.get("mcpServers")
    entry = servers.get(MATE_SERVER) if isinstance(servers, dict) else None
    if isinstance(entry, dict):
        return not mcp_entry_is_muted(entry) and not _referenced(spec.get("tools"))
    return True


def with_member_goal_tools(spec: dict[str, Any], agent: str, session_key: str) -> dict[str, Any]:
    """*spec* with ``kirocrew-mate`` mounted and granted, or *spec* unchanged.

    A new dict: the caller's spec, and the file it came from, are never written.
    """
    if not needs_member_goal_tools(spec, agent, session_key):
        return spec
    # circular import: agent imports the config loader, which sits below this module.
    from kiro_crew.agent import _MATE_AUTO_GRANTS, managed_mcp_spec_entry

    servers = spec.get("mcpServers")
    servers = dict(servers) if isinstance(servers, dict) else {}
    if not isinstance(servers.get(MATE_SERVER), dict):
        entry = managed_mcp_spec_entry(MATE_SERVER)
        if not isinstance(entry, dict):
            return spec
        servers[MATE_SERVER] = entry
    out = {**spec, "mcpServers": servers}
    tools = spec.get("tools")
    # KAS reads an absent ``tools`` as no tools and the projection fails closed
    # on one, so only a list is widened; ``"*"`` already covers the server.
    if isinstance(tools, list) and not _referenced(tools):
        out["tools"] = [*tools, _MATE_REF]
    allowed = spec.get("allowedTools")
    # Only a list the author wrote is widened: a spec with none grants through its
    # own ``permissions``, which the projection keeps only while no allowlist is
    # present (``kas_permissions.merge_user_permissions``), so adding one would drop
    # those grants. There the goal tools ask before their card.
    if isinstance(allowed, list):
        out["allowedTools"] = [*allowed, *(g for g in _MATE_AUTO_GRANTS if g not in allowed)]
    return out

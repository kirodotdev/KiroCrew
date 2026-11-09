"""Which tools a saved Capabilities spec switches off (``disabledTools``).

Split out of :mod:`kiro_crew.agent_capabilities`, which re-exports both names,
so callers keep that import path.
"""

from __future__ import annotations

from kiro_crew.mcp_cleanup import mcp_entry_is_muted


def tool_limited_servers(spec: dict) -> tuple[str, ...]:
    """Launched MCP servers in *spec* whose entry turns single tools off.

    The one definition both the chat-start seam (via
    :func:`prepare_member_capabilities`) and the Capabilities page warning read,
    so the page cannot name a different set than the start refuses on.
    """
    servers = spec.get("mcpServers", {})
    if not isinstance(servers, dict):
        return ()
    return tuple(
        sorted(
            name
            for name, server in servers.items()
            if isinstance(server, dict)
            and server.get("disabledTools")
            and not mcp_entry_is_muted(server)
        )
    )


def disabled_tool_names(spec: dict) -> tuple[str, ...]:
    """``server/tool`` per tool a :func:`tool_limited_servers` server turns off.

    A server whose ``disabledTools`` is not a list of names is named alone.
    """
    servers = spec.get("mcpServers", {})
    out: list[str] = []
    for name in tool_limited_servers(spec):
        tools = servers[name].get("disabledTools")
        names = (
            [t.strip() for t in tools if isinstance(t, str) and t.strip()]
            if isinstance(tools, list)
            else []
        )
        out.extend([f"{name}/{t}" for t in names] or [name])
    return tuple(out)

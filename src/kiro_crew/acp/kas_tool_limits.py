"""Which per-server keys the KAS customAgents wire drops, and the one Crew re-carries.

A sibling of :mod:`kiro_crew.acp.kas_agents`, which imports the projection's names
here and is the import path callers use for them. :func:`with_switched_off_tools`
runs after the projection, so the KAS harness imports it from here.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_cleanup import mcp_entry_is_muted

#: Per-server keys KAS's wire schema ACCEPTS and then throws away, so projecting
#: one is indistinguishable from omitting it. ``ClientAgentMcpServerSchema``
#: declares every name below, and then ``mapClientMcpServers`` rebuilds each
#: entry from ``command``/``args``/``env``/``timeout`` for a stdio server or
#: ``url``/``headers``/``env``/``timeout`` for a remote one — nothing else
#: survives the rebuild.
#:
#: ``type`` is absent from this tuple because it is lost one step EARLIER and for
#: a different reason: the schema has no slot for it at all, so the unknown key
#: is stripped before the mapper runs. The consequence of that is the registry
#: filter in ``kas_agents._project_mcp_servers``, not this tuple.
#:
#: Two are acted on. ``disabled`` is the one whose loss INVERTS the user's
#: decision — a muted server that arrives unmuted launches — and omitting the
#: declaration is a faithful way to express it. ``disabledTools`` is carried in
#: the agent's own ``excludedTools`` instead (:func:`disabled_tool_exclusions`),
#: a field the engine applies. The others are restrictions with no Crew-side
#: carrier: ``cwd`` changes where the server runs and not whether it runs, and
#: ``autoApprove`` is already dropped upstream of here for its own reason. They
#: are named so an operator reading the log learns the restriction had no effect.
#:
#: TODO(kiro-agent): copy these through in ``mapClientMcpServers`` (and add
#: ``type`` to ``ClientAgentMcpServerSchema``); the handling here then becomes a
#: no-op rather than something to unpick.
_KAS_DISCARDED_ENTRY_KEYS = ("autoApprove", "cwd", "disabled", "disabledTools")

#: The discarded keys whose restriction still holds, through another field.
_KAS_CARRIED_ELSEWHERE = frozenset({"disabledTools"})


def disabled_tool_exclusions(spec: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``@server/tool`` for each tool a launched server's ``disabledTools`` turns off.

    The wire drops ``disabledTools`` from the server entry, so the restriction is
    re-expressed in the agent's ``excludedTools``, as the crewmate worker's spec
    already does (``mate_worker.disabled_tool_refs``). KAS tags every MCP tool
    ``@<server>/<tool>`` with the names as declared, and drops a tool whose tag
    equals an ``excludedTools`` entry. A muted server is not declared at all, so
    its entry adds nothing here.

    Returns the refs and, sorted, the launched servers whose ``disabledTools`` is
    set but is not a list of non-blank tool names. Nothing can be carried for
    those, so the caller refuses the spec rather than run the server whole.
    """
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return [], []
    refs: list[str] = []
    unusable: list[str] = []
    for name, entry in servers.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        if mcp_entry_is_muted(entry):
            continue
        tools = entry.get("disabledTools")
        if tools is None or tools == []:
            continue
        if not isinstance(tools, list) or not all(isinstance(t, str) and t.strip() for t in tools):
            unusable.append(name)
            continue
        refs.extend(f"@{name}/{tool}" for tool in tools)
    return list(dict.fromkeys(refs)), sorted(unusable)


def with_switched_off_tools(
    custom_agents: list[dict[str, Any]], disabled_tools: frozenset[tuple[str, str]]
) -> list[dict[str, Any]]:
    """*custom_agents* with ``@server/tool`` in ``excludedTools`` for each pair.

    The pairs are ``session_mcp.session_mcp_disabled_tools``'s.
    :func:`disabled_tool_exclusions` reads the spec alone; the dashboard's tool-off
    action (``api_mcp_toggle_tool``) writes only the global settings file.
    """
    refs = [f"@{server}/{tool}" for server, tool in sorted(disabled_tools)]
    if not refs:
        return custom_agents
    return [
        {**entry, "excludedTools": list(dict.fromkeys([*entry.get("excludedTools", []), *refs]))}
        for entry in custom_agents
    ]

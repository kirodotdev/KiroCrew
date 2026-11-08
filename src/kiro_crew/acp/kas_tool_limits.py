"""Which per-server keys the KAS customAgents wire drops, and the one Crew re-carries.

A sibling of :mod:`kiro_crew.acp.kas_agents`, which imports the projection's names
here and is the import path callers use for them. :func:`with_switched_off_tools`
runs after the projection, so the KAS harness imports it from here.
"""

from __future__ import annotations

import logging
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

#: Most ``excludedTools`` entries one ``disabledTools`` glob may expand to.
_MAX_GLOB_BRANCHES = 64
#: Longest glob expanded: it may come from a settings file a checkout supplies,
#: and the expansion copies the pattern once per brace group.
_MAX_GLOB_CHARS = 512
#: Unmatched patterns the session-start notice names before it counts the rest.
_MAX_NOTICE_LINES = 8

logger = logging.getLogger(__name__)


def kas_globs(pattern: str) -> list[str] | None:
    """*pattern* as the ``excludedTools`` entries KAS matches the same way, else ``None``.

    kiro-cli compares a ``disabledTools`` entry exactly, and only an entry with
    ``*`` or ``?`` also goes through ``globset`` (``agent/util/glob.rs``
    ``matches_any_pattern``). KAS's ``excludedTools`` knows ``*`` and ``?`` and
    reads every other character literally (``tools/tool-filter.ts``
    ``globMatch``). So an entry without a wildcard is kept as is, and each
    ``{a,b}`` group is expanded into one entry per branch.

    ``None`` is a glob KAS has no spelling for: a ``[...]`` class, a ``\\``
    escape, ``**``, a nested, empty or unbalanced brace, more than
    :data:`_MAX_GLOB_BRANCHES` branches, or more than :data:`_MAX_GLOB_CHARS`
    characters. The callers carry such a glob as written, as before, and name it
    to the person (:func:`unmatched_globs_notice`): the server still runs.
    """
    if "*" not in pattern and "?" not in pattern:
        return [pattern]
    if len(pattern) > _MAX_GLOB_CHARS or any(c in pattern for c in "[]\\") or "**" in pattern:
        return None
    out = [""]
    rest = pattern
    while rest:
        start, end = rest.find("{"), rest.find("}")
        if start < 0 and end < 0:
            out = [head + rest for head in out]
            break
        if start < 0 or end < start:
            return None  # a "}" with no "{" before it
        body = rest[start + 1 : end]
        branches = body.split(",")
        # Checked before the product is built.
        if "{" in body or "" in branches or len(out) * len(branches) > _MAX_GLOB_BRANCHES:
            return None
        out = [head + rest[:start] + branch for head in out for branch in branches]
        rest = rest[end + 1 :]
    return list(dict.fromkeys(out))


def unmatched_disabled_globs(servers: Any) -> list[tuple[str, str]]:
    """``(server, pattern)`` for each ``disabledTools`` glob :func:`kas_globs` cannot carry.

    *servers* is an ``mcpServers`` mapping; a muted or malformed entry adds nothing.
    """
    if not isinstance(servers, dict):
        return []
    found: list[tuple[str, str]] = []
    for name, entry in servers.items():
        if not isinstance(name, str) or not isinstance(entry, dict) or mcp_entry_is_muted(entry):
            continue
        tools = entry.get("disabledTools")
        for tool in tools if isinstance(tools, list) else ():
            if isinstance(tool, str) and kas_globs(tool) is None:
                found.append((name, tool))
    return found


def unmatched_globs_notice(pairs: list[tuple[str, str]]) -> str:
    """The session-start lines for *pairs*, each also logged; empty when there are none."""
    if not pairs:
        return ""
    # Imported here: mcp_session_report reaches this module through kas_permissions.
    from kiro_crew.acp.mcp_session_report import NAME_CAP, sanitize_sink_text

    def shown(text: str) -> str:
        # Both come from a settings file or spec a checkout can supply.
        return sanitize_sink_text(text, NAME_CAP).replace("`", "'")

    for server, pattern in pairs:
        logger.warning(
            "KAS: disabledTools pattern %s for %s has no KAS spelling; that tool stays on",
            shown(pattern),
            shown(server),
        )
    lines = [
        f"⚠ disabledTools pattern `{shown(pattern)}` for `{shown(server)}`"
        " can't be applied on KAS, so that tool stays on."
        for server, pattern in pairs[:_MAX_NOTICE_LINES]
    ]
    rest = pairs[_MAX_NOTICE_LINES:]
    if rest:
        servers = ", ".join(f"`{shown(s)}`" for s in dict.fromkeys(s for s, _ in rest))
        lines.append(f"…and {len(rest)} more such patterns, for {servers}.")
    return "\n".join(lines)


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
    those, so the caller refuses the spec rather than run the server whole. A
    glob :func:`kas_globs` cannot carry is kept as written.
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
        refs.extend(f"@{name}/{g}" for tool in tools for g in kas_globs(tool) or [tool])
    return list(dict.fromkeys(refs)), sorted(unusable)


def with_switched_off_tools(
    custom_agents: list[dict[str, Any]], disabled_tools: frozenset[tuple[str, str]]
) -> list[dict[str, Any]]:
    """*custom_agents* with ``@server/tool`` in ``excludedTools`` for each pair.

    The pairs are ``session_mcp.session_mcp_disabled_tools``'s.
    :func:`disabled_tool_exclusions` reads the spec alone; the dashboard's tool-off
    action (``api_mcp_toggle_tool``) writes only the global settings file. A glob
    is carried as :func:`kas_globs` spells it, or as written when it has no spelling.
    """
    refs = [
        f"@{server}/{g}"
        for server, tool in sorted(disabled_tools)
        for g in kas_globs(tool) or [tool]
    ]
    if not refs:
        return custom_agents
    return [
        {**entry, "excludedTools": list(dict.fromkeys([*entry.get("excludedTools", []), *refs]))}
        for entry in custom_agents
    ]

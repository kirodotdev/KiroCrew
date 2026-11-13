"""Resolving a PreToolUse hook's MCP-form matcher to exact KAS resources.

:func:`kiro_crew.acp.kas_permissions.hook_gated_capabilities` asks these helpers
which real ``<server>/<tool>`` resources a matcher meets: the servers the policy's
``mcp`` allows name, each server's real tool names from the MCP probe cache, and
the hook store's own matcher run on the names the gate gives an MCP call. Split
out of ``kas_permissions`` by size; that module re-exports every public name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger("kiro_crew.acp.kas_permissions")

#: Marks an MCP server in a hook matcher (``@server/tool``), as in ``kas_permissions``.
_MCP_PREFIX = "@"

#: How an MCP tool's title spells it at the gate: ``mcp__<server>__<tool>``.
_MCP_TITLE_PREFIX = "mcp__"

#: KAS's capability for any MCP-served tool.
_MCP_CAPABILITY = "mcp"

#: Prefix of an :func:`kiro_crew.acp.kas_permissions.auto_approved_capabilities`
#: entry naming a server the ``mcp`` grant is confined to (``mcp-server:srv``).
#: None of them, with ``mcp`` approved, means the grant names no server it can be
#: confined to.
APPROVED_MCP_SERVER_PREFIX = "mcp-server:"


#: Reads the tool names Crew holds for one MCP server; ``None`` or empty when it
#: holds none.
McpToolNames = Callable[[str], "list[str] | None"]


def cached_mcp_tool_names(server: str) -> list[str] | None:
    """The tool names the MCP probe cache holds for *server*, read without spawning it.

    The last ``tools/list``, expired or not; the handshake's tool deferral reads it
    through this too (``runtime_spawn_launch._known_server_tool_names``). ``None``
    when the cache has no list.
    """
    # Deferred: discovery is heavy, and this module is imported by the projection.
    from kiro_crew.mcp_discovery import _get_cached

    try:
        _status, tools, *_rest = _get_cached(server)
    except Exception:  # noqa: BLE001 - an unreadable cache withholds all of MCP
        logger.debug("no tool list for MCP server %s", server, exc_info=True)
        return None
    return [t for t in tools if isinstance(t, str) and t] or None


def _names_mcp_tools(matcher: str) -> bool:
    """Whether *matcher* is written in one of the gate's MCP forms (``@...``, ``mcp__...``)."""
    return matcher.lower().startswith((_MCP_PREFIX, _MCP_TITLE_PREFIX))


def _hook_mcp_resources(
    matcher: str,
    servers: Iterable[str] | None,
    tool_names: McpToolNames | None,
) -> set[str] | None:
    """The exact ``<server>/<tool>`` resources a wildcard-free MCP-form *matcher*
    meets, or ``None``.

    No text is split or folded. First the auto-approved *servers* the matcher
    could name at all are found: those whose gate prefix it starts with
    (``@server/`` or ``mcp__server__``, compared as the hook store compares, in
    any case). Unless that is exactly one server, ``None``: ``mcp__a__b__c``
    names a tool on ``a`` and on ``a__b`` alike, and a tool list cannot prove
    which, since a server may expose a tool its cached list does not show yet.
    Then each real tool of that server is named the way the PreToolUse gate names
    an MCP call (``hook_dispatch.pre_tool_match_names``: ``@server/tool`` and
    ``mcp__server__tool``) and the hook store's own matcher decides. ``None`` --
    withhold all of MCP -- too when the servers or that server's tools are not
    known.
    """
    # Deferred: the hook store's module is heavy.
    from kiro_crew.hooks import _tool_matches

    if servers is None or tool_names is None:
        return None
    lowered = matcher.lower()
    candidates = [
        server
        for server in servers
        if lowered.startswith(
            (f"{_MCP_PREFIX}{server.lower()}/", f"{_MCP_TITLE_PREFIX}{server.lower()}__")
        )
    ]
    if len(candidates) != 1:
        return None
    server = candidates[0]
    resources: set[str] = set()
    for tool in tool_names(server) or ():
        names = (f"{_MCP_PREFIX}{server}/{tool}", f"{_MCP_TITLE_PREFIX}{server}__{tool}")
        if any(_tool_matches(matcher, name) for name in names):
            resources.add(f"{server}/{tool}")
    return resources


def _bare_mcp_resources(
    matcher: str,
    servers: Iterable[str] | None,
    tool_names: McpToolNames | None,
) -> set[str] | None:
    """The MCP resources a bare tool-name *matcher* (no MCP form, no glob, no ``/``)
    needs an ``ask`` on, or ``None``.

    The gate also names a KAS MCP call by its bare tool name
    (``hook_dispatch.pre_tool_match_names``), so ``get_creds`` meets that tool on
    every server. ``None`` -- the caller covers everything, as for any name it does
    not know -- unless the auto-approved *servers* are known and at least one of
    their cached tools meets the matcher, which proves it names an MCP tool. Then
    ``*/<name>`` on EVERY server, for the matcher as written and each cached
    spelling it meets, since a server the policy does not list can still be
    reached (a saved answer decides its call without a request); and the whole
    server for an auto-approved one with nothing cached.

    shortcut: a differently cased tool on an uncached, unlisted server is not
    held; covering it needs KAS to match MCP resources case-insensitively.
    """
    # Deferred: the hook store's module is heavy.
    from kiro_crew.hooks import _tool_matches

    if servers is None or tool_names is None or "/" in matcher:
        return None
    resources: set[str] = set()
    names: set[str] = set()
    for server in servers:
        tools = tool_names(server)
        if not tools:
            resources.add(f"{server}/*")
            continue
        names.update(tool for tool in tools if _tool_matches(matcher, tool))
    if not names:
        return None
    return resources | {f"*/{name}" for name in (matcher, *names)}


def auto_approved_mcp_servers(policy: Any) -> frozenset[str] | None:
    """The literal servers *policy*'s ``mcp`` allows are confined to.

    ``None`` when an allow reaches MCP without naming its servers (no ``match``, a
    server-part glob, or the ``all`` meta capability): the servers it grants
    cannot be listed. Empty when nothing allows MCP.
    """
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION, _bounded_mcp_servers

    rules = policy.get("rules") if isinstance(policy, dict) else None
    servers: set[str] = set()
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, dict) or rule.get("effect") != "allow":
            continue
        capability = rule.get("capability")
        if not isinstance(capability, str):
            continue
        if _MCP_CAPABILITY not in META_CAPABILITY_EXPANSION.get(capability, (capability,)):
            continue
        bounded = _bounded_mcp_servers(rule.get("match")) if capability == _MCP_CAPABILITY else None
        if bounded is None:
            return None
        servers.update(bounded)
    return frozenset(servers)


def approved_mcp_servers(approved: Iterable[str]) -> frozenset[str] | None:
    """The servers an :func:`auto_approved_capabilities` answer confines ``mcp`` to,
    or ``None`` when it names none."""
    servers = frozenset(
        e[len(APPROVED_MCP_SERVER_PREFIX) :]
        for e in approved
        if e.startswith(APPROVED_MCP_SERVER_PREFIX)
    )
    return servers or None

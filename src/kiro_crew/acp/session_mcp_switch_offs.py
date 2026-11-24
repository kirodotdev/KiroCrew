"""A project checkout's MCP switch-off keys, and their layering onto another spec.

Split out of :mod:`kiro_crew.acp.session_mcp`, which imports both back under the
same names: a checkout's spec never chooses a mirrored session's servers
(``session_mcp._project_mcp_trusted``), but its switch-offs still narrow the spec
the session runs, and KAS layers the same keys (``acp.harness.kas``).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.mcp_cleanup import mcp_entry_is_muted


def _project_restrictions(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The switch-off keys of a project spec's ``mcpServers``, every other key withheld.

    An allow list, not a deny list: ``disabledTools`` is kept, and ``disabled``
    only as ``True`` when :func:`mcp_entry_is_muted` reads the entry as muted.
    Everything else -- ``command``, ``args``, ``type``, ``env``, ``url``,
    ``headers``, ``cwd``, ``timeout``, ``autoApprove`` and any key added later --
    either launches a server or grants something, so none of it survives. An
    entry left with neither key is dropped, and one that keeps a key carries no
    ``command`` or ``url``, so
    :func:`~kiro_crew.acp.session_mcp.acp_server_element` never mounts it.
    """
    raw = spec.get("mcpServers")
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        kept: dict[str, Any] = {}
        tools = entry.get("disabledTools")
        if isinstance(tools, list):
            kept["disabledTools"] = list(tools)
        if "disabled" in entry and mcp_entry_is_muted(entry):
            kept["disabled"] = True
        if kept:
            out[str(name)] = kept
    return out


def _with_restrictions(
    base: dict[str, Any], restrictions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """*base* with *restrictions* layered on: tool lists joined, a mute wins.

    Only ever narrows *base*. ``restrictions`` carries ``disabled`` solely as
    ``True``, so a project entry cannot unmute a server *base* muted, and a name
    *base* does not declare gains a switch-off-only entry that nothing mounts.
    """
    if not restrictions:
        return base
    raw = base.get("mcpServers")
    servers: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    for name, kept in restrictions.items():
        own = servers.get(name)
        entry: dict[str, Any] = dict(own) if isinstance(own, dict) else {}
        tools = kept.get("disabledTools")
        if isinstance(tools, list):
            prior = entry.get("disabledTools")
            entry["disabledTools"] = [*(prior if isinstance(prior, list) else []), *tools]
        if kept.get("disabled") is True:
            entry["disabled"] = True
        servers[name] = entry
    return {**base, "mcpServers": servers}

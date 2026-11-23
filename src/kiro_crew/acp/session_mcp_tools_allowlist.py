"""The agent spec's ``tools`` allowlist, as a value every session element is held to.

Imported by :mod:`kiro_crew.acp.session_mcp` under its own names, so callers reach
them as ``session_mcp.ToolsAllowlist`` and ``session_mcp._tools_allowlist``.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from kiro_crew.agent_sdk.mcp_refs import parse_tools_refs


class ToolsAllowlist(NamedTuple):
    """The spec's ``tools`` allowlist, as a value rather than a filter pass.

    Exists so the SAME allowlist can gate every element that reaches a session
    -- the translated entries inside :func:`session_mcp_servers`, and the pooled
    broker stubs a mirror appends beside them. A stub carries the name of the
    entry it rewrites, so a stub for a server the spec's ``tools`` never
    references is an unreferenced server mounted anyway, which is the exact
    thing the filter on the translated half exists to prevent.

    ``applies`` is False when there is no spec at all: then there is no allowlist
    to apply and everything stands, the same reading :func:`session_mcp_servers`
    takes for the control plane. A spec whose ``tools`` is missing or not a list
    is an EMPTY allowlist, not "no filter" (see the filter's own comment).

    Why an allowlist at all: kiro-cli loads a server only when ``tools`` references
    it (``@server`` or ``@server/tool``), so an ``mcpServers`` entry with no
    reference is declared but never mounted. An ACP array has no such indirection
    -- everything in it is mounted -- so the reference is applied here instead.
    Without it, an entry the user deliberately left unreferenced (the shape every
    ``opt_in`` grant uses, and what a narrowed-by-hand spec looks like) would come
    alive the moment the session ran on a mirrored backend.

    The refs are read through :func:`~kiro_crew.agent_sdk.mcp_refs.parse_tools_refs`
    rather than scanned here, so this module and the unresolved-ref detector cannot
    disagree about what an entry names -- a guard that read ``@srv`` where this
    read nothing would report a ref as unresolved while the server mounted, and the
    reverse would mount a server the guard called absent. Only the bare ``*``
    grants everything: ``@*`` is a server LITERALLY named ``*`` there, matching
    this repo's other readers. ``@builtin`` is not special-cased -- a server
    actually called ``builtin`` is mountable, and the namespace exclusion belongs
    to the guard asking whether a ref resolves.
    """

    applies: bool
    grant_all: bool
    refs: frozenset[str]
    #: Servers switched off WHOLE (:func:`session_mcp_disabled_servers`). Never
    #: granted, whatever ``tools`` says, so a mirror's stub loop -- which asks this
    #: allowlist -- withholds them exactly as the translated half does.
    muted: frozenset[str] = frozenset()

    def grants(self, name: str) -> bool:
        if name in self.muted:
            return False
        return (not self.applies) or self.grant_all or name in self.refs


def _tools_allowlist(spec: dict[str, Any] | None) -> ToolsAllowlist:
    """The allowlist *spec* declares; see :class:`ToolsAllowlist` for the two edges."""
    if spec is None:
        return ToolsAllowlist(applies=False, grant_all=False, refs=frozenset())
    tools = spec.get("tools")
    grant_all, refs = parse_tools_refs(tools if isinstance(tools, list) else [])
    return ToolsAllowlist(applies=True, grant_all=grant_all, refs=frozenset(refs))

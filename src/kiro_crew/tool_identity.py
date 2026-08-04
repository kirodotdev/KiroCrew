"""Tool display-title normalization.

An ACP permission event carries a *display title*, not a tool id. The same tool
reaches a gate in four spellings:

    WorkspaceSearch                      bare
    Running: WorkspaceSearch             status-prefixed
    mcp__kirocrew-core__learn_list       ACP MCP form
    @kirocrew-core/learn_list            runtime MCP form

Allowlist gates therefore need two identities per title: the *bare* tool name
(to match a name-only allowlist) and the *server-qualified* name (to match an
entry that must only match on one server). This module is the single
implementation, shared by the heartbeat allowlist and the plan-mode gate.

Leaf module: standard library only, no KiroCrew imports, so security gates can
depend on it freely.
"""

from __future__ import annotations

#: Status prefixes kiro-cli's ACP layer prepends to tool titles.
DEFAULT_STATUS_PREFIXES: tuple[str, ...] = ("Running: ",)


def normalize_tool_identity(
    title: str,
    *,
    prefixes: tuple[str, ...] = DEFAULT_STATUS_PREFIXES,
) -> tuple[str, str]:
    """Split a tool display *title* into ``(bare_name, qualified_name)``.

    ``bare_name`` is the trimmed, prefix-stripped, server-stripped tool name —
    the form a name-only allowlist matches. ``qualified_name`` is the
    ``@server/Tool`` form when the title identified an MCP tool, else ``""``;
    a gate that must scope an entry to one server matches on this instead.

    Both are ``""`` for an empty or whitespace-only title, so a caller that
    fails closed on a falsy name rejects unusable titles for free.

    Only the FIRST matching status prefix is stripped: the prefixes are
    adapter-added decoration, and repeated stripping would let a tool literally
    named ``Running: x`` be renamed by this function.
    """
    if not title:
        return "", ""
    name = title.strip()
    if not name:
        return "", ""
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break

    # Compute the qualified form BEFORE stripping the server prefix, since
    # stripping is lossy — "@a/T" and "@b/T" both collapse to "T".
    qualified = ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            qualified = f"@{parts[1]}/{parts[2]}"
    elif name.startswith("@") and "/" in name:
        qualified = name

    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            name = parts[2]
    if name.startswith("@") and "/" in name:
        name = name.rsplit("/", 1)[-1]

    return name, qualified

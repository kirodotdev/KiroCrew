"""Whether an operator tool pattern can reach the calls of one MCP server.

A static grant of a whole server (``@server``, ``@server/q*``) runs every call it
covers with no permission request, so the gate never judges them against
``hooks.confirm_tools`` or ``hooks.auto_deny_tools``. Whoever writes such a grant
asks these whether a pattern could match a name the gate would judge one of those
calls under, for a tool name not known here; a deny pattern is held to the real
tools of Kiro Crew's own servers instead. Both err toward "it can": that costs a
prompt the gate then settles. ``platform.governance`` holds the callers
(``server_requires_confirmation``, ``_operator_denies``).
"""

from __future__ import annotations

import fnmatch
import functools

from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS

_WILDCARDS = "*?["


def may_match_spelling(text: str, exact: tuple[str, ...], heads: tuple[str, ...]) -> bool:
    """Whether lower-cased pattern *text* can match one of *exact* or a string beginning *heads*.

    Judged on the literal prefix, the text before the first wildcard, which begins
    every string the pattern matches. A pattern with no wildcard is compared exactly.
    """
    cut = min((i for i, ch in enumerate(text) if ch in _WILDCARDS), default=-1)
    if cut < 0:
        return text in exact or text.startswith(heads)
    prefix = text[:cut]
    return prefix.startswith(heads) or any(head.startswith(prefix) for head in heads)


def may_name_server_call(pattern: str, server: str) -> bool:
    """Whether an ``auto_deny_tools`` pattern can match a call on MCP server *server*.

    The gate judges the call under ``@server``, ``@server/<tool>``,
    ``Running: @server/<tool>``, ``mcp__server__<tool>`` and the bare tool name. A
    pattern beginning ``@`` or ``mcp__`` can match only the spellings of the server
    it names. Only ``Running: @server/<tool>`` holds whitespace, one space after
    ``running:``, so a pattern with whitespace (``*rm -rf*``) is judged against that
    spelling alone. Every other non-empty pattern, a bare name or wildcard
    (``read_private``, ``delete_*``), can match the bare tool name.

    A server in ``KIROCREW_BIN_MCP_SERVERS`` is Kiro Crew's own, and its tools are
    known, so it is asked of those spellings for each real tool instead
    (:func:`_managed_spellings`): ``*curl*`` names none of ``kirocrew-core``'s,
    ``delete_*`` names its ``delete_message``.
    """
    if server in KIROCREW_BIN_MCP_SERVERS:
        try:
            spellings = _managed_spellings(server)
        except Exception:  # noqa: BLE001 - a list that cannot be read may hold any tool
            return True
        wanted = pattern.lower()
        return any(fnmatch.fnmatch(name, wanted) for name in spellings)
    text, low = pattern.strip().lower(), server.lower()
    qualified = text.startswith(("@", "mcp__"))
    if qualified and may_match_spelling(text, (f"@{low}",), (f"@{low}/", f"mcp__{low}__")):
        return True
    gap = next((i for i, ch in enumerate(text) if ch.isspace()), -1)
    if gap < 0:
        return bool(text) and not qualified
    before, after = text[:gap], text[gap + 1 :]
    # A class before the whitespace may hold it, so the split proves nothing.
    if "[" in before:
        return True
    return fnmatch.fnmatch("running:", before) and may_match_spelling(
        after, (f"@{low}",), (f"@{low}/",)
    )


@functools.lru_cache(maxsize=None)
def _managed_spellings(server: str) -> tuple[str, ...]:
    """Every name the gate judges a call of managed *server* under, lower-cased.

    ``@server`` and, for each tool it can ever expose (switched-off ones included,
    ``managed_tool_names_for_policy``), ``@server/<tool>``,
    ``Running: @server/<tool>``, ``mcp__server__<tool>`` and the bare tool. Raises
    when the list is empty or cannot be read, so no answer is kept and the caller
    withholds. Imported here: the listing pulls in every managed server's module.
    """
    from kiro_crew.mcp_discovery_managed import managed_tool_names_for_policy

    tools = managed_tool_names_for_policy(server)
    if not tools:
        raise LookupError(f"no tool list for managed MCP server {server}")
    names = [f"@{server}"]
    for tool in tools:
        ref = f"@{server}/{tool}"
        names += [ref, f"Running: {ref}", f"mcp__{server}__{tool}", tool]
    return tuple(name.lower() for name in names)

"""The claude built-ins a crewmate worker keeps when its file excludes a native tool.

A worker whose crewmate file excludes a kiro-cli built-in (``shell``, ``fs_write``,
``@builtin/<tool>``, a tag such as ``write``) runs on claude with the session's
``tools`` option listing only the built-ins of the native tools it keeps, so a
built-in this table does not list is withheld, never let in: the claude a session
runs is the user's own Claude Code, whose built-ins change between releases.
:func:`excluded_builtin_rows` is also what decides, before spawn, whether such an
entry is carried at all (``mate_worker.uncarried_tool_limits_of``).

Importing it loads no ACP module; the one ACP-layer read (KAS's tool tags)
happens when :func:`excluded_builtin_rows` is called.
"""

from __future__ import annotations

from collections.abc import Iterable

from kiro_crew.platform.tool_names import canonical_kiro_cli_tool_name

__all__ = [
    "CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL",
    "CLAUDE_WORKER_KEPT_BUILTINS",
    "claude_worker_builtin_tools",
    "excluded_builtin_rows",
]

#: kiro-cli built-in -> every claude built-in that does its job. Kept apart from
#: ``acp.harness_tool_names.CLAUDE_TOOL_IDS_BY_KIRO_TOOL`` (hook naming) because this
#: one is pinned by the capture below, and its shell row lists every built-in that
#: runs or stops a command. Captured on Claude Code
#: 2.1.319: ``test/fixtures/acp_frames/claude/worker-native-tool-limits-live.jsonl``.
CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL: dict[str, tuple[str, ...]] = {
    "fs_read": ("Read",),
    "grep": ("Grep",),
    "glob": ("Glob",),
    "web_fetch": ("WebFetch",),
    "web_search": ("WebSearch",),
    "use_subagent": ("Task", "Agent"),
    "fs_write": ("Write", "Edit", "MultiEdit", "NotebookEdit"),
    "execute_bash": ("Bash", "Monitor", "PowerShell", "TaskStop"),
}

#: Built-ins such a worker keeps whatever it excludes: ``ToolSearch`` loads the
#: MCP tools claude defers.
CLAUDE_WORKER_KEPT_BUILTINS: tuple[str, ...] = ("ToolSearch",)


def excluded_builtin_rows(ref: str) -> tuple[str, ...]:
    """The :data:`CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL` rows an ``excludedTools`` *ref* takes out.

    A tag KAS reads (``read``, ``write``, ``shell``, ``web``, ``subagent``) takes
    out every tool it selects; any other entry is read under Crew's name for it
    (``@builtin/shell`` and ``executeCmd`` are ``execute_bash``). Empty for an
    entry no row covers (``use_aws``, ``@builtin``, a glob), which no ``tools``
    list can carry.
    """
    from kiro_crew.acp.kas_permissions import SPEC_HOOK_TOOL_TAGS

    ref = ref.strip()
    names = SPEC_HOOK_TOOL_TAGS.get(ref) or (canonical_kiro_cli_tool_name(ref),)
    return tuple(name for name in names if name in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL)


def claude_worker_builtin_tools(excluded: Iterable[str]) -> tuple[str, ...] | None:
    """The claude built-ins a worker whose spec excludes *excluded* keeps, or ``None``.

    ``None`` when no entry takes out a row, so the session keeps claude's whole
    preset. Otherwise every listed built-in of the rows left, then
    :data:`CLAUDE_WORKER_KEPT_BUILTINS`, in table order.
    """
    out = {row for ref in excluded for row in excluded_builtin_rows(ref)}
    if not out:
        return None
    kept = (
        tool
        for row, tools in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL.items()
        if row not in out
        for tool in tools
    )
    return tuple(dict.fromkeys((*kept, *CLAUDE_WORKER_KEPT_BUILTINS)))

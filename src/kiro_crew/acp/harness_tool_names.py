"""Name tables from kiro-cli's tool names to goose's and opencode's own.

A spec hook's tool matcher is written in kiro-cli's names (``execute_bash``,
``fs_write``). KAS states its own id for a call and
:func:`kiro_crew.acp.kas_permissions.kas_tool_match_names` maps it back. goose and
opencode state no such id on a permission request, but each names the tool on the
``tool_call`` frame that comes first:

* goose in ``_meta.goose.toolCall.toolName`` (``shell`` for its builtin shell);
* opencode in the frame's ``title``, which on that first frame is the tool's own
  name (``bash``); later updates reuse the title for the command.

The permission event carries that name as its ``harness_tool_id``, joined to the
backend by :data:`HARNESS_TOOL_ID_SEPARATOR` (``goose#shell``). A KAS id never
contains that character (``_dispatch._HARNESS_TOOL_ID_RE``), so a KAS id can never
be read through these tables, and a goose id never through KAS's.

Most ids below are tools the live harness offered: goose 1.50.1's tool list as its
request log sent it to the model, and opencode 1.18.30's as ``opencode debug agent
build`` resolved it. opencode also has writers and a lister it offers only to some
models or versions (``apply_patch``, whose name its binary carries, plus ``patch``,
``multiedit`` and ``list``); they get rows too, because a row for a name that is
never stated matches nothing, and a missing row lets that tool past an
``fs_write`` or ``fs_read`` deny. A tool with no row (opencode's ``todowrite``) is
met only by a matcher that names it as written.

An MCP tool needs one more step on opencode. goose states the server in its
``_meta`` block, so the ``@server/tool`` form is built from that. opencode states
only a fused ``<server>_<tool>`` title (``kirocrew-core_monitor_start``). So the
permission event's id names the split instead (``opencode#@kirocrew-core/
monitor_start``), taken against the servers the session is known to mount: the
ones Crew placed, and the ones opencode's own config mounts, which the spawn's
``opencode debug config`` read-back names. That keeps a name opencode rewrote
(``docs.server`` arrives as ``docs_server``) exact. The title is also split at
every ``_``, for a server neither source named. Every split is kept, so a deny
hook on any of them still runs. When the
fused title is also opencode's own tool (``apply_patch``), that tool's kiro-cli
name stays the first name, so a hook's stdin still reports ``fs_write``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from kiro_crew.acp.kas_permissions import KIRO_TOOL_ALIASES
from kiro_crew.acp_backends import ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE

#: Joins a backend to the tool name its frame stated. Outside the characters a KAS
#: ``toolId`` may carry, so the two id spaces cannot meet.
HARNESS_TOOL_ID_SEPARATOR = "#"

#: Bound on a tool name a goose or opencode ``tool_call`` frame states, and on a
#: server name Crew keeps to split one with. Wider than any name an exact spec
#: matcher can meet (a matcher is at most 200 characters, and ``mcp__server__tool``
#: is 6 longer than the fused ``server_tool``), and wider than the 64-128
#: characters model providers allow a tool name. It also bounds the splits
#: without a cap that would leave one out: a name this long has fewer than this
#: many ``_``, each split is ``@`` plus the name, and the server splits number at
#: most the known servers (Crew's array, plus at most
#: :data:`MAX_HARNESS_CONFIG_MCP_SERVERS` from opencode's config).
MAX_HARNESS_TOOL_NAME_LEN = 256

#: Bound on how many server names opencode's resolved config contributes. Only a
#: name opencode rewrites counts (:func:`opencode_rewrites_name`); past the bound
#: the session is refused, since a name left out would lose its exact matcher.
MAX_HARNESS_CONFIG_MCP_SERVERS = 256

#: The characters opencode keeps in each half of a fused MCP name; any other is
#: written as ``_`` (its ``replace(/[^a-zA-Z0-9_-]/g, "_")``).
_OPENCODE_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")

#: Joins the candidate ``@server/tool`` splits of one fused opencode MCP name.
#: A spec matcher cannot itself be written as ``@server/tool`` (the object form's
#: matcher rule takes no ``@`` or ``/``); it names an MCP tool as
#: ``mcp__server__tool``, which these splits also produce.
_MCP_SPLIT_SEPARATOR = "|"

#: kiro-cli tool name -> the goose builtin tools that do the same job.
GOOSE_TOOL_IDS_BY_KIRO_TOOL: dict[str, tuple[str, ...]] = {
    "execute_bash": ("shell",),
    "fs_read": ("tree", "read_image", "analyze"),
    "fs_write": ("write", "edit"),
    "use_subagent": ("delegate",),
}

#: kiro-cli tool name -> the opencode tools that do the same job.
OPENCODE_TOOL_IDS_BY_KIRO_TOOL: dict[str, tuple[str, ...]] = {
    "execute_bash": ("bash",),
    "fs_read": ("read", "list"),
    "fs_write": ("write", "edit", "apply_patch", "patch", "multiedit"),
    "grep": ("grep",),
    "glob": ("glob",),
    "web_fetch": ("webfetch",),
    "web_search": ("websearch",),
    "use_subagent": ("task",),
}

#: claude-agent-acp's read-only built-ins, keyed by the name the adapter stamps on
#: a ``tool_call`` frame (``_meta.claudeCode.toolName``), mapped to the kiro-cli
#: built-in that does the same job. It names that built-in on a permission event
#: (``AcpEvent.harness_builtin_tool``) for the deny floor and the read-only proof.
#: Not a :data:`HARNESS_TOOL_TABLES` row: that would also change which spec and
#: Hooks-page hooks match a claude call.
CLAUDE_READ_ONLY_BUILTINS: dict[str, str] = {
    "Read": "fs_read",
    "Grep": "grep",
    "Glob": "glob",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
}

#: The backends whose permission event carries a qualified harness tool id.
HARNESS_TOOL_TABLES: dict[str, dict[str, tuple[str, ...]]] = {
    ACP_BACKEND_GOOSE: GOOSE_TOOL_IDS_BY_KIRO_TOOL,
    ACP_BACKEND_OPENCODE: OPENCODE_TOOL_IDS_BY_KIRO_TOOL,
}

#: Every harness id in these tables, for the spec reader's "names no tool" warning.
HARNESS_TOOL_MATCH_VOCABULARY: frozenset[str] = frozenset(
    tool_id for table in HARNESS_TOOL_TABLES.values() for ids in table.values() for tool_id in ids
)


def opencode_rewrites_name(server: str) -> bool:
    """Whether opencode writes *server* differently in a fused tool title.

    A name of only ``[A-Za-z0-9_-]`` is written as it is, so the every-``_``
    split already yields its exact ``@server/tool``; only another name needs
    to be known to be split back exactly.
    """
    return bool(_OPENCODE_UNSAFE_NAME_CHARS.search(server))


def _opencode_mcp_splits(tool_name: str, mcp_servers: Iterable[str]) -> list[str]:
    """``@server/tool`` for every known server whose ``<server>_`` prefixes the name.

    Compared in opencode's own spelling of the server, since that is what the
    title carries (``docs.server`` arrives as ``docs_server_``); the split keeps
    the name Crew placed, so a matcher written with it still meets the call.
    """
    splits = []
    for server in sorted({s for s in mcp_servers if s}, key=len, reverse=True):
        prefix = f"{_OPENCODE_UNSAFE_NAME_CHARS.sub('_', server)}_"
        if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
            splits.append(f"@{server}/{tool_name[len(prefix):]}")
    # A server neither Crew placed nor the config read-back named still gets a
    # split, at every "_". Each split only adds names a deny hook may meet; none
    # is taken away.
    splits += [
        f"@{tool_name[:i]}/{tool_name[i + 1:]}"
        for i, c in enumerate(tool_name)
        if c == "_" and 0 < i < len(tool_name) - 1
    ]
    return list(dict.fromkeys(splits))


def qualified_harness_tool_id(backend: str, tool_name: str, mcp_servers: Iterable[str] = ()) -> str:
    """``backend#tool_name`` for a backend with a table, else ``""``.

    On opencode a name that is a placed server's fused MCP tool becomes its
    ``@server/tool`` split (see the module docstring).
    """
    if backend not in HARNESS_TOOL_TABLES or not tool_name:
        return ""
    if backend == ACP_BACKEND_OPENCODE:
        splits = _opencode_mcp_splits(tool_name, mcp_servers)
        if splits:
            tool_name = _MCP_SPLIT_SEPARATOR.join(splits)
    return f"{backend}{HARNESS_TOOL_ID_SEPARATOR}{tool_name}"


def split_harness_tool_id(tool_id: str) -> tuple[str, str] | None:
    """``(backend, tool_name)`` of a qualified id, or ``None`` for any other id."""
    backend, sep, name = tool_id.partition(HARNESS_TOOL_ID_SEPARATOR)
    if not sep or not name or backend not in HARNESS_TOOL_TABLES:
        return None
    return backend, name


def harness_tool_match_names(tool_id: str) -> tuple[str, ...] | None:
    """The names a tool matcher meets for a qualified id, or ``None`` if unqualified.

    Built as :func:`kiro_crew.acp.kas_permissions.kas_tool_match_names` builds KAS's:
    the kiro-cli name whose row reaches the tool first, then the harness's own name,
    then kiro-cli's aliases for that name. So ``execute_bash``, ``shell`` and
    ``bash`` all meet an opencode shell call, and the first name is the one a
    kiro-cli hook would be told the tool is called.
    """
    parts = split_harness_tool_id(tool_id)
    if parts is None:
        return None
    backend, name = parts
    if name.startswith("@"):
        # An MCP call: its @server/tool and mcp__server__tool forms, then the
        # fused name the harness stated. A fused name that is also the harness's
        # own tool (opencode's ``apply_patch``, split at its "_" as ``@apply/
        # patch``) keeps that tool's names too, so a hook on either reading still
        # runs -- and they go FIRST, because the first name is what a hook's stdin
        # reports as ``tool_name``: an ``fs_write`` deny that branches on it must
        # be told ``fs_write``, not a made-up MCP name.
        forms: list[str] = []
        fused: list[str] = []
        for split in name.split(_MCP_SPLIT_SEPARATOR):
            server, _, tool = split[1:].partition("/")
            forms += [split, f"mcp__{server}__{tool}"]
            fused.append(f"{_OPENCODE_UNSAFE_NAME_CHARS.sub('_', server)}_{tool}")
        table = HARNESS_TOOL_TABLES[backend]
        native_tools = [f for f in dict.fromkeys(fused) if any(f in ids for ids in table.values())]
        native = [n for f in native_tools for n in _native_match_names(backend, f)]
        return tuple(dict.fromkeys([*native, *forms, *fused]))
    return _native_match_names(backend, name)


def _native_match_names(backend: str, name: str) -> tuple[str, ...]:
    """The kiro-cli name whose row reaches *name*, then *name*, then the aliases."""
    table = HARNESS_TOOL_TABLES[backend]
    kiro_names = sorted(kiro for kiro, ids in table.items() if name in ids)
    aliases = sorted(alias for alias, kiro in KIRO_TOOL_ALIASES.items() if kiro in kiro_names)
    return tuple(dict.fromkeys([*kiro_names, name, *aliases]))

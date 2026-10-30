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

Most ids below are tools the live harness offered, measured on goose 1.50.1 (its
tool list as its request log sent it to the model) and on opencode 1.18.30 (as
``opencode debug agent build`` resolved it; the lane's pin is in
``test/real_adapters/package.json``). opencode also has writers and a lister it
offers only to some models or versions (``apply_patch``, whose name its binary
carries, plus ``patch``, ``multiedit`` and ``list``); they get rows too, because a
row for a name that is never stated matches nothing, and a missing row lets that
tool past an ``fs_write`` or ``fs_read`` deny. A tool with no row (opencode's
``todowrite``) is met only by a matcher that names it as written.

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

The module also holds the per-harness ARGUMENT spellings
(:data:`HARNESS_PARAM_ALIASES`), the same kind of table one level down: a harness
tool's own name for an argument Crew's gates read under another.
"""

from __future__ import annotations

import fnmatch
import functools
import re
import string
from collections.abc import Iterable

from kiro_crew.acp.kas_permissions import KIRO_TOOL_ALIASES
from kiro_crew.acp_backends import ACP_BACKEND_GOOSE, ACP_BACKEND_MIMOCODE, ACP_BACKEND_OPENCODE
from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS

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

#: kiro-cli tool name -> the MiMo Code tools that do the same job, by the ids its
#: 0.1.15 tool registry defines (``tool/*.ts``). A model the harness
#: addresses in PascalCase (``Bash``) still reports the internal id on the wire,
#: because the harness maps the name back before the frame is built. The one table
#: here whose backend is outside ``ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS``: it names a
#: call for the gate's own hooks on the calls that ask, while the spec's hooks are
#: withheld because some of the harness's tools never ask. The fork's subagent tool
#: is ``actor`` (``tool/actor.ts``, asking under its own ``actor`` permission); its
#: ``task`` is a work-item tracker that never asks, not opencode's subagent ``task``.
MIMOCODE_TOOL_IDS_BY_KIRO_TOOL: dict[str, tuple[str, ...]] = {
    "execute_bash": ("bash",),
    "fs_read": ("read", "view_image"),
    "fs_write": ("write", "edit", "apply_patch", "multiedit", "notebook_edit"),
    "grep": ("grep",),
    "glob": ("glob",),
    "web_fetch": ("webfetch",),
    "web_search": ("websearch",),
    "use_subagent": ("actor",),
}

#: The characters MiMo Code keeps in each half of a fused MCP name; any other is
#: written as ``_`` (0.1.15 ``mcp/index.ts:361``, ``replace(/[^a-zA-Z0-9_-]/g,
#: "_")``). Its MCP tools arrive under that fused ``<server>_<tool>`` title,
#: captured as ``crew-probe_crew_probe_echo``.
_MIMOCODE_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _mimocode_spelled(text: str, unsafe: re.Pattern[str]) -> str:
    """*text* with every *unsafe* character written as MiMo Code writes it.

    The harness's ``replace`` walks UTF-16 code units, so a character outside the
    Basic Multilingual Plane, two units there, becomes two ``_``.
    """
    return unsafe.sub(lambda m: "__" if ord(m.group()) > 0xFFFF else "_", text)


def mimocode_spelling(name: str) -> str:
    """One half of a fused MCP name as MiMo Code writes it (``repo.delete`` -> ``repo_delete``)."""
    return _mimocode_spelled(name, _MIMOCODE_UNSAFE_NAME_CHARS)


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

#: goose's read-only built-ins a Side Chat session may run, keyed by the pair goose
#: stamps on a ``tool_call`` frame: ``(_meta.goose.toolCall.extensionName, toolName)``
#: as written, before any prefix is stripped. Mapped to the kiro-cli built-in that
#: does the same job, named on the permission event as ``harness_builtin_tool``.
#: The pair is the ownership proof. goose names an MCP or config extension's tool
#: ``<extension>__<tool>``, so a server called ``developer`` that serves ``tree``
#: arrives as ``("developer", "developer__tree")`` and matches nothing here (measured
#: on goose 1.50.1). Only the builtin ``developer`` extension Crew loads with
#: ``--with-builtin`` states a bare name. ``analyze`` is not listed: goose serves it
#: from an extension named ``analyze``, which the identity-drift refusal turns down
#: on every goose session. No tool that returns a file's text is listed, because
#: goose has none apart from its shell.
GOOSE_READ_ONLY_BUILTINS: dict[tuple[str, str], str] = {
    ("developer", "tree"): "fs_read",
    ("developer", "read_image"): "fs_read",
}

#: The backends whose permission event carries a qualified harness tool id.
HARNESS_TOOL_TABLES: dict[str, dict[str, tuple[str, ...]]] = {
    ACP_BACKEND_GOOSE: GOOSE_TOOL_IDS_BY_KIRO_TOOL,
    ACP_BACKEND_OPENCODE: OPENCODE_TOOL_IDS_BY_KIRO_TOOL,
    ACP_BACKEND_MIMOCODE: MIMOCODE_TOOL_IDS_BY_KIRO_TOOL,
}

#: A harness's native tools that can complete with no permission request under its
#: seeded asking posture, by the id its ``tool_call`` frame names. The unasked-call
#: tripwire (``AcpClient._tripwire_pi_gate``) passes these and stops the harness on
#: any other call that completes unasked. MiMo Code 0.1.15 (``tool/registry.ts``):
#: ``memory``, ``history`` and ``task`` never call ``ctx.ask``; ``plan_exit`` and
#: ``question`` ask the user through the question service, not the permission
#: service; ``mcp_tool_search`` only searches the deferred MCP catalog; ``invalid``
#: answers a call to a tool that does not exist. ``actor`` asks only to start a
#: subagent, and its ``status``, ``wait``, ``send``, ``cancel`` and ``models`` actions
#: return before that ask; a subagent it starts runs its own tool calls through the
#: same session, where each one is judged. ``skill_search`` asks only to load the
#: skill it matched, so a search with no match, or one under its auto-load score,
#: completes unasked. Its keys are ``ACP_BACKENDS_UNASKED_NATIVE_TOOLS``.
HARNESS_UNASKED_TOOL_IDS: dict[str, frozenset[str]] = {
    ACP_BACKEND_MIMOCODE: frozenset(
        {
            "memory",
            "history",
            "task",
            "plan_exit",
            "question",
            "mcp_tool_search",
            "invalid",
            "actor",
            "skill_search",
        }
    ),
}

#: Every harness id in these tables, for the spec reader's "names no tool" warning.
#: Only the tables of backends Crew fires a spec's hooks for: a matcher that meets
#: only a MiMo Code id names a tool no fired spec hook ever sees, so it still warns.
HARNESS_TOOL_MATCH_VOCABULARY: frozenset[str] = frozenset(
    tool_id
    for backend, table in HARNESS_TOOL_TABLES.items()
    if backend in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    for ids in table.values()
    for tool_id in ids
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


def mimocode_rewrites_name(server: str) -> bool:
    """Whether MiMo Code writes *server* differently in a fused tool title.

    A name of only ``[A-Za-z0-9_-]`` is written as it is, so the every-``_``
    split already yields its exact ``@server/tool``; only another name needs
    to be known to be split back exactly.
    """
    return bool(_MIMOCODE_UNSAFE_NAME_CHARS.search(server))


def _mimocode_mcp_splits(tool_name: str, mcp_servers: Iterable[str]) -> list[str]:
    """``@server/tool`` for every known server whose ``<server>_`` prefixes the name.

    Compared in MiMo Code's own spelling of the server, since that is what the
    title carries (``docs.server`` arrives as ``docs_server_``); the split keeps
    the name Crew placed, so a matcher written with it still meets the call.
    """
    splits = []
    for server in sorted({s for s in mcp_servers if s}, key=len, reverse=True):
        prefix = f"{mimocode_spelling(server)}_"
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


#: MiMo Code's own tool ids, by the two tables above. Every 0.1.15 id that holds a
#: ``_`` (``apply_patch``, ``notebook_edit``, ``view_image``, ``plan_exit``,
#: ``mcp_tool_search``, ``skill_search``) is in one of them, and only an id with a
#: ``_`` can also read as a fused ``<server>_<tool>`` title.
_MIMOCODE_NATIVE_TOOL_IDS: frozenset[str] = (
    frozenset(tool_id for ids in MIMOCODE_TOOL_IDS_BY_KIRO_TOOL.values() for tool_id in ids)
    | HARNESS_UNASKED_TOOL_IDS[ACP_BACKEND_MIMOCODE]
)

#: What :func:`mimocode_mcp_identity` answers for a title it cannot attribute to one
#: server: more than one known server prefixes it, or it is a native tool id as well.
MIMOCODE_AMBIGUOUS_MCP_TITLE: tuple[str, str] = ("", "")


def mimocode_mcp_identity(title: str, mcp_servers: Iterable[str]) -> tuple[str, str] | None:
    """The ``(server, tool)`` a MiMo Code fused MCP title names, for governance.

    MiMo Code states an MCP call only as ``<server>_<tool>``, each half in its own
    spelling (:data:`_MIMOCODE_UNSAFE_NAME_CHARS`), and carries no ``_meta``
    identity. *mcp_servers* are the names this session is known to mount: the ones
    Crew placed and the ones the config read-back resolved. ``None`` when none of
    them prefixes *title*, which is then not an MCP call; the one server, under the
    name Crew knows it by, and the tool half as written, when exactly one does;
    :data:`MIMOCODE_AMBIGUOUS_MCP_TITLE` when two do (``a`` serving ``b_c`` and
    ``a_b`` serving ``c``, or two names with one spelling) or the title is also one
    of the harness's own tool ids, since judging it as either could permit what the
    other's rule denies.
    """
    if not title:
        return None
    prefixes = {server: f"{mimocode_spelling(server)}_" for server in mcp_servers if server}
    matches = sorted(
        server
        for server, prefix in prefixes.items()
        if title.startswith(prefix) and len(title) > len(prefix)
    )
    if not matches:
        return None
    if len(matches) > 1 or title in _MIMOCODE_NATIVE_TOOL_IDS:
        return MIMOCODE_AMBIGUOUS_MCP_TITLE
    return matches[0], title[len(prefixes[matches[0]]) :]


#: The characters a lowered MCP tool name keeps when MiMo Code writes it; any other
#: one (``_`` included) may stand behind a ``_`` of the written name.
_MIMOCODE_KEPT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")

#: Characters a regex rule is tried with at a ``_`` of the written name: ``_``, every
#: ASCII punctuation mark and space, and a letter, ideograph and astral character
#: from outside ASCII. A regex is not analysed, only tried.
_MIMOCODE_REGEX_PROBES = tuple(dict.fromkeys("_" + string.punctuation + " é中\U0001f600"))


def _mimocode_glob_tokens(pattern: str) -> list[tuple]:
    """*pattern* as ``fnmatch`` reads it: ``*``, ``?``, a ``[...]`` class (``!``
    negates, a leading ``]`` is a member, ``a-z`` a range), else one literal."""
    tokens: list[tuple] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        i += 1
        if c == "*":
            if tokens[-1:] != [("*",)]:
                tokens.append(("*",))
        elif c == "?":
            tokens.append(("?",))
        elif c == "[":
            j = i + (i < n and pattern[i] == "!")
            j += j < n and pattern[j] == "]"
            j = pattern.find("]", j)
            if j < 0:
                tokens.append(("lit", "["))
                continue
            body, i = pattern[i:j], j + 1
            negated = body.startswith("!")
            body = body[negated:]
            spans, k = [], 0
            while k < len(body):
                if k + 2 < len(body) and body[k + 1] == "-":
                    spans.append((ord(body[k]), ord(body[k + 2])))
                    k += 3
                else:
                    spans.append((ord(body[k]), ord(body[k])))
                    k += 1
            tokens.append(("cls", negated, tuple(spans)))
        else:
            tokens.append(("lit", c))
    return tokens


def _mimocode_token_takes(token: tuple, char: str | None, astral: bool = False) -> bool:
    """Whether *token* matches *char*, or with ``None`` any character the harness
    writes as ``_`` (as two, for an *astral* one)."""
    if token[0] == "?":
        return True
    if token[0] == "lit":
        if char is not None:
            return token[1] == char
        return token[1] not in _MIMOCODE_KEPT_CHARS and (ord(token[1]) > 0xFFFF) == astral
    negated, spans = token[1], token[2]
    if char is not None:
        return any(lo <= ord(char) <= hi for lo, hi in spans) != negated
    if negated:
        return True
    for lo, hi in spans:
        lo, hi = (max(lo, 0x10000), hi) if astral else (lo, min(hi, 0xFFFF))
        kept = sum(lo <= ord(c) <= hi for c in _MIMOCODE_KEPT_CHARS)
        if hi >= lo and hi - lo + 1 > kept:
            return True
    return False


def _mimocode_glob_family(pattern: str, written: str, wild_from: int) -> tuple[bool, bool]:
    """``(some, every)`` for a glob over the names the harness writes as *written*.

    Every ``_`` at or after *wild_from* stands for any character the harness writes
    that way, two for one astral character. *some*: a name in that family may match;
    *every*: each one surely does (each such ``_`` falls inside a ``*``). Both are
    read on the safe side: *some* can be true of no real name, *every* is only
    claimed when it holds.
    """
    tokens = _mimocode_glob_tokens(pattern.lower())
    text = written.lower()
    wild = [c == "_" and i >= wild_from for i, c in enumerate(text)]
    n, m = len(tokens), len(text)

    @functools.lru_cache(maxsize=None)
    def walk(t: int, p: int, every: bool) -> bool:
        if t == n:
            return p == m
        token = tokens[t]
        if token == ("*",):
            return any(walk(t + 1, q, every) for q in range(p, m + 1))
        if p == m:
            return False
        if not wild[p]:
            return _mimocode_token_takes(token, text[p]) and walk(t + 1, p + 1, every)
        if every:
            return False
        if _mimocode_token_takes(token, None) and walk(t + 1, p + 1, every):
            return True
        pair = p + 1 < m and wild[p + 1]
        return (
            pair and _mimocode_token_takes(token, None, astral=True) and walk(t + 1, p + 2, every)
        )

    if pattern == "*":
        return True, True
    return walk(0, 0, False), walk(0, 0, True)


def mimocode_call_targets(server: str, tool: str) -> tuple[str, ...]:
    """The spellings of an MCP call's identity the gate's rules meet that hold its
    tool half: ``@server/tool``, ``Running: @server/tool`` and ``mcp__server__tool``."""
    ref = f"@{server}/{tool}"
    return ref, f"Running: {ref}", f"mcp__{server}__{tool}"


def mimocode_unsafe_rule(
    server: str,
    tool: str,
    narrowing: Iterable[str],
    widening: Iterable[str],
    regexes: Iterable[str] = (),
) -> str:
    """The first rule the gate cannot be left to judge on *tool* as the harness wrote it.

    For a call whose real tool name could not be read: MiMo Code writes every
    character of a tool name outside ``[A-Za-z0-9_-]`` as ``_`` (two for one outside
    the BMP), so ``ops_repo_delete`` may be ``repo.delete`` or ``repo_delete``. Each
    glob is read in ``fnmatch``'s whole grammar against every identity spelling the
    gate matches (:func:`mimocode_call_targets`): a *narrowing* one (deny, confirm,
    floor) that may name a real name while missing *tool*, and a *widening* one
    (allow-list entry, auto-approve) that meets *tool* while another real name need
    not, are unsafe. A narrowing *regex* is tried at each ``_`` with
    :data:`_MIMOCODE_REGEX_PROBES` and is unsafe when a try matches and *tool* does
    not. ``""`` when none is, or *tool* holds no ``_``.
    """
    if "_" not in tool:
        return ""
    targets = mimocode_call_targets(server, tool)
    cut = [len(t) - len(tool) for t in targets]
    for rule, widens in [*((r, False) for r in narrowing), *((r, True) for r in widening)]:
        rule = str(rule)
        meets = any(fnmatch.fnmatchcase(t.lower(), rule.lower()) for t in targets)
        reads = [_mimocode_glob_family(rule, t, c) for t, c in zip(targets, cut)]
        if widens and meets and not any(every for _some, every in reads):
            return rule
        if not widens and not meets and any(some for some, _every in reads):
            return rule
    spots = [i for i, c in enumerate(tool) if c == "_"]
    tries = {tool.replace("_", c) for c in _MIMOCODE_REGEX_PROBES}
    tries |= {tool[:i] + c + tool[i + 1 :] for i in spots for c in _MIMOCODE_REGEX_PROBES}
    for rule in regexes:
        try:
            compiled = re.compile(str(rule), re.IGNORECASE)
        except re.error:
            continue
        if any(compiled.search(t) for t in targets):
            continue
        if any(compiled.search(t) for name in tries for t in mimocode_call_targets(server, name)):
            return str(rule)
    return ""


def mimocode_real_tool_names(tool: str, listed: Iterable[object]) -> tuple[str, ...]:
    """The names in a server's ``tools/list`` MiMo Code writes as *tool*.

    One is the call's real name. Two or more mean the harness registered one of them
    under *tool* and dropped the other (``mcp/index.ts:1175`` writes each into one
    table by its spelling), and nothing on the wire says which.
    """
    return tuple(
        sorted({n for n in listed if isinstance(n, str) and n and mimocode_spelling(n) == tool})
    )


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
    elif backend == ACP_BACKEND_MIMOCODE:
        splits = _mimocode_mcp_splits(tool_name, mcp_servers)
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


# Per-harness argument spellings, read onto the names Crew's gates judge.
#
# The path and patch checks in ``kiro_crew.platform.tool_paths`` read one set of
# argument names on every backend (``file_path``, ``patchText``, ...). A harness
# whose own tools name the same argument differently is listed here, by backend,
# and only that backend's permission events gain the canonical spelling: no other
# backend's arguments are read any differently, and the shared readers stay
# unaware of which harness sent a call.
#
# MiMo Code 0.1.15 names ``notebook_edit``'s target ``notebook_path``
# (``tool/notebook-edit.ts``) and ``apply_patch``'s envelope ``patch_text``
# (``tool/apply_patch.ts``; the envelope and its parser are OpenCode's). Without
# the alias a ``notebook_edit`` names no target the write-protected tier can read,
# so every call is refused (captured live on xiaomi ``mimo-v2.6-flash``).

#: backend -> {the harness's argument name: the name the gates read}.
HARNESS_PARAM_ALIASES: dict[str, dict[str, str]] = {
    ACP_BACKEND_MIMOCODE: {"notebook_path": "file_path", "patch_text": "patchText"},
}


def canonical_tool_params(backend: str, params: dict | None) -> dict | None:
    """*params* with each of *backend*'s aliases also present under its canonical name.

    The harness's own key is kept, so the approval and the audit still show what
    the harness received, and *params* itself is never modified (it is the
    caller's trusted cache entry). Nothing is chosen between two spellings, the
    rule ``tool_paths.target_paths`` states for path aliases: a canonical key that
    already holds a different value is given BOTH, as a list. The path walk reads
    every string in that list; the patch gate reads a non-string envelope as
    unverifiable and refuses the call. A backend with no aliases, and params that
    are not a dict, come back as they are.
    """
    aliases = HARNESS_PARAM_ALIASES.get(backend)
    if not aliases or not isinstance(params, dict):
        return params
    out = dict(params)
    for harness_key, canonical in aliases.items():
        if harness_key not in params:
            continue
        value = params[harness_key]
        if canonical not in out:
            out[canonical] = value
        elif out[canonical] != value:
            out[canonical] = [out[canonical], value]
    return out

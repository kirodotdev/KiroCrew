"""The batch write guard: refuse a write-class tool call issued in the same
parallel tool batch as a read-class call.

A model that sends a read and a write in ONE batch has not seen the read's
answer when it writes the write's arguments: the calls run in parallel, so a sha,
an id or a status the write quotes is a guess. This guard refuses the write and
tells the model to send it again once the read has returned. It judges the SET
of calls, not one command, which is why it is not a ``GATE_TIERS`` row or a
denied-command rule: the gate judges one call with no session state.

Three parts:

* :func:`classify` -- read, write, or neither, from explicit tables. A tool the
  tables do not name is neither, so the guard never refuses more than it names
  and an unknown peer never causes a refusal.
* :class:`BatchWindow` -- which calls form one batch, fed by the transport from
  the ``tool_call`` frames and their terminal ``tool_call_update``. A call that
  arrives while no call is open starts a new batch; one that arrives while any
  call is open joins it; a member stays a member after it settles, until the
  next batch starts. A write the window never saw is not judged, and neither is
  a write whose read peers are announced only after its own approval.
* :func:`screen_approval` -- called by both transports' ``approve_tool`` after
  the security floor. When it refuses, it has already steered the reason into
  the running turn (the request is still unanswered, so the steer is queued and
  not dropped); the transport then rejects through its floor-refusal branch.

Opt-in: ``hooks.batch_write_guard`` in config.json, default off (see
:func:`guard_settings`). ``hooks.batch_write_guard_commands`` (empty by default)
lists extra write-command prefixes for tools only a local install knows; the
call being approved AND its shell peers are classified with them. It is a
discipline guard, not a security control: the security floor runs first and is
unchanged.
"""

from __future__ import annotations

import logging
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kiro_crew.acp._dispatch_parts.tool_call_provenance import (
    MAX_ACTIVE_TOOL_CALLS,
    liveness_key,
)
from kiro_crew.constants import DENY_CAUSE_BATCH_READ_WRITE
from kiro_crew.platform.tool_names import KIRO_CLI_NAME_BY_KAS_TOOL, KIRO_TOOL_ALIASES

logger = logging.getLogger(__name__)

__all__ = [
    "DENY_CAUSE_BATCH_READ_WRITE",
    "MAX_CLASSIFIED_COMMAND_CHARS",
    "READ",
    "WRITE",
    "BatchWindow",
    "classify",
    "classify_command",
    "guard_settings",
    "screen_approval",
]

READ = "read"
WRITE = "write"

#: Characters of a shell command :func:`classify_command` reads, and the most a
#: window member keeps of one.
MAX_CLASSIFIED_COMMAND_CHARS = 8192

#: Seconds a batch member with no terminal update keeps its batch open
#: (see :class:`BatchWindow`). Longer than an ordinary tool call, so a slow read
#: still counts; short enough that a lost terminal does not hold the turn's
#: later writes in a stale batch.
OPEN_MEMBER_TTL_SECS = 600.0

#: SEL row for a batch-guard refusal: its own source, outcome and mechanism, so it
#: is never read as a security-floor refusal (``permission_floor.audit_refusal``).
AUDIT_SOURCE = "batch_guard"
OUTCOME_REJECTED_BATCH_GUARD = "rejected_batch_guard"
AUDIT_MECHANISM = "batch_write_guard"

# ── Tables ──

#: Built-ins by the kiro-cli name (KAS ids and kiro-cli's short stamps are mapped
#: onto these first, through ``platform.tool_names``).
_WRITE_BUILTINS: frozenset[str] = frozenset({"fs_write", "fs_append", "str_replace", "delete_file"})
_READ_BUILTINS: frozenset[str] = frozenset(
    {
        "fs_read",
        "list_directory",
        "read_file",
        "grep",
        "grep_search",
        "glob",
        "file_search",
        "web_fetch",
        "web_search",
        "remote_web_search",
    }
)
_SHELL_BUILTINS: frozenset[str] = frozenset({"execute_bash", "execute_pwsh", "run_command"})

#: Kiro Crew's own MCP tools, keyed by ``(server, tool)``.
_WRITE_MCP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("kirocrew-work", "work_report"),
        ("kirocrew-work", "work_ledger_record"),
        ("kirocrew-work", "work_ledger_rebuild"),
        ("kirocrew-dashboard", "session_send"),
        ("kirocrew-dashboard", "session_broadcast"),
        ("kirocrew-core", "send_message"),
        ("kirocrew-core", "session_ledger_record"),
    }
)
_READ_MCP_TOOLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("kirocrew-work", "work_brief"),
        ("kirocrew-work", "work_ledger_read"),
        ("kirocrew-dashboard", "session_status"),
        ("kirocrew-dashboard", "session_read_message"),
        ("kirocrew-dashboard", "chat_folder_tree"),
        ("kirocrew-core", "memory_recall"),
        ("kirocrew-core", "search_chat_history"),
        ("kirocrew-core", "get_chat_session"),
        ("kirocrew-core", "skill_search"),
        ("kirocrew-core", "list_sessions"),
        ("kirocrew-core", "session_ledger_read"),
        ("kirocrew-core", "spawn_status"),
        ("kirocrew-core", "spawn_list"),
        ("kirocrew-core", "resource_status"),
        ("kirocrew-core", "artifact_get"),
        ("kirocrew-core", "artifact_list"),
        ("kirocrew-core", "local_knowledge_search"),
        ("kirocrew-core", "workflow_status"),
        ("kirocrew-core", "workflow_result"),
        ("kirocrew-core", "monitor_inspect"),
    }
)

#: ACP ``kind`` fallback, read only when the frame carried no tool identity.
_READ_KINDS: frozenset[str] = frozenset({"read", "fetch", "search"})
_WRITE_KINDS: frozenset[str] = frozenset({"edit", "delete", "move"})

#: ``gh`` subcommands that write, by group. Matched on the program's own words,
#: never on argument text, so ``git log --grep='gh pr merge'`` stays a read.
_GH_WRITE_VERBS: dict[str, frozenset[str]] = {
    "pr": frozenset({"merge", "create", "review", "comment", "close", "edit", "ready", "reopen"}),
    "issue": frozenset({"create", "comment", "close", "edit", "reopen"}),
    "release": frozenset({"create", "edit", "delete", "upload"}),
}
#: ``gh`` options that take a value, skipped when finding the subcommand.
_GH_VALUED_OPTIONS: frozenset[str] = frozenset(
    {
        "-R",
        "--repo",
        # gh api options that take a value, so the endpoint is found after them.
        "-H",
        "--header",
        "--hostname",
        "-q",
        "--jq",
        "-t",
        "--template",
        "-p",
        "--preview",
        "--cache",
        "-X",
        "--method",
        "-f",
        "-F",
        "--field",
        "--raw-field",
        "--input",
    }
)
_GH_API_METHOD_OPTIONS: frozenset[str] = frozenset({"-X", "--method"})
#: ``gh api`` options that add a request parameter (gh then defaults to POST).
_GH_API_FIELD_OPTIONS: frozenset[str] = frozenset({"-f", "-F", "--field", "--raw-field"})
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
#: ``git`` subcommands that write, and the git options that take a value.
_GIT_WRITE_SUBCOMMANDS: frozenset[str] = frozenset({"push", "commit"})
_GIT_VALUED_OPTIONS: frozenset[str] = frozenset({"-C", "-c", "--git-dir", "--work-tree"})

#: ``aws <service> <op>`` operations that only read.
_AWS_READ_OP_PREFIXES: tuple[str, ...] = (
    "describe",
    "get",
    "list",
    "head",
    "query",
    "scan",
    "search",
    "lookup",
    "filter",
    "batch-get",
    "wait",
    "ls",
    "help",
    "validate",
    "tail",
    "presign",
)
#: aws global options that take a value, skipped when finding service and op.
_AWS_VALUED_OPTIONS: frozenset[str] = frozenset(
    {
        "--profile",
        "--region",
        "--output",
        "--endpoint-url",
        "--query",
        "--color",
        "--ca-bundle",
        "--cli-read-timeout",
        "--cli-connect-timeout",
    }
)
#: Characters that separate commands; a token made only of them is an operator.
_SEPARATOR_CHARS = ";&|\n"
#: Wrappers that run another program, by basename, each with the options that
#: take a separate value.
_WRAPPER_VALUED_OPTIONS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "env": frozenset({"-C", "--chdir", "-u", "--unset", "-S", "--split-string"}),
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "nohup": frozenset(),
}


def _segment_words(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _segment_texts(command: str) -> list[str]:
    """The command's segment texts, quoting intact.

    One pass that tracks the shell's quoting: a separator (``;``, ``&``, ``|``,
    newline, so also ``&&`` and ``||``) splits only outside single quotes, double
    quotes and a backslash escape. A ``#`` that starts a word outside quotes opens
    a comment that runs to the newline; the newline still separates. A ``#``
    inside a word (a URL fragment) is ordinary text. An unclosed quote runs to
    the end of the command, as the shell would read it. A here-document body
    (``<<WORD``, ``<<'WORD'``, ``<<"WORD"``, ``<<-WORD``) is data for its command,
    so the lines up to the terminator are dropped, not split into commands; a
    body with no terminator runs to the end.
    """
    segments: list[str] = []
    current: list[str] = []
    #: Here-documents opened on the current line: ``(terminator, strip_tabs)``.
    pending: list[tuple[str, bool]] = []
    quote = ""
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote == "'":
            current.append(c)
            if c == "'":
                quote = ""
        elif c == "\\" and command.startswith("\n", i + 1):
            # A line continuation (outside single quotes): the shell drops both
            # characters and joins the lines into one word stream.
            i += 2
            continue
        elif quote == '"':
            current.append(c)
            if c == "\\" and i + 1 < n:
                current.append(command[i + 1])
                i += 1
            elif c == '"':
                quote = ""
        elif c == "\\" and i + 1 < n:
            current.append(c)
            current.append(command[i + 1])
            i += 1
        elif command.startswith("<<<", i):
            # A here-string: one operator, never a here-document.
            current.append("<<<")
            i += 3
            continue
        elif c in "'\"":
            quote = c
            current.append(c)
        elif c == "#" and (not current or current[-1] in " \t\r"):
            newline = command.find("\n", i)
            i = n if newline < 0 else newline
            continue
        elif command.startswith("<<", i):
            # A here-document: remember its terminator; its body is skipped at
            # the end of this line (``_skip_heredocs``).
            heredoc, i = _heredoc_word(command, i + 2)
            if heredoc is not None:
                pending.append(heredoc)
            current.append("<<")
            continue
        elif c in _SEPARATOR_CHARS:
            segments.append("".join(current))
            current = []
            if c == "\n" and pending:
                i = _skip_heredocs(command, i + 1, pending)
                pending = []
                continue
        else:
            current.append(c)
        i += 1
    segments.append("".join(current))
    return segments


def _heredoc_word(command: str, i: int) -> tuple[tuple[str, bool] | None, int]:
    """Read a here-document's terminator starting just after ``<<``.

    Returns ``((word, strip_tabs), index after the word)``, or ``(None, i)`` when
    no word follows. Quotes around the word are removed, as the shell does."""
    n = len(command)
    strip_tabs = i < n and command[i] == "-"
    if strip_tabs:
        i += 1
    while i < n and command[i] in " \t":
        i += 1
    word: list[str] = []
    quote = ""
    while i < n:
        c = command[i]
        if quote:
            if c == quote:
                quote = ""
            else:
                word.append(c)
        elif c in "'\"":
            quote = c
        elif c in " \t\r\n;&|<>()":
            break
        else:
            word.append(c)
        i += 1
    if not word:
        return None, i
    return ("".join(word), strip_tabs), i


def _skip_heredocs(command: str, i: int, pending: list[tuple[str, bool]]) -> int:
    """The index after the bodies of *pending* here-documents starting at *i*."""
    n = len(command)
    for word, strip_tabs in pending:
        while i < n:
            end = command.find("\n", i)
            line = command[i:] if end < 0 else command[i:end]
            i = n if end < 0 else end + 1
            if (line.lstrip("\t") if strip_tabs else line).rstrip("\r") == word:
                break
    return i


def _segments(command: str) -> list[list[str]]:
    """The command's segments as dequoted word lists (see :func:`_segment_texts`)."""
    return [_segment_words(text) for text in _segment_texts(command)]


def _positionals(words: list[str], valued: frozenset[str]) -> list[str]:
    """The non-option words after the program, skipping the value of each option
    in *valued* (``-C dir``; ``--opt=value`` carries its own)."""
    positional: list[str] = []
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in valued
            continue
        positional.append(word)
    return positional


def _graphql_may_mutate(query: str) -> bool:
    """Whether a ``query`` field may hold a mutation. A body read from a file
    (``@path``) cannot be seen, so it counts as one; '#' comments before the
    operation are skipped."""
    if query.startswith("@"):
        return True
    text = query
    while True:
        text = text.lstrip()
        if not text.startswith("#"):
            return text.startswith("mutation")
        text = text.partition("\n")[2]


def _gh_is_write(words: list[str]) -> bool:
    positional = _positionals(words, _GH_VALUED_OPTIONS)
    if len(positional) >= 2 and positional[1] in _GH_WRITE_VERBS.get(positional[0], frozenset()):
        return True
    if positional[:1] != ["api"]:
        return False
    method = ""
    fields: list[str] = []
    has_input = False
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        flag, eq, inline = word.partition("=")
        if flag in _GH_API_METHOD_OPTIONS:
            method = inline if eq else nxt
        elif word.startswith("-X") and len(word) > 2:
            method = word[2:]
        elif flag in _GH_API_FIELD_OPTIONS:
            fields.append(inline if eq else nxt)
        elif word[:2] in ("-f", "-F") and len(word) > 2:
            # An attached operand (-fbody=done) is the same field as -f body=done.
            fields.append(word[2:])
        elif flag == "--input":
            has_input = True
    if "graphql" in positional[1:2]:
        # GraphQL always POSTs, whatever -X says; only a mutation writes.
        return (
            any(
                f.partition("=")[0] == "query" and _graphql_may_mutate(f.partition("=")[2])
                for f in fields
            )
            or has_input
        )
    if method:
        return method.upper() in _MUTATING_METHODS
    # With parameters and no explicit method, gh api sends POST.
    return bool(fields) or has_input


def _git_is_write(words: list[str]) -> bool:
    positional = _positionals(words, _GIT_VALUED_OPTIONS)
    return bool(positional) and positional[0] in _GIT_WRITE_SUBCOMMANDS


def _aws_is_write(words: list[str]) -> bool:
    positional = _positionals(words, _AWS_VALUED_OPTIONS)
    if len(positional) < 2:
        return False
    return not positional[1].lower().startswith(_AWS_READ_OP_PREFIXES)


def _prefix_words(extra_write_commands: tuple[str, ...]) -> list[list[str]]:
    prefixes = []
    for prefix in extra_write_commands:
        words = _segment_words(prefix.strip()) if isinstance(prefix, str) else []
        if words:
            prefixes.append(words)
    return prefixes


def _program_words(words: list[str]) -> list[str]:
    """A segment's words from its program on, the program reduced to its basename.

    Leading ``NAME=value`` assignments and the wrappers in
    :data:`_WRAPPER_VALUED_OPTIONS` are dropped with their own options (an option
    that takes a value drops that value too), ``timeout``'s duration and
    ``env``'s assignments included, so ``timeout -k 5 60s git push`` and
    ``/usr/bin/env -C dir X=1 gh pr create`` are judged by ``git`` and ``gh``.
    """
    while True:
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        if not words:
            return words
        program = words[0].rsplit("/", 1)[-1]
        valued = _WRAPPER_VALUED_OPTIONS.get(program)
        if valued is None:
            return [program, *words[1:]]
        words = words[1:]
        while words and words[0].startswith("-") and words[0] != "-":
            option = words[0]
            words = words[1:]
            if option in valued and words:
                words = words[1:]
        if program == "timeout" and words:
            words = words[1:]


def classify_command(command: str, extra_write_commands: tuple[str, ...] = ()) -> str:
    """READ, WRITE, or ``""`` (no command to judge) for a shell command line.

    A segment (split on ``&&``, ``||``, ``;``, ``|``, ``&``, newline, outside
    quotes) is a write when its
    program and subcommand words are a write in the tables, or when its words start
    with one of *extra_write_commands*, the operator's own prefixes
    (``hooks.batch_write_guard_commands``). Argument text is never matched.
    """
    # Bounded first: this runs on the event loop for every tool_call frame, and a
    # backend-authored command can be megabytes long, which ``shlex`` would take
    # minutes to split. A write named only past the head reads as a read: the
    # guard misses it, it never refuses more.
    command = command[:MAX_CLASSIFIED_COMMAND_CHARS]
    if not command.strip():
        return ""
    prefixes = _prefix_words(extra_write_commands)
    for segment in _segments(command):
        words = _program_words(segment)
        if not words:
            continue
        if any(words[: len(p)] == p for p in prefixes):
            return WRITE
        program = words[0]
        if program == "gh" and _gh_is_write(words):
            return WRITE
        if program == "git" and _git_is_write(words):
            return WRITE
        if program == "aws" and _aws_is_write(words):
            return WRITE
    return READ


def _tool_identity(event: Any) -> tuple[str, str]:
    """``(server, tool)`` from the harness-stamped identity, never the title."""
    server = str(getattr(event, "mcp_server_name", "") or "").lstrip("@")
    name = str(getattr(event, "harness_builtin_tool", "") or getattr(event, "tool_name", "") or "")
    name = name.lstrip("@")
    if "/" in name:
        prefix, name = name.rsplit("/", 1)
        server = server or prefix
    return server, name


def _command_of(event: Any) -> str:
    try:
        command = event.shell_command
    except Exception:
        command = None
    return command if isinstance(command, str) else ""


def _is_shell_call(event: Any) -> bool:
    server, name = _tool_identity(event)
    if server:
        return False
    if name:
        return KIRO_TOOL_ALIASES.get(name, name) in _SHELL_BUILTINS
    return bool(getattr(event, "is_shell", False))


def classify(event: Any, extra_write_commands: tuple[str, ...] = ()) -> str:
    """READ, WRITE, or ``""`` (not guarded) for one tool-call event.

    *extra_write_commands* reaches :func:`classify_command` for a shell call.
    """
    if _is_shell_call(event):
        return classify_command(_command_of(event), extra_write_commands)
    server, name = _tool_identity(event)
    if server:
        if (server, name) in _WRITE_MCP_TOOLS:
            return WRITE
        if (server, name) in _READ_MCP_TOOLS:
            return READ
        return ""
    if name:
        builtin = KIRO_TOOL_ALIASES.get(name, name)
        if builtin in _WRITE_BUILTINS:
            return WRITE
        if builtin in _READ_BUILTINS:
            return READ
        policy_name = KIRO_CLI_NAME_BY_KAS_TOOL.get(builtin, "")
        if policy_name in _READ_BUILTINS:
            return READ
        if policy_name in _WRITE_BUILTINS:
            return WRITE
        return ""
    kind = str(getattr(event, "tool_kind", "") or "")
    if kind in _READ_KINDS:
        return READ
    if kind in _WRITE_KINDS:
        return WRITE
    return ""


# ── Window ──


@dataclass
class _Member:
    """One call of the batch: its class, and for a shell call its bounded
    command, so it can be classified again with the operator's prefixes."""

    klass: str
    command: str = ""

    def class_with(self, extra_write_commands: tuple[str, ...]) -> str:
        if self.command and extra_write_commands:
            return classify_command(self.command, extra_write_commands)
        return self.klass


class BatchWindow:
    """The calls of the current parallel batch on one session.

    Keyed by :func:`liveness_key`, the bounded key the transports' in-flight
    bookkeeping holds, and bounded by the same :data:`MAX_ACTIVE_TOOL_CALLS`, so a
    backend that never sends terminal updates or sends huge ids cannot grow it.
    Calls past the bound are not admitted and so not judged; the first overflow of
    each batch is logged.

    A member counts as open from its ``tool_call`` until its terminal update, a
    refusal by this guard, or :data:`OPEN_MEMBER_TTL_SECS`, whichever comes first.
    The bound is for a terminal the backend never sends: without it the batch
    would stay open for the rest of the turn and every resent write would join it.
    Expiry only ever ends a batch early, so it can miss a refusal but never adds one.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._members: dict[str, _Member] = {}
        #: Open members by key, with the time each was dispatched.
        self._open: dict[str, float] = {}
        self._overflowed = False
        self._clock = clock

    def _expire_open(self) -> None:
        cutoff = self._clock() - OPEN_MEMBER_TTL_SECS
        for key in [k for k, ts in self._open.items() if ts <= cutoff]:
            del self._open[key]

    def __len__(self) -> int:
        return len(self._members)

    def dispatched(self, call_id: str, klass: str, command: str = "") -> None:
        """A ``tool_call`` frame announced *call_id* with class *klass* (and, for a
        shell call, *command*). A second frame for the same id updates it."""
        key = liveness_key(call_id)
        if not key:
            return
        member = _Member(klass, command[:MAX_CLASSIFIED_COMMAND_CHARS])
        if key in self._members:
            # A later frame for a call already held (a refinement, a repeat)
            # refreshes what it is, never whether it is still open: a settled
            # read must not reopen the batch.
            self._members[key] = member
            return
        self._expire_open()
        if not self._open:
            self._members.clear()
            self._overflowed = False
        if len(self._members) >= MAX_ACTIVE_TOOL_CALLS:
            if not self._overflowed:
                self._overflowed = True
                logger.warning(
                    "batch guard: a batch passed %d calls; later calls are not judged",
                    MAX_ACTIVE_TOOL_CALLS,
                )
            return
        self._members[key] = member
        self._open[key] = self._clock()

    def refresh(self, call_id: str, klass: str, command: str = "") -> None:
        """Update a held member with what a later event resolved (its permission
        request may name a tool its frame did not). A member the window does not
        hold, or an event that resolves no class, changes nothing; the member's
        open or settled state is never touched."""
        key = liveness_key(call_id)
        if klass and key in self._members:
            self._members[key] = _Member(klass, command[:MAX_CLASSIFIED_COMMAND_CHARS])

    def settled(self, call_id: str) -> None:
        """*call_id* reached a terminal status. It stays a member of its batch."""
        self._open.pop(liveness_key(call_id), None)

    def reset(self) -> None:
        """A new prompt turn: nothing carries over."""
        self._members.clear()
        self._open.clear()
        self._overflowed = False

    def read_peers(self, call_id: str, extra_write_commands: tuple[str, ...] = ()) -> int:
        """How many OTHER members of *call_id*'s batch are reads (0 when unseen)."""
        key = liveness_key(call_id)
        if key not in self._members:
            return 0
        return sum(
            1
            for k, m in self._members.items()
            if k != key and m.class_with(extra_write_commands) == READ
        )

    def refusal_for(
        self, call_id: str, klass: str, extra_write_commands: tuple[str, ...] = ()
    ) -> str | None:
        """The refusal for approving *call_id* (class *klass*), or ``None``."""
        if klass != WRITE:
            return None
        reads = self.read_peers(call_id, extra_write_commands)
        if not reads:
            return None
        return (
            "Blocked by the batch guard: this write-class call was issued in the same "
            f"parallel tool batch as {reads} read-class call(s), so it may carry a value "
            "those reads had not returned yet. Nothing ran. Wait for the reads to return, "
            "check their output, then send this call again on its own in your next step."
        )


def note_dispatched(window: BatchWindow | None, event: Any) -> None:
    """Feed a ``tool_call`` (or its refinement) into *window*. Never raises."""
    if window is None:
        return
    try:
        command = _command_of(event) if _is_shell_call(event) else ""
        window.dispatched(str(getattr(event, "tool_call_id", "") or ""), classify(event), command)
    except Exception:
        logger.debug("batch guard: could not classify a tool call", exc_info=True)


#: Refusal reasons a transport keeps for the caller of ``approve_tool`` to read.
#: An entry is taken once; a caller that never asks leaves at most this many.
MAX_KEPT_REFUSALS = 64


def _keep_refusal(transport: Any, request_id: Any, reason: str) -> None:
    kept = getattr(transport, "_batch_refusals", None)
    if not isinstance(kept, dict):
        kept = {}
        try:
            transport._batch_refusals = kept
        except Exception:
            return
    key = _refusal_key(request_id)
    if not key:
        return
    while len(kept) >= MAX_KEPT_REFUSALS:
        kept.pop(next(iter(kept)))
    kept[key] = reason


def _refusal_key(request_id: Any) -> str:
    """The bounded key a kept refusal is held under: a backend-authored request id
    goes through :func:`liveness_key`, which keeps a short id and digests a long
    one, at retention and at lookup alike."""
    return liveness_key(str(request_id))


def take_refusal(transport: Any, request_id: Any) -> str | None:
    """The guard's reason for refusing *request_id* on *transport*, taken once.

    ``approve_tool`` answers a guard refusal with ``False``, as it answers a
    security-floor refusal; a caller that settles requests by cause
    (``tool_permission``) reads the reason here to tell the two apart."""
    kept = getattr(transport, "_batch_refusals", None)
    if not isinstance(kept, dict):
        return None
    reason = kept.pop(_refusal_key(request_id), None)
    return reason if isinstance(reason, str) else None


def note_refined(window: BatchWindow | None, event: Any) -> None:
    """Re-classify a member from a refinement that carries its shell command.
    A refinement for a call the window does not hold changes nothing."""
    if window is None:
        return
    call_id = str(getattr(event, "tool_call_id", "") or "")
    if liveness_key(call_id) not in window._members:
        return
    try:
        command = _command_of(event) if _is_shell_call(event) else ""
        # ``refresh`` keeps the held member when this update resolves no class
        # (a title-only or kind-only refinement), so a known read is never erased.
        window.refresh(call_id, classify(event), command)
    except Exception:
        logger.debug("batch guard: could not refresh a member", exc_info=True)


def note_settled(window: BatchWindow | None, call_id: str) -> None:
    if window is not None and call_id:
        window.settled(call_id)


# ── Approval ──


def guard_settings() -> tuple[bool, tuple[str, ...]]:
    """``(enabled, extra write-command prefixes)`` from the ``hooks`` section.

    ``hooks.batch_write_guard`` turns the guard on; ``hooks.batch_write_guard_commands``
    adds the operator's own write-command prefixes to the shell table. Off, with no
    extras, when config cannot be read.
    """
    try:
        from kiro_crew import hooks as hooks_mod
        from kiro_crew.config import KiroCrewConfig

        config = hooks_mod.HooksConfig.from_dict(KiroCrewConfig.load().hooks)
        return config.batch_write_guard, tuple(config.batch_write_guard_commands)
    except Exception:
        logger.debug("batch guard: config unreadable; guard off", exc_info=True)
        return False, ()


async def screen_approval(transport: Any, event: Any) -> str | None:
    """The guard's refusal for approving *event* on *transport*, or ``None``.

    When it refuses, the reason has already been steered into the running turn,
    so the caller only audits and rejects. The window must hold the call and at
    least one other member before config is read; then the call and its peers
    are classified with the operator's extra write commands.
    """
    import asyncio

    window = getattr(transport, "_batch_window", None)
    if window is None or event is None:
        return None
    call_id = str(getattr(event, "tool_call_id", "") or "")
    # Every approval, a read's included, refreshes the call's own member: the
    # permission request may resolve a tool its tool_call frame named only by
    # server, and a later write in the batch is judged against that.
    try:
        command = _command_of(event) if _is_shell_call(event) else ""
        window.refresh(call_id, classify(event), command)
    except Exception:
        logger.debug("batch guard: could not refresh a member", exc_info=True)
    if len(window) < 2:
        return None
    if liveness_key(call_id) not in window._members:
        return None
    enabled, extra_write_commands = await asyncio.to_thread(guard_settings)
    if not enabled:
        return None
    reason = window.refusal_for(
        call_id, classify(event, extra_write_commands), extra_write_commands
    )
    if reason is None:
        return None
    from kiro_crew.deny_notice import steer_refusal_notice

    try:
        await steer_refusal_notice(
            transport,
            str(getattr(event, "title", "") or ""),
            reason,
            cause=DENY_CAUSE_BATCH_READ_WRITE,
        )
    except Exception:
        logger.debug("batch guard: notice steer failed; the reject still runs", exc_info=True)
    _keep_refusal(transport, getattr(event, "request_id", ""), reason)
    # A refused call never runs, and its own terminal update may never arrive:
    # settle it here, so the write the model resends alone starts a new batch.
    window.settled(call_id)
    return reason


def audit_refusal(event: Any, reason: str, *, request_id: str | int = "") -> None:
    """Record a batch-guard refusal under its own source, outcome and mechanism.

    Best-effort, like ``permission_floor.audit_refusal``, whose redaction it reuses;
    the row is never the floor's, so an operator can tell a timing refusal from a
    security one.
    """
    from kiro_crew import permission_floor

    try:
        permission_floor.sel_mod.sel().log_tool_invocation(
            session_key="",
            source=AUDIT_SOURCE,
            tool_name=permission_floor._redacted_label(getattr(event, "title", "")),
            tool_kind=permission_floor._redacted_label(getattr(event, "tool_kind", "")),
            outcome=OUTCOME_REJECTED_BATCH_GUARD,
            request_id=getattr(event, "request_id", "") or request_id,
            metadata={
                "reason": permission_floor.loggable_reason(reason),
                "mechanism": AUDIT_MECHANISM,
                "cause": DENY_CAUSE_BATCH_READ_WRITE,
            },
        )
    except Exception:
        logger.debug("batch guard SEL audit failed", exc_info=True)


async def reject(
    transport: Any, event: Any, request_id: str | int, reason: str, *, session_key: str
) -> bool:
    """Answer a request :func:`screen_approval` refused: drop any mediated-secret
    grant minted for the call, log and audit it as the guard's, and reject it.
    Returns ``False``, what ``approve_tool`` returns for a refusal."""
    import asyncio

    from kiro_crew import mediated_request_capability, permission_floor

    mediated_request_capability.invalidate_for_gate_event(session_key, event)
    logger.warning(
        "approve_tool: batch guard refused req=%s: %s",
        permission_floor._redacted_label(str(request_id)),
        permission_floor.loggable_reason(reason),
    )
    await asyncio.to_thread(audit_refusal, event, reason, request_id=request_id)
    await transport.reject_tool(request_id)
    return False

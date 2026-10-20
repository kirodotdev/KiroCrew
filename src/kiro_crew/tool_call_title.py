"""Human-readable tool-call titles, derived from the call's own arguments.

This module is a line-for-line Python mirror of
``website/src/utils/toolCallTitle.ts``. The dashboard renders titles through
the TypeScript half; the messaging renderers (Slack / Discord task labels, and
any other channel that has no i18n catalog at hand) render them through this
one. ``test/fixtures/tool_call_titles.json`` pins BOTH implementations to a
single case table (action + English title): change a rule on one side and the
fixture fails on whichever side you forgot.

Shape (same as the TypeScript module)::

    classify_tool_call(...)   -> ClassifiedToolCall | None   structure, language-neutral
    render_tool_action(action) -> str                        English template
    derive_tool_call_title(...) -> DerivedToolTitle          the two, plus fallbacks

Shell rules are a port of Codex's ``parse_command.rs``: a script is accepted
only when it consists of plain words, quoted strings and the connectors
``&&`` ``||`` ``;`` ``|``; each segment is classified as Read / List files /
Search / Git / package-manager / print / GitHub CLI / curl; ``cd`` only moves
the cwd that a later Read's relative path is joined onto (Search / List files
paths are shown as written, as in Codex); small formatting helpers are
dropped from pipelines. If any segment stays Unknown the whole title falls back
to the raw command. One display-only extension past Codex's grammar: the
stderr-silencing redirects ``2>/dev/null`` / ``2>&1`` are stripped before
parsing. Two safety departures the other way: a ``VAR=value`` env prefix (which
Codex strips) keeps the whole command raw, because the prefix can change what
the command does; and a segment that writes, uploads, deletes or rewrites
history through a flag the template would not show (``curl -T``,
``find -delete``, ``xargs rm``, ``git push --force``, ``gh api -X``) stays raw
too. The title may say less than the command only in detail, never in effect.
The invariant behind every such rule: no templated segment may carry code or
an exec-capable option. An interpreter (``sed``, ``awk``, ``perl``, ``ruby``,
``python``, ``node``) is never an ``xargs`` target, and is a formatting tail or a
read only when its script matches a positive read-only grammar
(``parse_sed_pure``, ``awk_program_is_pure``); a tool with an option that runs a
program (``find -exec``, ``fd -x``, ``rg --pre``, ``git -c``, ``xargs <anything
not on the read-only list>``) is raw whenever that option is present.

Rule-change policy: every change to a rule here lands in the same commit with
fixture cases in ``test/fixtures/tool_call_titles.json`` that exercise it, and
with the matching change in ``website/src/utils/shellCommandParse.ts`` /
``toolCallTitle.ts``; the fixture is what makes the two implementations one
rule set.

English templates below are the ``utils.toolCallTitle.*`` values of
``website/src/i18n/locales/en.manual.json``; ``{{x}}`` placeholders are
interpolated by hand so this module stays standard-library only.

The shell rules live in two owners, the Python half of
``website/src/utils/shellCommandParse.ts``: ``tool_call_title_shell_parse`` reads a
script's words (the tokenizer, operands and the read-only grammars for ``sed``,
``awk``, ``grep``, ``find`` and ``xargs``), and ``tool_call_title_shell_classify``
decides what each segment and the whole script do (the per-tool tables and
classifiers, ``summarize_segment``, ``classify_shell_command``). This module keeps
the tool-call types, the native and MCP classifiers, the English templates and the
derivation, and stays the import path for every name the owners define.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from kiro_crew.mcp_tool_titles import declared_tool_title
from kiro_crew.tool_call_title_shell_classify import (  # noqa: F401  (re-exported for importers)
    CURL_BODY_FLAGS,
    CURL_FETCH_METHODS,
    CURL_FLAGS_WITH_VALS,
    CURL_LOCAL_IO_FLAGS,
    GH_API_MUTATING_FLAGS,
    GH_FLAGS_WITH_VALS,
    GH_NOUN_DISPLAY,
    GIT_DESTRUCTIVE_FLAGS,
    GIT_GLOBAL_FLAGS_BARE,
    GIT_GLOBAL_FLAGS_WITH_VALS,
    GIT_PATHSPEC_AFTER_DD_SUBS,
    GIT_PATHSPEC_SUBS,
    GIT_REF_DELETING_SUBS,
    GIT_REF_PREFIX_RE,
    GIT_SUB_FLAGS_WITH_VALS,
    GIT_SUBCOMMANDS,
    HOST_RE,
    LINTERS,
    NODE_PMS,
    NPX_PASSTHROUGH,
    TEST_RUNNERS,
    classify_curl,
    classify_gh,
    classify_git,
    classify_node_pm,
    classify_shell_command,
    classify_tool,
    has_env_prefix,
    head_tail_read,
    host_of,
    list_action,
    read_action,
    same_action,
    search_action,
    simplify_once,
    strip_wrapper,
    summarize_segment,
    unknown,
)
from kiro_crew.tool_call_title_shell_parse import (  # noqa: F401  (re-exported for importers)
    ALWAYS_FORMATTING,
    AWK_PURE_BUILTINS,
    AWK_SIDE_EFFECT_RE,
    BARE_REJECT,
    DISPLAY_SEP,
    ENV_ASSIGN_RE,
    FIND_MUTATING_ACTIONS,
    NOISE_REDIRECT_RE,
    SED_PURE_FLAGS,
    SED_RANGE_SCRIPT_RE,
    SHORT_PATH_SKIP,
    XARGS_READ_ONLY_TARGETS,
    Tok,
    ToolAction,
    awk_data_file_operand,
    awk_is_pure_filter,
    awk_program_is_pure,
    cd_target,
    find_mutates,
    first_non_flag_operand,
    is_abs_like,
    is_digits,
    is_mutating_xargs,
    is_pathish,
    is_plus_digits,
    is_python_command,
    is_sed_substitute_script,
    is_small_formatting_command,
    join_paths,
    parse_fd_query_and_path,
    parse_find_query_and_path,
    parse_grep_like,
    parse_sed_pure,
    positional_operands,
    rg_has_preprocessor,
    sed_is_pure_filter,
    sed_read_path,
    short_display_path,
    single_non_flag_operand,
    skip_flag_values,
    sort_writes_file,
    split_segments,
    tokenize_shell,
    xargs_subcommand,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedToolCall:
    """Structure of one tool call: the main action plus what was folded in."""

    action: ToolAction
    # Further parsed actions folded into "and N more" (shell chains, multi-op reads).
    more: int
    # ACP-style kind for the icon; refined for shell reads/searches.
    kind: str


@dataclass(frozen=True)
class DerivedToolTitle:
    """The title a tool-call row should show, plus what to keep for the tooltip."""

    title: str
    kind: str
    # The verbatim command / incoming title, for tooltips and the expanded header.
    raw_title: str
    # True when ``title`` substitutes for ``raw_title`` -- a template applied, or the
    # backend sent its own description of the call; False when ``title`` is the
    # (formatted) raw title.
    derived: bool


@dataclass(frozen=True)
class _Input:
    """Mirror of the TS ``ToolCallTitleInput`` (empty string == undefined)."""

    tool_name: str = ""
    kind: str = ""
    raw_input: object = None
    title: str = ""
    is_shell: bool = False
    mcp_server: str = ""
    cwd: str = ""


# ---------------------------------------------------------------------------
# Native tools (fs_read / fs_write / grep / glob / web_fetch / KAS read/edit/write)
# ---------------------------------------------------------------------------

NATIVE_TOOL_NAMES = frozenset(
    [
        "fs_read",
        "fs_write",
        "read",
        "write",
        "edit",
        "glob",
        "grep",
        "web_fetch",
        "web_search",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
    ]
)
MAX_MCP_ARG_CHARS = 60
MAX_RAW_TITLE_CHARS = 80
# The ONLY argument keys an MCP title may name, in preference order. Nothing
# outside the list is ever shown -- the title reaches the sidebar, the approval
# bar and the Slack/Discord task label, so an unknown key (``password``,
# ``text``, ``token``) gets no "first string value" fallback.
MCP_SALIENT_KEYS = [
    "target",
    "path",
    "file_path",
    "url",
    "query",
    "pattern",
    "name",
    "title",
    "message",
    "task",
    "slug",
    "job_id",
    "doc_id",
    "repo",
    "id",
]
# Key-name segments that mark a value as a credential; applied after the
# allowlist too, so ``api_key`` / ``authToken`` never enter a title.
SENSITIVE_KEY_SEGMENTS = frozenset(
    [
        "password",
        "passwd",
        "pwd",
        "pass",
        "secret",
        "token",
        "key",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "bearer",
        "apikey",
    ]
)
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def is_sensitive_arg_key(key: str) -> bool:
    """True when a key name carries a credential-marking segment."""
    snake = _CAMEL_BOUNDARY_RE.sub(r"\1_\2", key).lower()
    return any(seg in SENSITIVE_KEY_SEGMENTS for seg in _KEY_SPLIT_RE.split(snake) if seg)


def as_record(v: object) -> dict[str, Any] | None:
    return v if isinstance(v, dict) else None


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v.strip() else None


def _num(v: object) -> int | float | None:
    """A finite JSON number; integral floats come back as ``int`` so the action
    serializes like the TS ``number`` (``5``, never ``5.0``)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == v and v not in (float("inf"), float("-inf")):
        return int(v) if v.is_integer() else v
    return None


def parse_tool_args(raw_input: object) -> dict[str, Any] | None:
    """Parse ``raw_input`` into an args dict when it is one (or a JSON string of one)."""
    rec = as_record(raw_input)
    if rec is not None:
        return rec
    if isinstance(raw_input, str):
        s = raw_input.strip()
        if not s.startswith("{"):
            return None
        try:
            return as_record(json.loads(s))
        except ValueError:
            return None
    return None


def rel_display_path(path: str, cwd: str = "") -> str:
    """Path relative to ``cwd`` when under it; else ``parent/basename``; a
    home-anchored path keeps its ``~/...`` form. The full path stays in the tooltip."""
    p = path.replace("\\", "/")
    parts = [x for x in p.rstrip(DISPLAY_SEP).split(DISPLAY_SEP) if x]
    if cwd:
        base = cwd.replace("\\", "/").rstrip("/")
        if base and p.startswith(base + "/"):
            return p[len(base) + 1 :]
        if base and p.rstrip("/") == base and parts:
            return parts[-1]
    if p.startswith("~/"):
        return p
    if len(parts) <= 2:
        return "/".join(parts) or p
    return "/".join(parts[-2:])


_DIFF_TARGET_RE = re.compile(r"^\+\+\+ (?:b/)?(.+?)\s*$", re.MULTILINE)
_DIFF_DEV_NULL_RE = re.compile(r"^--- /dev/null\s*$", re.MULTILINE)
_FINDING_RE = re.compile(r"^Finding\b", re.ASCII)


def diff_target_path(text: str) -> str | None:
    """Path named by a persisted unified-diff input (``+++ b/path`` / ``+++ path``)."""
    m = _DIFF_TARGET_RE.search(text)
    p = m.group(1) if m else None
    return p if p and p != "/dev/null" else None


# kiro-cli's own read title, ``Reading <path>`` or ``Reading <path>:<from>-<to>``.
# Parsed only when the call carries no arguments to read from, so a mixed
# transcript keeps one verb. Transport identifier matched exactly, not copy.
KIRO_CLI_READING_TITLE_RE = re.compile(r"^Reading\s+(\S+?)(?::(\d+)-(\d+))?$")


def read_from_kiro_cli_title(title: str) -> ClassifiedToolCall | None:
    m = KIRO_CLI_READING_TITLE_RE.match((title or "").strip())
    if m is None:
        return None
    path = m.group(1)
    action: ToolAction
    if m.group(2) is not None and m.group(3) is not None:
        action = {"type": "read", "path": path, "from": int(m.group(2)), "to": int(m.group(3))}
    else:
        action = {"type": "read", "path": path}
    return ClassifiedToolCall(action, 0, "read")


def classify_native(inp: _Input) -> ClassifiedToolCall | None:
    kind = inp.kind
    tool = inp.tool_name
    args = parse_tool_args(inp.raw_input)
    title = inp.title

    def rel(p: str) -> str:
        return rel_display_path(p, inp.cwd)

    # -- read ---------------------------------------------------------------
    if kind == "read" or tool in ("fs_read", "read", "Read"):
        if args is None:
            return read_from_kiro_cli_title(inp.title)
        raw_ops = args.get("operations")
        ops = (
            [o for o in (as_record(x) for x in raw_ops) if o is not None]
            if isinstance(raw_ops, list)
            else None
        )
        if ops:
            first = ops[0]
            path = _str(first.get("path"))
            if not path:
                return None
            distinct = len({p for p in (_str(o.get("path")) for o in ops) if p})
            more = max(0, distinct - 1)
            mode = _str(first.get("mode"))
            if mode == "Directory":
                return ClassifiedToolCall({"type": "list_files", "path": rel(path)}, more, "read")
            if mode == "Image":
                return ClassifiedToolCall({"type": "view_image", "path": rel(path)}, more, "read")
            if mode == "Search":
                q = _str(first.get("pattern"))
                if q is None:
                    q = _str(first.get("query"))
                if not q:
                    return None
                return ClassifiedToolCall(
                    {"type": "search", "query": q, "path": rel(path)}, more, "search"
                )
            frm = _num(first.get("offset"))
            if frm is None:
                frm = _num(first.get("start_line"))
            limit = _num(first.get("limit"))
            to = _num(first.get("end_line"))
            if to is None and frm is not None and limit is not None:
                to = frm + limit - 1
            action: ToolAction = (
                {"type": "read", "path": rel(path), "from": frm, "to": to}
                if frm is not None and to is not None and more == 0
                else {"type": "read", "path": rel(path)}
            )
            return ClassifiedToolCall(action, more, "read")
        path = _str(args.get("path"))
        if path is None:
            path = _str(args.get("file_path"))
        if not path:
            return None
        frm = _num(args.get("offset"))
        limit = _num(args.get("limit"))
        action = (
            {"type": "read", "path": rel(path), "from": frm, "to": frm + limit - 1}
            if frm is not None and limit is not None
            else {"type": "read", "path": rel(path)}
        )
        return ClassifiedToolCall(action, 0, "read")

    # -- edit / create --------------------------------------------------------
    if kind == "edit" or tool in ("fs_write", "write", "edit", "Write", "Edit"):
        if args is not None:
            path = _str(args.get("path"))
            if path is None:
                path = _str(args.get("file_path"))
            if not path:
                return None
            cmd = _str(args.get("command"))
            is_create = (
                cmd == "create"
                or tool in ("write", "Write")
                or (
                    cmd is None
                    and "content" in args
                    and "oldStr" not in args
                    and "old_string" not in args
                )
            )
            return ClassifiedToolCall(
                {"type": "create" if is_create else "edit", "path": rel(path)}, 0, "edit"
            )
        if isinstance(inp.raw_input, str) and inp.raw_input.startswith("---"):
            path = diff_target_path(inp.raw_input)
            if not path:
                return None
            is_create = _DIFF_DEV_NULL_RE.search(inp.raw_input) is not None
            return ClassifiedToolCall(
                {"type": "create" if is_create else "edit", "path": rel(path)}, 0, "edit"
            )
        return None

    # -- search (grep / glob) -------------------------------------------------
    if kind == "search" or tool in ("grep", "glob", "Grep", "Glob"):
        if args is None:
            return None
        pattern = _str(args.get("pattern"))
        if not pattern:
            return None
        path = _str(args.get("path"))
        is_glob = tool in ("glob", "Glob") or (
            tool == "" and "include" not in args and _FINDING_RE.match(title) is not None
        )
        action = (
            {"type": "find_files", "pattern": pattern}
            if is_glob
            else {"type": "search", "query": pattern}
        )
        if path:
            action["path"] = rel(path)
        return ClassifiedToolCall(action, 0, "search")

    # -- fetch / web search ---------------------------------------------------
    if kind == "fetch" or tool in ("web_fetch", "web_search", "WebFetch", "WebSearch"):
        if args is None:
            return None
        url = _str(args.get("url"))
        if url:
            host = host_of(url)
            return ClassifiedToolCall({"type": "fetch", "host": host}, 0, "fetch") if host else None
        query = _str(args.get("query"))
        return (
            ClassifiedToolCall({"type": "search_web", "query": query}, 0, "fetch")
            if query
            else None
        )
    return None


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

MCP_TITLE_RES = [
    re.compile(r"^(?:Running:\s*)?@([^/\s]+)/(\S+)\Z"),
    re.compile(r"^([a-z0-9][\w.-]*)___(\w+)\Z", re.IGNORECASE | re.ASCII),
    re.compile(r"^mcp__(.+?)__(\w+)\Z", re.ASCII),
]


def mcp_identity_from_title(title: str) -> tuple[str, str] | None:
    """``(server, tool)`` named by an MCP-shaped title, or None."""
    t = (title or "").strip()
    for rx in MCP_TITLE_RES:
        m = rx.match(t)
        if m:
            return m.group(1), m.group(2)
    return None


_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_SEP_RE = re.compile(r"[_\-.]+")


def humanize_tool_name(name: str) -> str:
    """``session_send`` / ``artifact-folder-create`` / ``readSessionTail`` -> ``Session send``."""
    words = _SEP_RE.sub(" ", _CAMEL_RE.sub(r"\1 \2", name)).strip().lower()
    if not words:
        return name
    return words[0].upper() + words[1:]


def one_line(s: str, mx: int) -> str:
    flat = re.sub(r"\s+", " ", s).strip()
    if len(flat) <= mx:
        return flat
    cut = flat.rfind(" ", 0, mx + 1)
    return (flat[:cut] if cut >= mx // 2 else flat[:mx]).rstrip() + "…"


def salient_mcp_arg(args: dict[str, Any] | None) -> str | None:
    """The first short string argument worth naming in an MCP title.

    Allowlisted keys only (``MCP_SALIENT_KEYS``), never a credential-marked key,
    never a fallback to whatever string comes first. ``None`` means the title is
    the bare tool name.
    """
    if args is None:
        return None
    for k in MCP_SALIENT_KEYS:
        if k not in args or is_sensitive_arg_key(k):
            continue
        s = _str(args[k])
        if s:
            return one_line(s, MAX_MCP_ARG_CHARS)
    return None


def classify_mcp(inp: _Input) -> ClassifiedToolCall | None:
    from_title = mcp_identity_from_title(inp.title)
    tool = inp.tool_name or (from_title[1] if from_title else None)
    if not tool:
        return None
    if not inp.mcp_server and not from_title:
        return None
    args = parse_tool_args(inp.raw_input)
    arg = salient_mcp_arg(args)
    action: ToolAction = {"type": "mcp", "tool": tool}
    declared = declared_tool_title(inp.mcp_server or (from_title[0] if from_title else ""), tool)
    if declared:
        action["title"] = declared
    if arg:
        action["arg"] = arg
    return ClassifiedToolCall(
        action, 0, inp.kind if inp.kind and inp.kind != "unknown" else "other"
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

SHELL_STUB_TITLES = frozenset(["shell", "execute_bash", "Run Command", "Terminal", "bash", "Bash"])
SHELL_TOOL_NAMES = frozenset(["execute_bash", "shell", "bash", "Bash", "execute", "terminal"])


def shell_command_of(inp: _Input) -> str | None:
    args = parse_tool_args(inp.raw_input)
    return _str(args.get("command")) if args is not None else None


def is_shell_call(inp: _Input) -> bool:
    if inp.is_shell or inp.kind == "execute":
        return True
    if inp.tool_name and inp.tool_name in SHELL_TOOL_NAMES:
        return True
    title = inp.title.strip()
    kind_unknown = not inp.kind or inp.kind == "unknown"
    if not kind_unknown:
        return False
    # kiro-cli's live shell title carries the command itself (``Running: <cmd>``);
    # an MCP call is ``Running: @server/tool`` and is not a shell.
    if title.startswith("Running:") and not title.startswith("Running: @"):
        return True
    return shell_command_of(inp) is not None and title in SHELL_STUB_TITLES


def shell_command_from_title(title: str) -> str | None:
    """The command a live kiro-cli title carries (``Running: <cmd>`` or the bare command)."""
    t = (title or "").strip()
    if not t or t in SHELL_STUB_TITLES:
        return None
    return re.sub(r"^Running:\s*", "", t)


def _collapse_ws(s: str) -> str:
    """Whitespace-collapsed form, for comparing a transport title against the command."""
    return " ".join(s.split())


_TRAILING_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.|\u2026)\s*$")


def _is_argument_digest_of(title: str, cmd: str) -> bool:
    """True when ``title`` is a digest OF ``cmd`` rather than a sentence ABOUT it.

    kiro-cli titles a shell call it received no purpose for by joining fragments
    of the command's own arguments with ``, `` and cutting the tail with an
    ellipsis (``--title, --text, Three, 1. ...``). Every fragment is a verbatim
    piece of the command, so the title says nothing the command does not already
    say. A description the model wrote carries words the command does not
    contain, so it never matches. Case-sensitive on purpose: a one-word sentence
    (``Rebuild`` for ``make rebuild``) stays a description. Mirror of
    ``isArgumentDigestOf``.
    """
    body = _TRAILING_ELLIPSIS_RE.sub("", title)
    fragments = [f.strip() for f in re.split(r",\s*", body) if f.strip()]
    if not fragments:
        return False
    haystack = _collapse_ws(cmd)
    return all(f in haystack for f in fragments)


def backend_shell_description(inp: _Input) -> str | None:
    """R0.0 -- the backend's own description of a shell call, when the transport
    sent one as the title; None otherwise. Mirror of ``backendShellDescription``.

    A shell title is a stub that names no command (``shell``, ``Run Command``),
    the command itself (kiro-cli's ``Running: <cmd>``, the gateway's
    ``_select_tool_title`` putting the command on the row), a digest of the
    command's own arguments (kiro-cli's purpose-less auto-title, see
    ``_is_argument_digest_of``), or a sentence the model wrote for this call
    (KAS with kiro-team/kiro-agent#2753, claude-agent-acp's Bash
    ``description``). Only the fourth is a description: a non-empty, non-stub
    title that differs from ``raw_input["command"]`` once whitespace is
    collapsed and is not assembled from the command's own pieces. With no
    command in the arguments the title is taken AS the command and never as a
    description.

    ``raw_input["description"]`` is deliberately not read: KAS keeps the
    argument but drops it from the title when the user edited the command
    before it ran, so the title is the transport's ruling on whether the
    sentence still describes the command that executed.
    """
    if not is_shell_call(inp):
        return None
    title = inp.title.strip()
    if not title or title in SHELL_STUB_TITLES:
        return None
    cmd = shell_command_of(inp)
    if cmd is None:
        return None
    t = _collapse_ws(re.sub(r"^Running:\s*", "", title))
    if not t or t == _collapse_ws(cmd):
        return None
    if _is_argument_digest_of(t, cmd):
        return None
    return title


def _classify(inp: _Input) -> ClassifiedToolCall | None:
    if is_shell_call(inp):
        cmd = shell_command_of(inp)
        if cmd is None:
            cmd = shell_command_from_title(inp.title)
        if not cmd:
            return None
        shell = classify_shell_command(cmd)
        if shell is None:
            return None
        action, more = shell
        if action["type"] in ("read", "list_files"):
            kind = "read"
        elif action["type"] in ("search", "find_files"):
            kind = "search"
        else:
            kind = "execute"
        return ClassifiedToolCall(action, more, kind)
    native_kind = inp.kind in ("read", "edit", "search", "fetch")
    if native_kind or (inp.tool_name and inp.tool_name in NATIVE_TOOL_NAMES):
        native = classify_native(inp)
        if native is not None:
            return native
        # An MCP tool can report a native kind (a server-side ``read``); fall through.
        if not inp.mcp_server and mcp_identity_from_title(inp.title) is None:
            return None
    return classify_mcp(inp)


def classify_tool_call(
    *,
    title: str = "",
    kind: str = "",
    raw_input: object = None,
    is_shell: bool = False,
    tool_name: str = "",
    mcp_server: str = "",
    cwd: str = "",
) -> ClassifiedToolCall | None:
    """Structure first: which action this call performs, language-neutral.
    Returns None when nothing better than the incoming title can be said."""
    return _classify(
        _Input(
            tool_name=tool_name or "",
            kind=kind or "",
            raw_input=raw_input,
            title=title or "",
            is_shell=bool(is_shell),
            mcp_server=mcp_server or "",
            cwd=cwd or "",
        )
    )


# ---------------------------------------------------------------------------
# Rendering -- English values of ``utils.toolCallTitle.*`` in en.manual.json
# ---------------------------------------------------------------------------


def render_tool_action(action: ToolAction) -> str:
    """English render of one action (the ``utils.toolCallTitle.*`` templates).

    Every argument slot is capped here, once, so a long grep pattern or a
    deep path cannot push a title past the 60 characters the MCP arg already
    obeys -- the channel task label renders this string.
    """
    t = action["type"]

    def cap(key: str) -> str:
        return one_line(str(action.get(key, "")), MAX_MCP_ARG_CHARS)

    if t == "read":
        if action.get("from") is not None and action.get("to") is not None:
            return f"Read {cap('path')} ({action['from']}–{action['to']})"
        return f"Read {cap('path')}"
    if t == "list_files":
        return f"List files in {cap('path')}" if action.get("path") else "List files"
    if t == "search":
        if action.get("path"):
            return f"Search '{cap('query')}' in {cap('path')}"
        return f"Search '{cap('query')}'"
    if t == "find_files":
        if action.get("path"):
            return f"Find files '{cap('pattern')}' in {cap('path')}"
        return f"Find files '{cap('pattern')}'"
    if t == "git":
        if action.get("path"):
            return f"Git {action['sub']} {cap('path')}"
        return f"Git {action['sub']}"
    if t == "install":
        return "Install dependencies"
    if t == "test":
        return "Run tests"
    if t == "script":
        return f"Run script {cap('name')}"
    if t == "build":
        return "Build"
    if t == "lint":
        return "Lint"
    if t == "print":
        return "Print"
    if t == "github":
        noun = GH_NOUN_DISPLAY.get(action["noun"], action["noun"])
        if action.get("target"):
            return f"GitHub {noun} {action['verb']} {cap('target')}"
        return f"GitHub {noun} {action['verb']}"
    if t == "github_api":
        return f"GitHub API {cap('path')}"
    if t == "fetch":
        if action.get("method"):
            return f"{action['method']} {cap('host')}"
        return f"Fetch {cap('host')}"
    if t == "search_web":
        return f"Search web for '{cap('query')}'"
    if t == "edit":
        return f"Edit {cap('path')}"
    if t == "create":
        return f"Create {cap('path')}"
    if t == "view_image":
        return f"View image {cap('path')}"
    if t == "mcp":
        label = action.get("title") or humanize_tool_name(action["tool"])
        return f"{label}: {action['arg']}" if action.get("arg") else label
    return str(action.get("cmd", ""))


def format_raw_command(cmd: str) -> str:
    """Raw-command fallback: first line, whitespace collapsed, <= 80 chars on a
    word boundary."""
    lines = [ln for ln in (ln.strip() for ln in cmd.split("\n")) if ln]
    if not lines:
        return ""
    first = re.sub(r"\s+", " ", lines[0])
    multi = len(lines) > 1
    if len(first) <= MAX_RAW_TITLE_CHARS:
        return first + " …" if multi else first
    cut = first.rfind(" ", 0, MAX_RAW_TITLE_CHARS + 1)
    return (first[:cut] if cut >= 40 else first[:MAX_RAW_TITLE_CHARS]).rstrip() + "…"


def derive_tool_call_title(
    *,
    title: str = "",
    kind: str = "",
    raw_input: object = None,
    is_shell: bool = False,
    tool_name: str = "",
    mcp_server: str = "",
    cwd: str = "",
) -> DerivedToolTitle:
    """The title a tool-call row should show, plus what to keep for the tooltip.

    Live and replayed rows go through this same function: a replayed shell row
    whose title degraded to ``shell`` is recomputed from ``raw_input["command"]``,
    and a replayed KAS row whose persisted title is the model's description keeps
    it (kiro-team/kiro-agent#2753 persists the description on the row).
    """
    inp = _Input(
        tool_name=tool_name or "",
        kind=kind or "",
        raw_input=raw_input,
        title=title or "",
        is_shell=bool(is_shell),
        mcp_server=mcp_server or "",
        cwd=cwd or "",
    )
    incoming = inp.title.strip()
    shell = is_shell_call(inp)
    cmd: str | None = None
    if shell:
        cmd = shell_command_of(inp)
        if cmd is None:
            cmd = shell_command_from_title(incoming)
    raw_title = cmd if cmd is not None else incoming
    if inp.kind and inp.kind != "unknown":
        kind_in = inp.kind
    else:
        kind_in = "execute" if shell else "other"
    classified = _classify(inp)
    # R0.0: the backend's own description outranks the template. The command
    # stays in ``raw_title``; the classifier still refines the icon kind.
    description = backend_shell_description(inp)
    if description is not None:
        return DerivedToolTitle(
            description, classified.kind if classified is not None else "execute", raw_title, True
        )
    if classified is not None:
        rendered = render_tool_action(classified.action)
        if classified.more > 0:
            rendered = f"{rendered} and {classified.more} more"
        return DerivedToolTitle(rendered, classified.kind, raw_title, True)
    if shell and cmd:
        return DerivedToolTitle(format_raw_command(cmd), "execute", raw_title, False)
    return DerivedToolTitle(incoming, kind_in, raw_title, False)

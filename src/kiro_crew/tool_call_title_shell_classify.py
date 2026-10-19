"""The per-command half of the shell-command classifier: what each segment and the whole script do.

The second half of the Codex ``parse_command.rs`` port, and with
:mod:`kiro_crew.tool_call_title_shell_parse` the Python mirror of
``website/src/utils/shellCommandParse.ts``. It holds the per-tool tables (git, curl,
the GitHub CLI, package managers, linters and test runners), the action
constructors, the per-tool classifiers, ``summarize_segment`` and the whole-script
pass that strips wrappers, folds duplicates and refuses a script with any Unknown
segment (``classify_shell_command``).

:mod:`kiro_crew.tool_call_title` stays the import path for every name here. This
module imports only :mod:`kiro_crew.tool_call_title_shell_parse`, never the facade.
"""

from __future__ import annotations

import re
from typing import Callable

from kiro_crew.tool_call_title_shell_parse import (
    DISPLAY_SEP,
    ENV_ASSIGN_RE,
    ToolAction,
    _at,
    awk_data_file_operand,
    cd_target,
    find_mutates,
    first_non_flag_operand,
    is_digits,
    is_pathish,
    is_plus_digits,
    is_python_command,
    is_small_formatting_command,
    join_paths,
    parse_fd_query_and_path,
    parse_find_query_and_path,
    parse_grep_like,
    positional_operands,
    rg_has_preprocessor,
    sed_read_path,
    short_display_path,
    single_non_flag_operand,
    skip_flag_values,
    split_segments,
    tokenize_shell,
)

# ---------------------------------------------------------------------------
# Shell -- per-segment classification (Codex ``summarize_main_tokens`` + our additions)
# ---------------------------------------------------------------------------

# Git flags that rewrite history, delete refs or discard work. A title of
# ``Git push`` for ``git push --force`` says less than the command in effect, so
# any of these leaves the command raw. ``-d``/``-D``/``--delete`` count only on
# the subcommands where they delete a ref.
GIT_DESTRUCTIVE_FLAGS = frozenset(
    ["--force", "-f", "--force-if-includes", "--hard", "--mirror", "--prune", "--delete", "-D"]
)
GIT_REF_DELETING_SUBS = frozenset(["push", "branch", "tag", "remote"])
_GIT_SHORT_CLUSTER_FD = re.compile(r"^-[a-zA-Z]*[fD][a-zA-Z]*$")
GIT_SUBCOMMANDS = frozenset(
    [
        "status",
        "diff",
        "log",
        "show",
        "add",
        "commit",
        "push",
        "pull",
        "fetch",
        "checkout",
        "switch",
        "branch",
        "rebase",
        "merge",
        "stash",
        "worktree",
        "clone",
        "rev-parse",
        "ls-remote",
        "blame",
        "restore",
        "reset",
        "tag",
        "remote",
        "rm",
        "mv",
    ]
)
# Sub-commands whose positional operands are pathspecs, not refs.
GIT_PATHSPEC_SUBS = frozenset(
    ["status", "diff", "log", "show", "add", "blame", "restore", "rm", "mv"]
)
# Sub-commands whose operands are refs unless separated by ``--``.
GIT_PATHSPEC_AFTER_DD_SUBS = frozenset(["checkout", "reset"])
# Global git options a templated command may carry. ``-c key=value`` and
# ``--config-env`` can point ``diff.external``, ``core.pager``, ``core.sshCommand``
# and friends at arbitrary programs, ``--exec-path`` swaps the git binaries: any of
# those, or an unknown global option, leaves the command raw.
GIT_GLOBAL_FLAGS_WITH_VALS = ["-C", "--git-dir", "--work-tree"]
GIT_GLOBAL_FLAGS_BARE = frozenset(
    [
        "--no-pager",
        "-P",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    ]
)
_GIT_DIR_EQ = re.compile(r"^--(git-dir|work-tree)=")
GIT_SUB_FLAGS_WITH_VALS = [
    "-m",
    "--message",
    "-C",
    "-c",
    "--author",
    "--date",
    "-b",
    "-B",
    "-u",
    "--set-upstream",
    "-M",
    "-S",
    "-G",
    "--since",
    "--until",
    "--grep",
    "-L",
    "--format",
    "--pretty",
    "-n",
    "--max-count",
    "--depth",
    "-o",
    "--origin",
]
GIT_REF_PREFIX_RE = re.compile(r"^(origin|upstream|refs)/")
NODE_PMS = frozenset(["npm", "pnpm", "yarn", "bun"])
LINTERS = frozenset(
    ["eslint", "flake8", "ruff", "mypy", "black", "prettier", "isort", "pylint", "stylelint"]
)
TEST_RUNNERS = frozenset(["pytest", "vitest", "jest", "mocha"])
NPX_PASSTHROUGH = frozenset(["vitest", "jest", "mocha", "tsc", "eslint", "prettier", "stylelint"])
CURL_FLAGS_WITH_VALS = [
    "-X",
    "--request",
    "-H",
    "--header",
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-urlencode",
    "-F",
    "--form",
    "-o",
    "--output",
    "-u",
    "--user",
    "-A",
    "--user-agent",
    "-e",
    "--referer",
    "-b",
    "--cookie",
    "-c",
    "--cookie-jar",
    "-m",
    "--max-time",
    "--connect-timeout",
    "-w",
    "--write-out",
    "--retry",
    "--retry-delay",
    "-T",
    "--upload-file",
    "-x",
    "--proxy",
    "--url",
    "-K",
    "--config",
    "--cacert",
    "--cert",
    "--key",
    "--resolve",
    "--limit-rate",
    "--json",
    "--form-string",
    "--data-ascii",
]
# Methods that ARE a fetch; any other explicit method is shown on the title.
CURL_FETCH_METHODS = frozenset(["GET", "HEAD"])
# curl flags that make the call more than a fetch of a URL -- they upload a local
# file, write a local file, or read further options from a file. A title cannot
# say what those do, so the command is left raw (unknown).
CURL_LOCAL_IO_FLAGS = frozenset(
    [
        "-T",
        "--upload-file",
        "-o",
        "--output",
        "-O",
        "--remote-name",
        "--remote-name-all",
        "-J",
        "--remote-header-name",
        "--output-dir",
        "--create-dirs",
        "-c",
        "--cookie-jar",
        "-D",
        "--dump-header",
        "-K",
        "--config",
    ]
)
# Short letters of the same flags, so a cluster like ``-sSO`` or ``-fsSLo`` is caught.
_CURL_LOCAL_IO_LETTERS = re.compile(r"[ToOJcDK]")
_CURL_SHORT_CLUSTER = re.compile(r"^-[A-Za-z]{2,}$")
# A single-dash token that is not a pure letter cluster carries an ATTACHED value
# (``-Tsecrets.txt``, ``-o/tmp/x``, ``-d@file``, ``-XPOST``). Which letter owns the
# value cannot be told apart from the letters before it without curl's own option
# table, so any such token naming a local-I/O, body or method letter leaves the
# command raw.
_CURL_ATTACHED_VALUE = re.compile(r"^-[A-Za-z]*[^A-Za-z-]")
_CURL_ATTACHED_GUARD_LETTERS = re.compile(r"[ToOJcDKdFX]")
_CURL_LEADING_LETTERS = re.compile(r"^[A-Za-z]*")
# Flags that send a request body; with no explicit ``-X`` curl sends a POST.
CURL_BODY_FLAGS = frozenset(
    [
        "-d",
        "--data",
        "--data-raw",
        "--data-binary",
        "--data-urlencode",
        "--data-ascii",
        "-F",
        "--form",
        "--form-string",
        "--json",
    ]
)
_CURL_FORM_FILE = re.compile(r"(^|=)[@<]")
# ``--data-urlencode`` forms: ``content`` | ``=content`` | ``name=content`` |
# ``@filename`` | ``name@filename`` -- an ``@`` before any ``=`` reads a file.
_CURL_URLENCODE_FILE = re.compile(r"^[^=]*@")
# `gh` nouns that read as English only in their initialism form.
GH_NOUN_DISPLAY = {"pr": "PR"}
GH_FLAGS_WITH_VALS = [
    "-R",
    "--repo",
    "-F",
    "--field",
    "-f",
    "--raw-field",
    "-H",
    "--header",
    "-q",
    "--jq",
    "-t",
    "--template",
    "-L",
    "--limit",
    "-s",
    "--state",
    "-l",
    "--label",
    "-a",
    "--assignee",
    "-A",
    "--author",
    "-S",
    "--search",
    "-b",
    "--body",
    "-B",
    "--body-file",
    "--title",
    "-X",
    "--method",
    "--json",
]
HOST_RE = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/\s]+@)?([A-Za-z0-9.-]+)(?::[0-9]+)?(?:[/?#]|\Z)"
)


def unknown(tokens: list[str]) -> ToolAction:
    return {"type": "unknown", "cmd": " ".join(tokens)}


def read_action(path: str) -> ToolAction:
    return {"type": "read", "path": path}


def list_action(path: str | None) -> ToolAction:
    return {"type": "list_files", "path": path} if path else {"type": "list_files"}


def search_action(query: str | None, path: str | None, tokens: list[str]) -> ToolAction:
    if query is None:
        return unknown(tokens)
    action: ToolAction = {"type": "search", "query": query}
    if path:
        action["path"] = path
    return action


def head_tail_read(tail: list[str], allow_plus: bool) -> str | None:
    """``head -n 50 file`` / ``head -n50 file`` / ``tail -n +10 file``
    (Codex head/tail branches)."""
    valid: Callable[[str], bool] = is_plus_digits if allow_plus else is_digits
    has_valid_n = False
    first = _at(tail, 0)
    if first == "-n":
        has_valid_n = len(tail) > 1 and valid(tail[1])
    elif first is not None and first.startswith("-n"):
        has_valid_n = valid(first[2:])
    if has_valid_n:
        candidates: list[str] = []
        i = 0
        while i < len(tail):
            if i == 0 and tail[i] == "-n" and i + 1 < len(tail) and valid(tail[i + 1]):
                i += 2
                continue
            candidates.append(tail[i])
            i += 1
        p = next((c for c in candidates if not c.startswith("-")), None)
        if p:
            return p
    if len(tail) == 1 and not tail[0].startswith("-"):
        return tail[0]
    return None


def host_of(url: str) -> str | None:
    m = HOST_RE.match(url)
    if not m:
        return None
    host = m.group(1)
    return host if "." in host or host == "localhost" else None


def classify_git(tokens: list[str]) -> ToolAction:
    # Skip global options (``git -C dir status``) by hand so a later ``--`` survives.
    idx = 1
    while idx < len(tokens) and tokens[idx].startswith("-"):
        t = tokens[idx]
        if t in GIT_GLOBAL_FLAGS_WITH_VALS:
            idx += 2
            continue
        if t in GIT_GLOBAL_FLAGS_BARE or _GIT_DIR_EQ.match(t):
            idx += 1
            continue
        return unknown(tokens)
    if idx >= len(tokens):
        return unknown(tokens)
    sub = tokens[idx]
    sub_tail = tokens[idx + 1 :]
    if sub == "grep":
        a = parse_grep_like(sub_tail)
        return unknown(tokens) if a["type"] == "unknown" else a
    if sub == "ls-files":
        p = first_non_flag_operand(
            sub_tail, ["--exclude", "--exclude-from", "--pathspec-from-file"]
        )
        return list_action(short_display_path(p) if p else None)
    if sub not in GIT_SUBCOMMANDS:
        return unknown(tokens)
    for t in sub_tail:
        if t == "--":
            break
        flag = _long_flag_name(t)
        if flag in GIT_DESTRUCTIVE_FLAGS or flag.startswith("--force-with-lease"):
            return unknown(tokens)
        if flag == "-d" and sub in GIT_REF_DELETING_SUBS:
            return unknown(tokens)
        if _GIT_SHORT_CLUSTER_FD.match(t):  # ``-fd``, ``-Df``
            return unknown(tokens)
        # A refspec can be destructive without any flag: ``git push origin
        # :release`` (empty source = delete the remote branch) and ``git push
        # origin +main`` (leading ``+`` = forced non-fast-forward).
        if sub in GIT_REF_DELETING_SUBS and t[:1] in (":", "+"):
            return unknown(tokens)
    if sub == "stash" and _at(sub_tail, 0) in ("drop", "clear"):
        return unknown(tokens)
    dd = sub_tail.index("--") if "--" in sub_tail else -1
    candidates: list[str] = []
    if sub in GIT_PATHSPEC_SUBS:
        candidates = (
            sub_tail[dd + 1 :]
            if dd >= 0
            else positional_operands(sub_tail, GIT_SUB_FLAGS_WITH_VALS)
        )
    elif sub in GIT_PATHSPEC_AFTER_DD_SUBS and dd >= 0:
        candidates = sub_tail[dd + 1 :]
    # Refs and ranges (``origin/main``, ``a..b``, ``HEAD~2``) are not paths.
    path = next(
        (
            c
            for c in candidates
            if is_pathish(c) and ".." not in c and not GIT_REF_PREFIX_RE.match(c)
        ),
        None,
    )
    action: ToolAction = {"type": "git", "sub": sub}
    if path:
        action["path"] = short_display_path(path)
    return action


def classify_node_pm(tokens: list[str]) -> ToolAction:
    head, tail = tokens[0], tokens[1:]
    ops = positional_operands(tail, [])
    sub = _at(ops, 0)
    if sub is None:
        return {"type": "install"} if head == "yarn" else unknown(tokens)
    if sub in ("install", "i", "add", "ci"):
        return {"type": "install"}
    if sub in ("test", "t"):
        return {"type": "test"}
    if sub in ("run", "run-script"):
        name = _at(ops, 1)
        return {"type": "script", "name": name} if name else unknown(tokens)
    if sub == "build":
        return {"type": "build"}
    if sub == "lint":
        return {"type": "lint"}
    return unknown(tokens)


def classify_tool(tokens: list[str]) -> ToolAction:
    head, tail = tokens[0], tokens[1:]
    ops = positional_operands(tail, [])
    op0 = _at(ops, 0)
    op1 = _at(ops, 1)
    if head in ("pip", "pip3"):
        return {"type": "install"} if op0 == "install" else unknown(tokens)
    if head == "uv":
        if op0 in ("sync", "add") or (op0 == "pip" and op1 == "install"):
            return {"type": "install"}
        if op0 == "run" and op1:
            return {"type": "test"} if op1 in TEST_RUNNERS else {"type": "script", "name": op1}
        return unknown(tokens)
    if head == "poetry":
        if op0 == "install":
            return {"type": "install"}
        if op0 == "run" and op1:
            return {"type": "test"} if op1 in TEST_RUNNERS else {"type": "script", "name": op1}
        return unknown(tokens)
    if head == "cargo":
        if op0 in ("add", "fetch"):
            return {"type": "install"}
        if op0 == "test":
            return {"type": "test"}
        if op0 == "build":
            return {"type": "build"}
        if op0 in ("clippy", "fmt"):
            return {"type": "lint"}
        return unknown(tokens)
    if head == "go":
        if op0 == "test":
            return {"type": "test"}
        if op0 == "build":
            return {"type": "build"}
        if op0 == "vet":
            return {"type": "lint"}
        if op0 == "mod" and op1 in ("download", "tidy"):
            return {"type": "install"}
        return unknown(tokens)
    if head == "make":
        return {"type": "script", "name": op0} if op0 else {"type": "build"}
    if head == "tsc":
        return {"type": "build"}
    return unknown(tokens)


# ``gh api`` flags that turn the call into a mutation: an explicit method, or a
# body (``-f``/``-F`` fields switch gh to POST; ``--input`` sends a file).
GH_API_MUTATING_FLAGS = frozenset(
    ["-X", "--method", "-f", "--raw-field", "-F", "--field", "--input"]
)
# The same short flags with a glued value: ``-XDELETE``, ``-fk=v``, ``-Fk=v``.
_GH_GLUED_MUTATING = re.compile(r"^-[XfF].")


def _long_flag_name(t: str) -> str:
    return t[: t.index("=")] if t.startswith("--") and "=" in t else t


def classify_gh(tokens: list[str]) -> ToolAction:
    if _at(tokens, 1) == "api" and any(
        _long_flag_name(t) in GH_API_MUTATING_FLAGS or _GH_GLUED_MUTATING.match(t)
        for t in tokens[2:]
    ):
        # The title would read ``GitHub API repos`` for a DELETE or a POST with
        # a body; the method and the payload are what an approver needs to see.
        return unknown(tokens)
    ops = positional_operands(tokens[1:], GH_FLAGS_WITH_VALS)
    if not ops:
        return unknown(tokens)
    op1 = _at(ops, 1)
    if ops[0] == "api":
        if not op1:
            return unknown(tokens)
        seg = op1.lstrip(DISPLAY_SEP).split(DISPLAY_SEP)[0]
        return {"type": "github_api", "path": seg} if seg else unknown(tokens)
    if not op1:
        return unknown(tokens)
    action: ToolAction = {"type": "github", "noun": ops[0], "verb": op1}
    target = _at(ops, 2)
    if target:
        action["target"] = target
    return action


def classify_curl(tokens: list[str]) -> ToolAction:
    tail = tokens[1:]
    method: str | None = None
    sends_body = False
    for i, t in enumerate(tail):
        has_eq = t.startswith("--") and "=" in t
        flag = t[: t.index("=")] if has_eq else t
        # Uploads, local writes and option files: the title would hide the half
        # that touches the filesystem, so the command stays raw.
        if flag in CURL_LOCAL_IO_FLAGS:
            return unknown(tokens)
        if _CURL_SHORT_CLUSTER.match(t) and _CURL_LOCAL_IO_LETTERS.search(t[1:]):
            return unknown(tokens)
        if _CURL_ATTACHED_VALUE.match(t):
            m = _CURL_LEADING_LETTERS.match(t[1:])
            if m and _CURL_ATTACHED_GUARD_LETTERS.search(m.group(0)):
                return unknown(tokens)
        if flag in CURL_BODY_FLAGS:
            # ``-d @file`` / ``-F name=@file`` / ``--data-urlencode name@file``
            # read a local file into the body.
            val = t[t.index("=") + 1 :] if has_eq else (_at(tail, i + 1) or "")
            if (
                val.startswith("@")
                or (flag in ("-F", "--form") and _CURL_FORM_FILE.search(val) is not None)
                or (flag == "--data-urlencode" and _CURL_URLENCODE_FILE.match(val) is not None)
            ):
                return unknown(tokens)
            sends_body = True
        if method is None and flag in ("-X", "--request"):
            nxt = t[t.index("=") + 1 :] if has_eq else _at(tail, i + 1)
            method = nxt.upper() if nxt is not None else None
    if method is None and sends_body:
        method = "POST"
    ops = positional_operands(tail, CURL_FLAGS_WITH_VALS)
    for op in ops:
        host = host_of(op)
        if host:
            action: ToolAction = {"type": "fetch", "host": host}
            if method and method not in CURL_FETCH_METHODS:
                action["method"] = method
            return action
    return unknown(tokens)


def summarize_segment(segment: list[str], single: bool) -> ToolAction:
    """Classify one pipeline segment. ``single`` is True when the whole script
    is this one segment (gates the Print rule: a chained ``echo`` is noise, a
    lone ``echo`` is the action)."""
    if not segment:
        return unknown(segment)
    # ``npx vitest ...`` classifies as ``vitest ...`` for the tools we know.
    tokens = (
        segment[1:]
        if segment[0] == "npx" and _at(segment, 1) and segment[1] in NPX_PASSTHROUGH
        else segment
    )
    head, tail = tokens[0], tokens[1:]
    if head in ("ls", "eza", "exa"):
        flags = (
            ["-I", "-w", "--block-size", "--format", "--time-style", "--color", "--quoting-style"]
            if head == "ls"
            else ["-I", "--ignore-glob", "--color", "--sort", "--time-style", "--time"]
        )
        p = first_non_flag_operand(tail, flags)
        return list_action(short_display_path(p) if p else None)
    if head == "tree":
        p = first_non_flag_operand(tail, ["-L", "-P", "-I", "--charset", "--filelimit", "--sort"])
        return list_action(short_display_path(p) if p else None)
    if head == "du":
        p = first_non_flag_operand(
            tail, ["-d", "--max-depth", "-B", "--block-size", "--exclude", "--time-style"]
        )
        return list_action(short_display_path(p) if p else None)
    if head in ("rg", "rga", "ripgrep-all"):
        # ``--pre <cmd>`` runs a command over every file; the search title would hide it.
        if rg_has_preprocessor(tail):
            return unknown(tokens)
        has_files = "--files" in tail
        non_flags = [
            a
            for a in skip_flag_values(
                tail,
                [
                    "-g",
                    "--glob",
                    "--iglob",
                    "-t",
                    "--type",
                    "--type-add",
                    "--type-not",
                    "-m",
                    "--max-count",
                    "-A",
                    "-B",
                    "-C",
                    "--context",
                    "--max-depth",
                    "-e",
                    "--regexp",
                ],
            )
            if not a.startswith("-")
        ]
        if has_files:
            first = _at(non_flags, 0)
            return list_action(short_display_path(first) if first else None)
        # ``-e PATTERN`` is the pattern; otherwise the first operand is.
        e_idx = next((i for i, a in enumerate(tail) if a in ("-e", "--regexp")), -1)
        query = _at(tail, e_idx + 1) if e_idx >= 0 else _at(non_flags, 0)
        path = _at(non_flags, 0) if e_idx >= 0 else _at(non_flags, 1)
        return search_action(query, short_display_path(path) if path else None, tokens)
    if head == "git":
        return classify_git(tokens)
    if head == "fd":
        # ``-x``/``--exec``/``-X``/``--exec-batch`` run a command per match; the
        # same rule as ``find -exec``.
        if any(
            t in ("-x", "-X", "--exec", "--exec-batch")
            or t.startswith("--exec=")
            or t.startswith("--exec-batch=")
            for t in tail
        ):
            return unknown(tokens)
        query, path = parse_fd_query_and_path(tail)
        if query is not None:
            action: ToolAction = {"type": "search", "query": query}
            if path:
                action["path"] = path
            return action
        return list_action(path)
    if head == "find":
        if find_mutates(tail):
            return unknown(tokens)
        query, path = parse_find_query_and_path(tail)
        if query is not None:
            action = {"type": "search", "query": query}
            if path:
                action["path"] = path
            return action
        return list_action(path)
    if head in ("grep", "egrep", "fgrep"):
        a = parse_grep_like(tail)
        return unknown(tokens) if a["type"] == "unknown" else a
    if head in ("ag", "ack", "pt"):
        non_flags = [
            a
            for a in skip_flag_values(
                tail,
                [
                    "-G",
                    "-g",
                    "--file-search-regex",
                    "--ignore-dir",
                    "--ignore-file",
                    "--path-to-ignore",
                ],
            )
            if not a.startswith("-")
        ]
        second = _at(non_flags, 1)
        return search_action(
            _at(non_flags, 0), short_display_path(second) if second else None, tokens
        )
    if head == "cat":
        p = single_non_flag_operand(tail, [])
        return read_action(p) if p else unknown(tokens)
    if head in ("bat", "batcat"):
        p = single_non_flag_operand(
            tail,
            [
                "--theme",
                "--language",
                "--style",
                "--terminal-width",
                "--tabs",
                "--line-range",
                "--map-syntax",
            ],
        )
        return read_action(p) if p else unknown(tokens)
    if head == "less":
        p = single_non_flag_operand(
            tail,
            [
                "-p",
                "-P",
                "-x",
                "-y",
                "-z",
                "-j",
                "--pattern",
                "--prompt",
                "--tabs",
                "--shift",
                "--jump-target",
            ],
        )
        return read_action(p) if p else unknown(tokens)
    if head == "more":
        p = single_non_flag_operand(tail, [])
        return read_action(p) if p else unknown(tokens)
    if head == "head":
        p = head_tail_read(tail, False)
        return read_action(p) if p else unknown(tokens)
    if head == "tail":
        p = head_tail_read(tail, True)
        return read_action(p) if p else unknown(tokens)
    if head == "awk":
        p = awk_data_file_operand(tail)
        return read_action(p) if p else unknown(tokens)
    if head == "nl":
        p = next(
            (
                a
                for a in skip_flag_values(tail, ["-s", "-w", "-v", "-i", "-b"])
                if not a.startswith("-")
            ),
            None,
        )
        return read_action(p) if p else unknown(tokens)
    if head == "sed":
        p = sed_read_path(tail)
        return read_action(p) if p else unknown(tokens)
    if head in ("npm", "pnpm", "yarn", "bun"):
        return classify_node_pm(tokens)
    if head in ("pytest", "vitest", "jest", "mocha"):
        return {"type": "test"}
    if head == "gh":
        return classify_gh(tokens)
    if head == "curl":
        return classify_curl(tokens)
    if head in ("echo", "printf"):
        return {"type": "print"} if single else unknown(tokens)
    if is_python_command(head):
        if _at(tail, 0) == "-m" and _at(tail, 1) == "pytest":
            return {"type": "test"}
        # ``python -c <script>`` is arbitrary code; a marker match on the script
        # text says nothing about what else it does. Raw.
        return unknown(tokens)
    if head in LINTERS:
        return {"type": "lint"}
    if head in NODE_PMS:
        return classify_node_pm(tokens)
    return classify_tool(tokens)


# ---------------------------------------------------------------------------
# Shell -- whole-script classification (Codex ``parse_shell_script`` + ``simplify_once``)
# ---------------------------------------------------------------------------


def strip_wrapper(segments: list[list[str]]) -> list[list[str]] | None:
    if len(segments) == 1:
        seg = segments[0]
        if len(seg) == 3 and seg[0] in ("bash", "zsh", "sh") and seg[1] in ("-c", "-lc"):
            inner = tokenize_shell(seg[2])
            return strip_wrapper(split_segments(inner)) if inner is not None else None
    if len(segments) >= 2 and len(segments[0]) == 1 and segments[0][0] in ("yes", "y", "no", "n"):
        return segments[1:]
    return segments


def has_env_prefix(seg: list[str]) -> bool:
    """True when the segment opens with ``VAR=value`` assignments."""
    return bool(seg) and ENV_ASSIGN_RE.match(seg[0]) is not None


def same_action(a: ToolAction, b: ToolAction) -> bool:
    return a == b


_ECHO_RE = re.compile(r"^echo(\s|\Z)")
_CD_RE = re.compile(r"^cd(\s|\Z)")
_NL_RE = re.compile(r"nl(\s+-\S+)*")


def _find_index(actions: list[ToolAction], pred: Callable[[ToolAction], bool]) -> int:
    return next((i for i, a in enumerate(actions) if pred(a)), -1)


def simplify_once(actions: list[ToolAction]) -> list[ToolAction] | None:
    """Codex ``simplify_once``: drop a leading ``echo``, a ``cd`` that is
    followed by something, ``|| true``, and a bare ``nl -flags``."""
    if len(actions) <= 1:
        return None
    first = actions[0]
    if first["type"] == "unknown" and _ECHO_RE.match(first["cmd"]):
        return actions[1:]
    cd_idx = _find_index(actions, lambda a: a["type"] == "unknown" and bool(_CD_RE.match(a["cmd"])))
    if cd_idx >= 0 and len(actions) > cd_idx + 1:
        return actions[:cd_idx] + actions[cd_idx + 1 :]
    true_idx = _find_index(actions, lambda a: a["type"] == "unknown" and a["cmd"] == "true")
    if true_idx >= 0:
        return actions[:true_idx] + actions[true_idx + 1 :]
    nl_idx = _find_index(
        actions, lambda a: a["type"] == "unknown" and _NL_RE.fullmatch(a["cmd"]) is not None
    )
    if nl_idx >= 0:
        return actions[:nl_idx] + actions[nl_idx + 1 :]
    return None


def classify_shell_command(script: str) -> tuple[ToolAction, int] | None:
    """Classify a shell script into its main action plus a count of further
    parsed actions, or None when the script is not fully parseable (the caller
    then shows the raw command)."""
    tokens = tokenize_shell(script)
    if tokens is None:
        return None
    segments = strip_wrapper(split_segments(tokens))
    if not segments:
        return None
    # An environment prefix can change what the command does (``LD_PRELOAD=``,
    # ``PATH=``, ``GIT_DIR=``); a title that drops it would say ``List files``
    # for a command that also loads a library. Any prefix leaves the command raw.
    if any(has_env_prefix(seg) for seg in segments):
        return None
    multi = len(segments) > 1
    if multi:
        segments = [s for s in segments if not is_small_formatting_command(s)]
    if not segments:
        return None

    actions: list[ToolAction] = []
    cwd: str | None = None
    for seg in segments:
        if seg[0] == "cd":
            d = cd_target(seg[1:])
            if d:
                cwd = join_paths(cwd, d) if cwd else d
            continue
        action = summarize_segment(seg, not multi)
        if action["type"] == "read" and cwd:
            action = {**action, "path": join_paths(cwd, action["path"])}
        actions.append(action)
    if len(actions) > 1:
        actions = [a for a in actions if not (a["type"] == "unknown" and a["cmd"] == "true")]
        nxt = simplify_once(actions)
        while nxt is not None:
            actions = nxt
            nxt = simplify_once(actions)
    # Collapse consecutive duplicates (Codex ``parse_command``).
    deduped: list[ToolAction] = []
    for a in actions:
        if not deduped or not same_action(deduped[-1], a):
            deduped.append(a)
    if not deduped or any(a["type"] == "unknown" for a in deduped):
        return None
    # Shell reads show the short display name, like Codex's ``Read file`` title.
    main = deduped[0]
    if main["type"] == "read":
        main = {**main, "path": short_display_path(main["path"])}
    return main, len(deduped) - 1

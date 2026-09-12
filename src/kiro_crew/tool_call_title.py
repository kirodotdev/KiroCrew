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
the cwd that later relative paths resolve against; small formatting helpers are
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
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ToolAction = dict[str, Any]
Tok = dict[str, str]


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
# Tokenizer -- Codex's "word-only commands sequence" grammar
# ---------------------------------------------------------------------------

# Characters that make an unquoted word non-literal (expansion, glob, escape,
# brace, tilde, comment, history) -- Codex ``is_literal_word_or_number``.
BARE_REJECT = frozenset(["{", "}", "*", "?", "[", "]", "\\", "~", "^", "#", "$", "`"])
# stderr-silencing redirects that change nothing about what a command DOES;
# stripped before parsing (display-only extension past Codex).
NOISE_REDIRECT_RE = re.compile(r"(?:^|\s)(?:2>&1|[12]?>>?\s*/dev/null|&>\s*/dev/null)(?=\s|\Z)")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=", re.ASCII)


def tokenize_shell(script: str) -> list[Tok] | None:
    """Tokenize a shell script into words and connectors, or None when the
    script uses anything outside the plain-words grammar (redirects, ``$VAR``,
    ``$(...)``, globs, subshells, heredocs, backgrounding, comments)."""
    src = NOISE_REDIRECT_RE.sub(" ", script)
    out: list[Tok] = []
    word = ""
    has_word = False
    i = 0
    n = len(src)

    def flush() -> None:
        nonlocal word, has_word
        if has_word:
            out.append({"word": word})
        word = ""
        has_word = False

    while i < n:
        ch = src[i]
        if ch in (" ", "\t", "\r"):
            flush()
            i += 1
            continue
        if ch == "\n":
            flush()
            out.append({"op": ";"})
            i += 1
            continue
        if ch in ("&", "|", ";"):
            flush()
            two = src[i : i + 2]
            if two in ("&&", "||"):
                out.append({"op": two})
                i += 2
                continue
            if ch == "&":
                return None  # backgrounding / redirect fragment
            out.append({"op": ch})
            i += 1
            continue
        if ch in ("<", ">", "(", ")"):
            return None
        if ch == "'":
            end = src.find("'", i + 1)
            if end < 0:
                return None
            word += src[i + 1 : end]
            has_word = True
            i = end + 1
            continue
        if ch == '"':
            j = i + 1
            buf = ""
            closed = False
            while j < n:
                c = src[j]
                if c == '"':
                    closed = True
                    break
                if c in ("$", "`"):
                    return None  # expansion inside quotes
                if c == "\\" and j + 1 < n and src[j + 1] in '"\\':
                    buf += src[j + 1]
                    j += 2
                    continue
                if c == "\\":
                    return None
                buf += c
                j += 1
            if not closed:
                return None
            word += buf
            has_word = True
            i = j + 1
            continue
        if ch in BARE_REJECT:
            return None
        word += ch
        has_word = True
        i += 1
    flush()
    return out


def split_segments(tokens: list[Tok]) -> list[list[str]]:
    segs: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if "op" in t:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t["word"])
    if cur:
        segs.append(cur)
    return segs


# ---------------------------------------------------------------------------
# Shell helpers -- ports of the Codex helper set
# ---------------------------------------------------------------------------

SHORT_PATH_SKIP = frozenset(["build", "dist", "node_modules", "src"])
# Separator of the paths being DISPLAYED. They come out of a shell command
# string or a tool argument and are normalised to forward slashes for the
# title, on every host OS -- this is text about a path, not a filesystem
# call, so ``os.path`` (which would emit backslashes on Windows and change the
# title) is deliberately not used.
DISPLAY_SEP = "/"


def short_display_path(path: str) -> str:
    """Last path component, skipping ``build``/``dist``/``node_modules``/``src``
    (Codex ``short_display_path``): ``webview/src`` -> ``webview``,
    ``packages/app/node_modules/`` -> ``app``."""
    trimmed = path.replace("\\", "/").rstrip("/")
    parts = [p for p in trimmed.split(DISPLAY_SEP) if p and p not in SHORT_PATH_SKIP]
    return parts[-1] if parts else trimmed


def _at(items: list[str], i: int) -> str | None:
    """``items[i]`` or None, like a JS out-of-range index."""
    return items[i] if 0 <= i < len(items) else None


def is_digits(s: str | None) -> bool:
    return bool(s) and re.fullmatch(r"[0-9]+", s or "") is not None


def is_pathish(s: str) -> bool:
    return (
        s == "." or s == ".." or s.startswith("./") or s.startswith("../") or "/" in s or "\\" in s
    )


def is_abs_like(p: str) -> bool:
    return p.startswith("/") or re.match(r"^[A-Za-z]:\\", p) is not None or p.startswith("\\\\")


def join_paths(base: str, rel: str) -> str:
    if is_abs_like(rel) or not base:
        return rel
    return base.rstrip(DISPLAY_SEP) + DISPLAY_SEP + rel


def skip_flag_values(args: list[str], flags_with_vals: list[str]) -> list[str]:
    """Skip values consumed by ``flags_with_vals`` and ``--flag=value`` forms;
    ``--`` passes everything after it through (Codex ``skip_flag_values``)."""
    out: list[str] = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--":
            out.extend(args[i + 1 :])
            break
        if a.startswith("--") and "=" in a:
            continue
        if a in flags_with_vals:
            if i + 1 < len(args):
                skip_next = True
            continue
        out.append(a)
    return out


def positional_operands(args: list[str], flags_with_vals: list[str]) -> list[str]:
    """Non-flag operands after flag-value skipping (Codex ``positional_operands``)."""
    out: list[str] = []
    after_dd = False
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if after_dd:
            out.append(a)
            continue
        if a == "--":
            after_dd = True
            continue
        if a.startswith("--") and "=" in a:
            continue
        if a in flags_with_vals:
            if i + 1 < len(args):
                skip_next = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def first_non_flag_operand(args: list[str], flags_with_vals: list[str]) -> str | None:
    return _at(positional_operands(args, flags_with_vals), 0)


def single_non_flag_operand(args: list[str], flags_with_vals: list[str]) -> str | None:
    ops = positional_operands(args, flags_with_vals)
    return ops[0] if len(ops) == 1 else None


# sed flags a read-only invocation may carry. Anything else (``-i``, ``-f``, ``-s``,
# ``--debug``, an unknown flag) leaves the command raw: the allowlist is the rule.
SED_PURE_FLAGS = frozenset(
    [
        "-n",
        "--quiet",
        "--silent",
        "-E",
        "-r",
        "--regexp-extended",
        "-z",
        "--null-data",
        "-u",
        "--unbuffered",
    ]
)
# A sed script that only selects or prints lines: ``12p``, ``10,20p``, ``$p``, ``3d``.
SED_RANGE_SCRIPT_RE = re.compile(r"^(\d+|\$)(,(\d+|\$))?[pd]$")
_SED_DELIM_FORBIDDEN = re.compile(r"[A-Za-z0-9\\\s]")
_SED_SUBST_FLAGS = re.compile(r"^[gIip0-9]*$")


def is_sed_substitute_script(script: str) -> bool:
    """True for a substitute ``s<d>pattern<d>replacement<d>flags`` with a
    non-alphanumeric delimiter and only g/i/I/p/number flags -- never ``w file``
    or ``e``, the two flags through which ``s`` leaves the stream. A linear walk
    (a backslash escapes the next character) rather than a regex: an alternation
    of ``\\.`` and ``[^d]`` backtracks exponentially on runs of ``\a``."""
    if len(script) < 4 or script[0] != "s":
        return False
    d = script[1]
    if _SED_DELIM_FORBIDDEN.match(d):
        return False
    i = 2
    delims = 0
    while i < len(script) and delims < 2:
        c = script[i]
        if c == "\\":
            i += 2
            continue
        if c == d:
            delims += 1
        i += 1
    if delims != 2:
        return False
    return _SED_SUBST_FLAGS.match(script[i:]) is not None


_SED_SHORT_CLUSTER = re.compile(r"^-[nErzu]+$")
_SED_SHORT_CLUSTER_E = re.compile(r"^-[nErzu]*e$")


def parse_sed_pure(args: list[str]) -> tuple[list[str], list[str], bool] | None:
    """(scripts, files, quiet) for a sed invocation, or None when a flag outside the
    allowlist, a script file, or a non-pure script is present."""
    scripts: list[str] = []
    operands: list[str] = []
    quiet = False
    after_dd = False
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if after_dd:
            operands.append(a)
            continue
        if a == "--":
            after_dd = True
            continue
        if a in ("-e", "--expression"):
            if i >= len(args):
                return None
            scripts.append(args[i])
            i += 1
            continue
        if a.startswith("--expression="):
            scripts.append(a[len("--expression=") :])
            continue
        if a.startswith("-"):
            if a in ("-n", "--quiet", "--silent"):
                quiet = True
            if a in SED_PURE_FLAGS:
                continue
            if _SED_SHORT_CLUSTER.match(a):
                if "n" in a:
                    quiet = True
                continue
            if _SED_SHORT_CLUSTER_E.match(a):
                if "n" in a:
                    quiet = True
                if i >= len(args):
                    return None
                scripts.append(args[i])
                i += 1
                continue
            return None
        operands.append(a)
    if not scripts:
        if not operands:
            return None
        scripts.append(operands.pop(0))
    for sc in scripts:
        if not SED_RANGE_SCRIPT_RE.match(sc) and not is_sed_substitute_script(sc):
            return None
    return scripts, operands, quiet


def sed_read_path(args: list[str]) -> str | None:
    """``sed -n <range>p file``: a read of ``file``. Requires ``-n`` and exactly one
    range-print script."""
    parsed = parse_sed_pure(args)
    if parsed is None:
        return None
    scripts, files, quiet = parsed
    if not quiet or len(files) != 1 or len(scripts) != 1:
        return None
    if not scripts[0].endswith("p") or not SED_RANGE_SCRIPT_RE.match(scripts[0]):
        return None
    return files[0]


def sed_is_pure_filter(args: list[str]) -> bool:
    """sed as a pipeline filter: pure scripts and no file operand."""
    parsed = parse_sed_pure(args)
    return parsed is not None and not parsed[1]


# awk's whole surface beyond its input: ``system()``, ``getline`` (from a file or a
# command), output redirection and pipes (``>``, ``>>``, ``|``), ``close``/``fflush``.
# A program using any of them is code, not a filter, and the command stays raw.
AWK_SIDE_EFFECT_RE = re.compile(r"system\s*\(|getline|[>|@]|close\s*\(|fflush\s*\(")
# Every function an awk filter program may CALL. A call to anything else -- a user
# function, a gawk extension -- makes the program code, not a filter.
AWK_PURE_BUILTINS = frozenset(
    [
        "print",
        "printf",
        "length",
        "substr",
        "split",
        "index",
        "match",
        "sub",
        "gsub",
        "sprintf",
        "toupper",
        "tolower",
        "int",
        "sqrt",
        "exp",
        "log",
        "sin",
        "cos",
        "atan2",
        "rand",
        "strtonum",
        "if",
        "while",
        "for",
    ]
)
_AWK_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def awk_program_is_pure(program: str) -> bool:
    if AWK_SIDE_EFFECT_RE.search(program):
        return False
    return all(m.group(1) in AWK_PURE_BUILTINS for m in _AWK_CALL_RE.finditer(program))


def _awk_has_script_file(args: list[str]) -> bool:
    return any(a in ("-f", "--file") or a.startswith("--file=") for a in args)


def _awk_non_flags(args: list[str]) -> list[str]:
    return [
        a
        for a in skip_flag_values(args, ["-F", "-v", "--field-separator", "--assign"])
        if not a.startswith("-")
    ]


def awk_data_file_operand(args: list[str]) -> str | None:
    if not args:
        return None
    # A program read from a file cannot be inspected; it is not a read of the data file.
    if _awk_has_script_file(args):
        return None
    non_flags = _awk_non_flags(args)
    if len(non_flags) < 2:
        return None
    if not awk_program_is_pure(non_flags[0]):
        return None
    return non_flags[1]


def awk_is_pure_filter(args: list[str]) -> bool:
    """True when awk is a pure filter: an inline program with no side-effect construct."""
    if _awk_has_script_file(args):
        return False
    non_flags = _awk_non_flags(args)
    return len(non_flags) == 1 and awk_program_is_pure(non_flags[0])


def is_python_command(cmd: str) -> bool:
    return (
        cmd in ("python", "python2", "python3")
        or cmd.startswith("python2.")
        or cmd.startswith("python3.")
    )


def cd_target(args: list[str]) -> str | None:
    target: str | None = None
    for i, a in enumerate(args):
        if a == "--":
            return _at(args, i + 1)
        if a in ("-L", "-P") or a.startswith("-"):
            continue
        target = a
    return target


_GREP_VALUE_FLAGS = (
    "-m",
    "--max-count",
    "-C",
    "--context",
    "-A",
    "--after-context",
    "-B",
    "--before-context",
)


def parse_grep_like(args: list[str]) -> ToolAction:
    operands: list[str] = []
    pattern: str | None = None
    after_dd = False
    i = 0
    while i < len(args):
        a = args[i]
        if after_dd:
            operands.append(a)
            i += 1
            continue
        if a == "--":
            after_dd = True
            i += 1
            continue
        if a in ("-e", "--regexp", "-f", "--file"):
            if i + 1 < len(args) and pattern is None:
                pattern = args[i + 1]
            i += 2
            continue
        if a in _GREP_VALUE_FLAGS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        operands.append(a)
        i += 1
    has_pattern = pattern is not None
    query = pattern if has_pattern else _at(operands, 0)
    path_idx = 0 if has_pattern else 1
    path = _at(operands, path_idx)
    if query is None:
        return {"type": "unknown", "cmd": ""}
    action: ToolAction = {"type": "search", "query": query}
    if path:
        action["path"] = short_display_path(path)
    return action


def parse_fd_query_and_path(tail: list[str]) -> tuple[str | None, str | None]:
    non_flags = [
        a
        for a in skip_flag_values(
            tail, ["-t", "--type", "-e", "--extension", "-E", "--exclude", "--search-path"]
        )
        if not a.startswith("-")
    ]
    if len(non_flags) == 1:
        if is_pathish(non_flags[0]):
            return None, short_display_path(non_flags[0])
        return non_flags[0], None
    if len(non_flags) >= 2:
        return non_flags[0], short_display_path(non_flags[1])
    return None, None


# ``find`` actions that mutate or run arbitrary commands; such a ``find`` is
# never a search.
FIND_MUTATING_ACTIONS = frozenset(
    ["-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"]
)


def find_mutates(tail: list[str]) -> bool:
    return any(a in FIND_MUTATING_ACTIONS for a in tail)


def parse_find_query_and_path(tail: list[str]) -> tuple[str | None, str | None]:
    path: str | None = None
    for a in tail:
        if not a.startswith("-") and a not in ("!", "(", ")"):
            path = short_display_path(a)
            break
    query: str | None = None
    for i, a in enumerate(tail):
        if a in ("-name", "-iname", "-path", "-regex"):
            if i + 1 < len(tail):
                query = tail[i + 1]
            break
    return query, path


# ---------------------------------------------------------------------------
# Shell helpers -- formatting-helper detection (Codex ``is_small_formatting_command``)
# ---------------------------------------------------------------------------

# ``tee`` is deliberately absent: it writes its operands, so a pipeline ending
# in ``tee out.txt`` is not a formatting tail and must fall back to the raw
# command.
ALWAYS_FORMATTING = frozenset(["wc", "tr", "cut", "uniq", "column", "yes", "printf"])

# Commands ``xargs`` may fan out to and still count as a formatting tail. Every
# entry only reads its operands. Anything else (``rm``, ``mv``, ``chmod``,
# ``git``, an unknown binary) keeps the ``xargs`` segment in the pipeline, where
# it classifies as Unknown and drops the whole command to the raw fallback.
XARGS_READ_ONLY_TARGETS = frozenset(
    [
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "wc",
        "head",
        "tail",
        "ls",
        "stat",
        "file",
        "echo",
        "sort",
        "uniq",
        "cut",
        "tr",
        "column",
        "md5sum",
        "sha256sum",
        "du",
    ]
)


def sort_writes_file(args: list[str]) -> bool:
    return any(
        a == "-o"
        or a.startswith("--output")
        or (a.startswith("-") and not a.startswith("--") and len(a) > 1 and "o" in a[1:])
        for a in args
    )


def is_plus_digits(s: str) -> bool:
    return is_digits(s[1:]) if s.startswith("+") else is_digits(s)


def rg_has_preprocessor(args: list[str]) -> bool:
    """ripgrep's exec-capable options: a preprocessor command run per file."""
    return any(
        a in ("--pre", "--pre-glob") or a.startswith("--pre=") or a.startswith("--pre-glob=")
        for a in args
    )


def xargs_subcommand(tokens: list[str]) -> list[str] | None:
    if _at(tokens, 0) != "xargs":
        return None
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            return tokens[i + 1 :] if len(tokens) > i + 1 else None
        if not t.startswith("-"):
            return tokens[i:]
        takes_value = t in ("-E", "-e", "-I", "-L", "-n", "-P", "-s")
        i += 2 if takes_value and len(t) == 2 else 1
    return None


def is_mutating_xargs(tokens: list[str]) -> bool:
    """True unless the ``xargs`` target is a read-only command used read-only.

    A bare ``xargs`` (implicit ``echo``) is not mutating; a target outside
    ``XARGS_READ_ONLY_TARGETS`` is, whatever its flags -- the allowlist is the
    rule, not a denylist of known-destructive names.
    """
    sub = xargs_subcommand(tokens)
    if not sub:
        return False
    head, tail = sub[0], sub[1:]
    if head not in XARGS_READ_ONLY_TARGETS:
        # No interpreter (sed, awk, perl, ruby, python, node) is ever an xargs
        # target: the program it runs is code, and this is where a title would hide it.
        return True
    if head == "rg":
        return "--replace" in tail or "-r" in tail or rg_has_preprocessor(tail)
    if head == "sort":
        return sort_writes_file(tail)
    return False


def is_small_formatting_command(tokens: list[str]) -> bool:
    if not tokens:
        return False
    cmd = tokens[0]
    if cmd in ALWAYS_FORMATTING:
        return True
    if cmd == "sort":
        return not sort_writes_file(tokens[1:])
    if cmd == "xargs":
        return not is_mutating_xargs(tokens)
    if cmd == "awk":
        return awk_is_pure_filter(tokens[1:])
    if cmd == "head":
        if len(tokens) == 1:
            return True
        if len(tokens) == 2:
            return tokens[1].startswith("-")
        if len(tokens) == 3 and tokens[1] in ("-n", "-c") and is_digits(tokens[2]):
            return True
        return False
    if cmd == "tail":
        if len(tokens) == 1:
            return True
        if len(tokens) == 2:
            return tokens[1].startswith("-")
        if len(tokens) == 3 and tokens[1] in ("-n", "-c") and is_plus_digits(tokens[2]):
            return True
        return False
    if cmd == "sed":
        return sed_is_pure_filter(tokens[1:])
    return False


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
            # ``-d @file`` / ``-F name=@file`` read a local file into the body.
            val = t[t.index("=") + 1 :] if has_eq else (_at(tail, i + 1) or "")
            if val.startswith("@") or (
                flag in ("-F", "--form") and _CURL_FORM_FILE.search(val) is not None
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


def backend_shell_description(inp: _Input) -> str | None:
    """R0.0 -- the backend's own description of a shell call, when the transport
    sent one as the title; None otherwise. Mirror of ``backendShellDescription``.

    A shell title is a stub that names no command (``shell``, ``Run Command``),
    the command itself (kiro-cli's ``Running: <cmd>``, the gateway's
    ``_select_tool_title`` putting the command on the row), or a sentence the
    model wrote for this call (KAS with kiro-team/kiro-agent#2753,
    claude-agent-acp's Bash ``description``). Only the third is a description: a
    non-empty, non-stub title that differs from ``raw_input["command"]`` once
    whitespace is collapsed. With no command in the arguments the title is taken
    AS the command and never as a description.

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
        label = humanize_tool_name(action["tool"])
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

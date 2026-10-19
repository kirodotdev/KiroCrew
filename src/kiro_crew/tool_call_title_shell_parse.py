"""The word-level half of the shell-command classifier: tokenizer, operands, read-only grammars.

A port of the first half of Codex's ``parse_command.rs``, and with
:mod:`kiro_crew.tool_call_title_shell_classify` the Python mirror of
``website/src/utils/shellCommandParse.ts``. It reads a script the way the
classifier needs it: the word-only tokenizer and the connectors that split it into
segments, the path and operand helpers, and the positive read-only grammars that
decide when an interpreter or an exec-capable tool may still be templated
(``parse_sed_pure``, ``awk_program_is_pure``, the grep/fd/find query readers, the
``xargs``/``sort``/``rg`` exec-option checks and the formatting-tail test). It also
holds the ``ToolAction`` / ``Tok`` shapes every layer passes.

:mod:`kiro_crew.tool_call_title` stays the import path for every name here; that
module's docstring states the rule set and its change policy. This module imports no
other part of the tool-call title family.
"""

from __future__ import annotations

import re
from typing import Any

ToolAction = dict[str, Any]
Tok = dict[str, str]


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

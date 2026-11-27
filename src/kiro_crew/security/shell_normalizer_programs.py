"""Shell text reader: the program vocabulary that attribution reads.

The sets of program names :mod:`kiro_crew.security.shell_normalizer` treats
specially: nested shells and their verbs, environment splitters, data consumers whose
argv is a mention rather than an invocation, and the Python interpreter spellings and
the flags that take an operand or an inline program, kept in one module so a program is
added in one place; ``shell_normalizer`` reads it and re-exports every name. It also holds
the program-word reader every floor shares: ``_program_basename`` (the program a word
invokes), the wrapper/keyword skip (``_WRAPPERS``, ``_program_index``) and the argv[0]
walks (``_program_positions``, ``_argv_programs``).
"""

from __future__ import annotations

import os
import re

from ..trust_patterns import ENV_ASSIGNMENT_RE
from .call_views import once_per_call
from .shell_normalizer_walk import (
    _CONTROL_OPERATOR_RE,
    _EMPTY_SUBST_RE,
    _KEEPS_COMMAND_POSITION,
    _SHELL_WRAPPER_CHARS,
    _ends_argv,
    _push_token_redirection,
    _resolve_param_defaults,
    _strip_redirect,
    _substitution_program,
)

#: Interpreter names that accept ``-m <module>``. Versioned spellings (``python3``,
#: ``python3.12``) and the Windows launcher included; ``.exe`` is stripped by
#: ``_program_basename`` before this is applied.
_PYTHON_PROGRAM_RE = re.compile(r"\Apy(?:thon)?[0-9.]*(?:\.exe)?\Z")


#: Interpreter flags that consume the NEXT token as their operand. Their operand must be
#: skipped when scanning for ``-m <module>``, or it terminates the scan and the mint slips
#: through (``python -X dev -m kiro_crew token``). ``-c`` and ``-m`` are deliberately absent:
#: both END the option list, and `-m` is what this scan is looking for.
#:
#: LOWERCASE, because the floor runs over an already-lowercased command (`_is_credential_mint`
#: takes `text_lower`), so a `-X` in the operator's shell reaches this set as `-x`. Storing the
#: uppercase spelling made every separate-operand form match nothing — the bypass stayed open
#: while the ATTACHED spellings (`-Xdev`) passed, which is the shape of a fix that looks tested.
#: Python's real flags are case-sensitive (`-x` skips the first line, `-X` sets an
#: implementation option), so this over-matches `-x` slightly: `python -x -m kiro_crew token`
#: would skip `-m` as an operand and MISS. Guarded by also treating a bare `-m` as the marker
#: on the next iteration — see the loop.
_PYTHON_OPERAND_FLAGS = frozenset({"-x", "-w", "-q", "--check-hash-based-pycs"})


#: Interpreter flags that take an INLINE PROGRAM as their operand: ``-c`` a statement string,
#: ``-`` / no flag a stdin script. ``python -c "from kiro_crew.cli import main; main()" token``
#: mints the identical token as ``python -m kiro_crew token`` — the payload is one argv word
#: carrying the import name, so the ``-m`` marker scan never fires and the "not a flag ⇒ not
#: the module shape" bail treated the payload as a script name and returned False. Same escape,
#: one flag over.
_PYTHON_INLINE_PROGRAM_FLAGS = ("-c",)


# Programs whose ``-c`` argument is a shell script: its text is a COMMAND, so a
# self-protection check has to look inside it rather than treat it as an operand.
_NESTED_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"})


_NESTED_SHELL_VERBS = frozenset({"eval", "source", "."})


_ENV_SPLIT_PROGRAMS = frozenset({"env"})


# product name appearing in their argv is a mention, not an invocation:
# ``echo kirocrew token`` prints two words.
#
# This list is deliberately a DENYLIST of data consumers rather than an ALLOWLIST
# of executors, because the two fail in opposite directions.  Many commands pass
# their remaining argv to an executor -- ``ssh host …``, ``docker exec c …``,
# ``sudo``, ``env``, ``nohup``, ``timeout``, ``runuser``, ``chroot``, ``pkexec``,
# ``systemd-run``, ``nice``, ``xargs`` -- and enumerating THOSE means a forgotten
# entry is a silent BYPASS.  Enumerating data consumers instead means a forgotten
# entry is a false positive: annoying, visible, and safe.  So the default for an
# unrecognised program is "this could execute the name".
_DATA_CONSUMER_PROGRAMS = frozenset(
    {
        # filesystem inspectors: every argument is a path to DESCRIBE, so a glob
        # or brace word among them (``ls -d dir/*``) is a filename, not a program.
        "ls",
        "stat",
        "file",
        "du",
        "readlink",
        "realpath",
        "dirname",
        "basename",
        # filesystem movers: every argument is a path to COPY, MOVE, LINK, REMOVE or
        # re-mode, never a program to run (``cp $dir/*; ...``).  ``tar`` is absent on
        # purpose: ``-I <prog>`` / ``--use-compress-program`` executes its argument.
        "cp",
        "mv",
        "ln",
        "rm",
        "mkdir",
        "rmdir",
        "touch",
        "chmod",
        "chown",
        "echo",
        "printf",
        "print",
        "cat",
        "tac",
        "tee",
        "head",
        "tail",
        "less",
        "more",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "ack",
        "sed",
        "awk",
        "cut",
        "tr",
        "sort",
        "uniq",
        "wc",
        "nl",
        "fold",
        "column",
        "comm",
        "diff",
        "strings",
        "jq",
        "yq",
        "base64",
        "md5sum",
        "sha256sum",
        "xxd",
        "od",
    }
)


#: Command wrappers whose own option words are stepped over to reach the program they
#: run, as ``(value-taking option flags, trailing positional words)``. Defined here with
#: ``_program_index`` so ``_program_positions`` and ``argv_rules`` share ONE wrapper/
#: keyword skip; a word that takes a value leaves the value read as the program, which
#: only misses.
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "sudo": (
        frozenset(
            {
                *("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-T", "-D", "-R"),
                *("--user", "--group", "--close-from", "--host", "--prompt"),
                *("--other-user", "--role", "--type", "--chdir", "--chroot"),
            }
        ),
        0,
    ),
    "doas": (frozenset({"-u", "-C"}), 0),
    "pkexec": (frozenset({"--user"}), 0),
    "runuser": (frozenset({"-u", "-g", "-G", "--user", "--group"}), 0),
    "env": (frozenset({"-u", "-U", "-C", "--unset", "--chdir"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "nohup": (frozenset(), 0),
    "setsid": (frozenset(), 0),
    "time": (frozenset({"-f", "-o", "--format", "--output"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "ionice": (frozenset({"-c", "-n", "-p", "--class", "--classdata"}), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1),
    "chroot": (frozenset({"--userspec", "--groups"}), 1),
    "nsenter": (frozenset({"-t", "--target", "-S", "--setuid", "-G", "--setgid"}), 0),
    "unshare": (frozenset({"--propagation", "--setgroups"}), 0),
    "flock": (frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}), 1),
    "taskset": (frozenset(), 1),
    "chrt": (frozenset(), 1),
    "xargs": (
        frozenset(
            {
                *("-I", "-n", "-P", "-L", "-s", "-S", "-d", "-E", "-a"),
                *("--max-args", "--max-procs", "--max-lines", "--max-chars"),
                *("--delimiter", "--arg-file", "--process-slot-var"),
            }
        ),
        0,
    ),
    "systemd-run": (frozenset({"-p", "-u", "-E", "--unit", "--property", "--setenv"}), 0),
    # cmd.exe's ``start`` and ``call`` run the command after them (``cmd /c`` is
    # read by ``_win_program_index``, since its switch is not a ``-`` option).
    "start": (frozenset(), 0),
    "call": (frozenset(), 0),
}


def _program_index(tokens: "list[str]", options: "list[str] | None" = None) -> "int | None":
    """Index in *tokens* of the program a command runs, wrappers and ``VAR=x`` stepped over.

    *options* is *tokens* in the case they were written in, when the caller has
    it: a wrapper's option words are read from it (``sudo -H`` takes no value,
    ``sudo -h`` does), and ``sudo -e`` / ``--edit`` runs no program at all.
    Without it the option words are read from *tokens* as they are.
    """
    opts = tokens if options is None else options
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # An empty word is stepped over too, as the other deny views elide it.
        # ``time`` keeps command position but takes options (``time -p``): read it
        # as a wrapper so ``-p`` is not taken for the program.
        keyword = token in _KEEPS_COMMAND_POSITION and token not in _WRAPPERS
        if not token or ENV_ASSIGNMENT_RE.match(token) or keyword:
            i += 1
            continue
        program = _program_basename(token)
        wrapper = _WRAPPERS.get(program)
        if wrapper is None:
            return i
        value_opts, positionals = wrapper
        i += 1
        while i < len(tokens) and opts[i].startswith("-") and opts[i] != "-":
            if opts[i] == "--":
                i += 1
                break
            if options is not None and program == "sudo" and opts[i] in ("-e", "--edit"):
                return None  # sudoedit: every later word is a file it edits
            i += 2 if opts[i] in value_opts else 1
        i += positionals
    return None


#: Builtins that set a variable in the CURRENT shell when given an assignment word.
#: ``_program_positions`` marks them as argv[0]; the prefix test steps over them so an
#: assignment they carry (``export T=$(...)``) is still read as the command's prefix.
_DECLARATION_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})


def _program_positions(tokens: "list[str]") -> "list[bool]":
    """For each token, True where it is the PROGRAM word (argv[0]) of its command.

    Splits *tokens* into commands at argv boundaries (``_ends_argv``) and asks
    ``_program_index`` which token in each is the program -- the ONE shared walk that
    steps over leading ``VAR=value`` assignments, command keywords
    (``_KEEPS_COMMAND_POSITION``) and wrappers (``_WRAPPERS``: ``env``, ``sudo``,
    ``exec``, ``nohup``, ``timeout`` ...), so a wrapper before the CLI does not hide it
    (``env kirocrew ...`` marks ``kirocrew``). A leading REDIRECTION is removed first --
    recognised only when the token BEGINS with one (optional fd number, ``&`` or
    ``{name}`` then the operator, via ``_push_token_redirection``), so ``>/dev/null
    kirocrew ...`` finds the CLI while a program with a redirect GLUED ON
    (``kirocrew>/dev/null``) keeps its slot -- because ``_program_index`` reads no
    redirections. The token marked True is the one whose basename names the program, so
    this agrees with ``_is_self_program``.
    """
    positions = [False] * len(tokens)
    n = len(tokens)
    start = 0
    for i, token in enumerate(tokens):
        if not (_ends_argv(token) or i == n - 1):
            continue
        # Command segment tokens[start:i+1]: drop redirections (and a separate target)
        # with a map back to the original index, then find the program in what remains.
        argv: list[str] = []
        idx_map: list[int] = []
        skip_target = False
        for j in range(start, i + 1):
            if skip_target:
                skip_target = False
                continue
            is_redirect, expects_target = _push_token_redirection(tokens[j])
            if is_redirect:
                skip_target = expects_target
                continue
            argv.append(tokens[j])
            idx_map.append(j)
        # `sudo --edit` (sudoedit) runs no program, whatever case it was typed in. The
        # floor has already lowered the text, so a lowered `-e` may have been typed `-E`
        # (--preserve-env, which DOES run the command): sudo's `-e` is read as `-E`.
        options, wrapper = [], ""
        for w in argv:
            program = _program_basename(w)
            wrapper = program if program in _WRAPPERS else wrapper
            options.append("-E" if w == "-e" and wrapper == "sudo" else w)
        k = _program_index(argv, options)
        if k is not None:
            positions[idx_map[k]] = True
        start = i + 1
    return positions


@once_per_call()
def _program_basename(token: str) -> str:
    """The program name a token invokes, with shell wrappers stripped.

    Strips quoting, command-substitution wrappers and any attached redirection
    before taking the basename, so an expansion-produced program name
    (``$(which pkill)``, ``"$(command -v bash)"``) or a redirect-glued one
    (``kirocrew>/tmp/out``) is compared as the program it resolves to rather than as
    literal punctuation.  Every program check goes through this -- comparing a raw
    ``os.path.basename`` lets ``$(which pkill) -f <name>`` past the kill rule.

    The layers are peeled to a FIXED POINT rather than once in a fixed order.
    A wrapper and a redirect interleave freely, and any single ordering leaves a
    hole for the interleavings it does not match: ``$(which kirocrew)>/tmp/out`` needs
    the redirect gone before its closing paren reaches the end of the word, while
    ``kirocrew)`` needs the paren gone with no redirect in play at all.  Looping until
    nothing changes makes the peel order-independent, which closes the class
    instead of whichever spelling a fixed order happened to cover.
    """
    if not token:
        return ""
    previous = ""
    substituted = False
    while token != previous:
        previous = token
        token = _strip_redirect(token)
        token = _resolve_param_defaults(token)
        token = _EMPTY_SUBST_RE.sub("", token)
        if token.startswith("$(") or token.startswith("`"):
            substituted = True
        token = token.removeprefix("$(").strip(_SHELL_WRAPPER_CHARS)
        # ANSI-C / locale quoting: ``$'name'`` and ``$"name"`` are just quoting
        # forms, so the ``$`` left behind after the quotes come off is not part of
        # the program name.  ``$(`` and ``${`` are handled above and below.
        if token.startswith("$") and not token.startswith(("$(", "${")):
            token = token[1:]
    if substituted:
        token = _substitution_program(token)
    # A control operator GLUED to the name (``true;kirocrew``, ``x&&kirocrew``)
    # means the program that actually runs is what follows the LAST operator --
    # ``shlex`` splits on whitespace only, so it hands the whole run over as one
    # word and a comparison against it matches nothing.  Taking the trailing
    # segment is what bash does; a trailing operator leaves an empty tail, so the
    # last NON-EMPTY segment is the one that names a program.
    segments = [s for s in _CONTROL_OPERATOR_RE.split(token) if s]
    if segments:
        token = segments[-1]
    return os.path.basename(token.rstrip("/"))


def _argv_programs(tokens: "list[str]") -> "list[str]":
    """For each token, the program name of the command that token belongs to.

    Walks the argv tracking command boundaries (``_ends_argv``) and skipping
    leading ``VAR=value`` assignments, which precede the program rather than being
    it.  Asks "what command is this name an argument OF?" -- the difference
    between ``echo <name> <verb>`` (data) and ``ssh host <name> <verb>`` (executed).
    """
    programs: list[str] = []
    current = ""
    expect_program = True
    for token in tokens:
        if expect_program and token and not ENV_ASSIGNMENT_RE.match(token):
            current = _program_basename(token)
            expect_program = False
        programs.append(current)
        if _ends_argv(token):
            current = ""
            expect_program = True
    return programs

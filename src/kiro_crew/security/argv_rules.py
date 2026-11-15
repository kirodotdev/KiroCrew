"""Deny rules matched on a command's argv, not on its text.

A text regex such as ``chmod.*/usr/.*`` runs across a whole compound command, so
it fires on words that belong to other commands: ``chmod +x build/usr/bin/x``
(or ``chmod +x a && ls /usr/lib``) read as a chmod on ``/usr``, and an ssh
``-o BatchMode`` read as ``chmod``. These rules decide on the real program and its own arguments
instead, read by the shared payload walk (``bash -c``, ``eval``, heredocs,
``| bash`` and substitutions included, via ``shell_normalizer._shell_payload_walk``)
plus the common command-running wrappers (``sudo``, ``timeout``, ``xargs``,
``find -exec``, ``su -c`` ...).

Narrower on purpose. An over-block stops ordinary work and is worse than a rare
miss: a command the reader cannot resolve to a row's program (a
program name built from a substitution, say) is not refused here. The sandbox
still confines what any command can touch.

Each rule keeps its catalog row, so the row's toggle and its SEL ``rule_id``
are unchanged; only its pattern stops being run as a regex
(:data:`ARGV_RULE_PATTERNS`, stripped from the regex tier like the git-publish
rows).
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import rm_floor as _rm
from . import shell_commands as _reader
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES

#: Programs that run another command from their arguments, as
#: ``wrapper -> (options that take a value, positional words before the command)``:
#: ``timeout 5 chown ...`` has one positional (the duration), ``chroot / chmod ...``
#: one (the new root). Long options are listed in their ``--opt value`` form; the
#: ``--opt=value`` form is one word and needs no entry. An unlisted option that
#: takes a value leaves the value read as the program, which only misses.
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "sudo": (
        frozenset(
            {
                *("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D", "-R"),
                *("--user", "--group", "--close-from", "--host", "--prompt"),
                *("--other-user", "--role", "--type", "--chdir", "--chroot"),
            }
        ),
        0,
    ),
    "doas": (frozenset({"-u", "-C"}), 0),
    "pkexec": (frozenset({"--user"}), 0),
    "runuser": (frozenset({"-u", "-g", "-G", "--user", "--group"}), 0),
    "env": (frozenset({"-u", "-C", "--unset", "--chdir"}), 0),
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
                *("-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a"),
                *("--max-args", "--max-procs", "--max-lines", "--max-chars"),
                *("--delimiter", "--arg-file", "--process-slot-var"),
            }
        ),
        0,
    ),
    "systemd-run": (frozenset({"-p", "-u", "-E", "--unit", "--property", "--setenv"}), 0),
}

#: ``find`` options whose following words, up to ``;`` or ``+``, are a command.
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})

_SEPARATORS = frozenset({";", "|", "||", "&", "&&", "{", "}"})

#: Programs whose ``-c`` value is a command line. Read again from the dequoted
#: word, since the shared walk can misread a payload inside ``"$( )"``.
_RUNS_C_VALUE = frozenset({"sh", "bash", "zsh", "dash", "ksh", "su"})

_SYSTEM_DIRS = ("/usr", "/etc", "/sbin", "/boot", "/lib", "/lib64")

#: A symbolic chmod mode written with a leading ``-`` (``-x``, ``-rwx``, ``-w,u+x``).
_CHMOD_DASH_MODE = re.compile(r"^-[rwxXst]+(?:,[ugoa]*[-+=][rwxXst]*)*$")


def _program_args(tokens: "list[str]") -> "tuple[str, list[str]] | None":
    """The program a command runs and its arguments, wrappers and ``VAR=x`` stepped over."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        # An empty word is stepped over too, as the other deny views elide it.
        # ``time`` keeps command position but takes options (``time -p``): read it
        # as a wrapper so ``-p`` is not taken for the program.
        keyword = token in _shell._KEEPS_COMMAND_POSITION and token not in _WRAPPERS
        if not token or ENV_ASSIGNMENT_RE.match(token) or keyword:
            i += 1
            continue
        program = _shell._program_basename(token)
        wrapper = _WRAPPERS.get(program)
        if wrapper is None:
            return program, tokens[i + 1 :]
        value_opts, positionals = wrapper
        i += 1
        while i < len(tokens) and tokens[i].startswith("-") and tokens[i] != "-":
            if tokens[i] == "--":
                i += 1
                break
            i += 2 if tokens[i] in value_opts else 1
        i += positionals
    return None


def _split_commands(tokens: "list[str]") -> "list[list[str]]":
    """*tokens* (one frame's flat argv) cut into one argv per command.

    A comment runs to the next separator; a ``find -exec`` command is its own argv.
    """
    out: list[list[str]] = []
    current: list[str] = []
    in_comment = False
    for token in tokens:
        if in_comment:
            in_comment = token != ";"
            continue
        if not token:  # an empty word (``chmod "" 777 x``) is an argument, not a separator
            current.append(token)
            continue
        if token.startswith("#"):
            in_comment = True
        elif _shell._ends_argv(token):
            # A separator glued to a word (``https://x;``) keeps the word.
            word = token.rstrip(";|&")
            if word and word not in _SEPARATORS and not word.endswith("{"):
                current.append(word)
        else:
            current.append(token)
            continue
        if current:
            out.append(current)
        current = []
    if current:
        out.append(current)
    return [*out, *(sub for argv in out for sub in _find_exec_argvs(argv))]


def _find_exec_argvs(argv: "list[str]") -> "list[list[str]]":
    """Each ``-exec`` command of a ``find`` argv, behind any wrapper (``sudo find``)."""
    parsed = _program_args(argv)
    if parsed is None or parsed[0] != "find":
        return []
    args, out = parsed[1], []
    for index, token in enumerate(args):
        if token in _FIND_EXEC:
            rest = args[index + 1 :]
            end = next((j for j, t in enumerate(rest) if t in (";", "+", "\\;")), len(rest))
            if rest[:end]:
                out.append(rest[:end])
    return out


#: A heredoc operator and its delimiter word (``<<EOF``, ``<<-'EOF'``), not ``<<<``.
_HEREDOC_OP = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*([^\s;&|<>()]+)")


def _without_heredoc_bodies(text: str) -> str:
    """*text* with each heredoc operator and its body lines cut out.

    The shared reader stops at a heredoc, so a command after its body (or after
    the operator on the same line) was never read. This reading only adds
    commands: it is read beside the original, and a body is data either way.
    """
    if "<<" not in text:
        return text
    out: list[str] = []
    pending: list[tuple[bool, str]] = []  # (strip leading tabs, delimiter)
    for line in text.split("\n"):
        if pending:
            strip_tabs, delimiter = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                pending.pop(0)
            continue
        for match in _HEREDOC_OP.finditer(line):
            delimiter = re.sub(r"['\"\\]", "", match.group(2))
            pending.append((match.group(1) == "-", delimiter))
        out.append(_HEREDOC_OP.sub(" ", line))
    return "\n".join(out)


def _in_body(spans: "list[list[int]]", starts: "list[int]", at: int) -> bool:
    """*at* lies in one of the merged, sorted body *spans* (*starts* their starts)."""
    index = bisect.bisect_right(starts, at) - 1
    return index >= 0 and at < spans[index][1]


def _past_heredocs(text: str) -> "list[list[str]]":
    """Argv of each command the shared reader finds reading past each heredoc.

    The other readings stop at a ``<<`` or take one in ``$((1<<2))`` for a heredoc,
    so a command after a heredoc operator on a later line was never read. In heredoc
    mode the reader reads on, leaves each body out and reads a ``<<`` in a ``$((…))`` /
    ``((…))`` span that closes on its line as a shift. A command read from a
    substitution in a body is dropped: a body is data here, as in the other readings.
    A ``<<`` followed by no tag and no operand (``cat <<;``) is a syntax error that
    stops bash on its line, so no command from that line on is read here. This
    reading only adds commands: it is read beside the others.
    """
    if "<<" not in text:
        return []
    commands = _reader.read_commands(text, heredocs=True)
    spans: list[list[int]] = []  # a body nested in another body's substitution merges
    for start, end in sorted((d.start, d.end) for c in commands for d in c.heredocs):
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    starts = [start for start, _ in spans]
    cutoff = len(text)
    for cmd in commands:
        for word, _target in cmd.redirects:
            at = word.raw.find("<<")
            if at < 0 or "<<<" in word.raw or _in_body(spans, starts, word.start):
                continue
            tag, strip, quoted = _reader._heredoc_tag(text, word.start + at + 2)
            rest = text[word.start + at + 2 + strip :].lstrip(" \t")
            # ``<<''`` is a heredoc ending at an empty line; a shift's operand may start
            # with ``(`` or sit on the next line, but never starts with these
            if not tag and not quoted and rest[:1] in ("", ";", "|", "&", "<", ">", ")"):
                cutoff = min(cutoff, text.rfind("\n", 0, word.start) + 1)
    out, depth, in_backtick = [], 0, False
    for cmd in commands:
        argv = cmd.argv
        top = not depth and not in_backtick
        depth = max(depth + (cmd.separator == "(") - (cmd.separator == ")"), 0)
        in_backtick ^= cmd.separator == "`"
        # Inside parens the words may be arithmetic operands (``((x, chmod +6 /usr))``);
        # a subshell or substitution is read by the walk as a frame of its own.
        if not top or not argv or argv[0].start >= cutoff:
            continue
        if _in_body(spans, starts, argv[0].start):
            continue
        joined = " ".join(word.raw for word in argv)
        out.append(_shell._shell_tokens(_shell._fold_line_continuations(joined)))
    return out


def _commands(command: str) -> "list[tuple[str, list[str]]]":
    """Every ``(program, args)`` in *command* and in each nested-shell payload in it.

    Nested payloads (``bash -c``, ``eval``, heredocs, ``| bash``, ``$( )``) come from
    the shared walk the git-publish and self-protection floors use.
    """
    out = []
    values: set[str] = set()
    for source, tokens in _shell._shell_payload_walk(command):
        # Readings of each frame, each only adding commands: the shared reader
        # cuts at every live separator, glued ones included (``ls x& chmod ...``),
        # and reads past each heredoc in heredoc mode; the walk's own tokens keep
        # a heredoc body the reader stops before.
        # The reader's own argv, never re-cut, is a third reading: a dequoted
        # ``'#'`` or ``'a=b;'`` word is an argument there, not a comment or separator.
        readings = dict.fromkeys((source, _without_heredoc_bodies(source)))
        words_read = [list(words) for text in readings for words in _rm._command_tokens(text)]
        found = [*words_read, *_past_heredocs(source)]
        found += [s for w in found for s in _find_exec_argvs(w)]
        # The walk's flat tokens hold a heredoc body: read them as commands only
        # when a shell is the program here, so ``cat <<EOF`` data stays data.
        if any((_program_args(w) or ("",))[0] in _RUNS_C_VALUE for w in words_read):
            found += _split_commands(tokens)
        for argv in found:
            parsed = _program_args(argv)
            if parsed is None:
                continue
            out.append(parsed)
            program, args = parsed
            if program in _RUNS_C_VALUE:  # ``bash -c 'cmd'`` / ``su -c 'cmd'``
                for flag, value in zip(args, args[1:]):
                    if flag in ("-c", "--command") and len(value) < len(command):
                        values.add(value)
    for value in values:  # each strictly shorter than *command*, so the recursion ends
        out.extend(_commands(value))
    return out


def _operands(args: "list[str]") -> "list[str]":
    """The non-option arguments, everything after ``--`` included; empty words dropped."""
    out, done = [], False
    for arg in args:
        if not arg:
            continue
        if done or arg == "-" or not arg.startswith("-"):
            out.append(arg)
        elif arg == "--":
            done = True
    return out


def _options(args: "list[str]") -> "list[str]":
    """The arguments before ``--``: after it every word is a file operand."""
    return args[: args.index("--")] if "--" in args else args


def _paths(program: str, args: "list[str]") -> "list[str]":
    """The file operands of ``chmod``/``chown``: the operands after the mode or owner."""
    if program == "chmod":
        # A symbolic mode may start with ``-`` (``chmod -x f``): it is the mode
        # operand, not an option, so ``f`` is still read as the path.
        args = ["+" + a[1:] if _CHMOD_DASH_MODE.match(a) else a for a in args]
    if "--reference" in _options(args):  # ``--reference FILE``: FILE is only read
        at = args.index("--reference")
        args = args[:at] + args[at + 2 :]
        return _operands(args)  # no mode/owner operand
    operands = _operands(args)
    if any(a.startswith("--reference=") for a in _options(args)):
        return operands  # no mode/owner operand
    return operands[1:]


def _under(path: str, root: str) -> bool:
    path = path.rstrip("/") or "/"
    return path == root or path.startswith(root + "/")


def _changes_system_dir(root: str) -> "Callable[[str, list[str]], bool]":
    return lambda program, args: any(_under(p, root) for p in _paths(program, args))


def _is_mode_777(_program: str, args: "list[str]") -> bool:
    if any(a == "--reference" or a.startswith("--reference=") for a in _options(args)):
        return False  # ``chmod --reference=f x`` has no mode operand
    # A symbolic mode may start with ``-`` (``chmod -w 777``): it is the mode.
    args = ["+" + a[1:] if _CHMOD_DASH_MODE.match(a) else a for a in args]
    return _operands(args)[:1] in (["777"], ["0777"])


#: A ``dd`` output operand naming a whole disk or a partition. ``dd`` reads standard
#: input when no ``if=`` is given, so this operand alone is the destructive half.
_RAW_DISK_OUTPUTS = tuple(
    "of=/dev/" + name
    for name in ("sd", "xvd", "hd", "vd", "nvme", "mmcblk", "loop", "dm-", "md", "dasd", "disk/")
)


def _writes_raw_disk(_program: str, args: "list[str]") -> bool:
    return any(arg.startswith(_RAW_DISK_OUTPUTS) for arg in args)


#: rule id -> (the program it is about, the test on that program's arguments).
_PREDICATES: dict[str, tuple[str, Callable[[str, list[str]], bool]]] = {
    "local-destructive-chmod-777": ("chmod", _is_mode_777),
    "local-destructive-dd-of-device": ("dd", _writes_raw_disk),
    **{
        f"local-destructive-{verb}-{root.strip('/')}": (verb, _changes_system_dir(root))
        for verb in ("chmod", "chown")
        for root in _SYSTEM_DIRS
    },
}


def _could_name(program: str) -> "re.Pattern[str]":
    """The program's letters in order, any quoting or ``$( )`` glue between them.

    A cheap pre-check: a command whose text cannot spell the program is never
    read, so most tool calls pay nothing for these rules.
    """
    return re.compile(r"[^\w\s]*".join(map(re.escape, program)), re.IGNORECASE)


_COULD_NAME = {program: _could_name(program) for program, _ in _PREDICATES.values()}

#: rule id -> catalog pattern, for every rule matched here.
ARGV_RULE_BY_ID: dict[str, str] = {
    rule.id: rule.pattern for rule in BUILTIN_DENIED_RULES if rule.id in _PREDICATES
}
#: The catalog patterns the regex tier must NOT run (this module enforces them).
ARGV_RULE_PATTERNS: frozenset[str] = frozenset(ARGV_RULE_BY_ID.values())

ARGV_RULE_NOTE = (
    "Matched on the program and its own arguments, not on the pattern text across "
    "the whole command line."
)


def argv_rule_hit(command: str, enabled_patterns: "set[str] | frozenset[str]") -> "str | None":
    """The id of the first enabled argv rule *command* breaks, else ``None``.

    Never raises: a command the reader cannot read is let through here (the
    other tiers and the sandbox still apply), since refusing it would over-block.
    """
    wanted = {
        _PREDICATES[rule_id][0]
        for rule_id, pattern in ARGV_RULE_BY_ID.items()
        if pattern in enabled_patterns
    }
    # The shortcut reads raw text, so a spelling only the reader decodes
    # (``$'\x63url'``, a backslash-newline inside the name) always goes to the reader.
    if (
        "\\" not in command
        and "$'" not in command
        and not any(_COULD_NAME[program].search(command) for program in wanted)
    ):
        return None
    try:
        commands = [(p, a) for p, a in _commands(command) if p in wanted]
    except Exception:  # noqa: BLE001 - an unreadable command is not refused here
        return None
    for rule_id, pattern in ARGV_RULE_BY_ID.items():
        if pattern not in enabled_patterns:
            continue
        program, test = _PREDICATES[rule_id]
        if any(p == program and test(p, args) for p, args in commands):
            return rule_id
    return None

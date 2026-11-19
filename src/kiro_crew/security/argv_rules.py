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

import re
from collections.abc import Callable

from ..trust_patterns import ENV_ASSIGNMENT_RE
from . import rm_floor as _rm
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

#: Reserved words after which the next word is still in command position.
_COMPOUND_OPENERS = frozenset({"then", "do", "else", "elif", "{", "(", "!"})

#: Programs whose ``-c`` value is a command line. Read again from the dequoted
#: word, since the shared walk can misread a payload inside ``"$( )"``.
_RUNS_C_VALUE = frozenset({"sh", "bash", "zsh", "dash", "ksh", "su"})

_SYSTEM_DIRS = ("/usr", "/etc", "/sbin", "/boot", "/lib", "/lib64")

#: A symbolic chmod mode written with a leading ``-`` (``-x``, ``-rwx``, ``-w,u+x``).
_CHMOD_DASH_MODE = re.compile(r"^-[rwxXst]+(?:,[ugoa]*[-+=][rwxXst]*)*$")


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
        keyword = token in _shell._KEEPS_COMMAND_POSITION and token not in _WRAPPERS
        if not token or ENV_ASSIGNMENT_RE.match(token) or keyword:
            i += 1
            continue
        program = _shell._program_basename(token)
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


def _program_args(tokens: "list[str]") -> "tuple[str, list[str]] | None":
    """The program a command runs and its arguments, wrappers and ``VAR=x`` stepped over."""
    i = _program_index(tokens)
    if i is None:
        return None
    return _shell._program_basename(tokens[i]), tokens[i + 1 :]


def _split_commands_indexed(tokens: "list[str]") -> "list[list[tuple[int, str]]]":
    """*tokens* cut into one command per argv, each word kept with its index.

    The one place comment and separator boundaries are decided; callers that
    need the original token index (the ssh floor's execute-position walk) read
    it here rather than re-deriving the boundaries. A comment runs to the next
    separator. ``find -exec`` subcommands are NOT included (they are appended by
    :func:`_split_commands`, which has no index to preserve).
    """
    out: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    in_comment = False
    for i, token in enumerate(tokens):
        if in_comment:
            in_comment = token != ";"
            continue
        if not token:  # an empty word (``chmod "" 777 x``) is an argument, not a separator
            current.append((i, token))
            continue
        if token.startswith("#"):
            in_comment = True
        elif _shell._ends_argv(token):
            # A separator glued to a word (``https://x;``) keeps the word.
            word = token.rstrip(";|&")
            if word and word not in _SEPARATORS and not word.endswith("{"):
                current.append((i, word))
        else:
            current.append((i, token))
            continue
        if current:
            out.append(current)
        current = []
    if current:
        out.append(current)
    return out


def _split_commands(tokens: "list[str]") -> "list[list[str]]":
    """*tokens* (one frame's flat argv) cut into one argv per command.

    A comment runs to the next separator; a ``find -exec`` command is its own argv.
    """
    out = [[word for _, word in command] for command in _split_commands_indexed(tokens)]
    return [*out, *(sub for argv in out for sub in _find_exec_argvs(argv))]


def _executed_program_indices(tokens: "list[str]", cased: "list[str] | None" = None) -> "set[int]":
    """Indices in *tokens* that bash runs as a program, wrappers and ``VAR=x`` stepped over.

    One per command and per ``case`` arm body: program position, or the program a wrapper in
    :data:`_WRAPPERS` runs (``env s?h``, ``sudo s?h``, ``xargs s?h``).  Every
    other word is an operand of some program.  A word ending in ``)`` in
    program position is a ``case`` arm pattern (``s*)``) or a subshell close,
    not a program.  ``find -exec`` commands are not marked (their words keep no
    index).  *cased* is *tokens* in the case they were written in, read for the
    wrapper option words (see :func:`_program_index`).
    """
    executed: set[int] = set()
    case_patterns, arm_bodies = _case_arm_pattern_indices(tokens)
    for command in _split_commands_indexed(tokens):
        words = [word for _, word in command]
        # A word cut off a glued separator (``-H;``) keeps its lowered reading.
        options = None
        if cased is not None:
            options = [cased[j] if cased[j].lower() == word else word for j, word in command]
        # A case arm body's first word (``x) s?h …``) starts a command too.
        for start in [0] + [k for k, (j, _) in enumerate(command) if k and j in arm_bodies]:
            found = _program_index(words[start:], None if options is None else options[start:])
            if found is None or words[start + found].endswith(")"):
                continue
            idx = start + found
            origin = command[idx][0]
            # A word in a ``case`` arm's pattern list (``s* | r* )``) is a pattern the
            # shell matches ``$x`` against, not a program it runs; the ``|`` is case
            # alternation, not a pipe, so the command splitter wrongly starts a new
            # command at each alternative. Do not read such a word as a run verb.
            if origin in case_patterns:
                continue
            # ``x=$(ls s* l*)`` / ``n=`ls s* l*` `` / ``files=( s* *.* )``: the
            # resolver steps over the ``VAR=`` word and lands on the next token, but
            # bash does not run that token as a program here.  It is an array element,
            # or the operand of a command the substitution runs in its own frame.
            if found and _assignment_opens_a_construct(words[idx - 1]):
                continue
            executed.add(origin)
    return executed


def _case_arm_pattern_indices(tokens: "list[str]") -> "tuple[set[int], set[int]]":
    """Indices in *tokens* that sit in a ``case`` arm's pattern list, and the
    index of each arm body's first word.

    Between ``case``'s ``in`` (or an arm's ``;;``, ``;;&`` or ``;&``) and that
    arm's closing ``)``,
    each word -- and the ``|`` that separates alternatives -- is a pattern the
    shell matches the case word against, never a program it runs. The command
    splitter treats the ``|`` as a pipe and would start a fresh command at each
    alternative, so those words must be taken out of the run-verb reading. The
    word right after the closing ``)`` is the arm body's program, which the
    splitter does not see as a command start. Nested
    ``case`` statements are tracked on a stack; glued separators (``esac;``,
    ``id;;``) are read off the token. Only a ``case`` in command position opens
    one (``echo case in x`` does not): a command's first word, a word after a
    :data:`_COMPOUND_OPENERS` word in command position, or an arm body's first word.
    """
    patterns: set[int] = set()
    bodies: set[int] = set()
    stack: list[str] = []  # per open case: "await_in", "pattern" or "body"
    starts = {command[0][0] for command in _split_commands_indexed(tokens)}
    program_position = False
    for i, tok in enumerate(tokens):
        here, program_position = program_position or i in starts, False
        if tok.rstrip(";&| ") == "esac":
            if stack:
                stack.pop()
            continue
        if tok == "case":
            if here:
                stack.append("await_in")
            continue
        program_position = here and tok in _COMPOUND_OPENERS
        state = stack[-1] if stack else None
        if state == "await_in":
            if tok == "in":  # a bare ``for … in`` with no open case never reaches here
                stack[-1] = "pattern"
        elif state == "pattern":
            if tok != "|":
                patterns.add(i)
                if ")" in tok:  # the arm's close ends the pattern list
                    stack[-1] = "body"
                    program_position = True
                    bodies.add(i + 1)
        elif state == "body" and (";;" in tok or ";&" in tok):
            # ``;;``, ``;;&`` and ``;&`` (fall through) each end an arm, so the
            # next word opens the next arm's pattern list.
            stack[-1] = "pattern"
    return patterns, bodies


def _assignment_opens_a_construct(word: str) -> bool:
    """True if *word* is a ``VAR=`` assignment opening an array literal or an unclosed
    command substitution.

    The token the resolver lands on after such a word is an array element
    (``files=( s* …``) or the first operand of the command the substitution runs
    (``x=$(ls s* …``, ``n=`ls s* …``).  Neither is a program bash runs in this
    frame -- the substitution's own program is marked in the frame the floor
    walks for that substitution -- so the ssh gate must not read it as a run verb.
    A CLOSED substitution (``x=$(true) prog``) does not shield the word after it.
    """
    if not ENV_ASSIGNMENT_RE.match(word):
        return False
    rest = word.split("=", 1)[1]
    # ``(`` masks to ``\x05`` inside a quoted nested payload (``bash -c '…( … )…'``).
    if rest[:1] in ("(", "\x05"):  # VAR=( elem ...   an array literal
        return True
    if "$(" in rest and rest.count("(") > rest.count(")"):  # VAR=$(cmd ...  open $( )
        return True
    return rest.count("`") % 2 == 1  # VAR=`cmd ...   an open backtick


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


def _commands(command: str) -> "list[tuple[str, list[str]]]":
    """Every ``(program, args)`` in *command* and in each nested-shell payload in it.

    Nested payloads (``bash -c``, ``eval``, heredocs, ``| bash``, ``$( )``) come from
    the shared walk the git-publish and self-protection floors use.
    """
    out = []
    values: set[str] = set()
    for source, tokens in _shell._shell_payload_walk(command):
        # Two readings of each frame, each only adding commands: the shared reader
        # cuts at every live separator, glued ones included (``ls x& chmod ...``),
        # and the walk's own tokens keep a heredoc body the reader stops before.
        # The reader's own argv, never re-cut, is a third reading: a dequoted
        # ``'#'`` or ``'a=b;'`` word is an argument there, not a comment or separator.
        readings = dict.fromkeys((source, _without_heredoc_bodies(source)))
        words_read = [list(words) for text in readings for words in _rm._command_tokens(text)]
        found = [*words_read, *(s for w in words_read for s in _find_exec_argvs(w))]
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

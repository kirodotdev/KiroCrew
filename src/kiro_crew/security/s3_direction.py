"""Read the direction of ``aws s3 cp|mv|sync`` commands, or refuse to read them.

The three ``credential-exfil-s3-*`` rows match ``<verb> .* s3://`` on the raw text,
so a download with a leading flag (``cp --recursive s3://b/p ./d``) reads as an
upload. A regex cannot split flags from operands the way bash and the aws CLI do:
a backslash-escaped space and a glued ``$IFS`` each make bash see words the regex
does not. So this module does not see through shell syntax at all. It reads only
commands built from plain literal words, splits them as bash would, reads the
words with a fixed table of aws CLI flags, and answers True only when EVERY
``aws s3`` transfer on the line is a download: its one source is ``s3://`` and its
one destination is not. Anything else -- an expansion it cannot resolve, a glob,
an escape, an unclear separator, an unknown flag, a subshell, a pipe, a command
substitution -- answers False and the rows' deny stands, so this can only narrow a
row on a command it fully read.

It reads a SEQUENCE of top-level statements split on ``;``, ``&&``, ``||`` and
newlines, judging each on its own:

* A plain literal assignment made earlier on the line (``B=b``, then ``s3://$B/…``)
  is resolved, but only when the value is one plain literal word and the name is
  assigned exactly once by an assignment that always runs (not after ``&&`` or
  ``||``). Names are case-sensitive, as in bash. A reassignment, a ``+=`` append,
  or a name that steers splitting, lookup or ``~`` (``IFS``, ``PATH``, ``HOME``,
  ``PWD``, …) or that bash sets itself (``_``, ``RANDOM``, ``OPTARG``, ``BASH*``, …)
  keeps the line denied.
* Any other statement must run one program from a short inert list (``cat``,
  ``echo``, ``touch``, ``mkdir``, ``ls``, ``cd``, ``pwd``, ``true``) with no ``$``,
  backtick, backslash or ``!``, and with no ``>`` or ``<`` (a redirect or
  here-document could write ``~/.aws/config`` before the download reads it). None of
  those sets a variable or runs other code, so a
  resolved value stays what bash will use. ``export``, ``read``, ``printf -v``, a
  ``${X:=…}``, a loop, ``eval`` or ``bash`` keeps the line denied. Narrowing removes
  only the s3 rows, so every other deny rule still judges the statement in
  :mod:`~kiro_crew.security.deny_gate`.

It reads ``aws s3`` and nothing else: ``python -c``, ``curl`` and every other way
to move a file are out of its reach and stay with the rows and the OS sandbox.

It reads POSIX shell only, so on Windows it reads nothing. A script hook there runs
under cmd.exe, where a single quote is a plain character and an ``&`` inside one
starts a second command: ``'./d & aws s3 cp ./f s3://x/k'`` is one POSIX word but
two cmd.exe commands.
"""

from __future__ import annotations

import shlex

from kiro_crew import platform_compat

from . import shell_commands as _commands
from . import shell_normalizer as _shell
from .denied_rules import BUILTIN_DENIED_RULES, _deny_pattern_matches
from .paths import MAX_SCANNABLE_COMMAND_CHARS

_VERBS = frozenset({"cp", "mv", "sync"})
_S3_TRANSFER_PATTERNS: frozenset[str] = frozenset(
    rule.pattern
    for rule in BUILTIN_DENIED_RULES
    if rule.id in {f"credential-exfil-s3-{v}" for v in _VERBS}
)

#: Characters a word may hold outside quotes. No ``$``, backtick, ``\``, glob
#: (``*?[``), brace, ``#``, ``!``, operator or separator: each one is syntax.
#: ``~`` stays: bash expands it only to a local home path, never to ``s3://`` or a flag.
_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_./:@%+=,~-")
#: Refused anywhere, quoted or not. Inside double quotes ``$``, backtick and ``\``
#: are live; refusing them in single quotes too only over-refuses.
_NEVER = frozenset("$`\\!")
_BOOL, _VALUE, _EQ_ONLY = "bool", "value", "eq-only"
#: aws CLI flags this reader understands. Global options are accepted anywhere,
#: as the aws CLI does. A flag one verb rejects makes the aws CLI exit before any
#: transfer, so one table serves all three. ``_EQ_ONLY`` flags take an OPTIONAL
#: value (argparse ``nargs='?'``) and so would swallow the next word: only the
#: ``--flag=value`` spelling is read. Every flag not here refuses.
_FLAGS: dict[str, str] = {
    **dict.fromkeys(
        "--debug --no-verify-ssl --no-paginate --no-sign-request --no-cli-pager "
        "--recursive --quiet --dryrun --only-show-errors --no-progress --follow-symlinks "
        "--no-follow-symlinks --no-guess-mime-type --ignore-glacier-warnings "
        "--force-glacier-transfer --delete --exact-timestamps --size-only --no-overwrite".split(),
        _BOOL,
    ),
    **dict.fromkeys(
        "--profile --region --endpoint-url --output --query --ca-bundle --cli-read-timeout "
        "--cli-connect-timeout --color --cli-binary-format --include --exclude --page-size "
        "--source-region --checksum-mode --sse-c-key --expected-size".split(),
        _VALUE,
    ),
    **dict.fromkeys(("--sse-c", "--request-payer"), _EQ_ONLY),
}

#: The programs a statement that is neither an ``aws s3`` transfer nor an assignment
#: may run, with plain words only. None of them sets a shell variable or runs other
#: code, so they cannot change what a later statement reads. Anything else (a loop
#: keyword, ``export``, ``printf -v``, ``eval``, ``bash``, a ``${X:=…}``) keeps the
#: deny. Such a statement may not redirect either: any ``>`` or ``<`` (quoted or
#: not, here-documents included) keeps the deny, since ``echo … > ~/.aws/config``
#: could install a ``credential_process`` the next download runs. shortcut: a short
#: list from real download scripts; add a program only when it can set no variable
#: and run nothing.
_INERT = frozenset("cat echo touch mkdir ls cd pwd true".split())
#: Names whose value changes how bash splits words, finds programs or expands a
#: ``~``; assigning one keeps the deny.
_SPECIAL_NAMES = frozenset(("IFS", "PATH", "ENV", "BASH_ENV", "CDPATH", "HOME", "PWD", "OLDPWD"))
#: Names bash sets or rewrites itself (``$_`` after every command, ``$RANDOM`` on
#: each read, ``$OPTARG`` from ``getopts``), so a value assigned earlier is not the
#: value a later ``$NAME`` reads; assigning one keeps the deny. Every ``BASH*`` name
#: is in this class too.
_BASH_SET_NAMES = frozenset(
    "_ RANDOM SRANDOM SECONDS LINENO EPOCHREALTIME EPOCHSECONDS PPID UID EUID GROUPS "
    "HOSTNAME HOSTTYPE MACHTYPE OSTYPE OPTIND OPTARG OPTERR REPLY PIPESTATUS FUNCNAME "
    "SHLVL SHELLOPTS HISTCMD COLUMNS LINES DIRSTACK MAPFILE COPROC".split()
)
#: The characters a resolved value may hold: a plain word, case kept.
_VALUE_CHARS = _PLAIN | frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
#: How many characters resolving ``$NAME`` may add across a whole line.
_SUBSTITUTION_MARGIN = 256


def _is_name(text: str) -> bool:
    """Whether *text* is a shell variable name: ASCII letters, digits, ``_``, no leading digit."""
    return text.isascii() and text.isidentifier()


def _is_assignment_prefix(token: str) -> bool:
    """Whether *token* starts with ``NAME=`` or ``NAME+=``.

    It steps over inline assignment prefixes (``AWS_PROFILE=p aws …``) and spots a
    lone-assignment try.
    """
    name, eq, _value = token.partition("=")
    if name.endswith("+"):
        name = name[:-1]
    return bool(eq) and _is_name(name)


def _lone_assignment(raw: str) -> "tuple[str, str] | None":
    """``(NAME, value)`` of a standalone ``NAME=value`` statement, else None.

    The one assignment shape resolved: a name, ``=``, and a value of plain
    characters only (it may be empty). It is read on the case-preserved text:
    shell names are case-sensitive (``B`` is not ``b``).
    """
    name, eq, value = raw.partition("=")
    if not eq or not _is_name(name) or not all(ch in _VALUE_CHARS for ch in value):
        return None
    return name, value


def _name_end(text: str, start: int) -> int:
    """End of the variable name at ``text[start]``, or *start* when none starts there."""
    end = start
    while end < len(text) and text[end].isascii() and (text[end].isalnum() or text[end] == "_"):
        end += 1
    if end > start and text[start].isdigit():
        return start
    return end


def _plain_words(command: str) -> "list[str] | None":
    """The argv bash builds from *command*, or None when it is not plain literal words."""
    text = _shell._fold_line_continuations(command).strip(" \t\n")
    if not text or _NEVER.intersection(text):
        return None
    state = 0
    for step in _shell._iter_shell_chars(text):
        state = step.state
        if not " " <= step.char <= "~" and step.char != "\t":
            return None  # newline, control or non-ASCII: a separator or a lookalike
        if step.active and step.char not in _PLAIN and step.char not in " \t'\"":
            return None
    if state != 0:
        return None
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return None


def _skip_flag(words: "list[str]", i: int) -> "int | None":
    """Index after the flag at ``words[i]`` and its value, or None when it is not read."""
    name, eq, _value = words[i].partition("=")
    arity = _FLAGS.get(name)
    if arity is None or (eq and arity == _BOOL) or (not eq and arity == _EQ_ONLY):
        return None
    if eq or arity == _BOOL:
        return i + 1
    if i + 1 >= len(words) or words[i + 1].startswith("-"):
        return None
    return i + 2


def _one_plain_download(command: str) -> bool:
    """Whether *command* is exactly one plain ``aws s3 cp|mv|sync s3://… <local>``.

    *command* is already lower-cased and any resolvable variable already resolved.
    A leading ``running:`` (kiro-cli's title for a shell call) is stepped over.
    """
    words = _plain_words(command)
    if words and words[0] == "running:":
        words = words[1:]
    # A ``file://`` / ``fileb://`` value makes the aws CLI READ a local file and
    # send it (``--sse-c-key fileb://...`` goes out in a request header).
    if not words or words[0] != "aws" or any(("file://" in w or "fileb://" in w) for w in words):
        return False
    stage, operands, i = 0, [], 1  # stage 0 wants ``s3``, 1 the verb, 2 operands
    while i < len(words):
        word = words[i]
        if word.startswith("-") and word != "-":  # a bare ``-`` is stdin/stdout
            nxt = _skip_flag(words, i)
            if nxt is None:
                return False
            i = nxt
            continue
        if stage == 2:
            operands.append(word)
        elif (stage == 0 and word == "s3") or (stage == 1 and word in _VERBS):
            stage += 1
        else:
            return False
        i += 1
    return (
        stage == 2
        and len(operands) == 2
        and operands[0].startswith("s3://")
        and not operands[1].startswith("s3://")
    )


def _substitute(text: str, assigns: "dict[str, str]", budget: int) -> "str | None":
    """*text* with each ``$NAME`` / ``${NAME}`` of a resolved *assigns* name replaced.

    Only a name assigned exactly once to a plain literal is in *assigns*, so the
    replacement is a plain word and cannot itself introduce an expansion. An
    unresolved reference keeps its ``$`` and leaves the statement unreadable.
    One left-to-right scan that looks up each reference once: a pattern per name
    made a line of many assignments and statements cost their product. None when
    the result would pass *budget* characters: a long value read many times would
    otherwise grow a short line into megabytes before the tokenizer reads it.
    """
    out: list[str] = []
    size = i = 0
    while i < len(text):
        dollar = text.find("$", i)
        if dollar < 0:
            dollar = len(text)
        size += dollar - i
        if size > budget:
            return None
        out.append(text[i:dollar])
        if dollar == len(text):
            break
        braced = text.startswith("{", dollar + 1)
        start = dollar + 2 if braced else dollar + 1
        end = _name_end(text, start)
        if braced and end < len(text) and text[end] == "}" and end > start:
            stop = end + 1  # ``${NAME}``; any other ``${…}`` form is left as is
        elif not braced and end > start:
            stop = end  # ``$NAME`` takes the whole name: ``$AB`` is never ``$A`` + ``B``
        else:
            stop = start = end = dollar + 1  # a lone ``$`` stays as it is
        value = assigns.get(text[start:end]) if end > start else None
        piece = text[dollar:stop] if value is None else value
        size += len(piece)
        if size > budget:
            return None
        out.append(piece)
        i = stop
    return "".join(out)


def _program_after_assigns(tokens: "list[str]") -> "tuple[str, list[str]]":
    """The program a statement runs and the words after it, inline ``VAR=x`` stepped over."""
    i = 0
    while i < len(tokens) and _is_assignment_prefix(tokens[i]):
        i += 1
    if i >= len(tokens):
        return "", []
    return _shell._program_basename(tokens[i]), tokens[i + 1 :]


def _statement_raw(command: str, cmd: "_commands.ShellCommand") -> str:
    """The raw text of one statement, or "" when it has no steps (an empty segment)."""
    if not cmd.steps:
        return ""
    return command[cmd.steps[0].start : cmd.steps[-1].end].strip(" \t")


def _reads_all_downloads(text: str) -> bool:
    """Whether *text* is a sequence of statements whose every s3 transfer is a download.

    *text* keeps its case: bash matches a here-document tag and a variable name
    case-sensitively, so the split and the assignments are read on it, and each
    statement is lower-cased only after its variables are resolved. Returns False
    on anything the reader cannot split cleanly or cannot read in full, so the
    rows' deny stands.
    """
    stripped = text.lstrip()
    if stripped.lower().startswith("running: ") or stripped.lower() == "running:":
        text = stripped[len("running:") :]
    try:
        commands = _commands.read_commands(text, heredocs=True)
    except Exception:
        return False
    # Structural gate: refuse anything that makes the split or a statement unclear.
    for cmd in commands:
        if cmd.separator not in (";", "&&", "||", "\n", ""):
            return False  # pipe, ``&`` backgrounding, backtick or paren
        if any(step.char == _commands.SUBST for step in cmd.steps):
            return False  # command substitution
    assigns: dict[str, str] = {}
    saw_transfer = saw_conditional = False
    # Resolving ``$NAME`` may add at most a small margin over the whole line.
    budget = min(len(text) + _SUBSTITUTION_MARGIN, MAX_SCANNABLE_COMMAND_CHARS)
    for cmd in commands:
        raw = _statement_raw(text, cmd)
        tokens = _shell._shell_tokens(raw) if raw else []
        if tokens:
            # A standalone assignment feeds the variable map; one it cannot
            # resolve cleanly keeps the line denied. Only an assignment that always
            # runs is read: after any ``&&`` or ``||`` bash may skip it, and the
            # name keeps whatever value it already had.
            if len(tokens) == 1 and _is_assignment_prefix(tokens[0]):
                lone = _lone_assignment(raw)
                if lone is None or lone[0] in assigns or saw_conditional:
                    return False
                name, value = lone
                if name in _SPECIAL_NAMES or name in _BASH_SET_NAMES or name.startswith("BASH"):
                    return False
                assigns[name] = value
            else:
                resolved = _substitute(raw, assigns, budget)
                if resolved is None:
                    return False  # resolving would pass the cap: the deny stands
                budget -= len(resolved)
                sub_raw = resolved.lower()
                program, rest = _program_after_assigns(_shell._shell_tokens(sub_raw))
                # The gate's own linear-time matcher: a raw ``re.search`` on these
                # rows backtracks exponentially on a run of flags.
                matches_regex = any(
                    _deny_pattern_matches(pat, sub_raw, True) for pat in _S3_TRANSFER_PATTERNS
                )
                if matches_regex or (program == "aws" and "s3" in rest):
                    saw_transfer = True
                    if not _one_plain_download(sub_raw):
                        return False
                elif (
                    (sub_raw.split(None, 1) or [""])[0] not in _INERT
                    or _NEVER.intersection(sub_raw)
                    or ">" in sub_raw
                    or "<" in sub_raw
                ):
                    return False  # empty, could run code, change a variable or touch a file
        if cmd.separator in ("&&", "||"):
            saw_conditional = True
    return saw_transfer


def _s3_download_only(command: str, raw: str = "") -> bool:
    """Whether every ``aws s3`` transfer in *command* is a plain readable download.

    *raw* is the same command before case folding: KELVIN SIGN folds to ASCII ``k``.
    The statements are read on *raw*, so a *raw* that is not *command* in another
    case (a caller that judges a rewritten view) reads nothing.
    """
    if platform_compat.IS_WINDOWS:  # no POSIX shell reads it there (module docstring)
        return False
    if not (command.isascii() and raw.isascii()):
        return False
    if raw and raw.lower() != command:
        return False
    return _reads_all_downloads(raw or command)

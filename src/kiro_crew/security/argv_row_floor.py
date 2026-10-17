"""One argv-structural floor shared by the catalog rows that name a command literal.

Eight built-in rows are a ``<program> <word>`` literal: ``terraform destroy.*``,
``git reset --hard.*``, ``dd if=.*`` and five more.  Each recognises one SPELLING
of a command, so an ordinary option between the program and the word the row
anchors on -- ``terraform -chdir=x destroy``, ``git -C . reset --hard``,
``dd bs=1M if=/dev/zero`` -- runs the identical command while the regex admits it.

An option-run regex CAN be written -- several ``aws`` and ``git push`` rows in the
catalog carry one -- but a regex over the text cannot tell a command from text
that only mentions it: it refuses ``git reset --soft HEAD~1 # not --hard``, a
multi-line commit message or ``pulumi -s destroy up``, and still misses ``g""it``,
``--har`` or ``$'…'``. This floor reads the argv the shell builds instead, so the
destructive command is refused and ordinary text stays allowed. The catalog
patterns stay the simple literal (pins and golden fixture included).

Each row is a program name, a matcher over the words that program receives, and the
rule id that ties it to the catalog.  Three matcher shapes cover every row, because
the rows anchor on three kinds of word:

* a SUBCOMMAND chain (``terraform destroy``, ``kubectl delete namespace``): the
  leading operand after any options, then the leading operand after that one's
  options;
* a subcommand plus an OPTION it carries anywhere (``git reset --hard``);
* an option or a ``key=value`` operand ANYWHERE in the argv (``ncat -e``,
  ``dd if=``), because those programs permute their arguments.

Scope is the OPTION SPELLINGS of those exact commands, no wider.  The program word
is compared by basename (``/usr/bin/terraform`` is ``terraform``); a glob or ``.exe``
spelling of it, and a sibling binary name (``netcat`` for ``nc``, ``aws-cdk`` for
``cdk``), are NOT resolved -- those are program-word spellings, a different axis the
regex rows never covered.

A shell keyword opener (``then``, ``do``, ``(`` ...) is skipped to the real program,
and a PROGRAM RUNNER is followed to the command it runs: the shared
``readonly_bash._PROGRAM_RUNNERS`` set (``sudo``, ``env``, ``timeout``, ``xargs`` ...)
is the one list of "runs its argument as a program" both this floor and the
read-only classifier read, so the two cannot drift.  A runner's OPTIONS and
assignments are skipped, its mandatory leading data operands too (``timeout``'s
duration, via ``_RUNNER_POSITIONALS``), and the operand after them is the program --
so ``timeout 600 terraform -chdir=x destroy`` is matched while ``sudo tar terraform
-C x destroy`` (where ``tar`` is the program and ``terraform`` its data) is not.  A
runner outside that set (``chroot``, ``doas``, ``npx``) is read as an ordinary
program, matching the classifier's own boundary.  The command's program is resolved
through every nested shell payload too, so ``bash -c 'git -C . reset --hard'`` is read
as the reset it runs.

Which options take a value is not known per program, and guessing would be the
bypass: ``git -C <dir>`` consumes the next word, ``git --no-pager`` does not.  So
the word right after an option with no attached value is read BOTH ways -- as that
option's value and as the subcommand -- and the leading operand that no option
precedes is certain (:func:`_leading_operands` tags each).  A subcommand at a
certain operand uses the row's ``.*`` prefix test; at an ambiguous operand it must
match the whole word (or a ``word/`` path or a plural), so an option value that
merely starts with the verb (``pulumi -s destroy-test up``) is not a refusal.

It is a UNION with the regex rows, never a replacement: each row stays in the regex
tier, the floor checks a row only while it is in the effective set (an operator
opt-out disables both), and it runs AFTER the regex pass so a literal hit keeps its
own refusal.  The table is keyed by rule id and the pattern is read from the catalog,
so a renamed row fails at import, loudly.

The command is read with the shared shell reader
(``_split_push_command_segments`` / ``_split_shell_words`` / ``_dequote_token``),
which honours quoting and every operator spelling -- so a glued ``true&&terraform
-chdir=x destroy`` is two commands, a quoted ``git -C "repo;old" reset --hard`` keeps
its argument whole, ``(git -C . reset --hard)`` loses its grouping parens, and an
attached redirection (``git reset --hard>/dev/null``) is peeled off the word it hides.
On top of that reader this floor splits at a lone background ``&``, reads ``<>`` and
``<&`` whole, and resolves ``$'…'`` before reading the argv. Every nested shell payload (``bash -c``, ``eval``, ``$( )``) is walked the same way.
Nothing imports this module but the facade's evaluator.
"""

import bisect
import functools
import logging
import re
from collections.abc import Callable, Iterator
from typing import NamedTuple

from kiro_crew.trust_patterns import ENV_ASSIGNMENT_RE

from .denied_rules import _RULES_BY_ID
from .readonly_bash import _COMMAND_POSITION_OPENERS, _PROGRAM_RUNNERS
from .shell_normalizer import (
    _decode_printf_escapes,
    _decode_shell_quoted_literals,
    _dequote_token,
    _fold_line_continuations,
    _iter_shell_chars,
    _program_basename,
    _shell_join_continuations,
    _shell_payload_walk,
    _split_push_command_segments,
    _split_shell_words,
    _substitution_bodies,
)

logger = logging.getLogger(__name__)

#: A redirection operator read WHOLE, so its target word is consumed with it: the
#: shared ``_REDIRECT_START_RE`` plus the read-write ``<>`` and the duplicating
#: ``<&``, which it reads as a bare ``<`` and so leaves the target as an argument
#: (``git -C . <> /dev/null reset --hard`` is a reset).
_REDIRECT_OPERATOR_RE = re.compile(
    r"(?:\d+|&|\*|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:<>|>{1,2}[&|!]?|<{1,3}&?)"
)

#: Words that open a command POSITION without being the program that runs there:
#: the shared grouping/list openers (``(``, ``then``, ``do`` ...) plus the compound
#: openers whose following command is the CONDITION and runs too -- ``if git -C .
#: reset --hard; then …`` runs the reset. ``for`` is excluded: the word after it is
#: a loop variable, not a command.
_OPENERS = _COMMAND_POSITION_OPENERS | frozenset({"if", "while", "until"})

#: Leading positional operands a program runner consumes BEFORE the command it
#: runs: ``timeout DURATION cmd`` and (were it in the runner set) ``chroot DIR cmd``.
#: Every other runner in ``_PROGRAM_RUNNERS`` takes only options and assignments
#: before the command, so its default is 0. Reusing ``_PROGRAM_RUNNERS`` keeps this
#: floor and the read-only classifier reading ONE list of "runs its argument".
_RUNNER_POSITIONALS = {"timeout": 1}

#: An option that MAY consume the next word as its value: a letters-only option,
#: long (``--chdir``) or single-dash (``-u``, Go-flag's ``-chdir``). A ``=`` or a
#: glued non-letter (``-n1``, ``-I{}``) means the value is already attached, so the
#: next word is the operand, not the value.
_MAY_TAKE_VALUE_RE = re.compile(r"--[a-z][a-z-]*|-[a-z]+")

#: Short options of nc / ncat that DEFINITELY consume the rest of their cluster as
#: a value (source/proxy address and port, timeouts, interval, max-conns, output
#: file, hex-dump file, shell-exec command). Past one of these the cluster is a
#: value, so an ``e`` in it is data, not the exec flag -- ``ncat -s2001:db8::e host
#: 80`` connects, it does not exec. Lower-cased. Valueless flags (``-t`` telnet,
#: ``-v``, ``-z`` ...) are deliberately ABSENT: including one would stop the scan
#: early and MISS a packed ``-te`` exec flag. ``e`` is absent too -- reaching it is
#: the match.
_NC_VALUE_SHORTS = frozenset("spwiogmx")


def _strip_group(word: str) -> str:
    """Drop the shell grouping parens bash reads off a word before the program.

    ``(git`` and ``--hard)`` are a subshell's parens glued to the first and last
    words; the shell removes them, so the words are ``git`` and ``--hard``.
    Stripping can only widen a match, so it is the safe direction.
    """
    return word.lstrip("(").rstrip(")")


def _segment_argv(segment: str) -> "list[str]":
    """The argv the shell hands the program of one command *segment*.

    The segment is one command (the caller split operators and honoured quoting),
    so the shell syntax left to drop is a redirection -- a leading ``2>/dev/null``
    or a bare ``>`` plus its target, and one GLUED inside a word
    (``--hard>/dev/null``, which otherwise hides the option) -- and the grouping
    parens of a subshell.

    A redirection is read on the RAW token, before de-quoting, and only when its
    operator is UNQUOTED: a quoted or escaped ``">"`` is a literal argument
    (``git -C ">" reset --hard`` names a ref ``>``), not a redirection, so reading
    it after de-quoting would wrongly drop the word that follows. Quotes come off
    the surviving word last, with the shared de-quoter.
    """
    words: list[str] = []
    target_pending = False
    # An output redirection the program's stdout goes to (``dd … > /dev/sda``):
    # recorded as the ``_STDOUT_TARGET`` word, so a row that needs a write target
    # sees it. ``2>`` and ``/dev/null`` are not a write target.
    redirected = False
    pending_out = ""
    # ``$'…'`` is resolved first, as bash does before the program sees it:
    # ``reset $'--hard'`` passes ``--hard``. The shared decoder re-quotes each value
    # so a decoded quote, space or ``>`` stays data inside one word.
    for raw in _split_shell_words(_decode_shell_quoted_literals(segment)):
        if target_pending:
            target_pending = False
            if pending_out and _is_stdout_write(pending_out, _dequote_token(raw)):
                redirected = True
            pending_out = ""
            continue
        leading = _REDIRECT_OPERATOR_RE.match(raw)
        if leading is not None:
            if leading.end() == len(raw):
                target_pending = True
                pending_out = leading.group(0)
            elif _is_stdout_write(leading.group(0), _dequote_token(raw[leading.end() :])):
                redirected = True
            continue
        cut = _unquoted_redirect_offset(raw)
        if cut >= 0:
            # A redirection glued after the word (``--hard>``) whose operator has no
            # attached target takes the NEXT word as its target, so that word must
            # not be read as an argument: ``git reset --hard> /dev/null`` is a reset.
            operator = _REDIRECT_OPERATOR_RE.match(raw[cut:])
            if operator is not None and operator.end() == len(raw) - cut:
                target_pending = True
                pending_out = operator.group(0)
            elif operator is not None and _is_stdout_write(
                operator.group(0), _dequote_token(raw[cut + operator.end() :])
            ):
                redirected = True
        word = _strip_group(_dequote_token(raw if cut < 0 else raw[:cut]))
        if word:
            words.append(word)
    if redirected:
        words.append(_STDOUT_TARGET)
    return words


#: The argv word standing for "stdout is redirected to a file or device".
#: It holds a NUL, which no real argument can, so no matcher reads it as one.
_STDOUT_TARGET = "\x00stdout>"


def _is_stdout_write(operator: str, target: str) -> bool:
    """Does *operator* send stdout to a DEVICE *target* (the raw-disk write)?

    Stdout is descriptor 1: no number, ``1``, or ``&`` (``&>`` is stdout and
    stderr); ``>&file`` writes to that file, as ``&>file`` does. Only a device
    under ``/dev/`` counts -- ``dd … > /tmp/header.bin`` writes an ordinary file --
    and a duplication onto another descriptor (``1>&3``), whose file is not known
    here. ``>&2`` and ``>&-`` write nothing. An empty *target* is decided once the
    next word is read.
    """
    if ">" not in operator or not operator.startswith((">", "1", "&")):
        return False
    if operator.endswith(">&") and target in ("2", "-"):
        return False
    if operator.endswith(">&") and target.isdigit():
        return True
    return _is_device(target)


#: Devices that read or write nothing persistent.
_HARMLESS_DEVICES = frozenset(
    {"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/1", "/dev/fd/2"}
)


def _is_device(path: str) -> bool:
    """Is *path* a device node? ``/dev/shm/…`` is a tmpfs holding ordinary files."""
    return (
        path.startswith("/dev/")
        and path not in _HARMLESS_DEVICES
        and not path.startswith("/dev/shm/")
    )


def _split_background(segment: str) -> "list[str]":
    """Split one segment at each UNQUOTED lone ``&``, the background operator.

    ``true & git -C . reset --hard`` is two commands; the shared splitter keeps a
    lone ``&`` inside the segment because the push check reads it as glue, so the
    split happens here, for this floor only. An ``&`` that belongs to a
    redirection (``2>&1``, ``&>log``, ``<&3``) or to ``|&`` is not a boundary.
    """
    pieces: list[str] = []
    begin = 0
    for step in _iter_shell_chars(segment):
        if not (step.active and step.char == "&"):
            continue
        before = segment[step.offset - 1] if step.offset else ""
        after = segment[step.offset + 1 : step.offset + 2]
        if before in ("<", ">", "|", "&") or after in (">", "&"):
            continue
        pieces.append(segment[begin : step.offset])
        begin = step.offset + 1
    pieces.append(segment[begin:])
    return pieces


def _unquoted_redirect_offset(raw: str) -> int:
    """Offset of the first UNQUOTED ``<``/``>`` in *raw*, or ``-1``.

    Read through the shell's own quote/escape machine, so a quoted or escaped
    operator (``">"``, ``\\>``) is data and does not count -- only a bare operator
    is a redirection the shell would act on.
    """
    for step in _iter_shell_chars(raw):
        if step.active and step.char in "<>":
            return step.offset
    return -1


def _is_option(word: str) -> bool:
    return word.startswith("-") and len(word) > 1 and word != "--"


def _leading_operands(words: "list[str]", start: int) -> "Iterator[tuple[int, bool]]":
    """``(index, certain)`` for each word at/after *start* that may be a subcommand.

    Options are skipped wherever they sit, and a leading ``NAME=value`` assignment
    with them. The word right after an option that may take a value (see
    ``_MAY_TAKE_VALUE_RE``) is its value OR the operand, so it is yielded with
    ``certain=False`` and the scan goes on; the first operand that no such option
    precedes is yielded with ``certain=True`` and ends the scan. After ``--`` the
    next word is the certain operand.
    """
    maybe_value = False
    index = start
    while index < len(words):
        word = words[index]
        if word == "--":
            if index + 1 < len(words):
                yield index + 1, True
            return
        if _is_option(word):
            maybe_value = _MAY_TAKE_VALUE_RE.fullmatch(word) is not None
            index += 1
            continue
        if ENV_ASSIGNMENT_RE.match(word):
            maybe_value = False
            index += 1
            continue
        yield index, not maybe_value
        if not maybe_value:
            return
        maybe_value = False
        index += 1


def _link_matches(word: str, link: str, certain: bool) -> bool:
    """Does *word* name subcommand *link*?

    At a CERTAIN operand the row's ``<link>.*`` literal is a prefix test, so
    ``namespace`` covers ``namespaces`` and ``namespace/prod``. At an AMBIGUOUS
    operand (possibly an option's value) the word must BE the subcommand -- the
    whole word, a ``link/`` path, or the plural -- so an option value that merely
    starts with it (``pulumi -s destroy-test up``) is not read as the subcommand.
    A KNOWN value option's value never reaches here (``kubectl delete -f
    namespace/x.yaml``; see ``_operand_table``).
    """
    if certain:
        return word.startswith(link)
    return word == link or word.startswith(link + "/") or word == link + "s"


def _subcommands(
    *chain: str,
    value_opts: "frozenset[str]" = frozenset(),
    flag_opts: "frozenset[str]" = frozenset(),
) -> "Callable[[list[str]], list[bool]]":
    """Matcher: the leading operands from *start* spell *chain*, each past its options.

    The reading is :func:`_leading_operands` applied link by link, computed right
    to left once per link instead of re-scanning the tail for every ambiguous
    candidate, so a 20 KB run of ``-x delete`` stays linear. ``ahead[i]`` says the
    links after this one match from word *i*; ``fresh[i]`` / ``after_option[i]``
    say this link and the rest match when the scan reaches *i* with no pending
    option value / with one (the two states of ``_leading_operands``).
    """

    def _table(words: "list[str]") -> "list[bool]":
        ahead = [True] * (len(words) + 2)
        for link in reversed(chain):
            ahead = _operand_table(
                words, functools.partial(_link_on, link), ahead, value_opts, flag_opts
            )
        return ahead

    return _table


def _link_on(link: str, word: str, certain: bool) -> bool:
    return _link_matches(word, link, certain)


def _operand_table(
    words: "list[str]",
    link: "Callable[[str, bool], bool]",
    ahead: "list[bool]",
    value_opts: "frozenset[str]" = frozenset(),
    flag_opts: "frozenset[str]" = frozenset(),
) -> "list[bool]":
    """``table[i]``: a leading operand from *i* meets *link* and ``ahead`` holds after it.

    One right-to-left pass over the two states of :func:`_leading_operands` --
    ``fresh`` (no option value pending) and ``after_option`` (the word may be the
    previous option's value, so the scan goes on past it). An option in
    *value_opts* is one the program is KNOWN to give a value (``pulumi -s``), so the
    next word is that value and never the subcommand: ``pulumi -s destroy up`` is
    an ``up`` on a stack named ``destroy``.
    """
    n = len(words)
    fresh = [False] * (n + 2)
    after_option = [False] * (n + 2)
    for i in range(n - 1, -1, -1):
        word = words[i]
        if word == "--":
            fresh[i] = after_option[i] = i + 1 < n and link(words[i + 1], True) and ahead[i + 2]
        elif word in value_opts:
            fresh[i] = after_option[i] = fresh[i + 2]
        elif word in flag_opts:
            fresh[i] = after_option[i] = fresh[i + 1]
        elif _is_option(word):
            takes = _MAY_TAKE_VALUE_RE.fullmatch(word) is not None
            fresh[i] = after_option[i] = (after_option if takes else fresh)[i + 1]
        elif ENV_ASSIGNMENT_RE.match(word):
            fresh[i] = after_option[i] = fresh[i + 1]
        else:
            fresh[i] = link(word, True) and ahead[i + 1]
            after_option[i] = (link(word, False) and ahead[i + 1]) or fresh[i + 1]
    return fresh


def _long_option(word: str, name: str, shortest: int) -> bool:
    """True if *word* is ``--name`` or an abbreviation getopt would accept for it."""
    spelled = word.split("=", 1)[0]
    return len(spelled) >= shortest and spelled.startswith("--") and name.startswith(spelled)


def _subcommand_with_option(
    verb: str, option: str, value_opts: "frozenset[str]" = frozenset()
) -> "Callable[[list[str]], list[bool]]":
    """Matcher: subcommand *verb*, with the long *option* anywhere after it.

    ``git reset -q --hard`` and ``git reset HEAD~1 --hard`` are the same reset, and
    git accepts any unambiguous prefix (``--har``); a ``--h`` is read as it too.
    Scanning stops at ``--``: after it ``--hard`` is a pathspec, not the flag
    (``git reset -- --hard`` touches a file named ``--hard``).
    """

    def _table(words: "list[str]") -> "list[bool]":
        # ``flagged[i]``: the option appears at or after word *i*, before the next
        # ``--``. The verb is then one more link whose tail is ``flagged``.
        flagged = [False] * (len(words) + 2)
        for i in range(len(words) - 1, -1, -1):
            word = words[i]
            flagged[i] = word != "--" and (_long_option(word, option, 3) or flagged[i + 1])
        return _operand_table(words, lambda word, _certain: word == verb, flagged, value_opts)

    return _table


def _option_anywhere(
    short: str, long_name: str, value_shorts: "frozenset[str]"
) -> "Callable[[list[str]], list[bool]]":
    """Matcher: option ``-<short>`` (alone or in a cluster) or ``--<long>``, anywhere.

    getopt permutes, so the option counts at any position before ``--``. A packed
    cluster is read letter by letter and stops at the first letter in
    *value_shorts*, which consumes the cluster's tail as its value -- so the ``e``
    in ``-s2001:db8::e`` is the source address, not the exec flag.
    """

    def _is_flag(word: str) -> bool:
        if _long_option(word, long_name, 3):
            return True
        if word.startswith("-") and not word.startswith("--"):
            for letter in word[1:]:
                if letter == short:
                    return True
                if letter in value_shorts:
                    break
        return False

    def _table(words: "list[str]") -> "list[bool]":
        table = [False] * (len(words) + 1)
        for i in range(len(words) - 1, -1, -1):
            word = words[i]
            table[i] = word != "--" and (_is_flag(word) or table[i + 1])
        return table

    return _table


def _operand_and_target(prefix: str, target: str) -> "Callable[[list[str]], list[bool]]":
    """Matcher: a *prefix* operand at any position, with *target* in the argv too.

    The row guards raw-disk writes: ``dd`` reads ``if=`` and writes a DEVICE,
    through ``of=/dev/…`` or stdout redirected to one (``_STDOUT_TARGET``). A
    respelled ``dd`` that prints (``dd bs=1 count=16 if=firmware.bin | xxd``) or
    writes an ordinary file (``of=/tmp/test.img``) is not that, so the floor asks
    for both. *target* is looked for anywhere in a word, not only at its start, so
    a reading that glued two operands into one word still counts it -- the wider
    reading, so the safe one. The literal ``dd if=.*`` regex row is untouched and
    still refuses its own spelling.
    """

    def _writes_device(word: str) -> bool:
        if word == _STDOUT_TARGET:
            return True
        at = word.find(target)
        if at < 0:
            return False
        tail = word[at + len(target) :].split()
        return bool(tail) and _is_device(tail[0])

    def _table(words: "list[str]") -> "list[bool]":
        has_prefix = [False] * (len(words) + 1)
        has_target = [False] * (len(words) + 1)
        for i in range(len(words) - 1, -1, -1):
            has_prefix[i] = words[i].startswith(prefix) or has_prefix[i + 1]
            has_target[i] = _writes_device(words[i]) or has_target[i + 1]
        return [p and t for p, t in zip(has_prefix, has_target)]

    return _table


#: Spaced options each program always gives a value, so the next word is that
#: value and not the subcommand. Only options whose arity is certain are listed;
#: any other option keeps the "read both ways" rule. The text is lower-cased
#: before it gets here, so ``pulumi -C`` and ``git -C`` are spelled ``-c``.
_PULUMI_VALUE_OPTS = frozenset({"-s", "--stack", "-c", "--cwd"})
_KUBECTL_VALUE_OPTS = frozenset(
    {"-n", "--namespace", "--context", "--kubeconfig", "-f", "--filename", "-l", "--selector"}
)
_CDK_VALUE_OPTS = frozenset({"--profile", "-a", "--app", "-c", "--context", "-o", "--output"})

#: Options each program never gives a value, so the next word is the operand and
#: is read only as such: ``cdk --verbose deploy destroyerstack`` deploys.
_CDK_FLAG_OPTS = frozenset(
    {
        "--verbose",
        "-v",
        "--debug",
        "--json",
        "-j",
        "--force",
        "-f",
        "--all",
        "--ci",
        "--strict",
        "--no-notices",
        "--no-color",
    }
)
_GIT_VALUE_OPTS = frozenset({"-c", "--git-dir", "--work-tree", "--namespace"})


class _ArgvRow(NamedTuple):
    rule_id: str
    program: str
    #: ``table(words)[i]``: the row's argv matcher holds for the words from *i*.
    #: Every matcher is one right-to-left pass, so every candidate program position
    #: of a command is answered from ONE table -- linear however many runner
    #: operands repeat the program name.
    table: "Callable[[list[str]], list[bool]]"

    def matches(self, words: "list[str]", start: int) -> bool:
        return self.table(words)[start]


#: The rows this floor stands under: id, the program that must RUN, and the matcher.
_ARGV_ROWS: "tuple[_ArgvRow, ...]" = (
    _ArgvRow(
        "iac-teardown-cdk-destroy",
        "cdk",
        _subcommands("destroy", value_opts=_CDK_VALUE_OPTS, flag_opts=_CDK_FLAG_OPTS),
    ),
    _ArgvRow(
        "iac-teardown-pulumi-destroy",
        "pulumi",
        _subcommands("destroy", value_opts=_PULUMI_VALUE_OPTS),
    ),
    _ArgvRow("iac-teardown-terraform-destroy", "terraform", _subcommands("destroy")),
    _ArgvRow(
        "iac-teardown-kubectl-delete-namespace",
        "kubectl",
        _subcommands("delete", "namespace", value_opts=_KUBECTL_VALUE_OPTS),
    ),
    _ArgvRow(
        "local-destructive-git-reset-hard",
        "git",
        _subcommand_with_option("reset", "--hard", _GIT_VALUE_OPTS),
    ),
    _ArgvRow("reverse-shell-ncat", "ncat", _option_anywhere("e", "--exec", _NC_VALUE_SHORTS)),
    _ArgvRow("reverse-shell-nc", "nc", _option_anywhere("e", "--exec", _NC_VALUE_SHORTS)),
    _ArgvRow("local-destructive-dd-if", "dd", _operand_and_target("if=", "of=")),
)

#: Words a row's command must contain, in some spelling view, before a reader
#: failure is refused under that row. Each is a substring of every spelling the
#: row's matcher accepts (``--h`` covers every ``--hard`` abbreviation git takes,
#: ``e`` every ``-e`` / ``--exec`` form), so the check can only skip text that
#: cannot be the row's command.
_UNREADABLE_ANCHORS: "dict[str, tuple[str, ...]]" = {
    "iac-teardown-cdk-destroy": ("destroy",),
    "iac-teardown-pulumi-destroy": ("destroy",),
    "iac-teardown-terraform-destroy": ("destroy",),
    "iac-teardown-kubectl-delete-namespace": ("delete", "namespace"),
    "local-destructive-git-reset-hard": ("reset", "--h"),
    "reverse-shell-ncat": ("e",),
    "reverse-shell-nc": ("e",),
    "local-destructive-dd-if": ("if=",),
}

#: ``pattern -> row``, read from the catalog: a renamed row raises ``KeyError`` here.
_ARGV_ROW_BY_PATTERN: "dict[str, _ArgvRow]" = {
    _RULES_BY_ID[row.rule_id].pattern: row for row in _ARGV_ROWS
}

ARGV_ROW_FLOOR_NOTE = (
    "Matched structurally on the command's argv, not by the pattern text above: the "
    "program runs (directly, or inside a nested shell payload) and its argv carries "
    "the word this rule names, with options in any position or packing. The rule "
    "blocks the command however its options are spelled."
)

ARGV_ROW_UNREADABLE_NOTE = (
    "The command names this rule's program, but its shell syntax could not be read "
    "into an argv to check it, so it is refused rather than allowed unchecked."
)


def _runner_targets(
    words: "list[str]", start: int, positional: int, visited: "set[tuple[int, bool, int]]"
) -> "Iterator[tuple[int, bool]]":
    """Program-candidate indices for a runner whose command starts at *start*.

    A runner's own options and assignments are skipped by ``_leading_operands``.
    Then its *positional* mandatory data operands (``timeout``'s duration) are
    read, but the slot they occupy is kept as a candidate TOO, not just skipped:
    a valueless option or a missing duration shifts the command into that slot, so
    reading only the slot after would miss it. Every operand slot from the first
    through the one after the positionals is yielded, and the command is matched at
    each -- a duration that is not a row program simply matches nothing.

    The scan is :func:`_leading_operands` with its state made explicit, and
    *visited* -- shared by every runner of one command -- records each
    ``(word, value pending, positionals left)`` state reached. The scan is
    deterministic from a state, so reaching a recorded one means everything after
    it was already yielded, and the scan stops: ``env -u env -u … git`` stays
    linear instead of rescanning the tail from every runner.
    """
    position = start
    for remaining in range(positional, -1, -1):
        certain_index = None
        maybe_value = False
        index = position
        while index < len(words):
            state = (index, maybe_value, remaining)
            if state in visited:
                return
            visited.add(state)
            word = words[index]
            if word == "--":
                if index + 1 < len(words):
                    yield index + 1, True
                    certain_index = index + 1
                break
            if _is_option(word):
                maybe_value = _MAY_TAKE_VALUE_RE.fullmatch(word) is not None
                index += 1
                continue
            if ENV_ASSIGNMENT_RE.match(word):
                maybe_value = False
                index += 1
                continue
            yield index, not maybe_value
            if not maybe_value:
                certain_index = index
                break
            maybe_value = False
            index += 1
        if certain_index is None:
            return
        position = certain_index + 1


def _invocation_positions(words: "list[str]") -> "list[int]":
    """Indices in *words* that RUN a program.

    The command's program is the first word past any shell keyword opener
    (``then``, ``do``, ``(`` ...). A program runner (``sudo``, ``env``, ``timeout``
    ... -- the shared ``_PROGRAM_RUNNERS`` set) runs a program among its own
    operands, so the runner's program candidates are followed, and a runner nested
    behind another is followed in turn. A runner's option VALUE can repeat a
    program name (``sudo -u git git …``), so both the ambiguous and the certain
    operand are candidates; a runner's DATA operands (``sudo tar … terraform``,
    ``timeout 600 …``) are not.
    """
    positions: list[int] = []
    seen: set[int] = set()
    visited: "set[tuple[int, bool, int]]" = set()
    stack = [0]
    while stack:
        position = stack.pop()
        while position < len(words) and (
            words[position] in _OPENERS or ENV_ASSIGNMENT_RE.match(words[position])
        ):
            position += 1
        if position >= len(words) or position in seen:
            continue
        seen.add(position)
        positions.append(position)
        name = _program_basename(words[position])
        if name in _PROGRAM_RUNNERS:
            positional = _RUNNER_POSITIONALS.get(name, 0)
            for index, _certain in _runner_targets(words, position + 1, positional, visited):
                if index not in seen:
                    stack.append(index)
    return sorted(positions)


def _segment_row(words: "list[str]", rows: "list[_ArgvRow]") -> "_ArgvRow | None":
    """The row whose program this command runs and whose matcher its argv meets.

    Each row's table is built once per command and shared by every position.
    """
    tables: "dict[str, list[bool]]" = {}
    for position in _invocation_positions(words):
        name = _program_basename(words[position])
        for row in rows:
            if name != row.program:
                continue
            if row.rule_id not in tables:
                tables[row.rule_id] = row.table(words)
            if tables[row.rule_id][position + 1]:
                return row
    return None


#: Programs that run their stdin as shell commands, so a heredoc fed to one is code.
_STDIN_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "busybox"})

#: The delimiter word after ``<<``: quoted parts read whole, and it ends at an
#: unquoted blank or shell operator, so ``cat <<'EOF';`` closes on ``EOF``.
_HEREDOC_DELIMITER_RE = re.compile(r"""(-?)[ \t]*((?:'[^']*'|"[^"]*"|\\.|[^\s;&|<>()'"\\])+)""")


def _quoted_newlines_as_spaces(source: str) -> str:
    """Turn each newline INSIDE quotes into a space, for this floor's reading only.

    The shared segment splitter cuts at every newline, quoted or not, so the
    second line of ``git commit -m "fix\\ngit reset … --hard"`` read as its own
    command. Inside quotes a newline is part of the word, so a space keeps it one
    word. An escaped newline is left alone. A heredoc body (any program's) is
    never folded and never quote-read: an apostrophe in it is data, but the quote
    reader would take it as an open quote. Each region between bodies is folded on
    its own, from a clean quote state, so a multi-line ``-m`` after a heredoc is
    still one word. A body with no closing line runs to the end, unfolded.
    """
    if "<<" not in source:
        return _fold_quoted_newlines(source)
    out: list[str] = []
    cursor = 0
    for start, end in _heredoc_body_spans(source):
        # The opener line's own newline ends the line: it is never folded, so the
        # body's first line stays a line (``eval "$(cat <<'EOF'`` + newline + …).
        opener_end = max(cursor, start - 1)
        out.append(_fold_quoted_newlines(source[cursor:opener_end]))
        out.append(source[opener_end:end])
        cursor = end
    out.append(_fold_quoted_newlines(source[cursor:]))
    return "".join(out)


def _fold_quoted_newlines(text: str) -> str:
    return "".join(
        " " if step.char == "\n" and step.text == "\n" and step.state != 0 else step.text
        for step in _iter_shell_chars(text)
    )


def _heredoc_body_spans(source: str) -> "list[tuple[int, int]]":
    """``(start, end)`` of every heredoc body in *source*, whatever reads it.

    A body runs from the line after its opener through its closing line; an
    opener line with a second heredoc, or a body with no closing line, runs to the
    end of the text.
    """
    spans: list[tuple[int, int]] = []
    lines = source.split("\n")
    offset = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        offset_after = offset + len(line) + 1
        index += 1
        at = _heredoc_offset(line)
        match = _HEREDOC_DELIMITER_RE.match(line, at + 2) if at >= 0 else None
        if match is not None:
            if _heredoc_offset(line[match.end() :]) >= 0:
                spans.append((min(offset_after, len(source)), len(source)))
                break
            strip_tabs, delimiter = bool(match.group(1)), _dequote_token(match.group(2))
            cursor = offset_after
            for end in range(index, len(lines)):
                cursor += len(lines[end]) + 1
                body_line = lines[end].lstrip("\t") if strip_tabs else lines[end]
                if body_line == delimiter:
                    spans.append((offset_after, min(cursor, len(source))))
                    index = end + 1
                    offset_after = cursor
                    break
            else:
                spans.append((min(offset_after, len(source)), len(source)))
                break
        offset = offset_after
    return spans


def _comment_spans(source: str) -> "list[tuple[int, int]]":
    """``(start, end)`` of each unquoted comment, from a word-starting ``#`` to the newline.

    A ``#`` inside a word (``a#b``, ``$#``), in quotes, or inside a backtick,
    ``$( )`` or ``${ }`` that is still open (`` reset `echo #` --hard``) is not a
    comment.
    """
    spans: list[tuple[int, int]] = []
    start = -1
    previous = ""
    backtick = False
    depth = 0
    for step in _iter_shell_chars(source):
        if start >= 0:
            if step.char == "\n":
                spans.append((start, step.offset))
                start = -1
            continue
        if step.active:
            if step.char == "`":
                backtick = not backtick
            elif source.startswith(("$(", "${"), step.offset):
                depth += 1
            elif step.char in ")}" and depth:
                depth -= 1
            elif (
                step.char == "#"
                and not backtick
                and not depth
                and (previous == "" or previous in " \t\n;&|(")
            ):
                start = step.offset
                continue
        previous = step.char if step.active else "x"
    if start >= 0:
        spans.append((start, len(source)))
    return spans


def _strip_comments(source: str) -> str:
    """*source* without its comments, so ``git status # x; git reset --hard`` is one command."""
    return _cut_spans(source, _comment_spans(source))


def _cut_spans(source: str, spans: "list[tuple[int, int]]") -> str:
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(source[cursor:start])
        cursor = end
    out.append(source[cursor:])
    return "".join(out)


#: The programs whose heredoc body is data: git and gh read it as a message,
#: ``cat`` prints it or writes it to a file, ``tee`` copies it to one. Inside
#: ``"$(cat <<'EOF' … )"`` cat's output is the argument of the outer command,
#: which is judged in its own frame (an outer ``eval`` or ``bash -c`` keeps the
#: body as code there).
_HEREDOC_DATA_PROGRAMS = frozenset({"git", "gh", "cat", "tee"})

#: Words on a heredoc line that could hand the body to a shell after all.
_HEREDOC_RUNNERS = _STDIN_SHELLS | _PROGRAM_RUNNERS | frozenset({"eval", "source", "."})

#: Cuts a heredoc opener line into the words the runner test reads. It ignores
#: quotes, so it may cut more than the shell does, which only finds more runners.
_RUNNER_WORD_SPLIT_RE = re.compile(r"[\s;&|<>()]+")


def _data_heredoc_spans(source: str) -> "list[tuple[int, int, bool]]":
    """``(start, end, quoted)`` of each heredoc body that a data program reads.

    The body is data when the line's program is in ``_HEREDOC_DATA_PROGRAMS`` and
    nothing on the line could run it -- no pipe, no process substitution, no
    shell, runner, ``eval`` or ``source`` word; a ``>`` file redirection is fine,
    since cat or tee only writes the body there. Its plain lines are never
    commands. With an UNQUOTED delimiter the shell still expands the body's
    ``$( )``, so *quoted* says whether its substitutions are inert too; the walk
    reads those as their own frames either way. A heredoc with no closing line,
    and a line that starts inside a quoted string, are read as commands.
    """
    spans: list[tuple[int, int, bool]] = []
    lines = source.split("\n")
    # Line numbers per exact text and per tab-stripped text, so each opener finds
    # its closing line by lookup rather than a scan: many openers with no closing
    # line stay linear on the 20 KB ceiling.
    exact: "dict[str, list[int]]" = {}
    stripped: "dict[str, list[int]]" = {}
    starts = [0]
    for number, text in enumerate(lines):
        exact.setdefault(text, []).append(number)
        stripped.setdefault(text.lstrip("\t"), []).append(number)
        starts.append(starts[-1] + len(text) + 1)
    offset = 0
    index = 0
    state = 0
    while index < len(lines):
        line = lines[index]
        offset_after = offset + len(line) + 1
        index += 1
        line_state = state
        for step in _iter_shell_chars(line + "\n", state):
            state = step.state
        opener = _data_heredoc_opener(line) if line_state == 0 else None
        if opener is not None:
            strip_tabs, delimiter, quoted = opener
            numbers = (stripped if strip_tabs else exact).get(delimiter, [])
            at = bisect.bisect_left(numbers, index)
            if at < len(numbers):
                end = numbers[at]
                cursor = starts[end + 1]
                spans.append((offset_after, min(cursor, len(source)), quoted))
                index = end + 1
                offset_after = cursor
                state = 0
        offset = offset_after
    return spans


#: Shell syntax that can end a command or expand inside one blank-free word.
_PREFIX_SYNTAX_RE = re.compile(r"[&;|<>()$`]")

#: Words after a prefixed program that let it hand stdin to a shell
#: (``git -c alias.x='!sh' x``, ``--config-env``).
_PREFIXED_RUNNER_HINT_RE = re.compile(r"!|\A-c\Z|\A--config-env|\Aalias\.")

#: The only variables a prefix may set and still leave the body data: identity,
#: date and locale names, none of which names a program git or a pager runs.
#: The text is lower-cased before it gets here.
_DATA_PREFIX_ASSIGNMENT_RE = re.compile(
    r"\A(?:git_(?:author|committer)_(?:name|email|date)|lang|language|lc_[a-z]+|tz)="
)

#: ``env`` options that only change the environment and never run anything else.
_ENV_FLAG_OPTS = frozenset({"-i", "-", "--ignore-environment"})
_ENV_VALUE_OPTS = frozenset({"-u", "--unset"})


def _data_prefix_word(word: str) -> int:
    """Words one prefix item spans (an assignment, or an ``env`` option), else 0."""
    if _DATA_PREFIX_ASSIGNMENT_RE.match(word) or word in _ENV_FLAG_OPTS:
        return 1
    if word in _ENV_VALUE_OPTS:
        return 2
    return 1 if word.startswith("--unset=") or (word.startswith("-u") and len(word) > 2) else 0


def _data_head_prefix(head_words: "list[str]") -> int:
    """How many leading words only set the environment of the command's program.

    Allowlisted ``NAME=value`` assignments (``_DATA_PREFIX_ASSIGNMENT_RE``), and
    one plain ``env`` with ``-i`` / ``-u NAME`` / ``--unset=NAME`` / allowlisted
    assignments, so ``GIT_AUTHOR_NAME=x git commit -F - <<'EOF'`` and ``env -u
    PORT git commit -F - <<'EOF'`` judge ``git``. Any other word stops the skip,
    so ``GIT_EDITOR=sh git commit -e -F -`` is not read as data; a word carrying
    shell syntax (``X=1&sh${IFS}-s``) is never skipped.
    """
    index = 0
    seen_env = False
    while index < len(head_words):
        word = _dequote_token(head_words[index])
        if not seen_env and _program_basename(word) == "env":
            seen_env = True
            index += 1
            continue
        span = _data_prefix_word(word)
        if not span or (word.startswith("-") and not seen_env):
            break
        if any(_PREFIX_SYNTAX_RE.search(item) for item in head_words[index : index + span]):
            break
        index += span
    return index


def _data_heredoc_opener(line: str) -> "tuple[bool, str, bool] | None":
    """``(strip_tabs, delimiter, quoted)`` of a data heredoc opened on *line*.

    The program judged is the one of the command that holds the ``<<``, after the
    last ``&&`` or ``;`` before it: in ``mkdir -p out && cat > out/notes <<'EOF'``
    that is ``cat``. Every word on the whole line still counts for the runner test,
    cut at shell operators as well as blanks, so ``cat <<'EOF'>x;sh x`` names ``sh``.
    """
    words = [
        _program_basename(_dequote_token(word))
        for word in _RUNNER_WORD_SPLIT_RE.split(line)
        if word
    ]
    heredoc_at = _heredoc_offset(line)
    if heredoc_at < 0:
        return None
    command_start = 0
    for step in _iter_shell_chars(line[:heredoc_at]):
        if step.active and (step.char == ";" or line.startswith("&&", step.offset)):
            command_start = step.offset + (1 if step.char == ";" else 2)
    head_words = line[command_start:heredoc_at].split()
    skip = _data_head_prefix(head_words)
    if skip >= len(head_words) or _program_basename(_dequote_token(head_words[skip])) not in (
        _HEREDOC_DATA_PROGRAMS
    ):
        return None
    if skip:
        command_words = [_dequote_token(word) for word in head_words[skip + 1 :]]
        if any(_PREFIXED_RUNNER_HINT_RE.search(word) for word in command_words):
            return None
        # Only the one ``env`` that sets or unsets variables for the data program
        # leaves the runner test; every other word on the line still counts.
        prefix = [
            word for word in head_words[:skip] if _program_basename(_dequote_token(word)) != "env"
        ]
        rest = " ".join([line[:command_start], *prefix, *head_words[skip:], line[heredoc_at:]])
        words = [
            _program_basename(_dequote_token(word))
            for word in _RUNNER_WORD_SPLIT_RE.split(rest)
            if word
        ]
    if any(mark in line for mark in ("||", "<(", ">(")) or any(
        word in _HEREDOC_RUNNERS for word in words
    ):
        return None
    # A pipe is fine only into another data program (``| gh pr comment 1
    # --body-file -``, ``| tee notes.md``); any other stage could run the body.
    for stage in line.split("|")[1:]:
        head = stage.split()
        if not head or _program_basename(_dequote_token(head[0])) not in _HEREDOC_DATA_PROGRAMS:
            return None
    offset = _heredoc_offset(line)
    if offset < 0:
        return None
    match = _HEREDOC_DELIMITER_RE.match(line, offset + 2)
    if match is None:
        return None
    if _heredoc_offset(line[match.end() :]) >= 0:
        # A second heredoc on the same line: its body follows the first one's,
        # which this reader does not track, so both bodies stay read.
        return None
    quoted = any(quote in match.group(2) for quote in "'\"\\")
    return bool(match.group(1)), _dequote_token(match.group(2)), quoted


def _heredoc_offset(line: str) -> int:
    """Offset of the first ``<<`` on *line* that opens a heredoc, else ``-1``.

    ``<<`` opens one where the shell reads syntax: unquoted, or inside a ``$(``
    that is itself in double quotes (``-m "$(cat <<'EOF'``). The quote reader
    marks the second as quoted text, so the open ``$(`` depth is tracked. A
    ``<<`` inside plain quotes is text, and ``<<<`` is a here-string.
    """
    depth = 0
    state = 0
    skip_to = 0
    for step in _iter_shell_chars(line):
        syntax = step.active or (depth > 0 and state == 2)
        if state != 1 and line.startswith("$(", step.offset):
            depth += 1
        elif state != 1 and step.char == ")" and depth:
            depth -= 1
        state = step.state
        if syntax and line.startswith("<<", step.offset) and step.offset >= skip_to:
            if line.startswith("<<<", step.offset):
                # A here-string, not a heredoc: skip it and keep looking.
                skip_to = step.offset + 3
                continue
            return step.offset
    return -1


def _drop_literal_heredocs(source: str) -> str:
    """*source* without its data heredoc bodies (see :func:`_data_heredoc_spans`)."""
    return _cut_spans(source, [(start, end) for start, end, _q in _data_heredoc_spans(source)])


def _inert_bodies(sources: "list[str]") -> "dict[str, bool]":
    """For each decoded ``$( )`` / backtick body in *sources*: is it ONLY ever inert?

    The payload walk reads every substitution body as a command, quoted or not.
    The shell expands none inside single quotes, a comment or a quoted-delimiter
    data heredoc, so in ``git commit -m '…`git -C . reset --hard`…'`` the
    backticks are text. A body is inert only if EVERY place it appears, across
    every frame, is such a span: a body single-quoted in the outer text can sit in
    double quotes in a nested ``bash -c`` frame, where the shell does expand it.
    Each frame is read once.
    """
    inert: dict[str, bool] = {}
    for source in sources:
        folded = _fold_line_continuations(source)
        quiet = _single_quoted_offsets(folded)
        for start, end in _comment_spans(folded):
            quiet.update(range(start, end))
        for start, end, quoted in _data_heredoc_spans(folded):
            if quoted:
                quiet.update(range(start, end))
        # A backslash-escaped backtick (``\```) is a literal character, not a
        # substitution: ``--body "Removed the \`terraform destroy\` step"``.
        quiet.update(step.offset + 1 for step in _iter_shell_chars(folded) if step.text == "\\`")
        for body in _substitution_bodies(folded):
            decoded = _decode_printf_escapes(body)
            here = True
            start = folded.find(body)
            while start >= 0 and here:
                opener = start - 1 if folded[start - 1 : start] == "`" else start - 2
                here = opener in quiet
                start = folded.find(body, start + 1)
            inert[decoded] = inert.get(decoded, True) and here
    return inert


#: Programs that only print their arguments.
_PRINTERS = frozenset({"echo", "printf"})

#: Text in a parent that could turn printed words back into a command. A command
#: whose program is ``eval``, ``source`` or a shell is no printer, so the printer
#: test below already refuses to skip it.
_RE_EXECUTORS = ("|", "$(", "`", "<(", ">(")


def _printer_index(sources: "list[str]") -> "dict[str, list[tuple[int, bool]]]":
    """For each argv word of every frame: ``(frame index, prints only)`` per command.

    *prints only* holds when the command is a plain ``echo`` / ``printf`` (each
    program position is a printer) and its frame has nothing that could run the
    printed text: no pipe, ``eval``, ``source``, or substitution. Each frame is
    split and read once, so asking about every frame stays linear in the frames.
    """
    index: dict[str, list[tuple[int, bool]]] = {}
    for at, source in enumerate(sources):
        risky = any(mark in source for mark in _RE_EXECUTORS)
        for segment in _split_push_command_segments(source):
            for command in _split_background(segment):
                words = _segment_argv(command)
                prints = not risky and all(
                    _program_basename(words[p]) in _PRINTERS for p in _invocation_positions(words)
                )
                for word in set(words):
                    index.setdefault(word, []).append((at, prints))
    return index


def _printed_only(child: str, at: int, printers: "dict[str, list[tuple[int, bool]]]") -> bool:
    """Is frame *at* (text *child*) only ever an argument that ``echo`` / ``printf`` prints?

    The payload walk reads ``bash -c '<text>'`` as a payload wherever the words
    appear, so ``echo bash -c 'git -C . reset --hard'`` -- which prints the words
    and runs nothing -- became a child command. It is skipped only when some other
    frame holds it as a word and every command that does is a printer
    (:func:`_printer_index`).
    """
    holders = [prints for parent, prints in printers.get(child, ()) if parent != at]
    return bool(holders) and all(holders)


def _payload_texts(frames: "list[tuple[str, list[str]]]") -> "set[str]":
    """Texts a frame hands a shell as a payload: each argv word, and each ``eval`` tail.

    A ``bash -c`` / ``sh -c`` payload is one argv word of its parent, and an
    ``eval`` payload is the words after ``eval`` joined; either is code wherever
    else the same text also appears as an inert substitution body.
    """
    texts: set[str] = set()
    for _source, tokens in frames:
        texts.update(tokens)
        for index, token in enumerate(tokens):
            if _program_basename(token) == "eval":
                texts.add(" ".join(tokens[index + 1 :]))
    return texts


def _single_quoted_offsets(text: str) -> "set[int]":
    """Offsets in *text* that sit inside a single-quoted (or ``$'…'``) span."""
    quoted: set[int] = set()
    state = 0
    for step in _iter_shell_chars(text):
        if state == 1:
            quoted.add(step.offset)
        state = step.state
    return quoted


def _prefilter_views(text_lower: str) -> "tuple[str, ...]":
    """The spellings the cheap program pre-filter looks in.

    The raw text, and the text with line continuations folded, ``$'…'`` resolved
    and every quote and backslash removed -- the program word the shell runs, so
    ``g""it``, ``g\\it`` and ``$'\\x67it'`` are seen as ``git``. A decoding failure
    keeps the raw view only; the reader then meets the same text and fails closed.
    """
    try:
        folded = _decode_shell_quoted_literals(_shell_join_continuations(text_lower))
    except Exception:
        return (text_lower,)
    return (text_lower, _dequote_token(folded))


def argv_row_denial(text_lower: str, enabled_patterns: "list[str]") -> "tuple[str, str] | None":
    """``(pattern, note)`` of the first enabled row *text_lower* runs, else ``None``.

    Judged on the argv of every command in the input and in every nested shell
    payload. Runs inside the PreToolUse gate, which must return a decision and never
    raise. A reader failure FAILS CLOSED for a row whose program AND anchor words
    (``_UNREADABLE_ANCHORS``) the text names: the reader could not prove it is not
    that row's command, and the regex row alone cannot catch a re-spelled form.
    A text naming the program without the row's words (``git push origin x``)
    cannot be that row's command, so a reader failure there leaves it to the other
    tiers rather than refusing every ``git`` command under the reset row.
    """
    rows = [_ARGV_ROW_BY_PATTERN[p] for p in enabled_patterns if p in _ARGV_ROW_BY_PATTERN]
    views = _prefilter_views(text_lower) if rows else ()
    named = [row for row in rows if any(row.program in view for view in views)]
    if not named:
        return None
    # An escape can spell an anchor word inside a nested payload that the views do
    # not decode (``bash -c "git reset \$'\\x2d\\x2dhard'"``), so text carrying one
    # is suspect under every row it names.
    escaped = "\\" in text_lower or "$'" in text_lower
    suspect = [
        row
        for row in named
        if escaped
        or any(all(word in view for word in _UNREADABLE_ANCHORS[row.rule_id]) for view in views)
    ]
    unreadable = (
        (_RULES_BY_ID[suspect[0].rule_id].pattern, ARGV_ROW_UNREADABLE_NOTE) if suspect else None
    )
    try:
        # Fold ``\`` + newline FIRST, the way the shell removes a line continuation
        # while reading, so ``terraform -chdir=infra \<newline> destroy`` is the one
        # command bash runs rather than two the segment splitter would cut it into.
        frames = _shell_payload_walk(_shell_join_continuations(text_lower))
        if not frames:
            # The walk drops a frame its tokenizer could not read, so a text that
            # names a program yet yields no frame at all was not read either.
            logger.warning("argv_row_floor: no readable frame; refusing fail-closed")
            return unreadable
        sources = [source for source, _tokens in frames]
        inert = _inert_bodies(sources)
        payloads = _payload_texts(frames)
        printers = _printer_index(sources)
        for index, source in enumerate(sources):
            # The walk keeps one frame per text, so a body that is ALSO a real
            # payload (``bash -c '<same text>'``) is read, never skipped.
            if index and inert.get(source, False) and source not in payloads:
                continue
            if index and _printed_only(source, index, printers):
                continue
            # Comments go first, so a ``<<`` or an apostrophe in a comment is gone
            # before the heredoc and quote readers look.
            # Quoted newlines are folded while the heredoc bodies are still there to
            # be skipped; the data bodies are dropped after.
            text = _drop_literal_heredocs(_quoted_newlines_as_spaces(_strip_comments(source)))
            for segment in _split_push_command_segments(text):
                for command in _split_background(segment):
                    row = _segment_row(_segment_argv(command), rows)
                    if row is not None:
                        return _RULES_BY_ID[row.rule_id].pattern, ARGV_ROW_FLOOR_NOTE
    except Exception:
        logger.warning("argv_row_floor: shell reader failed; refusing fail-closed", exc_info=True)
        return unreadable
    return None

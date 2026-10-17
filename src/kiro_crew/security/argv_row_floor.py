"""One argv-structural floor shared by the catalog rows that name a command literal.

Eight built-in rows are a ``<program> <word>`` literal: ``terraform destroy.*``,
``git reset --hard.*``, ``dd if=.*`` and five more.  Each recognises one SPELLING
of a command, so an ordinary option between the program and the word the row
anchors on -- ``terraform -chdir=x destroy``, ``git -C . reset --hard``,
``dd bs=1M if=/dev/zero`` -- runs the identical command while the regex admits it.

An option-run regex CAN be written -- several ``aws`` and ``git push`` rows in the
catalog carry one -- but ONE shared argv table stands in for eight more per-row
option-run regexes, so the catalog patterns stay the
simple literal (pins and golden fixture included) and this floor
carries the option-awareness for all eight at once.

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

import functools
import logging
import re
from collections.abc import Callable, Iterator
from typing import NamedTuple

from kiro_crew.trust_patterns import ENV_ASSIGNMENT_RE

from .denied_rules import _RULES_BY_ID
from .readonly_bash import _COMMAND_POSITION_OPENERS, _PROGRAM_RUNNERS
from .shell_normalizer import (
    _decode_shell_quoted_literals,
    _dequote_token,
    _iter_shell_chars,
    _program_basename,
    _shell_join_continuations,
    _shell_payload_walk,
    _split_push_command_segments,
    _split_shell_words,
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
    # ``$'…'`` is resolved first, as bash does before the program sees it:
    # ``reset $'--hard'`` passes ``--hard``. The shared decoder re-quotes each value
    # so a decoded quote, space or ``>`` stays data inside one word.
    for raw in _split_shell_words(_decode_shell_quoted_literals(segment)):
        if target_pending:
            target_pending = False
            continue
        leading = _REDIRECT_OPERATOR_RE.match(raw)
        if leading is not None:
            if leading.end() == len(raw):
                target_pending = True
            continue
        cut = _unquoted_redirect_offset(raw)
        if cut >= 0:
            # A redirection glued after the word (``--hard>``) whose operator has no
            # attached target takes the NEXT word as its target, so that word must
            # not be read as an argument: ``git reset --hard> /dev/null`` is a reset.
            operator = _REDIRECT_OPERATOR_RE.match(raw[cut:])
            if operator is not None and operator.end() == len(raw) - cut:
                target_pending = True
        word = _strip_group(_dequote_token(raw if cut < 0 else raw[:cut]))
        if word:
            words.append(word)
    return words


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
    """
    if certain:
        return word.startswith(link)
    return word == link or word.startswith(link + "/") or word == link + "s"


def _subcommands(*chain: str) -> "Callable[[list[str]], list[bool]]":
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
            ahead = _operand_table(words, functools.partial(_link_on, link), ahead)
        return ahead

    return _table


def _link_on(link: str, word: str, certain: bool) -> bool:
    return _link_matches(word, link, certain)


def _operand_table(
    words: "list[str]", link: "Callable[[str, bool], bool]", ahead: "list[bool]"
) -> "list[bool]":
    """``table[i]``: a leading operand from *i* meets *link* and ``ahead`` holds after it.

    One right-to-left pass over the two states of :func:`_leading_operands` --
    ``fresh`` (no option value pending) and ``after_option`` (the word may be the
    previous option's value, so the scan goes on past it).
    """
    n = len(words)
    fresh = [False] * (n + 2)
    after_option = [False] * (n + 2)
    for i in range(n - 1, -1, -1):
        word = words[i]
        if word == "--":
            fresh[i] = after_option[i] = i + 1 < n and link(words[i + 1], True) and ahead[i + 2]
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


def _subcommand_with_option(verb: str, option: str) -> "Callable[[list[str]], list[bool]]":
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
        return _operand_table(words, lambda word, _certain: word == verb, flagged)

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


def _operand_anywhere(prefix: str) -> "Callable[[list[str]], list[bool]]":
    """Matcher: a ``key=value`` operand starting with *prefix* at any position."""

    def _table(words: "list[str]") -> "list[bool]":
        table = [False] * (len(words) + 1)
        for i in range(len(words) - 1, -1, -1):
            table[i] = words[i].startswith(prefix) or table[i + 1]
        return table

    return _table


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
    _ArgvRow("iac-teardown-cdk-destroy", "cdk", _subcommands("destroy")),
    _ArgvRow("iac-teardown-pulumi-destroy", "pulumi", _subcommands("destroy")),
    _ArgvRow("iac-teardown-terraform-destroy", "terraform", _subcommands("destroy")),
    _ArgvRow(
        "iac-teardown-kubectl-delete-namespace", "kubectl", _subcommands("delete", "namespace")
    ),
    _ArgvRow("local-destructive-git-reset-hard", "git", _subcommand_with_option("reset", "--hard")),
    _ArgvRow("reverse-shell-ncat", "ncat", _option_anywhere("e", "--exec", _NC_VALUE_SHORTS)),
    _ArgvRow("reverse-shell-nc", "nc", _option_anywhere("e", "--exec", _NC_VALUE_SHORTS)),
    _ArgvRow("local-destructive-dd-if", "dd", _operand_anywhere("if=")),
)

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
    raise. A reader failure FAILS CLOSED: the command names a row's program (the
    pre-filter below), the reader could not prove it is not that row's command, and
    the regex row alone cannot catch a re-spelled form, so it is refused under the
    first enabled row whose program the text names.
    """
    rows = [_ARGV_ROW_BY_PATTERN[p] for p in enabled_patterns if p in _ARGV_ROW_BY_PATTERN]
    views = _prefilter_views(text_lower) if rows else ()
    named = [row for row in rows if any(row.program in view for view in views)]
    if not named:
        return None
    unreadable = (_RULES_BY_ID[named[0].rule_id].pattern, ARGV_ROW_UNREADABLE_NOTE)
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
        for source, _tokens in frames:
            for segment in _split_push_command_segments(source):
                for command in _split_background(segment):
                    row = _segment_row(_segment_argv(command), rows)
                    if row is not None:
                        return _RULES_BY_ID[row.rule_id].pattern, ARGV_ROW_FLOOR_NOTE
    except Exception:
        logger.warning("argv_row_floor: shell reader failed; refusing fail-closed", exc_info=True)
        return unreadable
    return None

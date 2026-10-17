"""The inert-MENTION narrowing for the permission-verb deny rows.

One question, asked of one derived set of catalog rows: is every occurrence of a
permission verb in this command an ARGUMENT of a command that treats arguments as
data? The catalog's path-scoped ``chmod``/``chown`` rows are ``re.search``
patterns over the whole command text, so they cannot tell a verb that RUNS from
the same word handed to a search tool as a pattern -- which is why an ordinary
audit of those very rules is refused. :func:`_perm_verb_mention_only` supplies
the missing question, and ``is_denied`` consults it for that derived set and
nothing else (see ``denied_rules._PERM_VERB_MENTION_PATTERNS``).

It reuses the primitives the argv floor already relies on rather than adding a
parser: ``_shell_payload_sources`` for the frame walk (which descends ``bash
-c``, ``eval``, heredocs and command/process substitution to any depth),
``_argv_programs`` for "which command is this token an argument of", and
``_data_consumer_exempt`` for the per-token data judgement.

Every gate here is a REFUSAL, so the predicate fails closed: a construct it
cannot read keeps the deny. It lives in its own module rather than inside
``argv_floor`` because it is the one family there that narrows a catalog row
instead of adding a floor, and because that file is held to a per-module line
ceiling by ``test_no_single_module_grows_back_into_a_monolith``.
"""

import re
import shlex

from . import shell_normalizer as _shell_normalizer
from .argv_floor import _shell_payload_sources
from .denied_rules import _PERM_VERB_MENTION_VERBS
from .shell_normalizer import (
    _CONTROL_OPERATOR_RE,
    _DATA_CONSUMER_PROGRAMS,
    _argv_programs,
    _data_consumer_command_disqualified,
    _data_consumer_exempt,
    _ends_argv,
    _program_basename,
)

#
# The catalog's path-scoped ``chmod``/``chown`` rows are ``re.search`` patterns
# over the whole command text, so they cannot tell a verb that RUNS from the
# same word handed to a search tool as a pattern.  ``_perm_verb_mention_only``
# supplies the missing question -- "is every occurrence of this verb an ARGUMENT
# of a command that treats arguments as data?" -- and ``is_denied`` consults it
# for that derived set and nothing else (see ``_PERM_VERB_MENTION_PATTERNS``).
#
# It reuses the primitives the self-protection floor already relies on rather
# than adding a parser: ``_shell_payload_sources`` for the frame walk (which
# descends ``bash -c``, ``eval``, heredocs and command/process substitution to
# any depth), ``_argv_programs`` for "which command is this token an argument
# of", and ``_data_consumer_exempt`` for the per-token data judgement.
#
# Every gate below is a REFUSAL, so the predicate fails closed: a construct it
# cannot read keeps the deny.  The ones that were reachable bypasses while this
# was being written, each now closed:
#
#   * ``X=1 chmod 777 /etc/shadow`` -- ``_argv_programs`` skips the leading
#     assignment, so the verb becomes its own command's PROGRAM and
#     ``_data_consumer_exempt`` then reads ``programs[i] == "chmod"``, which IS
#     in ``_DATA_CONSUMER_PROGRAMS`` (it is listed there as a filesystem mover
#     whose own arguments are paths).  ``_PERM_VERB_MENTION_PROGRAMS`` removes
#     the permission verbs from the accepted set, so the verb can never
#     exonerate itself.
#   * ``echo 'chmod 777 /etc/x' > /tmp/s.sh`` -- ``echo``/``printf`` emit their
#     argument AS the file's content, and ``>`` is not a command separator, so
#     exonerating them would exonerate writing the command to a script.  This is
#     the same case ``_INERT_SEARCH_VERBS`` declined to open; the emitters and
#     the filesystem mutators are both removed from the accepted set, and any
#     redirect other than to ``/dev/null`` refuses outright.
#   * ``grep -h 'chmod 777 /etc/x' f | python`` -- a downstream stage can EXECUTE
#     what the search emitted.  ``_pipes_into_evaluator`` covers the shells;
#     the downstream-program gate below covers the rest by requiring every
#     program AFTER the mention to be an accepted data consumer too.
#   * ``echo "$(bash -c 'chmod 777 /etc/x')"`` -- frame 0 reads as pure data
#     (``echo``'s argument opens with a quote, not with the substitution), so the
#     mention is only caught in the NESTED frame.  That is why the walk must be
#     over every frame and why one bad frame refuses the whole exemption.
#
# Quoting is load-bearing in one place.  ``grep -nE 'chmod|chown|/etc/'`` hands
# ``shlex`` a token carrying ``|``, which ``_data_consumer_exempt`` reads as a
# control operator introducing a new program -- correct for a BARE
# ``grep -nE chmod|chown /etc/passwd``, where bash really does run
# ``chown /etc/passwd``.  The two are indistinguishable after POSIX quote
# removal, so this walk tokenizes in NON-POSIX mode (quotes retained) and masks
# operators only inside a token that is provably one SINGLE-quoted literal.
# Single quotes are absolute in POSIX shell -- no substitution, no escape, no
# operator -- so the mask states a fact about the shell rather than trusting the
# spelling.  A double-quoted token gets no mask: ``"x|$(chmod 777 /etc/y)"``
# still expands, and the mask would hide the ``|`` while the substitution ran.
# The mask is also kept OUT of the command-level disqualification, which is
# computed on the unmasked argv: ``awk '{print | "sh"}'`` must stay
# disqualified, and that reading depends on the ``|`` the mask would remove.

_PERM_VERB_WORD_RE = re.compile(r"\b(?:" + "|".join(sorted(_PERM_VERB_MENTION_VERBS)) + r")\b")

#: Data consumers that may NOT exonerate a permission-verb mention.  Subtracted
#: from ``_DATA_CONSUMER_PROGRAMS`` rather than replacing it, so a consumer added
#: there in future is inherited here and a mistake in this list costs a false
#: positive (the safe direction), never a bypass.
#:
#: Four reasons to be on it: the program MUTATES the filesystem (so its own
#: arguments are a destination, and a permission verb among them is not merely
#: text), it EMITS its argument as output (so a redirect turns the mention into
#: a script on disk), it SPAWNS a helper named by an option or script operand
#: (so that operand can run code instead of remaining text), or it names its
#: SINK with an option rather than by operand position (so ``_writes_a_second_operand``,
#: which counts operands, cannot see the write).
_PERM_VERB_MENTION_EXCLUDED_PROGRAMS: frozenset[str] = frozenset(
    {
        # permission verbs themselves -- a verb must never exonerate itself
        "chmod",
        "chown",
        "chgrp",
        # filesystem mutators
        "cp",
        "mv",
        "ln",
        "rm",
        "mkdir",
        "rmdir",
        "touch",
        "tee",
        # argument emitters -- the mention becomes the output verbatim
        "echo",
        "printf",
        "print",
        # helper spawners -- an option or script operand can run another command
        "ack",
        "ag",
        "awk",
        "less",
        "more",
        "rg",
        "sed",
        "sort",
        # option-named sinks -- the write target is a FLAG's argument, which
        # operand counting cannot reach.  macOS ``/usr/bin/base64`` takes
        # ``-o out_file`` / ``--output=FILE`` (its own usage line; GNU coreutils
        # has no such flag, and the platform that has it is one this repo
        # supports), and ``yq -i`` / ``--inplace`` rewrites its operand in place.
        # Excluded rather than option-matched: an option allow-list fails OPEN on
        # the option nobody enumerated, the same reason ``rg``/``sed``/``sort``
        # are here instead of having their flags parsed.  ``jq`` stays exempt --
        # it has no in-place flag -- and ``xxd``/``uniq`` stay exempt because
        # their sink is an OPERAND, which is countable.
        #
        # ``file -C -m NAME`` is the same criterion reached by a different verb:
        # it COMPILES the magic file and writes ``NAME.mgc``, truncating whatever
        # is there.  The destination is derived from the ``-m`` flag's argument
        # and carries a suffix the operand never spells, so neither operand
        # counting nor a sink-token check can see the write.
        "base64",
        "file",
        "yq",
    }
)

_PERM_VERB_MENTION_PROGRAMS: frozenset[str] = (
    _DATA_CONSUMER_PROGRAMS - _PERM_VERB_MENTION_EXCLUDED_PROGRAMS
)

#: Longest command the mention walk will read.  This is a COST bound, not a
#: correctness one, and it can only refuse: over the bound the deny stands, so
#: there is nothing to bypass by padding.  It exists because the walk descends
#: every nested payload, and the self-protection floor that shares that descent
#: skips it for text carrying no expansion machinery (``_self_floor_can_fire``) --
#: so without a bound a 20k command of plain words would pay a descent today's
#: gate never pays.  A search command a human actually types is two orders of
#: magnitude under this.
_PERM_VERB_MENTION_MAX_CHARS = 4096

#: The redirects an exonerated frame may carry.  Anything else can persist the
#: mention (``> /tmp/s.sh``) or feed it somewhere this walk cannot see.  Matched
#: as glued text so the spaced spelling (``> /dev/null``) is refused too --
#: over-strict, which is the direction that cannot lose a denial.
#:
#: The second alternative is file-descriptor DUPLICATION (``2>&1``, ``>&2``): it
#: points one stream at where another already goes and names no new destination,
#: so it cannot persist the mention.  A real sink alongside it is a token of its
#: own and is still judged on its own -- ``... > /tmp/s.sh 2>&1`` stays refused
#: because of the first token, not the second.
#:
#: BOTH alternatives end at a token boundary, and that lookahead is load-bearing.
#: This pattern is substituted out of the WHOLE frame text, not one token, so an
#: unanchored alternative would consume a PREFIX of a longer token and carry its
#: own ``>`` away with it: ``>&1x`` is not a redirection at all -- the shell reads
#: it as ``> &1x``, a write to the file named ``&1x`` -- yet stripping ``>&1``
#: leaves the inert ``x`` and no ``>`` for the check below to see.  ``>/dev/nullx``
#: is the same shape one alternative over.  Anchoring keeps the strip confined to
#: a redirect that really is the whole token.
_PERM_VERB_MENTION_SINK_RE = re.compile(r"(?:>>?/dev/null|\d*>&\d+)(?=\s|$)")

#: File-descriptor redirection spellings that carry an ``&`` the shell does not
#: read as a command boundary -- the asymmetry ``_ends_argv`` encodes on purpose.
#: Stripped before the uncut-operator question so an audit may end in ``2>&1``.
_REDIRECT_AMP_RE = re.compile(r"\d*>&\d+|&>>?")

#: The tokens ``shlex`` hands over as a control operator STANDING ALONE.  Only
#: these carry an operator that ``_argv_programs`` reads as a boundary, so only
#: these may carry one and still be judged -- see ``_uncut_control_operator``.
_WHOLE_CONTROL_OPERATOR_TOKENS: frozenset[str] = frozenset({"&", "&&", "|", "||", ";", ";;", "\n"})

#: Accepted data consumers that WRITE their SECOND operand.  ``xxd in out`` and
#: ``uniq in out`` both truncate ``out``; every other member of the accepted set
#: either reads its operands or writes only to standard output.  They are not
#: excluded outright because the exemption's commonest shape pipes INTO them
#: with no operand at all (``... | uniq | head``, ``... | xxd``), which writes
#: nothing -- see ``_writes_a_second_operand``.
_SECOND_OPERAND_WRITER_PROGRAMS: frozenset[str] = frozenset({"uniq", "xxd"})


def _single_quoted_literal(token: str) -> bool:
    """True if *token* is provably ONE single-quoted literal.

    Requires the quote at both ends and NO single quote between them: without
    that last condition ``'a'|'b'`` (two literals glued around a real pipe)
    reads as one literal and its operator would be masked away.
    """
    return len(token) >= 2 and token[0] == "'" == token[-1] and "'" not in token[1:-1]


def _double_quoted_literal(token: str) -> bool:
    """True if *token* is provably ONE double-quoted literal with NO expansion.

    Inside double quotes the shell reads ``;``, ``&`` and ``|`` as ordinary text,
    so a fully double-quoted word carries no control operator -- which makes the
    far more common spelling of an audit (``grep -rnE "<verb>|/etc/" src/``)
    exempt for the same reason the single-quoted spelling already is.

    Two conditions narrow it, and both are load-bearing:

    * no ``"`` between the ends, so ``"a"|"b"`` (two literals glued around a REAL
      pipe) cannot read as one literal -- the same trap ``_single_quoted_literal``
      guards against;
    * no ``$`` and no backtick anywhere inside.  Double quotes do NOT suppress
      expansion, so ``"$(ls /etc/x& <verb> -R g+w /etc/x)"`` holds an operator the
      shell really does act on.  Rather than parse the substitution, refuse the
      mask whenever the machinery that could carry one is present.
    """
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return False
    inner = token[1:-1]
    return '"' not in inner and "$" not in inner and "`" not in inner


def _mask_quoted_operators(token: str) -> str:
    """*token* with control operators neutralized inside a quoted literal.

    The replacement is a word character, so it cannot introduce a boundary
    ``_ends_argv`` or ``_data_consumer_exempt`` would read.  Any other token is
    returned unchanged, which keeps a REAL operator visible.
    """
    if _single_quoted_literal(token):
        return "'" + _CONTROL_OPERATOR_RE.sub("_", token[1:-1]) + "'"
    if _double_quoted_literal(token):
        return '"' + _CONTROL_OPERATOR_RE.sub("_", token[1:-1]) + '"'
    return token


def _uncut_control_operator(token: str) -> bool:
    """True if *token* carries a control operator this walk cannot read.

    ``shlex`` splits on whitespace alone, so an operator the author GLUED to a
    word arrives inside that word.  ``_argv_programs`` tracks command boundaries
    by whole tokens, which means a program name sitting after a glued operator is
    never recorded as a program at all: ``grep -h '<verb> 600 /etc/shadow' f|bash``
    tokenizes to ``f|bash``, and ``bash`` is attributed to ``grep`` as one of its
    arguments.  Every gate this walk owns -- the mention's own program, the
    downstream-program sweep -- reads ``_argv_programs``, so one invisible program
    defeats all of them at once.

    The question is therefore asked DIRECTLY: is this token one of the operator
    tokens the tokenizer hands over standing alone?  If it is not, an operator
    inside it is glued, the frame's argv is not what it appears to be, and the
    deny stands.

    An earlier revision delegated to ``_ends_argv``, and that delegation was itself
    the defect -- a live bypass, not a near miss.  ``_ends_argv`` answers "does the
    argv end HERE", which is True for any token containing ``|``.  ``f|bash``
    satisfies it (the argv does end) while the program that follows stays
    unreadable, so the refusal never fired.  Phrasing the old check against the
    whole operator class instead of ``&`` alone did not help, because the class was
    never the problem.

    Asked on the MASKED argv, so an operator proven to sit inside a quoted literal
    has already been neutralized and a quoted alternation keeps its exemption.

    A REDIRECTION spelling of ``&`` is stripped before the question is asked.
    ``2>&1``, ``>&2`` and ``&>/dev/null`` duplicate a file descriptor; none of them
    starts a command, which is why ``_ends_argv`` declines to cut there.  Stripping
    only these fixed shapes keeps the refusal on every ``&`` that is not one of
    them, so ``ls /etc/x&2>&1 <verb> ...`` still refuses -- the bare ``&`` survives
    the strip.
    """
    if token in _WHOLE_CONTROL_OPERATOR_TOKENS:
        return False
    return bool(_CONTROL_OPERATOR_RE.search(_REDIRECT_AMP_RE.sub("", token)))


#: Characters that let a token DE-QUOTE into a word it does not literally spell.
#: A token holding none of them tokenizes to itself, so a raw-text miss on
#: ``_PERM_VERB_WORD_RE`` is also a miss after de-glue -- see
#: ``_deglues_to_perm_verb``, which uses this as its pre-filter.
_GLUE_CHARS_RE = re.compile(r"['\"\\$`]")

#: Characters that leave a PROGRAM name undecided at scan time: glob metacharacters
#: (the name is chosen by the working directory) and expansion syntax (chosen by
#: the environment).  A program carrying one is an unknown command -- see
#: ``_frame_voids_perm_verb_mention``.
_UNRESOLVED_PROGRAM_RE = re.compile(r"[?*\[\]${}`]")


def _deglues_to_perm_verb(token: str) -> bool:
    r"""True if *token* de-quotes to a permission verb it does not literally spell.

    The per-token walk keys on ``_PERM_VERB_WORD_RE`` over RAW text, so a token
    spelled ``ch""mod`` never enters the loop at all and none of the position gates
    below ever see it.  A shell does not need the raw spelling: ``ch""mod``,
    ``ch'mod'``, ``"ch"mod``, ``ch\mod`` and ``ch$()mod`` all execute the verb, and
    the deny view they are matched against de-quotes them back to it.

    Widening the loop's TRIGGER, rather than adding a gate beside it, is what makes
    the wrapper spellings fall to the gates that already exist: ``command``, ``env``,
    ``exec``, ``nohup``, ``time``, ``nice``, ``sudo``, ``xargs`` and ``find -exec``
    all leave the verb at an ARGUMENT position, where ``programs[index]`` is the
    wrapper and no wrapper is an accepted data consumer.  A hand-kept wrapper list
    would have to grow with every new pass-through program; the position gate does
    not.

    Pre-filtered on ``_GLUE_CHARS_RE`` so the ordinary token costs one regex search
    instead of a tokenize.  Fails closed: an untokenizable token returns True.
    """
    if not _GLUE_CHARS_RE.search(token):
        return False
    try:
        parts = _shell_normalizer._shell_tokens(token)
    except Exception:
        return True
    return any(_PERM_VERB_WORD_RE.search(part) for part in parts)


def _frame_voids_perm_verb_mention(source: str) -> bool:
    r"""True if *source* voids the exemption on its own, before any token is judged.

    Asked through ``_shell_tokens``, the tokenizer the deny VIEWS themselves are
    built on, rather than the ``shlex.split(posix=False)`` argv the per-token walk
    uses.  That is deliberate: where the two readings disagree, this is the one that
    describes the text Pass 2 actually matched, and the walk must not clear a text
    it is not reading.  Two frame-level facts void the exemption.

    **A permission verb in PROGRAM position.**  Without this an inert mention
    elsewhere in the command answers "every occurrence is inert" about a view that
    does contain a real invocation, and re-enables a permission change:

        ch""mod 777 ~/.ssh/id_rsa ; grep -h 'chmod 777 /etc/x' f

    The downstream sweep cannot close that by becoming symmetric.  It walks
    ``programs[index + 1:]`` on purpose: an UPSTREAM stage that is not an accepted
    consumer is normal and legitimate (``git show ... | grep -nE ...``), so refusing
    on any non-accepted upstream program would refuse the audit this narrowing
    exists to allow.  The invariant that closes it is narrower -- no frame may RUN
    the verb -- and it is asked of every frame regardless of where the mention sits,
    which is what removes the ordering asymmetry.

    **A program this walk cannot resolve.**  ``ch?od 777 x`` runs the verb whenever
    a matching name exists in the working directory, and no de-glue can see that:
    the name is decided by the filesystem, not by the text.  ``${x}chmod`` is the
    same shape with the environment deciding.  An unresolved program is an unknown
    command, so the deny stands rather than being cleared on a guess.

    Fails closed: an unreadable frame returns True and the deny stands.
    """
    try:
        programs = _argv_programs(_shell_normalizer._shell_tokens(source))
    except Exception:
        return True
    for program in programs:
        if program in _PERM_VERB_MENTION_VERBS:
            return True
        if _UNRESOLVED_PROGRAM_RE.search(program):
            return True
    return False


def _writes_a_second_operand(tokens: "list[str]") -> bool:
    """True if any command in *tokens* hands a second-operand writer a sink.

    ``xxd <verb> /usr/local/bin/git`` puts no permission verb in program position
    -- the verb is the INPUT file's name -- so every position gate reads it as
    inert.  The command still truncates the protected path named in its second
    operand, and that write is protection the matched rule was giving before this
    narrowing existed.  ``uniq <verb> /usr/local/bin/git`` is the same shape.

    Counted PER COMMAND, which is what keeps ``... | uniq | head`` readable: a
    writer invoked with no operand writes nothing.  The boundary walk deliberately
    mirrors ``_argv_programs``, since its reading is the one the rest of this walk
    is judged against.

    An option's own ARGUMENT counts as an operand (``xxd -l 100 f`` reads as two),
    which over-refuses in the fail-closed direction: the command is then denied
    exactly as it is with no narrowing at all.  A bare ``-`` counts too, because it
    names standard input as the INPUT and leaves the next word the sink.
    """
    program = ""
    expect_program = True
    operands = 0
    for token in tokens:
        if expect_program and token and not _shell_normalizer.ENV_ASSIGNMENT_RE.match(token):
            program = _program_basename(token)
            expect_program = False
            operands = 0
        elif program in _SECOND_OPERAND_WRITER_PROGRAMS and not (
            token.startswith("-") and len(token) > 1
        ):
            operands += 1
            if operands >= 2:
                return True
        if _ends_argv(token):
            program = ""
            expect_program = True
            operands = 0
    return False


def _perm_verb_mention_only(text_lower: str) -> bool:
    """True if every permission-verb occurrence in *text_lower* is inert data.

    "Inert" means the word sits at an ARGUMENT position of a command that
    treats arguments as data, in the command itself AND in every nested shell
    payload.  One occurrence in program position, one unreadable construct, or
    one frame that fails to tokenize refuses the whole thing.

    Refuses outright past ``_PERM_VERB_MENTION_MAX_CHARS``, which bounds the
    descent's cost without weakening anything -- see that constant.

    Returns False when the verb appears nowhere as a WORD.  The deny pattern can
    match a substring (``foochmodbar /etc/x``), and answering "no occurrence, so
    all occurrences are inert" would silently widen those inputs; refusing keeps
    them exactly as they are today.
    """
    if len(text_lower) > _PERM_VERB_MENTION_MAX_CHARS:
        return False
    if not _PERM_VERB_WORD_RE.search(text_lower):
        return False
    # A newline is a command separator that ``shlex`` consumes as whitespace, so
    # a second command on a second line would be read as arguments of the first.
    # Pass 2 of ``is_denied`` splits on newlines, so a multi-line search is still
    # judged line by line -- this only refuses to judge the joined text.
    if "\n" in text_lower or "\r" in text_lower:
        return False
    found = False
    for source in _shell_payload_sources(text_lower):
        # A frame that RUNS the verb, or whose program cannot be resolved, voids
        # the exemption before any token is judged -- see
        # ``_frame_voids_perm_verb_mention``.
        if _frame_voids_perm_verb_mention(source):
            return False
        if ">" in _PERM_VERB_MENTION_SINK_RE.sub("", source):
            return False
        try:
            tokens = shlex.split(source, posix=False)
        except ValueError:
            # Unbalanced quotes -- the argv this would produce is a guess.
            return False
        if not tokens:
            continue
        masked = [_mask_quoted_operators(token) for token in tokens]
        # An operator this frame's argv reader cannot see is a command this walk
        # cannot judge, so the deny stands -- see ``_uncut_control_operator``.
        if any(_uncut_control_operator(token) for token in masked):
            return False
        # A writer handed both an input and a sink is mutating a path, not reading
        # one, however inert the verb among its operands looks -- see
        # ``_writes_a_second_operand``.
        if _writes_a_second_operand(masked):
            return False
        programs = _argv_programs(masked)
        # Computed on the UNMASKED argv on purpose -- see the block comment.
        disqualified = _data_consumer_command_disqualified(tokens)
        for index, token in enumerate(tokens):
            # A token that only DE-QUOTES to the verb is judged by the same position
            # gates as one that spells it -- see ``_deglues_to_perm_verb``.
            if not _PERM_VERB_WORD_RE.search(token) and not _deglues_to_perm_verb(token):
                continue
            found = True
            if programs[index] not in _PERM_VERB_MENTION_PROGRAMS:
                return False
            if not _data_consumer_exempt(
                index,
                masked[index],
                programs,
                masked,
                command_disqualified=disqualified,
            ):
                return False
            # Nothing DOWNSTREAM of the mention may be able to execute it.
            # Upstream stages only feed data in, so they are not checked -- that
            # is what keeps ``git show … | grep -nE 'chmod|/etc/'`` readable.
            for program in programs[index + 1 :]:
                if program and program not in _PERM_VERB_MENTION_PROGRAMS:
                    return False
    return found

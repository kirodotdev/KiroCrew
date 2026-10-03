"""The argv-structural floor under the ``local-destructive-chmod-777`` row.

The catalog row is the literal ``chmod 777.*``: it recognises one SPELLING of the
world-rwx mode change, so any option between the program and the mode -- ``chmod
-R 777 ~``, a packed ``-Rv``, a split ``-R -v``, a long ``--recursive`` -- runs the
identical permission change while the PreToolUse gate admits it.
A regex that admits "any run of options" is the wrong instrument: it either pins
a few option spellings (and the one nobody enumerated, ``-Rvfc``, still bypasses)
or opens a gap the ReDoS screens in ``denied_rules`` exist to refuse.

So the question is asked of the ARGV instead, the way the self-protection rows
are asked (``argv_floor``): for every
command in the input, including each nested shell payload, is ``chmod`` the
program that runs, and does the FIRST OPERAND after its options spell the
world-rwx numeric mode?  Options are recognised by shape at every position --
GNU ``chmod`` permutes them, so ``chmod 777 -R ~`` and ``chmod -R 777 ~`` are one
command -- and the mode is whatever non-option word comes first, however many
options precede it and however they are packed.

It is a UNION with the regex row, never a replacement: the row stays in the
regex tier (so a text the tokenizer cannot see into is still caught, and a
tokenizer failure cannot fail OPEN), the floor runs only while the row is in the
effective set (an operator opt-out of the row disables both), and the catalog
pattern, the golden fixture and the governance pin map are untouched.

Every primitive here is one the package already relies on: the frame walk
(``_shell_payload_walk``) that descends ``bash -c``, ``eval``, heredocs and
substitutions to any depth; ``_argv_programs`` for "which command is this word
an argument OF"; ``_program_basename`` for the program a word invokes once
quoting, wrappers and redirects come off; and the shell reader's data-consumer
vocabulary to say which program a ``chmod`` word is an argument OF.  A word that
is a data consumer's argument (``grep -rn chmod src/``, ``rg -n chmod *``) is that
program's data and the words after it are that program's; a word under a program
that is NOT a data consumer -- ``sudo``, ``env``, ``nice``, ``xargs``,
``find -exec``, ``busybox``, a wrapper nobody enumerated -- or ``chmod`` itself
in program position is read as the invocation it is.

This module sits beside ``perm_verb_mention`` at the top of the package's
dependency order: it imports the reader and the floor, and nothing imports it
but the facade's evaluator (through ``_submodule``).  It lives in its own file
because ``argv_floor`` is held to a per-module line ceiling by
``test_no_single_module_grows_back_into_a_monolith``.
"""

import re

from .perm_verb_mention import _REDIRECT_AMP_RE
from .shell_normalizer import (
    _CONTROL_OPERATOR_RE,
    _DATA_CONSUMER_PROGRAMS,
    _REDIRECT_START_RE,
    _argv_programs,
    _ends_argv,
    _glob_could_expand_to,
    _program_basename,
    _shell_payload_walk,
)

#: The program this floor is about.  Compared against ``_program_basename`` of a
#: word, so ``/bin/chmod``, ``"chmod"``, ``ch''mod`` and ``chmod>/dev/null`` all
#: read as it, and ``--chmod=+x`` (a ``git update-index`` flag) does not.
_CHMOD_PROGRAM = "chmod"

#: A numeric mode that grants world read, write AND execute, as GNU ``chmod``
#: reads one: the permission triple ``777`` with an optional special-bits digit
#: in front, any run of leading zeros before that (``mode_compile`` folds octal
#: digits into one value and refuses only a value over ``07777``, so ``00777``
#: and ``000000777`` are ``777``), and optionally the ``=`` or ``+`` operator in
#: front -- ``=777`` sets exactly these bits and ``+777`` adds them, both measured
#: against the real binary, while ``-777`` REMOVES them and is not a grant.
#: ``1777``/``2777``/``4777``/``7777`` are the triple plus sticky, setgid or setuid,
#: which narrows nothing the row exists to refuse.  Symbolic spellings of the same
#: bits (``a+rwx``, ``a=rwx``) and comma lists (``u+x,=777``) are a different
#: grammar -- a letter clause is relative to the current bits and the umask, a
#: list is evaluated clause by clause -- and OUT of this floor on purpose: the
#: issue this closes asks that symbolic modes stay untouched.  Recorded as
#: residuals in the module's test file, pinned to the current answer.
_WORLD_RWX_MODE_RE = re.compile(r"[=+]?0*[0-7]?777\Z")

#: The spellings a brace- or glob-shaped mode word is tested against, for
#: :func:`_glob_could_expand_to`: bash expands ``{777,755}`` to ``777 755`` and
#: ``7{7,5}7`` to ``777 757`` BEFORE ``chmod`` runs, so the first word out is the
#: mode and it is one of these.  Leading zeros beyond one are not enumerated: a
#: brace group that produces ``00777`` is a spelling nobody reaches by accident,
#: and the literal (non-brace) path above reads any run of them.
_WORLD_RWX_MODE_SPELLINGS: tuple[str, ...] = tuple(
    f"{prefix}{special}777"
    for prefix in ("", "=", "+")
    for special in ("", "0", "1", "2", "3", "4", "5", "6", "7")
)


def _is_world_rwx_mode(word: str) -> bool:
    """True if *word*, as ``chmod`` receives it, is a world-rwx numeric mode.

    A literal word is matched as it stands.  A word carrying brace or glob syntax
    is read as what bash's expansion can make of it -- ``{777,755}`` puts ``777``
    first, ``7{7,5}7`` yields ``777`` -- and refused if any expansion is a
    world-rwx mode, the same fail-closed reading the self-protection floor gives a
    glob at program position.  Over-strict on ``{755,777}``, whose first word is
    the mode ``755`` and whose ``777`` is a file; nobody spells a mode that way by
    accident.  A parameter or arithmetic expansion stays literal, as everywhere in
    the reader: its value is not knowable without running the line.
    """
    if _WORLD_RWX_MODE_RE.fullmatch(word) is not None:
        return True
    return _glob_could_expand_to(word, _WORLD_RWX_MODE_SPELLINGS)


#: The one ``chmod`` long option that takes a VALUE -- the file whose mode is
#: copied.  With it present the command has no mode operand at all (its first
#: operand is a FILE), so the floor has nothing to read and says so.  GNU
#: ``getopt_long`` accepts any unambiguous abbreviation; ``--ref`` is the shortest
#: that is not also a prefix of ``--recursive``.
_REFERENCE_OPTION = "--reference"
_REFERENCE_ABBREVIATION_MIN = len("--ref")


#: A control operator the shell reads INSIDE a word, including a bare ``&``.  The
#: shared ``_CONTROL_OPERATOR_RE`` is the same class, but ``_ends_argv`` declines
#: to cut at a glued ``&`` because a redirection's ``>&`` carries one; here the
#: redirection has already been split off, so what is left of an ``&`` is bash's
#: own command boundary: ``./project&echo x`` runs ``echo`` as a NEW command.
_GLUED_BOUNDARY_RE = re.compile(r"[;&|\n]")

#: A redirection OPERATOR inside a word, the way bash lexes it: the word is
#: everything before the operator, UNLESS that prefix is a run of digits (or ``&``,
#: or ``{name}``), in which case it is the descriptor side of the redirection and
#: the word is empty.  ``+777>/dev/null`` is therefore the word ``+777`` plus a
#: redirect (measured: it sets ``777``), while ``777>/dev/null`` is fd 777
#: redirected and no word at all (measured: ``chmod: missing operand``).
_REDIRECT_OPERATOR_RE = re.compile(r">{1,2}[&|!]?|<{1,3}")
_DESCRIPTOR_PREFIX_RE = re.compile(r"\A(?:\d+|&|\{[A-Za-z_][A-Za-z0-9_]*\})\Z")


def _split_word(token: str) -> "tuple[str, bool]":
    """``(word, ends_command)`` for *token* as bash would hand it to ``chmod``.

    *word* is the argv element the program receives -- the text before any
    attached redirection and before any glued control operator -- and
    *ends_command* says whether a glued operator closes the command here, so the
    words after it belong to the NEXT command (``./project&echo --reference=t``
    hands ``chmod`` the file ``./project``; ``echo`` and its option are not its).

    An attached redirection is cut first, because its own ``&`` (``>&2``,
    ``&>/dev/null``) is the redirection's, not a boundary.  A descriptor-shaped
    prefix (digits, ``&``, ``{name}``) is the redirection's too and leaves no word.
    The leading redirection shape (a word that IS a redirection, like
    ``2>/dev/null``) is the caller's case and is handled before this is asked.
    """
    redirect = _REDIRECT_OPERATOR_RE.search(token)
    tail = ""
    if redirect is not None:
        word = token[: redirect.start()]
        tail = token[redirect.end() :]
        if _DESCRIPTOR_PREFIX_RE.match(word):
            word = ""
    else:
        word = token
    boundary = _GLUED_BOUNDARY_RE.search(word)
    if boundary is not None:
        return word[: boundary.start()], True
    # A boundary inside the redirection's TARGET (``>out&echo x``) also ends the
    # command; the target itself is the shell's and is never a word here.
    return word, _GLUED_BOUNDARY_RE.search(tail) is not None


def _is_reference_option(option: str) -> bool:
    """True if *option* is ``--reference`` or an unambiguous abbreviation of it."""
    name = option.split("=", 1)[0]
    return len(name) >= _REFERENCE_ABBREVIATION_MIN and _REFERENCE_OPTION.startswith(name)


def _chmod_argv_sets_world_rwx(argv: "list[str]") -> bool:
    """True if *argv* (the words AFTER the ``chmod`` word) names a world-rwx mode.

    Reads the words the way the shell and then GNU ``chmod`` do.  A REDIRECTION is
    the shell's, not ``chmod``'s: ``2>/dev/null``, ``>file``, ``&>/dev/null`` and
    ``2>&1`` never reach the program's argv, and a bare operator (``>``, ``2>``)
    takes the NEXT word with it as its target -- so ``chmod -R 2>/dev/null 777 ~``
    and ``chmod -R > /dev/null 777 ~`` both hand ``chmod`` the argv ``-R 777 ~``,
    which is what is read here.  Of what remains, every ``-x`` / ``--long`` word is
    an option wherever it sits (GNU permutes), ``--`` ends option parsing,
    ``--reference`` means the command carries NO mode operand (its first operand is
    a file, even one spelled ``777``), and the FIRST remaining word is the mode.

    The verdict waits until the whole argv has been read, because the option that
    changes the reading can come AFTER the word it changes: ``chmod -v 777
    --reference=t f`` copies ``t``'s mode onto the files ``777`` and ``f``.

    A word beginning with ``-`` that is really a symbolic mode (``chmod -x f``) is
    skipped as an option here, which costs nothing: it is not a grant, and the word
    after it is a FILE that this reads as the mode -- a reading that can only refuse
    a file literally named ``777``, the fail-closed direction.

    Stops at the first word that ends the argv (``;``, ``|``, ``&&``, a comment);
    a glued operator leaves the part before it to ``chmod`` -- see
    :func:`_operand_head`.
    """
    options_open = True
    reference = False
    mode: "str | None" = None
    redirect_target_pending = False
    reference_file_pending = False
    for token in argv:
        if token.startswith("#"):
            break
        # A word that IS a redirection (``2>/dev/null``, ``>``, ``&>/dev/null``) is
        # judged on the RAW word: its ``&`` is the operator's own, not a boundary.
        # It can sit between any two argv words, so it never settles a claim the
        # previous word made on the next one.
        redirect = _REDIRECT_START_RE.match(token)
        if redirect is not None:
            if redirect.end() == len(token):
                redirect_target_pending = True
            elif _GLUED_BOUNDARY_RE.search(token[redirect.end() :]):
                break
            continue
        head, ends_command = _split_word(token)
        if not head:
            pass
        elif redirect_target_pending:
            redirect_target_pending = False
        elif reference_file_pending:
            reference = True
            reference_file_pending = False
        elif options_open and head == "--":
            options_open = False
        elif options_open and head.startswith("-") and len(head) > 1:
            if head.startswith("--") and _is_reference_option(head):
                if "=" in head:
                    reference = True
                else:
                    reference_file_pending = True
        elif mode is None:
            mode = head
        if ends_command or _ends_argv(token):
            break
    if reference or mode is None:
        return False
    return _is_world_rwx_mode(mode)


def _names_chmod_invocation(token: str, program: str) -> bool:
    """True if *token* is the ``chmod`` that RUNS in its command, not a mention.

    *program* is what ``_argv_programs`` attributes the word to.  Three readings:

    * The word is its command's own program (``program`` is ``chmod``), or an
      argument of a program that EXECUTES its arguments -- ``sudo``, ``env``,
      ``nice``, ``time``, ``xargs``, ``find -exec``, ``busybox``, ``ssh``, a
      ``docker exec``.  No such program is a data consumer, so the test is
      membership in the shared ``_DATA_CONSUMER_PROGRAMS`` vocabulary, and a
      wrapper nobody enumerated is read as executing by default (fail closed).
    * An argument of a data consumer is DATA to that program, and the words after
      it are that program's, not ``chmod``'s: ``grep -rn chmod src/``,
      ``ls -l /bin/chmod``, ``rg -n chmod *``, ``sed -i 's/chmod -R 777/…/' f``.
      This is the WHOLE consumer set, not the narrower set the inert-mention
      narrowing accepts: ``rg``, ``sed``, ``awk`` and ``echo`` are excluded there
      because they can EMIT or SPAWN, which matters for exonerating a row the regex
      already matched -- but none of them hands its argv to ``chmod``, so reading
      ``rg``'s bare ``*`` as ``chmod``'s mode would refuse an ordinary search.  The
      regex row keeps catching the literal under them (``echo chmod 777 ~``) on its
      own, and the mention narrowing keeps deciding whether that is inert.
    * ...unless a control operator is GLUED inside the word (``f|chmod``):
      ``_argv_programs`` opens a new command only between whole tokens, so it
      attributes the word to the consumer BEFORE the operator while the shell
      runs what comes after it.  ``_program_basename`` takes the trailing
      segment, so the word names ``chmod`` as a PROGRAM, and it is read as one.
    """
    if _program_basename(token) != _CHMOD_PROGRAM:
        return False
    if program == _CHMOD_PROGRAM or program not in _DATA_CONSUMER_PROGRAMS:
        # ``chmod`` is itself in the consumer vocabulary (its own arguments are
        # paths), so program position is tested FIRST: the word that IS the
        # program is the invocation, whatever vocabulary lists its name.
        return True
    return _CONTROL_OPERATOR_RE.search(_REDIRECT_AMP_RE.sub("", token)) is not None


def _frame_sets_world_rwx(tokens: "list[str]") -> bool:
    """True if one command in this frame's *tokens* is a world-rwx ``chmod``."""
    programs = _argv_programs(tokens)
    for index, token in enumerate(tokens):
        if not _names_chmod_invocation(token, programs[index]):
            continue
        if _chmod_argv_sets_world_rwx(tokens[index + 1 :]):
            return True
    return False


#: Characters without which no word can de-quote or expand into a spelling it
#: does not literally contain.  The frame walk resolves quoting, escapes, ANSI-C
#: literals, empty-quote splices, same-line assignments and printf escapes in a
#: nested payload -- each needs one of these in the raw text -- so a command
#: carrying none of them and not the literal word can never reach the predicate.
#: The pre-filter is a NECESSARY condition for a hit, never the verdict: a false
#: positive here only pays for the walk the self-protection floors pay anyway.
_GLUE_CHARS_RE = re.compile(r"['\"\\$`]")


def _chmod_floor_can_fire(text_lower: str) -> bool:
    """Cheap necessary condition for :func:`_is_chmod_world_rwx` to return True."""
    return _CHMOD_PROGRAM in text_lower or _GLUE_CHARS_RE.search(text_lower) is not None


def _is_chmod_world_rwx(text_lower: str) -> bool:
    """True if *text_lower* runs ``chmod`` with a world-rwx numeric mode.

    Judged on the argv of the command itself and of every nested shell payload
    (``bash -c``, ``eval``, ``$( )``, heredocs), so a wrapper buys nothing.  The
    contract is the floor's: callers pass already-lowercased text, as
    ``is_denied`` does.

    Runs inside the PreToolUse gate, which must return a DECISION and never
    raise, so an exception from the walk is a False here: the regex row this
    floor is a union with is still in the tier, and that is what keeps a reader
    hiccup from being a bypass of the literal spelling.
    """
    if not _chmod_floor_can_fire(text_lower):
        return False
    try:
        frames = _shell_payload_walk(text_lower)
    except Exception:
        return False
    for _source, tokens in frames:
        try:
            if _frame_sets_world_rwx(tokens):
                return True
        except Exception:
            continue
    return False

"""The tar grammar the git/tar self-target floor reads a ``tar`` argv with.

GNU tar's option arity (which long and short options take a value, in either
case), the old-style bundle's archive slot (``tar cLf 1024 host:a.tar dir``), the
``host:file`` archive test, and the state one floor read shares with every read
nested in it: the memo, the command as submitted, and the frame's glob words.
``argv_floor_git_tar`` reads the tar argv with these and owns everything else.
Tar's short options are case-sensitive and the floor reads a command lowered,
so a bundle or an option word is read in its submitted case when that case can
be recovered, and in every case it could have been in when it cannot.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch
through it reaches this module (:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from .argv_floor_hosts import _host_is_self
from .argv_floor_ssh import _decode_scp_host, _operand_targets_self
from .shell_normalizer import _push_option_matches

if TYPE_CHECKING:
    from collections.abc import Iterable


#: What one outermost git/tar floor read (``_is_git_tar_to_self``) has already
#: worked out, shared by every read nested in it through ``_reenter_floors``, so
#: a value or environment many commands carry is read once: the literal read of
#: a command value, a carried command no floor refused (with the chain it was
#: read on), the parse of a ``TAR_OPTIONS`` or ``GIT_CONFIG_PARAMETERS`` value,
#: a frame's glob words, and the characters re-entered so far.  None outside a read.
_READ_MEMO: "ContextVar[dict[tuple[object, ...], Any] | None]" = ContextVar(
    "git_tar_floor_read_memo", default=None
)

#: The command as submitted (original case) of the outermost git/tar floor read,
#: kept for every read nested in it: tar's short options are case-sensitive, so
#: only this text tells ``L`` (takes a value) from ``l`` in an old-style bundle
#: (``_tar_bundle_archive_slot``).  Not a parameter: each carried command is cut
#: from the lowered text, so none has an original-case copy to hand down.
_RAW_COMMAND: "ContextVar[str | None]" = ContextVar("git_tar_floor_raw_command", default=None)

#: The frame the git/tar command being read comes from: its de-quoted words, and
#: those whose glob bash expands, joined by NUL (``_glob_word_spellings``).
_GLOB_WORDS: "ContextVar[tuple[frozenset[str], str]]" = ContextVar(
    "git_tar_glob_words", default=(frozenset(), "")
)


def _glob_host_is_self(word: str, host: str) -> bool:
    """True if the glob *host* of *word* keeps a literal character, may be active, and can name self.

    The glob is inert only when *word* is a whole word of the frame whose glob
    characters are all quoted and no word with an active glob holds it; an
    attached option value (``--remote=``, ``-cf<word>``) is read as active.
    """
    if not set("*?[") & set(host) or not re.sub(r"\[[^]]*\]|[*?]", "", host):
        return False
    words, active = _GLOB_WORDS.get()
    return (word not in words or word in active) and _host_is_self(host, dns_fallback=False)


#: The tar ARITY table (GNU tar 1.34 ``tar --help``), shaped like the git push
#: floor's ``_PUSH_VALUE_OPTS``: long options that take a REQUIRED value tar also
#: accepts as the NEXT word (``--owner 0:0``).  That word is the option's value,
#: never an archive, so it is not read as ``host:file`` (``0:0`` is not host ``0``).
#: Resolved with ``_push_option_matches``, so an abbreviation (``--own``) counts.
#: Optional-value options (``--backup``, ``--checkpoint``) bind attached only.
_TAR_VALUE_OPTS: frozenset[str] = frozenset(
    {
        *("add-file", "after-date", "blocking-factor", "checkpoint-action", "directory"),
        *("exclude", "exclude-from", "exclude-ignore", "exclude-ignore-recursive"),
        *("exclude-tag", "exclude-tag-all", "exclude-tag-under", "files-from", "format"),
        *("group", "group-map", "hole-detection", "index-file", "info-script", "label"),
        *("level", "listed-incremental", "mode", "mtime", "new-volume-script", "newer"),
        *("newer-mtime", "no-quote-chars", "owner", "owner-map", "pax-option"),
        *("quote-chars", "quoting-style", "record-size", "rmt-command", "sort"),
        *("sparse-version", "starting-file", "strip-components", "suffix", "tape-length"),
        *("to-command", "transform", "use-compress-program", "volno-file", "warning"),
        *("xattrs-exclude", "xattrs-include", "xform"),
    }
)
#: Value options whose next word stays read: the ``--file`` archive and the
#: ``--rsh-command`` command.  A word that may abbreviate one (``--fil``) is not a
#: plain value, even though it also prefixes ``files-from``.
_TAR_READ_VALUE_OPTS: frozenset[str] = frozenset({"file", "rsh-command"})
#: Short-option arity.  The gate reads the command case-folded, so a letter is a
#: value letter when either case takes a value (``c``/``C``, ``x``/``X``).  When
#: it is really the valueless twin, the swallowed word is a member name, which
#: tar never dials.  ``f`` is the archive: a bundle holding ``f`` or an unknown
#: letter leaves the next word read.
_TAR_VALUE_SHORTS: frozenset[str] = frozenset("bcghiklntvx")
_TAR_NO_VALUE_SHORTS: frozenset[str] = frozenset("adjmoprsuwz")


def _tar_option_takes_next_word(tok: str) -> bool:
    """True if tar option word *tok* takes the NEXT word as a plain value."""
    if "=" in tok:
        return False
    if tok.startswith("--"):
        return _push_option_matches(tok, _TAR_VALUE_OPTS) and not _push_option_matches(
            tok, _TAR_READ_VALUE_OPTS
        )
    letters = tok[1:].lower()
    known = all(letter in _TAR_VALUE_SHORTS or letter in _TAR_NO_VALUE_SHORTS for letter in letters)
    return known and letters[-1:] in _TAR_VALUE_SHORTS


def _tar_archive_targets_self(operand: str) -> bool:
    """True if a tar archive *operand* (``host:file``) rsh-dials THIS host.

    tar reads an archive name with a colon before any slash as ``host:file``
    (optionally ``user@host:file``) and reaches it over its remote shell, so a
    self host is a self dial.  A local name (no such colon) returns False.
    """
    t = operand.strip().strip("\"'")
    if not t:
        return False
    t = _decode_scp_host(t)
    head = t.split("/", 1)[0]
    if ":" not in head:
        return False
    host = head.split(":", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if not host:
        return False
    if _glob_host_is_self(operand.strip(), host):  # read like a git remote host
        return True
    return _host_is_self(host, dns_fallback=False, match_globs=False) or _operand_targets_self(
        t, host_position=True, dns=False, match_globs=False
    )


#: Case decides whether these GNU tar short options take a value: uppercase
#: ``L`` tape length, ``F`` info script, ``H`` format, ``I`` compress program,
#: ``N`` newer-than, ``V`` label, ``K`` starting file do; ``B``/``G`` do not.
_TAR_UPPER_VALUE_SHORTS = frozenset("FHIKLNV")
_TAR_UPPER_NO_VALUE_SHORTS = frozenset("BG")


def _tar_bundle_spellings(bundle: str, plain: str, lowered: str) -> "set[str]":
    """Every run of *plain* that lowers to *bundle*, with no letter or digit beside it.

    *plain* is the submitted command without quotes and backslashes, so a bundle
    quoted in a carried script (``sh -c 'tar cLf ...'``) or spliced from quote
    pieces (``c""Lf``) is found; *lowered* is *plain* lowered, the same length.
    """
    spellings: "set[str]" = set()
    start = lowered.find(bundle)
    while start != -1:
        end = start + len(bundle)
        if (start == 0 or not plain[start - 1].isalnum()) and (
            end == len(plain) or not plain[end].isalnum()
        ):
            spellings.add(plain[start:end])
        start = lowered.find(bundle, start + 1)
    return spellings


#: GNU tar's operation options, and its short options in their submitted case: the
#: operation letters, and the letters that take a value (the rest of the word or the next).
_TAR_OPERATION_OPTS = frozenset(
    {"catenate", "concatenate", "create", "diff", "compare"}
    | {"delete", "append", "list", "update", "extract", "get"}
)
_TAR_OPERATION_SHORTS = frozenset("Acdrtux")
_TAR_CASED_VALUE_SHORTS = frozenset("bCfFgHIKLNTVX")


def _tar_submitted_spellings(word: str) -> "tuple[set[str], bool]":
    """Each original-case spelling of lowered *word* in the submitted command, and
    whether those spellings are all it can be.

    Read from ``_RAW_COMMAND`` (``_tar_bundle_spellings``), kept per read in
    ``_READ_MEMO``.  Not exact with no submitted command, no spelling found, or
    ``$``/backtick expansion that can build a letter.
    """
    raw = _RAW_COMMAND.get()
    memo = _READ_MEMO.get()
    memo = {} if memo is None else memo
    found: "set[str]" = set()
    if raw is not None:
        # The submitted command is fixed for a read: prepare it and look each word up once.
        if ("tar-plain",) not in memo:
            plain = raw.replace("\\\n", "").replace("\\", "").replace("'", "").replace('"', "")
            lowered = plain.lower()
            # None when a lowercase is longer than its letter: no spelling lines up.
            memo[("tar-plain",)] = (plain, lowered) if len(lowered) == len(plain) else None
        key = ("tar-bundle-spellings", word)
        if memo[("tar-plain",)] is not None and key not in memo:
            memo[key] = _tar_bundle_spellings(word, *memo[("tar-plain",)])
        found = memo.get(key) or set()
    return found, bool(found) and raw is not None and "$" not in raw and "`" not in raw


def _tar_operation_readings(words: "Iterable[str]") -> "frozenset[bool]":
    """Every answer to "do option *words* name a tar operation?" over the readings of their case.

    *words* are lowered, and tar short options are case-sensitive (``-c`` creates,
    ``-C`` changes directory), so each short option word is read in its submitted
    case (``_tar_submitted_spellings``).  The read stops at ``--``.  FAIL-CLOSED:
    a word whose case cannot be recovered, and an operation word that may be the
    previous option's value or follow a ``--`` that may be one, give both answers.
    """
    readings = {False}
    takes_value = ended = False
    for word in words:
        certain = not takes_value and not ended
        if word == "--":
            ended = ended or not takes_value
            takes_value = False
            continue
        if word.startswith("--"):
            if word[2:].split("=", 1)[0] in _TAR_OPERATION_OPTS:
                if certain:
                    return frozenset({True})
                readings.add(True)
            takes_value = "=" not in word and _push_option_matches(
                word, _TAR_VALUE_OPTS | _TAR_READ_VALUE_OPTS
            )
            continue
        if not word.startswith("-") or word == "-":
            takes_value = False
            continue
        spellings, exact = _tar_submitted_spellings(word)
        answers: "set[bool]" = set()
        takes: "set[bool]" = set()
        for spelling in spellings if exact else ():
            names = False
            for pos, letter in enumerate(spelling[1:]):
                names = names or letter in _TAR_OPERATION_SHORTS
                if letter in _TAR_CASED_VALUE_SHORTS:
                    takes.add(pos == len(spelling) - 2)  # a value glued on, or the next word
                    break
            answers.add(names)
        if not exact:
            answers = {any(c in "acdrtux" for c in word[1:]), False}
            takes = {True}
        if answers == {True} and certain:
            return frozenset({True})
        readings |= answers
        takes_value = True in takes
    return frozenset(readings)


def _tar_bundle_archive_slot(
    bundle: str, rest: "list[str]", defaults: "frozenset[bool]"
) -> "frozenset[int]":
    """Every position the archive word can hold among the words after bundle *bundle*.

    In old-style ``tar`` (``tar cf a.tar dir``) only ``f`` names an archive, and
    each ``f`` (``tMff a b``) names the ONE word at its own positional slot.  Each
    option letter that takes its own value claims one word first and shifts that
    slot; a claimed word is the option's value, never the archive.  The operation
    letters ``c``/``t``/``x`` stand for ``C``/``T``/``X``
    (``--directory``/``--files-from``/``--exclude-from``) once they are not the
    single operation the bundle names, plus ``b`` and ``g``.  A lone
    ``c``/``t``/``x`` is the operation and claims no word, unless the operation is
    named elsewhere -- another operation letter in the bundle, an operation option
    among *rest* (``_tar_operation_readings``), or the ``TAR_OPTIONS`` value: each
    answer in *defaults* is one reading of whether it names one.

    The floor reads *bundle* lowered, so each original-case spelling of it in the
    submitted command (``_RAW_COMMAND``) is read too, with each letter's own arity
    (a lowercase ``c`` is the operation, ``C`` claims a word), and *bundle* as given.
    FAIL-CLOSED: with no spelling found, or ``$``/backtick expansion that can
    build a letter, both readings of every case-decided letter are taken; when
    whether the operation is named elsewhere has more than one answer, every
    answer is read.  Returns every slot a reading gives (empty when the bundle
    names no ``f``).
    """
    # c/t/x fold onto value-taking C/T/X; b and g are the lowercase value letters.
    value_variants = frozenset(letter for letter in "acdrtux" if letter in _TAR_VALUE_SHORTS)
    value_before_f = value_variants | frozenset("bg")
    if any(letter in "adru" for letter in bundle.lower()):
        named_readings = {True}
    else:
        rest_readings = _tar_operation_readings(rest)
        named_readings = {given or named for given in defaults for named in rest_readings}

    def _advance(state: "tuple[int, bool]", letter: str) -> "tuple[int, bool]":
        """``(slot, operation_seen)`` after reading one more letter before ``f``."""
        slot, operation_seen = state
        if letter in _TAR_UPPER_VALUE_SHORTS:
            return slot + 1, operation_seen
        if letter in _TAR_UPPER_NO_VALUE_SHORTS:
            return state
        low = letter.lower()
        if low in value_variants and not operation_seen:
            return slot, True  # the bundle's own operation letter claims no word
        if low in value_before_f:
            return slot + 1, operation_seen
        return state

    def _slots(spelling: str, operation_named: bool) -> "set[int]":
        # Each ``f`` claims the next word as one more archive (``tMff a b``: both).
        state, archive_slots = (0, operation_named), set()
        for letter in spelling:
            if letter == "f":
                archive_slots.add(state[0])
                state = (state[0] + 1, state[1])
            else:
                state = _advance(state, letter)
        return archive_slots

    def _cased_slots(spelling: str) -> "set[int]":
        # A submitted spelling fixes each letter's arity: ``c`` is the operation
        # even when ``--create`` repeats it, and ``C`` always claims a word.
        slot, archive_slots = 0, set()
        for letter in spelling:
            if letter == "f":
                archive_slots.add(slot)
            if letter in _TAR_CASED_VALUE_SHORTS:
                slot += 1
        return archive_slots

    found, exact = _tar_submitted_spellings(bundle.lower())
    slots = {slot for spelling in found for slot in _cased_slots(spelling)}
    slots.update(
        slot for operation_named in named_readings for slot in _slots(bundle, operation_named)
    )
    if not exact:
        # Both readings of every lowercase case-decided letter: for each ``f``
        # that may be the archive (every earlier ``f`` then being ``F``), every
        # slot from the fewest words claimed to the most.  Both readings carry on
        # letter by letter, so the bundle is read once per answer.
        for operation_named in named_readings:
            fewest = most = (0, operation_named)
            for letter in bundle:
                if letter == "f":
                    slots.update(range(fewest[0], most[0] + 1))
                    letter = "F"
                fewest = _advance(fewest, letter.upper() if letter in "bg" else letter)
                most = _advance(most, letter.upper() if letter in "hiklnv" else letter)
    return frozenset(slots)

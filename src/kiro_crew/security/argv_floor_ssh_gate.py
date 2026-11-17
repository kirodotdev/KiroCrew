"""The cheap gate in front of the ssh self-target floor: can this line run an ssh verb?

``_is_ssh_to_self`` reads a line only when it names an ssh-family verb, either as text
or as a glob word that bash could expand to one (``s?h``, ``/usr/bin/sc?``). This module
cuts a line into the words that gate reads and decides whether one of those glob words
can name an ssh-family program. It also holds the verb set and the sentinel bytes that
``argv_floor_ssh`` uses to mask quoted separators, because the gate must cut at those
sentinels too: a quoted ``"$(s?h ..."`` reaches the gate with its ``(`` masked. The
early check that skips the quoted-body readers when no mask can pass the gate lives
here for the same reason.

``kiro_crew.security.argv_floor`` serves every name defined here, and a patch through it
reaches this module (:mod:`kiro_crew._facade_mirror`). This module reads the glob
characters from ``argv_floor_hosts``, below it, and nothing else from the argv floor; it
reads the ANSI-C quote decoder from ``shell_normalizer``.
"""

from __future__ import annotations

import fnmatch

from .argv_floor_hosts import _GLOB_CHARS
from .shell_normalizer import _decode_shell_quoted_literals

_SSH_FAMILY_VERBS: frozenset[str] = frozenset({"ssh", "scp", "sftp", "rsync"})


# Sentinel bytes standing in for an ``_ends_argv`` boundary character that was
# QUOTED or backslash-escaped in the raw source, so it is literal DATA rather
# than a command separator.  ``_self_tokens`` runs shlex, which resolves quotes
# BEFORE the operand walk in ``_is_ssh_to_self`` runs, so by token time a quoted
# ``a;b`` is indistinguishable from a glued unquoted operator ``a;b`` -- yet the
# first is one operand and the second is a command boundary.  The distinguishing
# information exists only in the SOURCE, before tokenization, so the raw text is
# scanned there and every data separator is rewritten to a sentinel that
# ``_ends_argv`` does not treat as a boundary.  A real, unquoted separator keeps
# its character and still ends the walk.  The table covers EVERY character that
# can make ``_ends_argv`` end the walk (``;`` ``|`` ``&`` newline ``#`` ``(``
# ``{``) -- covering only a subset would let a quoted spelling of the missing
# one hide a self-host operand behind a fake boundary (``scp '&' localhost:/x``
# stops the walk at ``&`` with the target unexamined).  Control bytes are used
# because a real shell command never contains them.
_QUOTED_SEP_SENTINELS = {
    ";": "\x00",
    "|": "\x01",
    "&": "\x02",
    "\n": "\x03",
    "#": "\x04",
    "(": "\x05",
    "{": "\x06",
}


#: Characters that end a gate word: a blank, a command separator, a parenthesis,
#: a backtick, or a sentinel that stands for a quoted separator.  The gate reads
#: masked text, where a ``$(`` inside double quotes has its ``(`` masked, and bash
#: still runs that body as a command line.  A ``[`` opens a bracket class and a
#: ``]`` closes it; between them a separator is part of the word (``s[s)]h`` is
#: one word).
_GATE_WORD_SEPARATORS = frozenset(";&|()`").union(_QUOTED_SEP_SENTINELS.values())


def _gate_words(probe: str, *, group_brackets: bool) -> "list[str]":
    """Cut *probe* into words at blanks and the separators in ``_GATE_WORD_SEPARATORS``.

    With *group_brackets*, a ``[`` opens a bracket class that ``;&|()`` and a
    backtick cannot end (``s[s)]h`` stays one word) and a ``]`` closes it; a
    blank still ends the word.  A second ``[`` while one is open just reopens
    it, so a long run of ``[`` with no ``]`` is one linear pass, not a quadratic
    rescan.  A single plain character walk, no regex.
    """
    words: list[str] = []
    word: list[str] = []
    depth = 0
    for ch in probe:
        if group_brackets and ch == "[":
            depth = 1
            word.append(ch)
        elif group_brackets and ch == "]":
            depth = 0
            word.append(ch)
        elif ch.isspace() or (depth == 0 and ch in _GATE_WORD_SEPARATORS):
            if word:
                words.append("".join(word))
                word = []
        else:
            word.append(ch)
    if word:
        words.append("".join(word))
    return words


def _glob_can_name_ssh_verb(probe: str) -> bool:
    """True when a glob word in *probe* can expand to an ssh-family program.

    Words end at blanks and at the characters that start a command (``;`` ``&``
    ``|`` a parenthesis or a backtick), so a verb glued to a substitution opener
    (``$(s?h``, ``x=$(s?h``) or a separator (``true;s?h``) is still a word.  Three
    readings are joined, so each finer one only adds words: the blank-separated
    words, the words cut at those characters, and the same cut with a bracket
    class kept whole (``s[s\\)]h``, alone or glued to an opener).
    """
    words = {
        *probe.split(),
        *_gate_words(probe, group_brackets=False),
        *_gate_words(probe, group_brackets=True),
    }
    for word in words:
        if not _GLOB_CHARS.intersection(word):
            continue
        base = word.rsplit("/", 1)[-1]
        for verb in _SSH_FAMILY_VERBS:
            if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base):
                return True
    return False


def _cannot_name_ssh_verb(text: str) -> bool:
    """Whether the verb gate in :func:`_is_ssh_to_self` must fail on *text* however
    :func:`_mask_quoted_separators` masks it, so the quoted-body readers that the
    mask needs can be skipped. Each body re-reads the rest of the line, so their
    cost grows with the square of the body count.

    A mask turns some of the :data:`_QUOTED_SEP_SENTINELS` keys into control bytes.
    None of those keys is a letter, so a verb substring is the same either way. A
    mask can change two readings, and each makes this return False:
    a ``${`` default (a masked ``{`` changes what ``_resolve_param_defaults``
    deletes), and a ``[`` class in a word that holds a key (``[{-~]`` widens when
    its ``{`` becomes a control byte, and a masked newline joins two words).
    ``*`` and ``?`` match the same either way. The gate also ends a word at a
    sentinel, so a masked ``#`` or ``{`` splits a word the bare text keeps whole;
    the probe is read a second time with every key as a blank, the finest cut any
    mask can give.
    """
    probe = _decode_shell_quoted_literals(text)
    probe = probe.replace('"', "").replace("'", "").replace("\\", "")
    if "${" in probe or any(verb in probe for verb in _SSH_FAMILY_VERBS):
        return False
    # A masked newline can join words, so a word here runs up to any other space.
    spaced = "".join(" " if ch.isspace() and ch != "\n" else ch for ch in probe)
    for word in spaced.split(" "):
        if "[" in word and any(key in word for key in _QUOTED_SEP_SENTINELS):
            return False
    split = "".join(" " if ch in _QUOTED_SEP_SENTINELS else ch for ch in probe)
    return not _glob_can_name_ssh_verb(probe) and not _glob_can_name_ssh_verb(split)

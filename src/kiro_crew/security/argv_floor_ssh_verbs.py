"""Which ssh-family programs a word in a command line can run, read once per program.

The ssh self-target floor (``argv_floor_ssh``) reads each word of a frame through
these helpers: :func:`_ssh_family_verbs` names every ssh-family program a word can
invoke (each match of a glob), and :func:`_ssh_verb_readings` yields one reading per
program for a word bash runs in a run position (program position or a same-line
``cp``/``mv``/``ln``/``install`` destination), so each program's option reading is
checked rather than one picked in set order.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator

from .argv_floor_hosts import _GLOB_CHARS
from .argv_floor_ssh_gate import _SSH_FAMILY_VERBS
from .shell_normalizer import _program_basename, _strip_redirect


def _ssh_verb_basename(token: str) -> str:
    """*token*'s program basename as ``_ssh_family_verbs`` reads it (``ssh.exe`` is ``ssh``)."""
    base = _program_basename(_strip_redirect(token.strip("\"'")))
    return base[: -len(".exe")] if base.endswith(".exe") else base


def _ssh_family_verbs(token: str) -> "tuple[str, ...]":
    """Every ssh-family verb *token* can invoke: none, one, or each a glob can name.

    Path-qualified (``/usr/bin/ssh``) and Windows (``ssh.exe``) spellings
    resolve to the bare verb.  A glob basename (``s?h``, ``*s*``, see
    ``_GLOB_CHARS``) resolves to every verb it can expand to.
    """
    base = _ssh_verb_basename(token)
    if base in _SSH_FAMILY_VERBS:
        return (base,)
    if not _GLOB_CHARS.intersection(base):
        return ()
    return tuple(
        verb
        for verb in _SSH_FAMILY_VERBS
        if fnmatch.fnmatchcase(verb, base) or fnmatch.fnmatchcase(verb + ".exe", base)
    )


def _ssh_verb_readings(
    tokens: "list[str]", executed: "set[int]", bound: "dict[str, tuple[str, ...]]"
) -> "Iterator[tuple[int, str, str | None, bool]]":
    """Each ``(index, token, verb, from_copy)`` reading of one frame's words.

    A glob that bash runs (``*s*``, ``[rs]s*``) is read once per ssh-family verb
    it can name, so it gets each program's option reading; a glob bash does not
    run, and a word of only ``*`` (a cron field, see ``_is_ssh_to_self``), keeps
    one.  A word a same-line copy bound (*bound*, read at each step so
    a copy binds the words after it) is read once per verb its source could
    name, with ``from_copy`` set.  Every other word is read once, as ``None``.
    """
    for i, token in enumerate(tokens):
        verbs = _ssh_family_verbs(token)
        if verbs:
            for verb in verbs if i in executed and token.strip("*") else verbs[:1]:
                yield i, token, verb, False
            continue
        stripped = token.strip("\"'")
        copied = bound.get(stripped) or bound.get(_program_basename(stripped)) or ()
        for copied_verb in copied or (None,):
            yield i, token, copied_verb, bool(copied)

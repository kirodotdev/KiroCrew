"""The base binary a simple shell command invokes, for channel trust grants.

Moved out of :mod:`kiro_crew.channel` (the file line cap) unchanged; ``channel``
imports ``_offerable_base_binary`` back, the one name a caller reads from there.
"""

from __future__ import annotations

import re
import shlex


def _shell_base_binary(cmd: str) -> str | None:
    """The single binary a SIMPLE shell command invokes, else None.

    Shell-aware (shlex) tokenization, fail-closed on everything that is not
    one plain invocation: parse failures, empty commands, shell operators or
    substitution anywhere (``|``, ``&``, ``;``, newline, backtick, ``$(``,
    redirects), and env-assignment or quoted/space-bearing first tokens. A
    base-command trust grant keyed on anything richer than one unambiguous
    binary name has already been shown to widen scope (naive first-token
    slicing turned ``"./my tool" --safe`` into a ``"./my`` prefix grant, and
    env prefixes into match-anything grants), so anything ambiguous simply
    has no base.
    """
    if not cmd or _CHANNEL_SHELL_OPERATOR_RE.search(cmd):
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not tokens:
        return None
    base = tokens[0]
    # An env-assignment first token means the binary is the SECOND token —
    # refuse rather than guess (the grant would silently cover any command
    # run under that prefix).
    if not base or "=" in base:
        return None
    # A base containing whitespace can only come from quoting; a quoted
    # executable path is matched by the exact-command tier, not by base.
    if any(c.isspace() for c in base):
        return None
    # POSITIVE shape check: the base must look like a plain binary name or
    # path. Enumerating bad metacharacters one by one (``!``, ``(``, …) is a
    # losing game — the shell always has one more; anything outside this
    # conservative charset is refused instead.
    if not _CHANNEL_BASE_BINARY_RE.fullmatch(base):
        return None
    # Shell reserved words pass the charset but are interpreted by the shell,
    # not executed as a binary — ``time rm ...`` must not yield a ``time``
    # grant that covers ``time <anything>``.
    if base in _CHANNEL_SHELL_RESERVED_WORDS:
        return None
    return base


def _offerable_base_binary(cmd: str) -> str | None:
    """The base a "Trust all <base> commands" consent may name, else None.

    ``_shell_base_binary`` tokenizes in POSIX mode, where a backslash is an
    escape, so the Windows path ``.\\deploy.bat`` derives ``.deploy.bat``: a
    name the user never typed, and one that also covers a different file
    literally called ``.deploy.bat``. A consent label must name what the user
    read, so a first token holding a backslash gets no base offer and no base
    grant; the exact-command tier still covers that command.
    """
    stripped = cmd.lstrip() if cmd else ""
    first = stripped.split(None, 1)[0] if stripped else ""
    if "\\" in first:
        return None
    return _shell_base_binary(cmd)


# Anything that makes a command more than ONE plain invocation. Deliberately
# broader than the enforcement splitter's operator set: over-matching here
# fails toward "no per-command grant available", never toward a wider grant.
# Parentheses and braces cover subshell / brace grouping — "(rm /tmp/a)" must
# not yield "(rm" as a trusted binary.
_CHANNEL_SHELL_OPERATOR_RE = re.compile(r"[|&;`\n\r<>(){}]|\$\(")

# What a trusted base binary is ALLOWED to look like: alphanumerics plus the
# few characters real binary names and paths use. Everything else — negation
# ``!``, test ``[``, arithmetic ``((`` — simply has no derivable base.
_CHANNEL_BASE_BINARY_RE = re.compile(r"[A-Za-z0-9._/+-]+")

# Words the shell interprets instead of executing. A grant keyed on one would
# cover whatever command the shell runs under it.
_CHANNEL_SHELL_RESERVED_WORDS = frozenset(
    "! case coproc do done elif else esac fi for function if in select "
    "then time until while".split()
)

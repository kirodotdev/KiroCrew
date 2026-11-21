"""How a Windows command line spells a program word, read as the program Windows runs.

Windows runs ``aws.exe``, ``aws.cmd`` and ``aws.bat`` as ``aws``, cmd and PowerShell drop
an escape inside a word (``a^w^s``, ``a`w`s``), and ``cmd /c "<line>"`` runs its quoted
line as a command; :mod:`kiro_crew.security.deny_gate` matches these readings as added
views. Every submodule a function reads, other than ``shell_normalizer``, is imported when
the function runs, so importing this module loads none of them.
"""

from __future__ import annotations

import logging

from .shell_normalizer import _WIN_PROGRAM_SUFFIXES, _strip_win_program_suffix

logger = logging.getLogger("kiro_crew.security")


def _cmd_line_args(args: "list[str]") -> "list[str] | None":
    """The words after ``/c`` or ``/k`` in a ``cmd`` argv, past other ``/x`` switches.

    cmd.exe also takes the switch glued to another switch or to the line (``/s/c``,
    ``/c"dir"``); the rest of that word is then the first word of the line.
    """
    for i, arg in enumerate(args):
        if not arg.startswith("/"):
            return None
        low = arg.lower()
        for switch in ("/c", "/k"):
            at = low.find(switch)
            if at != -1:
                tail = arg[at + 2 :]
                return ([tail] if tail else []) + args[i + 1 :]
    return None


def _win_program_index(tokens: "list[str]") -> "int | None":
    """Index of the program word, past ``sudo``/``env``-style wrappers and ``cmd /c``."""
    from . import argv_rules

    start = 0
    while (found := argv_rules._program_args(tokens[start:])) is not None:
        program, args = found
        rest = _cmd_line_args(args) if _strip_win_program_suffix(program) == "cmd" else None
        if not rest:
            return len(tokens) - len(args) - 1
        start = len(tokens) - len(rest)
    return None


def _cmd_c_payload(tokens: "list[str]") -> "list[str]":
    """The quoted line of ``cmd /c "<line>"`` (or ``/k``), read as a nested command."""
    from . import argv_rules

    found = argv_rules._program_args(tokens)
    if found is None or _strip_win_program_suffix(found[0]) != "cmd":
        return []
    rest = _cmd_line_args(found[1])
    return rest if rest is not None and len(rest) == 1 and " " in rest[0].strip() else []


def _suffixed_program_words(text: str) -> set[str]:
    """The program words of *text* that end in ``.exe``, ``.cmd`` or ``.bat``.

    The program word of every command (``_win_program_index``), at the top level and in
    every nested literal shell payload, as the parser reads it.
    """
    from . import argv_floor
    from . import shell_normalizer as shell

    words: set[str] = set()
    for source in {text, *argv_floor._shell_payload_sources(text)}:
        for piece in shell._CMD_SPLIT_RE.split(source):
            tokens = shell._shell_tokens(piece)
            at = _win_program_index(tokens)
            if at is not None and _strip_win_program_suffix(tokens[at]) != tokens[at]:
                words.add(tokens[at])
    return words


def _unsuffixed_whole_views(lower: str) -> list[str]:
    """*lower*, already lowercase, with each Windows-suffixed program word read bare: a
    one-view list, empty when that view is *lower*.

    Each suffixed program word ``_suffixed_program_words`` finds (top level and nested
    literal payloads) is read bare wherever it is written and not followed by a letter,
    digit or ``_``, an argument spelled the same (``curl.exe … -o curl.exe``) included, so
    a nested script's spelling (``bash -c "curl.exe \\"…\\"" | sh``) and a pipe outside it
    read as their bare twins. Where the word goes on (``os.execvp(`` under a program word
    ``os.exe``) it stays as written. The view is matched after the raw string, so it adds
    refusals and never an allow. It is of the whole command because a row that spans a
    separator (``curl .* \\| sh``) is in no segment's view. A failure returns ``[]``: this
    runs in the permission gate, so it costs the view, never the call.
    """
    if not any(s in lower for s in _WIN_PROGRAM_SUFFIXES):
        return []
    view = lower
    try:
        # Longest word first; an occurrence a word character follows keeps its suffix.
        for word in sorted(_suffixed_program_words(lower), key=lambda w: (-len(w), w)):
            bare, parts = _strip_win_program_suffix(word), view.split(word)
            view = parts[0]
            for part in parts[1:]:
                view += (word if part[:1].isalnum() or part[:1] == "_" else bare) + part
    except Exception:
        logger.debug("unsuffixed view failed; raw text only", exc_info=True)
        return []
    return [view] if view != lower else []


# An escape INSIDE a word (cmd ``a^w^s``, PowerShell ``a`w`s``) that the shell drops.
# Only between two ASCII alphanumerics, so a PowerShell line continuation, ``git^``,
# ``$(...)`` and POSIX command substitution (`` `date` ``) are left alone.
def _fold_win_in_word_escapes(text: str) -> str:
    sides = zip(text, " " + text[:-1], text[1:] + " ")  # (char, before, after)
    return "".join(
        c for c, b, a in sides if c not in "^`" or not (b + a).isascii() or not (b + a).isalnum()
    )

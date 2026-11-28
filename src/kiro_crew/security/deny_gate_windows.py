"""The whole command with each Windows-suffixed program word read as Windows runs it.

Windows runs ``curl.exe`` as ``curl``; :mod:`kiro_crew.security.deny_gate` matches this
view after the raw command, for the rows that span a separator. Every submodule a function
reads, other than ``shell_normalizer``, is imported when the function runs, so importing
this module loads none of them.
"""

from __future__ import annotations

import logging

from .shell_normalizer import _WIN_PROGRAM_SUFFIXES, _strip_win_program_suffix

logger = logging.getLogger("kiro_crew.security")


def _suffixed_program_words(text: str) -> set[str]:
    """The program words of *text* that end in ``.exe``, ``.cmd`` or ``.bat``.

    The program word of every command (``argv_rules._win_program_index``), at the top level
    and in every nested literal shell payload, as the parser reads it.
    """
    from . import argv_floor, argv_rules
    from . import shell_normalizer as shell

    words: set[str] = set()
    for source in {text, *argv_floor._shell_payload_sources(text)}:
        for piece in shell._CMD_SPLIT_RE.split(source):
            tokens = shell._shell_tokens(piece)
            at = argv_rules._win_program_index(tokens)
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

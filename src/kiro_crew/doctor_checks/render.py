"""Display helpers the ``kirocrew doctor`` sections share.

An inert rendering for a value read off disk, the detail indent, and wrapping that
never splits a token an operator may paste.
"""

from __future__ import annotations

import textwrap


def _safe_display(value: object) -> str:
    """Render a value read off disk so a terminal cannot act on it.

    Agent specs are NOT all trusted input: a cloned repository can ship its own
    ``<project>/.kiro/agents/*.json``, and an installed app registers specs in
    the user-level directory, so a ``model`` string (or a configured agent name)
    can carry OSC/ANSI control sequences. ``repr`` escapes every non-printable
    character, so the value is shown verbatim-but-inert instead of executing
    terminal controls or spoofing the surrounding diagnostic lines.
    """
    return repr(value)


_INDENT = "               "


def _print_wrapped(text: str) -> None:
    """Print ``text`` wrapped to the doctor's detail indent, never splitting a token.

    ``textwrap``'s two splitting defaults are both off for every caller, because at width
    80 they break a long data-home path across lines and insert a break after an embedded
    hyphen -- which turns a remedy naming ``find <dir> -samefile <file>`` into fragments
    that run as nothing. Doctor's details are diagnostics an operator PASTES, so a line
    that overflows the width is the better failure: it can still be copied. That argument
    holds for every detail this function prints, so it is not a per-caller choice -- a flag
    here would leave the remedies that did not pass it broken for the same reason.
    """
    for line in textwrap.wrap(
        text,
        width=80,
        break_long_words=False,
        break_on_hyphens=False,
    ):
        print(f"{_INDENT}{line}")

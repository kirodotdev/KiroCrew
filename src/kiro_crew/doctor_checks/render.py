"""Display helpers the ``kirocrew doctor`` sections share.

An inert rendering for a value read off disk, the detail indent, and wrapping that
never splits a token an operator may paste.
"""

from __future__ import annotations

import contextlib
import os
import textwrap
from collections.abc import Callable, Iterator


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


@contextlib.contextmanager
def _unreadable_skips_section(
    issues: list[str],
    label: str,
    confined: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Let a denied read end ONE doctor section instead of the whole report.

    Inside the agent sandbox, parts of the data home (the task store, cron history,
    the crew log, ...) are hidden on purpose, so a section that reads them raises
    ``PermissionError``; uncaught, that ended ``kirocrew doctor`` half-way with a
    traceback and every later section unreported. Only ``PermissionError`` is
    absorbed: any other exception is still a bug in the section and propagates.

    ``confined`` answers whether THIS process is known to run inside a sandbox
    (the agent sandbox, or any other). When it is, the denial is the sandbox's, so the row is
    a skip and not an issue. Otherwise an unreadable file in the data home is a real
    problem and is counted, so a run from the operator's own terminal still fails on
    it. A probe that itself raises counts as "not known to be confined".
    """
    try:
        yield
    except PermissionError as exc:
        shown = (
            _safe_display(os.fsdecode(exc.filename))
            if exc.filename is not None
            else "a file it reads"
        )
        is_confined = False
        if confined is not None:
            try:
                is_confined = bool(confined())
            except Exception:  # noqa: BLE001 — a broken probe must not end the report
                is_confined = False
        if is_confined:
            print(f"  {label}: ⏭  skipped — {shown} is not readable from this shell")
            _print_wrapped(
                "This shell runs inside a sandbox (an agent's shell is one), which "
                "can hide part of the Kiro Crew data home. Run `kirocrew doctor` from "
                "your own terminal to check this section."
            )
            return
        print(f"  {label}: ❌ {shown} is not readable ({exc.strerror or exc})")
        _print_wrapped(
            "If this shell runs inside an agent sandbox, that sandbox hides part of "
            "the Kiro Crew data home on purpose: run `kirocrew doctor` from your own "
            "terminal. Otherwise check the ownership and permissions of that path."
        )
        issues.append(f"{label}: {shown} is not readable")

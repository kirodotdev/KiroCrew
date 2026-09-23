"""The decision seam: one function, four gates in front of it, off by default.

A caller asks typed questions about a state and gets typed answers, or ``None``:

    from kiro_crew.decisions import decide, Choice

    answers = await decide(
        "skills.select",
        {"message": text, "candidates": names},
        [Choice(id="skill", prompt="Which skill applies?", options=names)],
        session_key=session_key,
    )
    if answers is None:
        ...  # exactly what the code did before
    else:
        ...  # only reached when the seam is enabled and the session is sampled

``None`` covers every refusal and every failure -- see :func:`decide` -- so a
call site needs no try/except and no feature check of its own. That is what makes
this safe to place in a hot path: without consent on the keystone
``decisions_consent.json`` for the configured endpoint (the default: no file), the
call performs exactly one small keystone read, off the event loop, and then
returns -- no network, no log write, no import beyond this package.

Import this package lazily, inside the function that calls ``decide``. Nothing
here imports the config loader, ``aiohttp`` or the session layer at module scope,
but a top-level import in a hot module would still put this package on that
module's import path for no benefit.

The package deliberately does NOT import from ``decisions.points``: points
depend on the seam, never the reverse, so the dependency stays acyclic and a
broken point file cannot make ``decide`` unimportable.
"""

from __future__ import annotations

from kiro_crew.decisions.gate import (
    DECISION_POINT_NAMES,
    decide,
    history_budget_chars,
    is_enabled,
    timeout_secs,
)
from kiro_crew.decisions.types import Answer, Answers, Choice, Question

__all__ = [
    "DECISION_POINT_NAMES",
    "Answer",
    "Answers",
    "Choice",
    "Question",
    "decide",
    "history_budget_chars",
    "is_enabled",
    "timeout_secs",
]

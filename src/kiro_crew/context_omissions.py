"""Record what the context composer left out, at the decision that left it out.

Every budgeted block of an assembled prompt -- the two lesson tiers, the preference
facts, the preferences file, the background pool, a member briefing and folder
steering -- writes an omission notice INTO the prompt when it cuts. The model reads
that notice and nothing else does, so a reader of the usage record could see how big
a block was but not whether anything was dropped to reach that size. This module is
the structured twin of those notices: the code that decides a cut and formats its
notice also calls
:func:`record`, and the turn that composed the prompt gathers the records with
:func:`collect` and writes them beside the block sizes on ``context/composed``.

The numbers come from the composer's own decision, so there is no second budget
calculation to drift from the first, and nothing is parsed back out of prompt text
that stored content could imitate.

The collector is a :class:`~contextvars.ContextVar`, not a counter on the store or
the builder: two sessions compose against one lesson store at once, and state on a
shared object would hand either session's numbers to the other. The composer runs in
a worker thread that ``copy_context`` starts, which carries the collector with it.
Outside a collection every call is a no-op, so a recall tool, a consolidation or a
preview that renders the same blocks records nothing.

Only counts and a closed vocabulary are recorded -- never omitted text, a memory key,
a rule or any other stored content -- so a record is bounded by construction: at most
one entry per kind in :data:`KINDS`, each a few integers and at most
:data:`BACKGROUND_SECTIONS` names.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

LESSON_RULES: Final = "lesson_rules"
LESSON_FINDINGS: Final = "lesson_findings"
PREFERENCE_FACTS: Final = "preference_facts"
PREFERENCES_FILE: Final = "preferences_file"
BACKGROUND: Final = "background"
MEMBER_BRIEFING: Final = "member_briefing"
FOLDER_STEERING: Final = "folder_steering"

#: Every block that can record a cut. The record is keyed by these names only.
KINDS: Final[tuple[str, ...]] = (
    LESSON_RULES,
    LESSON_FINDINGS,
    PREFERENCE_FACTS,
    PREFERENCES_FILE,
    BACKGROUND,
    MEMBER_BRIEFING,
    FOLDER_STEERING,
)

#: Why a block was cut. ``budget`` is a character budget the block shares or owns,
#: ``cap`` a hard ceiling on one block, and ``relevance`` a tier withheld because no
#: entry in it shares wording with the request.
REASONS: Final[tuple[str, ...]] = ("budget", "cap", "relevance")

#: The background pool's section names, as the admission names them in its notice,
#: mapped to the identifiers the record carries.
BACKGROUND_SECTIONS: Final[dict[str, str]] = {
    "background context": "background_context",
    "older thread history": "older_thread_history",
    "skills": "skills",
}

#: Integer members a record entry may carry.
COUNT_FIELDS: Final[tuple[str, ...]] = ("omitted", "total", "budget_chars", "omitted_chars")

_SINK: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar(
    "kiro_crew_context_omissions", default=None
)


@contextmanager
def collect() -> Iterator[dict[str, dict[str, Any]]]:
    """Gather every cut recorded while the block runs, keyed by kind.

    The yielded mapping is filled in place; read it after the block exits.
    """
    sink: dict[str, dict[str, Any]] = {}
    token = _SINK.set(sink)
    try:
        yield sink
    finally:
        _SINK.reset(token)


def record(
    kind: str,
    reason: str,
    *,
    omitted: int = 0,
    total: int = 0,
    budget_chars: int = 0,
    omitted_chars: int = 0,
    sections: Iterable[str] = (),
) -> None:
    """Record that *kind* was cut for *reason*, replacing any earlier record of it.

    A block can be rendered more than once in one composition (lessons are re-fitted
    when protected content runs over its ceiling), and the LAST render is the one the
    prompt carries, so a later record replaces an earlier one.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown omission kind {kind!r}")
    if reason not in REASONS:
        raise ValueError(f"unknown omission reason {reason!r}")
    sink = _SINK.get()
    if sink is None:
        return
    entry: dict[str, Any] = {"reason": reason}
    for name, value in zip(COUNT_FIELDS, (omitted, total, budget_chars, omitted_chars)):
        if value > 0:
            entry[name] = int(value)
    named = sorted({BACKGROUND_SECTIONS[name] for name in sections if name in BACKGROUND_SECTIONS})
    if named:
        entry["sections"] = named
    sink[kind] = entry


def clear(kind: str) -> None:
    """Drop any record of *kind*: its final render kept everything."""
    sink = _SINK.get()
    if sink is not None:
        sink.pop(kind, None)


def replay(records: dict[str, dict[str, Any]]) -> None:
    """Record cuts another collection gathered, as if they were made here.

    For reads done ahead of the build that uses them (an early memory read): their
    records were collected apart and belong to the build that takes the result.
    """
    sink = _SINK.get()
    if sink is not None:
        sink.update({kind: dict(entry) for kind, entry in records.items()})

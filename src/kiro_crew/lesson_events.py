"""The ``lessons.changed`` signal: a lesson in the global store was added, updated or removed.

Lessons change on three paths: ``POST /api/lessons`` (the ``learn_add`` tool, the
Memory tab, the CLI), ``DELETE /api/lessons``, and history consolidation, which runs
on a worker thread. Each path calls :func:`emit_lessons_changed` once the store has
actually changed, and the gateway registers the one observer that turns it into a
``lessons.changed`` WebSocket frame. An app receives that frame only when its
manifest declares the ``lessons`` event scope (``dashboard/ws_event_scope.py``).

The payload says THAT lessons changed and how, never what they say: no rule text,
no NOT-clause, no repo scope, no row key. An app that wants the rows re-reads
``GET /api/lessons`` through its own ``permissions.api`` grant, which applies that
route's own checks. Only the GLOBAL store is announced, because it is the store an
app token's read resolves to: a write into a named silo (a crew's or a crewmate's
memory) stays silent, so the frame never tells an app about memory it cannot read.

A process that registers no observer (the CLI, a test) emits nothing. The observer
is module-level rather than per-instance for the reason the pending-skill hook in
``skills_auto_model`` gives: consolidation holds its own store handles, so an
instance callback would miss the path that writes most lessons.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

#: WebSocket frame type, classified under the ``lessons`` declaration.
LESSONS_CHANGED_EVENT = "lessons.changed"

#: What happened to the store. ``updated`` is an existing row that gained a clause.
LESSON_ADDED = "added"
LESSON_UPDATED = "updated"
LESSON_REMOVED = "removed"
LESSON_CHANGE_ACTIONS = frozenset({LESSON_ADDED, LESSON_UPDATED, LESSON_REMOVED})

#: Who caused it. ``user_explicit`` is the ``/api/lessons`` routes (a person or the
#: agent's ``learn_add`` / ``learn_remove``); ``consolidation`` is the extractor.
#: The same words the rows' own ``source`` field uses.
LESSON_SOURCE_USER = "user_explicit"
LESSON_SOURCE_CONSOLIDATION = "consolidation"
LESSON_CHANGE_SOURCES = frozenset({LESSON_SOURCE_USER, LESSON_SOURCE_CONSOLIDATION})

_HOOK: Callable[[dict], None] | None = None


def set_lessons_changed_hook(fn: Callable[[dict], None] | None) -> None:
    """Register (or clear, with ``None``) the one lessons-changed observer.

    A later call replaces the hook, so a re-created dashboard state does not stack
    duplicate frames.
    """
    global _HOOK
    _HOOK = fn


def lessons_changed_payload(action: str, source: str, count: int = 1) -> dict:
    """The frame payload. Refuses a value outside the closed vocabularies."""
    if action not in LESSON_CHANGE_ACTIONS:
        raise ValueError(f"unknown lesson change action: {action!r}")
    if source not in LESSON_CHANGE_SOURCES:
        raise ValueError(f"unknown lesson change source: {source!r}")
    return {"action": action, "source": source, "count": max(1, int(count))}


def emit_lessons_changed(action: str, source: str, count: int = 1) -> None:
    """Tell the observer that the global lesson store changed.

    Call it only after the change is on disk. Best-effort: the write already
    succeeded, so a missing or failing observer never turns it into a failure.
    Safe from any thread; the observer owns marshalling onto the gateway loop.
    """
    hook = _HOOK
    if hook is None:
        return
    try:
        hook(lessons_changed_payload(action, source, count))
    except Exception:
        logger.debug("lessons-changed observer failed", exc_info=True)


def lesson_write_action(outcome: str, superseded: bool) -> str | None:
    """The change a lesson write made, from its outcome word, or ``None`` for none.

    ``inserted`` adds a row and ``enriched`` attaches a clause to one. A declining
    outcome (``unchanged``, ``deduped``, ``refused``) can still have deleted rows it
    superseded, which is a removal; otherwise it changed nothing.
    """
    if outcome == "inserted":
        return LESSON_ADDED
    if outcome == "enriched":
        return LESSON_UPDATED
    if superseded:
        return LESSON_REMOVED
    return None

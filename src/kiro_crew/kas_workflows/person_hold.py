"""Approval futures that hold a KAS plan handoff for a person's own answer.

A plan handoff (:func:`kiro_crew.kas_workflows.chat.needs_a_person`) passes no
grant rung when it is asked. A grant made WHILE it waits must not answer it
either: the all-slots ``trust`` and ``yolo`` sweeps in
``dashboard/chat_api/slot_approval.py`` settle every pending background and
channel future, so each hold site marks its future here and the sweeps skip a
marked one. Only the card's own buttons settle it.

Weakly held: a future leaves the set when it is collected, so a hold site needs
no cleanup on any exit path.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

_HELD: weakref.WeakSet[asyncio.Future[Any]] = weakref.WeakSet()


def hold_for_person(future: asyncio.Future[Any]) -> None:
    """Mark *future* as answerable only by a person's click on its card."""
    _HELD.add(future)


def held_for_person(future: object) -> bool:
    """Whether *future* holds a plan handoff no bulk grant may settle."""
    try:
        return future in _HELD
    except TypeError:
        return False

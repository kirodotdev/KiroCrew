"""Who asked for a linked dashboard slot's running turn, when one shared user did.

A Slack thread linked to a dashboard slot runs its turns there, and the slot's
tool prompts are mirrored back to the thread (``post_linked_approval``). A shared
user (``slack.shared_access``) answers only the prompts their own turn raised, so
the mirrored card has to name that person. This module is the record it reads:
the turn's task, mapped to the one shared user whose Slack message opened it.

Fail-closed by shape. The record is keyed by the turn's own task, so every turn
that did not start from :func:`note_turn_requester` reads ``""`` -- a dashboard
or owner turn, a queue drain (its own task, possibly merging several senders), a
cron, workflow or wake turn. Any text steered into the running turn, by anyone,
drops the record (:func:`forget_turn_requester`): the turn then carries more than
one person's words. ``""`` means "the owner alone may answer".
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

#: ``turn task -> the shared user who opened it``. Weak: a finished turn's task
#: is freed with its entry.
_requesters: "weakref.WeakKeyDictionary[asyncio.Task[Any], str]" = weakref.WeakKeyDictionary()


def note_turn_requester(task: "asyncio.Task[Any]", user_id: str) -> None:
    """Record that *user_id*'s Slack message opened the turn running as *task*."""
    if user_id:
        _requesters[task] = user_id


def forget_turn_requester(slot: Any) -> None:
    """Drop the record for *slot*'s running turn: other text reached it."""
    task = getattr(slot, "task", None)
    if task is not None:
        _requesters.pop(task, None)


def live_card_requester(entry: Any) -> str:
    """Who besides the owner may answer the linked card *entry* now, or ``""``.

    The card records the requester when it is posted; text another person steers
    into the turn afterwards revokes it. So the card answers to its requester only
    while its own slot's running turn still names that same person.
    """
    # circular import: slack.handler imports the modules that call this.
    from kiro_crew.slack import handler

    slots = getattr(handler._dashboard_state, "_slots", None)
    asker = str(getattr(entry, "requester", "") or "")
    slot = (slots or {}).get(str(getattr(entry, "slot_key", "") or ""))
    if not asker or slot is None or turn_requester(slot) != asker:
        return ""
    return asker


def turn_requester(slot: Any) -> str:
    """The shared user who opened *slot*'s running turn, or ``""``."""
    task = getattr(slot, "task", None)
    if task is None or task.done():
        return ""
    return _requesters.get(task, "")

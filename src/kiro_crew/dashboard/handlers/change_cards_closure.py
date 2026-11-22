"""Whether a change card's conversation was closed: the evidence its retirement reads.

A proposal whose conversation the user closed is retired (cancelled) by the card
settlement in :mod:`kiro_crew.dashboard.handlers.change_cards`, which imports these and
serves them under its own name; this module never imports it. Only positive closure
evidence counts: an in-memory close tombstone, or a persisted ``closed`` flag read off
the loop. A slot merely absent reads open.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore

logger = logging.getLogger("kiro_crew.dashboard.handlers.change_cards")


def _slot_is_open(
    state: Any, closed_meta_keys: "frozenset[str] | set[str]" = frozenset()
) -> Callable[[str], bool]:
    """The "this card's conversation is still open" predicate for retirement.

    Retirement cancels a proposal nobody can confirm, so it must fire only on a
    slot the user ACTUALLY closed -- never on one merely absent. Absence is not
    closure: ``slot_exists`` cannot carry this because it reads false in the
    window BEFORE the open-tab restore starts (``open_slots_restored`` False and
    ``restoring_open_slots`` False both -- a tab the restore has not reached
    yet), and a restore-budget skip is left out of ``unrestored_slot_keys``
    entirely, so both would be read as closed and cancelled under the user.

    Two gates, in order:

    1. **Until the open-tab restore has run once** (``open_slots_restored`` is
       not yet True) nothing is proven closed -- the live slot set is not yet
       authoritative -- so every key reads OPEN and no card retires.

    2. **After the restore** a key retires only on POSITIVE closure evidence:
       an in-memory close tombstone (:func:`channel_slots.slot_closed_since` --
       the slot was popped and :func:`note_slot_closed` recorded the instant),
       or a persisted ``closed`` flag read off the loop by :func:`_settle` and
       passed in as *closed_meta_keys*. A key with neither -- absent but never
       recorded closed -- stays OPEN. There is NO fallback to ``get_slot`` or
       bare ``slot_exists``: absence must never stand in as closure proof.
    """
    from kiro_crew.dashboard.channel_slots import slot_closed_since

    def is_open(key: str) -> bool:
        # Gate 1: the restore has not run, so the live set is not authoritative.
        if not bool(getattr(state, "open_slots_restored", False)):
            return True
        # A reopened live slot wins over a close recorded before its reopening.
        exists = getattr(state, "slot_exists", None)
        if callable(exists) and exists(key):
            return True
        # Gate 2: open unless there is real evidence this slot was closed.
        if key in closed_meta_keys:
            return False
        try:
            if slot_closed_since(state, key, 0.0):
                return False
        except Exception:
            logger.debug("close-tombstone read failed for %s", key, exc_info=True)
        return True

    return is_open


async def _closed_on_disk(state: Any, store: CardStore) -> "frozenset[str]":
    """Slot keys of pending cards whose session persists a ``closed`` flag.

    Positive closure evidence read off the loop, so the synchronous retirement
    sweep never blocks on disk and never treats a bare absence as closure. Empty
    until the open-tab restore has run (``open_slots_restored``): before that the
    live set is not authoritative, so nothing is proven closed and no read is
    worth doing. Each candidate slot's metadata is read through
    ``get_metadata_status`` -- never ``get_metadata`` -- so an unreadable or
    absent record reports NOT-readable and is left OPEN, not retired.
    """
    if not bool(getattr(state, "open_slots_restored", False)):
        return frozenset()
    log = getattr(state, "conversation_log", None)
    reader = getattr(log, "get_metadata_status", None)
    if not callable(reader):
        return frozenset()
    keys = {
        rec["slot_key"] for rec in store.pending(None) if rec.get("status") == cards.STATUS_PENDING
    }
    closed: set[str] = set()
    for key in keys:
        if await _persisted_close(reader, key):
            closed.add(key)
    return frozenset(closed)


async def _persisted_close(reader: Callable[[str], Any], slot_key: str) -> bool:
    """Whether *slot_key*'s session metadata, read off the loop, carries ``closed``.

    An unreadable or absent record, and a read that raises, answer False: only a
    record that was read and says closed counts as closure.
    """
    from kiro_crew.dashboard.chat_utils import drained_to_thread, slot_transcript_key

    try:
        meta, readable = await drained_to_thread(reader, slot_transcript_key(slot_key))
    except Exception:
        logger.debug("persisted-close read failed for %s", slot_key, exc_info=True)
        return False
    return bool(readable and meta and meta.get("closed"))


async def slot_closure_recorded(state: Any, slot_key: str) -> bool:
    """Whether *slot_key* has a readable persisted ``closed`` flag.

    Only a close whose history save committed counts. An absent slot and an
    in-memory close tombstone both answer False. Callers own the other gates:
    the restore having run, and no live slot or runtime carrying the key.
    """
    log = getattr(state, "conversation_log", None)
    reader = getattr(log, "get_metadata_status", None)
    if not callable(reader):
        return False
    return await _persisted_close(reader, slot_key)

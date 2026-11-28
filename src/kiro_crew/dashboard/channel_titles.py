"""Renaming a channel conversation's open dashboard tabs from the channel.

Split out of :mod:`kiro_crew.dashboard.channel_slots`, which re-exports
:func:`rename_channel_title_live` so callers keep that import path. A channel's
generated name takes :func:`apply_generated_channel_title_live`, which keeps it
``auto``.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.dashboard.chat_title import (
    _TITLE_ORIGIN_AUTO,
    apply_manual_title,
    apply_regenerated_title,
)
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.messaging.upload_gate import live_dashboard_slot

# The logger these lines always wrote under, before the split.
logger = logging.getLogger("kiro_crew.dashboard.channel_slots")


async def rename_channel_title_live(
    dashboard_state: Any,
    session_key: str,
    title: str,
) -> bool:
    """Rename the open dashboard slot for *session_key* and keep every view aligned.

    Returns ``False`` when no live slot owns the session, so the channel can fall
    back to its conversation-log-only path. A live slot is authoritative once it
    exists: changing only transcript metadata lets its later save rewrite the old
    in-memory title over the new one. So the live slot is renamed through
    :func:`~kiro_crew.dashboard.chat_title.apply_manual_title`, the code the
    sidebar rename and ``session_rename`` share: the slot is updated
    synchronously, its title epoch bumped so a background titler stands down,
    the title persisted through the epoch-aware helper, then broadcast.

    The save is best-effort by dashboard contract. A failed immediate metadata
    write leaves the updated live slot authoritative, and a later slot save can
    recover it; the dashboard's own rename endpoint makes the same trade.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    slots = [slot] if slot is not None else _channel_born_slots(dashboard_state, session_key)
    if not slots:
        return False

    for slot in slots:
        if not await apply_manual_title(
            dashboard_state, slot, title, route="channel_title", caller=session_key
        ):
            logger.warning("channel title update is live but not yet durable for %s", session_key)
    return True


async def apply_generated_channel_title_live(
    dashboard_state: Any,
    session_key: str,
    title: str,
) -> bool:
    """Give the open dashboard slots for *session_key* a channel's generated *title*.

    The generated-name counterpart of :func:`rename_channel_title_live`, for a
    channel's auto-title turn: the same slots, renamed through
    :func:`~kiro_crew.dashboard.chat_title.apply_regenerated_title`, so the title
    stays ``auto`` (refreshable) instead of turning manual. Without it a live
    slot's next save writes its old title back over the transcript's new one.

    A slot whose title someone set (a recorded origin other than ``auto``) keeps
    it. Returns whether any slot took the title.
    """
    slot = live_dashboard_slot(dashboard_state, session_key)
    slots = [slot] if slot is not None else _channel_born_slots(dashboard_state, session_key)
    renamed = False
    for slot in slots:
        # The same guard the auto-title task uses (``chat_title``): a permanent
        # delete unlinks the transcript and then awaits before it removes the
        # slot, so slot identity alone would let this title's metadata write
        # recreate the deleted conversation as a title-only file. Re-checked
        # inside the write lock by ``apply_regenerated_title``.
        def still_current(slot: Any = slot) -> bool:
            if dashboard_state._slots.get(slot.key) is not slot:
                return False
            log = getattr(dashboard_state, "conversation_log", None)
            return log is None or not log.delete_in_flight(slot_history_key(slot))

        if not still_current():
            continue
        # Only a title with a recorded origin was set by someone. A surfaced
        # channel tab shows the listing's fallback with no origin at all
        # (``surface_channel_session``), and a reloaded legacy title is
        # rehydrated as ``user``, so an empty origin is never a hand-set name.
        if getattr(slot, "_titled", False) and slot._title_origin not in (
            "",
            _TITLE_ORIGIN_AUTO,
        ):
            continue
        if not await apply_regenerated_title(
            dashboard_state, slot, title, still_current=still_current
        ):
            logger.warning("channel title update is live but not yet durable for %s", session_key)
        renamed = True
    return renamed


def _channel_born_slots(dashboard_state: Any, session_key: str) -> list[Any]:
    """Every open slot running on channel key *session_key* (``slack:<ts>`` etc.).

    Such a slot has no ``dashboard:`` key of its own: it carries the channel key in
    ``linked_session_key``, so :func:`live_dashboard_slot` never finds it, and a
    rename that skipped it would be saved back over by the tab's next save. More
    than one can share the key (a workflow result tab injected into the thread
    beside the thread's own tab), so all of them are returned.
    """
    slots = getattr(dashboard_state, "_slots", None) if dashboard_state is not None else None
    if not isinstance(slots, dict):
        return []
    return [
        slot
        for slot in list(slots.values())
        if getattr(slot, "linked_session_key", "") and effective_session_key(slot) == session_key
    ]

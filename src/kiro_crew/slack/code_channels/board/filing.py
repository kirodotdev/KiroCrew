"""File a conductor's workers under the channel's sidebar folder.

The sidebar already NESTS a worker under the conductor that created it (the
session tree's creator edge, which ``session_create`` records). What it cannot do
is put a worker in a folder: a worker created without ``folder`` lands at the top
level, away from the channel's session. So each board read files every live,
UNFILED worker into the conductor's own folder, once.

Only an unfiled worker is moved, and each worker at most once (the channel's
surface record remembers it): one the person, the conductor or anything else
placed in a folder keeps its place, and one the person moves back out stays out. The write is the
dashboard's own: the slot's ``folder_id`` under the state-wide slot-metadata lock
the sidebar's folder endpoint takes, merged alone into the session record (see
:func:`_place`), rolled back when it does not commit, then one slots push.
SEL-recorded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.board.source import find_slot
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: Filed-worker lists a store write failed to keep, per channel: the next read
#: uses them, as the publisher does for unsaved alert tokens, so a placement made
#: is never repeated over an owner's later move. Bounded like its tables.
_unsaved: dict[str, list[str]] = {}
_MAX_UNSAVED = 4096


def _max_filed() -> int:
    """Worker keys remembered per channel: one per session that can be open at once."""
    from kiro_crew.dashboard.state import MAX_LIVE_SLOTS

    return MAX_LIVE_SLOTS


def _folder_of(slot: Any) -> str:
    return str(getattr(slot, "folder_id", "") or "")


def _ended(state: Any, key: str) -> bool:
    """Whether *key*'s session has ended: not open, being built, restored or unread.

    ``slot_exists`` is the dashboard's own existence answer, which reads a
    restoring, unrestored or under-construction session as open. Any doubt (a
    failing or missing check) reads as not ended, so the marker is kept.
    """
    try:
        return find_slot(state, key) is None and state.slot_exists(key) is False
    except Exception:
        return False


async def file_unfiled_workers(binding: Binding, snapshot: Any) -> int:
    """File each unfiled worker on *snapshot* into the conductor's folder; how many."""
    state = surface.dashboard_state()
    conductor = find_slot(state, binding.session_key)
    folder = _folder_of(conductor) if conductor is not None else ""
    if state is None or not folder:
        return 0
    if len(_unsaved) >= _MAX_UNSAVED and binding.channel_id not in _unsaved:
        # Full of lists the store has not kept: no new placement until they are,
        # so no kept marker is ever dropped to make room.
        return 0
    record = await get_async(binding.channel_id)
    unsaved = _unsaved.get(binding.channel_id)
    filed = list(unsaved) if unsaved is not None else list(record.board_filed) if record else []
    stored = list(filed)
    cap = _max_filed()
    pruned = False
    moved = 0
    for row in getattr(snapshot, "rows", ()):
        slot: Any = find_slot(state, row.session_key)
        if slot is None or slot.key in filed:
            continue
        if len(filed) >= cap and not pruned:
            # Full: forget only the markers of sessions that have ended, once a pass.
            filed = [key for key in filed if not _ended(state, key)]
            pruned = True
        if len(filed) >= cap:
            # Still full: this worker is filed on a later pass, never by dropping
            # the marker of a session that is still open.
            continue
        if _folder_of(slot):
            # Already in a folder at first sight (``session_create`` filed it, or a
            # person did): remembered as handled, so a person who later moves it
            # out is not overridden by the next board read.
            filed.append(slot.key)
            continue
        placed = await _place(state, slot, folder)
        if placed or _folder_of(slot):
            # Once: a worker the person moves back out stays out. A failed save
            # is not remembered, so the next read tries again.
            filed.append(slot.key)
        if placed:
            moved += 1
            surface.audit(
                "slack",
                "slack.code_channel.file_worker",
                "allowed",
                f"channel={binding.channel_id} worker={slot.key} folder={folder}",
            )
    if filed != stored or unsaved is not None:
        try:
            await update_async(binding.channel_id, board_filed=filed)
            _unsaved.pop(binding.channel_id, None)
        except Exception:
            # The placements were made: the next read must not file them again
            # over an owner who moves one out meanwhile.
            _unsaved[binding.channel_id] = filed
            logger.debug("code channel: filed-worker list not stored", exc_info=True)
    return moved


def forget(channel_id: str) -> None:
    """Drop *channel_id*'s unsaved filed-worker list (the channel was archived or unbound)."""
    _unsaved.pop(channel_id, None)


async def _place(state: Any, slot: Any, folder: str) -> bool:
    """Put *slot* in *folder*: the live field, and ``folder_id`` alone on its record.

    The write merges that one key under the transcript lock instead of rewriting
    the whole record (a forced full save), so a close or another placement that
    lands meanwhile is never erased: the guard refuses a record that is closed,
    missing, or in ANOTHER folder (one already in this folder, e.g. a dirty-slot
    flush that saved the live value first, is the placement done). Refused, failed or cancelled, the
    live field goes back, unless something else changed it since.
    """
    from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock
    from kiro_crew.dashboard.chat_utils import slot_history_key

    log = getattr(state, "conversation_log", None)
    if log is None:
        return False

    def _undo() -> None:
        if _folder_of(slot) == folder:
            slot.folder_id = ""

    async with _slot_meta_txn_lock(state):
        if _folder_of(slot) or state.get_slot(slot.key) is not slot:
            return False  # filed meanwhile, or the slot was closed or replaced
        history_key = slot_history_key(slot)
        # Set first, so a full save that runs meanwhile writes the same value.
        slot.folder_id = folder
        write = asyncio.ensure_future(
            asyncio.to_thread(
                log.update_metadata_if,
                history_key,
                {"folder_id": folder},
                lambda meta: not meta.get("closed") and meta.get("folder_id") in (None, "", folder),
                require_existing=True,
                require_write=True,
            )
        )
        try:
            committed = bool(await asyncio.shield(write))
        except asyncio.CancelledError:
            # The thread may still commit: undo only once it says it did not.
            write.add_done_callback(
                lambda f: _undo() if f.cancelled() or f.exception() or not f.result() else None
            )
            raise
        except Exception:
            logger.warning("code channel: filing worker %s failed", slot.key, exc_info=True)
            committed = False
        if committed and (
            state.get_slot(slot.key) is not slot or slot_history_key(slot) != history_key
        ):
            # Rebound or replaced during the write: the record written is not this
            # slot's any more, so the placement is not claimed.
            committed = False
        if not committed:
            _undo()
        surface.push_slots(state)
        return committed

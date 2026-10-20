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
the sidebar's folder endpoint takes, persisted with the same pins
(``cron_inject``'s placement does the same), rolled back when it does not commit,
then one slots push. SEL-recorded.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.board.source import find_slot
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_store import get_async, update_async

logger = logging.getLogger(__name__)

#: Worker keys remembered per channel as already filed once.
MAX_FILED = 500


def _folder_of(slot: Any) -> str:
    return str(getattr(slot, "folder_id", "") or "")


async def file_unfiled_workers(binding: Binding, snapshot: Any) -> int:
    """File each unfiled worker on *snapshot* into the conductor's folder; how many."""
    state = surface.dashboard_state()
    conductor = find_slot(state, binding.session_key)
    folder = _folder_of(conductor) if conductor is not None else ""
    if state is None or not folder:
        return 0
    record = await get_async(binding.channel_id)
    filed = list(record.board_filed) if record else []
    stored = list(filed)
    moved = 0
    for row in getattr(snapshot, "rows", ()):
        slot: Any = find_slot(state, row.session_key)
        if slot is None or slot.key in filed or _folder_of(slot):
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
    if filed != stored:
        try:
            await update_async(binding.channel_id, board_filed=filed[-MAX_FILED:])
        except Exception:
            logger.debug("code channel: filed-worker list not stored", exc_info=True)
    return moved


async def _place(state: Any, slot: Any, folder: str) -> bool:
    from kiro_crew.dashboard.chat_folders import _slot_meta_txn_lock
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
    from kiro_crew.dashboard.chat_utils import slot_history_key

    async with _slot_meta_txn_lock(state):
        if _folder_of(slot):
            return False  # filed meanwhile: that placement stands
        slot.folder_id = folder
        committed = False
        try:
            committed = bool(
                await save_slot_off_loop(
                    state,
                    slot,
                    force=True,
                    best_effort=False,
                    expected_history_key=slot_history_key(slot),
                    expected_slot_name=slot.key,
                )
            )
        except Exception:
            logger.warning("code channel: filing worker %s failed", slot.key, exc_info=True)
        if not committed and _folder_of(slot) == folder:
            slot.folder_id = ""
        surface.push_slots(state)
        return committed

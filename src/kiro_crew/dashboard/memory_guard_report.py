"""The memory guard's report on a worker's item (rfc-host-memory-guard.md §4.4).

When the host memory guard stops a tree a bound worker started, the gateway files one
``question`` report on that worker's item, so the conductor that dispatched it wakes
through the existing report wake. It is recorded the way a worker's own report is: the
store commits (``work_ledger.apply_guard_report``), then the board's copy is appended to
the conductor's crew log, and the commit is undone when that entry does not land. The
recording helpers are read through the ``handlers.work_ledger`` facade at call time.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from kiro_crew import work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers import work_ledger as wlh
from kiro_crew.work_ledger import WorkLedgerError

logger = logging.getLogger(__name__)

_OPERATION = "memory_guard_report"


async def record_guard_report(state: Any, worker_slot_key: str, summary: str) -> str:
    """File the guard's ``question`` report on *worker_slot_key*'s bound item.

    Returns the item id, or ``""`` when nothing was written: the worker is unbound,
    its item is closed or gone, the crew log is off, or the record did not land (then
    the commit is undone). Never raises: the stop already happened and is logged.
    """
    try:
        binding = await asyncio.to_thread(work_ledger.read_binding, worker_slot_key)
        if binding is None:
            return ""
        conductor_key, item_id = binding
        return await _record(state, worker_slot_key, conductor_key, item_id, summary)
    except Exception:
        logger.exception("memory guard: report on %s's item failed", worker_slot_key)
        return ""


async def _record(
    state: Any, worker_key: str, conductor_key: str, item_id: str, summary: str
) -> str:
    unit, refusal = await asyncio.to_thread(
        wlh._acting_unit, state, worker_key, _OPERATION, item_id, conductor_key
    )
    if refusal is not None or not unit:
        return ""
    acting_unit: str = unit

    async with wlh._board_lock(conductor_key):
        if await wlh._refuse_if_dirty(conductor_key, worker_key, _OPERATION) is not None:
            return ""

        # Commit then record as one drained transaction, as the worker's report does,
        # so a shutdown cannot leave the commit with no crew-log entry and no undo.
        async def _commit_and_record() -> str:
            snapshot = await asyncio.to_thread(
                functools.partial(work_ledger.snapshot_for_write, conductor_key, item_id=item_id)
            )
            try:
                result = await asyncio.to_thread(
                    functools.partial(
                        work_ledger.apply_guard_report, conductor_key, item_id, summary=summary
                    )
                )
            except WorkLedgerError as exc:
                logger.info("memory guard: no report on item %s (%s)", item_id, exc.code)
                return ""
            if result is None:
                logger.info("memory guard: item %s holds a done or reasoned report", item_id)
                return ""
            item, event = result["item"], result["event"]
            landed = False
            try:
                board = await asyncio.to_thread(work_ledger.read_conductor, conductor_key)
                established, board = await wlh._board_header(conductor_key, board)
                if established:
                    # The entry carries what the commit holds, as the worker's report's
                    # does, so the rebuild equals the cache. Too wide a line does not land
                    # and is undone below.
                    entry = wlh._entry_with_baseline(
                        conductor_key,
                        item,
                        board,
                        actor="worker",
                        by=worker_key,
                        action="report",
                        item_id=item.item_id,
                        generation=getattr(board, "generation", None) or None,
                        round=item.round,
                        status=item.status,
                        reason=item.reason,
                        wake_at=item.wake_at,
                        summary=item.summary,
                        artifacts=item.artifacts,
                        discussion=item.discussion,
                        pr=item.pr,
                        last_report_at=item.last_report_at,
                        event=event.text,
                        event_kind="report",
                        event_id=event.id or None,
                        event_ts=event.ts or None,
                    )
                    landed = await _land(state, conductor_key, acting_unit, entry)
            except Exception:
                # Raised after the commit (the entry could not be built): undo it
                # below like any record that did not land.
                logger.exception("memory guard: report on %s could not be recorded", item_id)
            if not landed:
                await wlh._refuse_unrecorded(
                    worker_key, _OPERATION, item.item_id, conductor_key, snapshot
                )
                return ""
            await asyncio.to_thread(wlh._mark_recorded, conductor_key, item.item_id)
            return item.item_id

        return await wlh._drain_before_cancelling(_commit_and_record())


async def _land(state: Any, conductor_key: str, acting_unit: str, entry: dict[str, Any]) -> bool:
    """Append *entry* where a worker's own report would go; whether it landed.

    The board's copy into the conductor's live unit first; a live unit with no log
    yet (an agent switch prewarmed it before its first turn) hands it to the
    board's newest unit that has one; a board with no log of its own takes the
    worker's copy, as ``work_ledger_worker.api_work_report`` does.
    """
    from kiro_crew import session_ledger

    parent_unit = await asyncio.to_thread(wlh._conductor_unit, state, conductor_key)
    if not parent_unit:
        return bool(await asyncio.to_thread(crew_log_emit.on_work_recorded, acting_unit, entry))
    board_copy = {**entry, "board_copy": True}
    if await asyncio.to_thread(crew_log_emit.on_work_recorded, parent_unit, board_copy):
        return True
    fallback = await asyncio.to_thread(session_ledger.newest_work_unit, conductor_key)
    if fallback and fallback != parent_unit:
        return bool(await asyncio.to_thread(crew_log_emit.on_work_recorded, fallback, board_copy))
    if not fallback:
        return bool(await asyncio.to_thread(crew_log_emit.on_work_recorded, acting_unit, entry))
    return False

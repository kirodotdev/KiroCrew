"""Reclaim the worker a lost create reply leaves on a crew.

A crew makes its bound worker (slot, mailbox binding, Trust) before it answers the
create. When that reply is lost, or the hub stops before it records the ``bind``,
the crew holds a worker no hub item names and refuses the item ``duplicate_item``
from then on. The crew's batch reply names each such worker under ``held``
(``remote_work_items_pull``), and the pull that reached the crew settles it:

* the item is open, unbound and was never retired from that crew, and the crew's
  copy is open: the ``bind`` the
  create would have recorded is recorded now, and the hub's decision forwarded;
* anything else (closed on either side, bound elsewhere, retired from that crew,
  unknown): the
  close path of a turn the owner did not send (``remote_ingest.CLOSE_BY_WAKE``):
  ``trust: false`` now, and a DELETE only once the crew lists the slot idle and
  its copy reported ``done``, with the close backoff. A running worker is never
  deleted from here.

A worker some item still holds, bound or retired, is the pull's or the close
sweep's and is left alone. Each worker is decided while no work-item create may
start (``remote_placement.creates_held``) and from the item as settled then: with
a create in flight nothing is decided, since its reply may still be on its way
and its own bind would find the item taken.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from typing import Any

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_placement
from kiro_crew.dashboard.handlers.work_ledger import _audit, _record_conductor_write

logger = logging.getLogger(__name__)


def _holds(item: work_ledger.WorkItem, crew: str, slot: str) -> bool:
    """Whether *item* already holds *slot* on *crew*, bound or retired."""
    if (item.worker_instance_id, item.worker_session_key) == (crew, slot):
        return True
    return any(
        (b["worker_instance_id"], b["worker_session_key"]) == (crew, slot)
        for b in item.retired_bindings
    )


async def reclaim(
    state: Any,
    mgr: Any,
    board: str,
    held: dict[str, dict[str, dict[str, Any]]],
    deadline: float,
    reached: Collection[str] = (),
) -> None:
    """Bind or close each worker in *held* (``{crew: {item id: row}}``), then send
    again the decision of each item whose crew answered this pull (*reached*, item
    ids) and whose remote record says ``decision_unsent``. Never raises.

    Each one is cut off at one item's pull budget; past *deadline* the rest wait
    for the next pull, which finds them again.
    """
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    if mgr is None:
        return
    loop = asyncio.get_running_loop()
    for crew, rows in held.items():
        for item_id, row in rows.items():
            left = deadline - loop.time()
            if left <= 0:
                return
            try:
                await asyncio.wait_for(
                    _settle(state, mgr, board, crew, item_id, str(row["slot"]), row.get("closed")),
                    min(ri.PULL_TIMEOUT_SECS, left),
                )
            except Exception:  # noqa: BLE001 - the next pull finds it again
                logger.info("could not reclaim %s on %s", item_id, crew, exc_info=True)
    # After the held workers, and only to a crew that just answered: a crew that
    # stalls spends no reclaim's time.
    for item_id in sorted(reached):
        left = deadline - loop.time()
        if left <= 0:
            return
        try:
            if (await asyncio.to_thread(ri._stored, board, item_id)).decision_unsent:
                await asyncio.wait_for(
                    _forward(state, board, item_id), min(ri.PULL_TIMEOUT_SECS, left)
                )
        except Exception:  # noqa: BLE001 - still marked for the next pull
            logger.info("could not forward the decision of %s", item_id, exc_info=True)


async def _settle(
    state: Any,
    mgr: Any,
    board: str,
    crew: str,
    item_id: str,
    slot: str,
    closed: Any = False,
) -> None:
    """Decide one held worker. No create starts meanwhile, and the item is read
    settled inside that hold, so neither a create nor a bind can move under it.
    A worker whose crew copy is *closed* is never bound, only closed."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    async with remote_placement.creates_held(state) as idle:
        if not idle:
            return
        readable, item = await _settled(board, item_id)
        if not readable or (item is not None and _holds(item, crew, slot)):
            return
        if (
            closed is not True
            and item is not None
            and not item.is_terminal
            and not item.worker_session_key
            and crew not in ri.retired_crews(item)
        ):
            # A decide recorded while the item was unbound reached no peer copy. It
            # goes before the bind, so a failed forward leaves the worker held and
            # the next pull tries both again.
            if item.decision:
                sent = await ri._forward_one(state, item, to=(crew, slot))
                if not sent.get("ok"):
                    logger.info("reclaim of %s waits: decision not forwarded", item_id)
                    return
            # Stored before the bind can commit, so a forward the bind's own
            # decide misses, or a settle cancelled after it, is sent by a later
            # pull, after a restart too.
            if not await _mark_unsent(board, item_id, True):
                return
            bound = await _bind(state, board, crew, slot, item)
            if not bound:
                await _mark_unsent(board, item_id, False)
        else:
            _schedule_close(state, mgr, board, crew, slot)
            return
    if bound:
        # A decide that landed after the read above found the item unbound too.
        await _forward(state, board, item_id, unless=item.decision)


async def _forward(state: Any, board: str, item_id: str, *, unless: str | None = None) -> None:
    """Forward *item_id*'s settled decision; one that fails stays marked
    ``decision_unsent`` for the next pull, and is audited."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    outcome = await ri.forward_decide(state, board, item_id, unless=unless)
    code = str(outcome.get("code") or "")
    if outcome.get("ok") or code == work_ledger.CODE_UNKNOWN_ITEM:
        # Delivered, or no worker is bound to carry it any more.
        await _mark_unsent(board, item_id, False)
        return
    _audit(board, "work_ledger_record.remote_decide", "failure", resources=item_id, error=code)


async def _mark_unsent(board: str, item_id: str, unsent: bool) -> bool:
    """Store ``decision_unsent`` on *item_id*'s remote record; whether it holds now."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    async with ri._board_lock(board):
        record = await asyncio.to_thread(ri._stored, board, item_id)
        if record.decision_unsent == unsent:
            return True
        record.decision_unsent = unsent
        return await asyncio.to_thread(ri._store, board, item_id, record)


async def _settled(board: str, item_id: str) -> tuple[bool, work_ledger.WorkItem | None]:
    """``(readable, item)`` for *item_id* under the board lock: ``item`` is ``None``
    for an item this board does not hold, and ``readable`` False when the board
    cannot be read now (dirty, torn), which decides nothing."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    async with ri._board_lock(board):
        if await ri._refuse_if_dirty(board, board, "work_ledger_read.reclaim"):
            return False, None
        try:
            # Strict: an item file present but unreadable now must not read as absent,
            # which would close the worker its item should bind.
            return True, await asyncio.to_thread(
                work_ledger.read_work_item, board, item_id, strict=True
            )
        except (work_ledger.WorkLedgerError, OSError):
            return False, None


async def _bind(state: Any, board: str, crew: str, slot: str, item: work_ledger.WorkItem) -> bool:
    """Record the ``bind`` a lost create reply never let the hub record; whether it did."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    try:
        # Refused when a retire's fresh record has not stored yet: the next pull
        # looks again.
        await ri.open_unbound_item(board, item.item_id)
    except ri._routes().CrewCallError:
        return False
    unit, unit_error = await ri._board_unit(state, board)
    if unit_error is not None:
        return False
    resp = await ri._drain_before_cancelling(
        _record_conductor_write(
            state,
            board,
            unit,
            "bind",
            {
                "action": "bind",
                "item_id": item.item_id,
                "worker_session_key": slot,
                "worker_instance_id": crew,
            },
            operation="work_ledger_read.reclaim",
        )
    )
    if resp.status != 200:
        logger.info("reclaim bind of %s not recorded (%s)", item.item_id, ri._response_code(resp))
        return False
    logger.info("reclaimed %s on %s for item %s", slot, crew, item.item_id)
    return True


def _schedule_close(state: Any, mgr: Any, board: str, crew: str, slot: str) -> None:
    """Start the wake-turn close of *slot* on *crew* unless one is in flight or backing off."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    key = (crew, slot)
    if not ri._close_due(key):
        return
    failures = ri._CLOSE_RETRY.get(key, (0, 0.0))[0]
    ri._CLOSE_RETRY[key] = (failures, float("inf"))
    task = asyncio.ensure_future(
        ri._close_retired_slot(state, mgr, board, crew, slot, only_if_done=True, record=False)
    )
    ri._CLOSE_TASKS.add(task)
    task.add_done_callback(ri._CLOSE_TASKS.discard)


__all__ = ["reclaim"]

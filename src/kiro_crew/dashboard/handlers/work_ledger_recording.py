"""Recording a ledger write in the crew log, which the store files are a cache of.

The fit probe a write passes before it commits (the widest values the store can
generate, the baseline an unrecorded item is owed), the ``work/recorded`` entry, the
per-board lock and the drain that finishes a write before a cancellation, the dirty
cache refusal, the board header an entry's generation comes from, the stamps that mark an
item or header as held whole once its entry lands, the undo when the record does not land,
and the crew-log unit and crew store a write is recorded into.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
from typing import Any

from aiohttp import web

from kiro_crew import work_ledger
from kiro_crew.constants import env_file_display
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.resolve import UNKNOWN, unit_for_session_key
from kiro_crew.dashboard.handlers.office_slides import _KeyedLocks
from kiro_crew.dashboard.handlers.work_ledger_refusals import _audit, _refuse_400, _refuse_409
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.work_ledger import WorkLedgerError

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


# The widest values the store can commit for the fields it generates, so a fit
# probe built before the commit is never narrower than the entry written after it.
# Event text is capped in characters; an astral character serializes as twelve bytes.
_WIDEST_COUNTER = 2**31 - 1
_WIDEST_ITEM_ID = "it_ffffffff"
_WIDEST_EVENT_TEXT = "W" * (12 * work_ledger.MAX_EVENT_TEXT_CHARS)
# Stamps are ``_now_iso()`` (seconds precision, local offset, at most an
# ``+HH:MM:SS`` offset); a generation is ``token_hex(8)`` and an event id a
# sha256 prefix, both 16 hex characters.
_WIDEST_STAMP = "9999-12-31T23:59:59+23:59:59"
_WIDEST_HEX_ID = "f" * 16


def _current_item_and_board(slot: str, item_id: str | None) -> tuple[Any, Any]:
    """The cached item and header as they stand, ``None`` for each that is absent.

    Read for the fit probe only: a baseline is due when the item is unstamped and
    the probe must carry it, or a near-limit write would commit and be undone
    with a 503 where a 400 was promised. Nothing here bootstraps; a missing
    ledger, or an id the store will refuse, reads as ``None``.
    """
    try:
        header = work_ledger.read_conductor(slot)
        item = work_ledger.read_work_item(slot, item_id) if item_id else None
    except (WorkLedgerError, OSError):
        return None, None
    return item, header


def _mark_goal_recorded(slot: str) -> None:
    """Stamp the header as held by the log; a failure only defers the header's check."""
    try:
        work_ledger.mark_goal_recorded(slot)
    except (WorkLedgerError, OSError):
        logger.debug("work ledger: could not mark the header recorded", exc_info=True)


def _mark_recorded(slot: str, item_id: str) -> None:
    """Stamp the item as held whole by the log; a failure only costs one more baseline."""
    try:
        work_ledger.mark_item_recorded(slot, item_id)
    except (WorkLedgerError, OSError):
        logger.debug("work ledger: could not mark %s recorded", item_id, exc_info=True)


def _entry_with_baseline(slot: str, item: Any, header: Any, **fields: Any) -> dict[str, Any]:
    """A ``work/recorded`` payload: the action's fields over a baseline when one is due.

    A field the action passes as ``None`` means "this action did not set it", not
    "clear it": it must not knock the baseline's value out, or a legacy item whose
    first recorded mutation is a ``decide`` would be recorded without its title.
    """
    baseline = _baseline_fields(item, header) if item is not None else {}
    set_fields = {name: value for name, value in fields.items() if value is not None}
    return _work_entry(slot, **crew_log_emit.safe_work_fields({**baseline, **set_fields}))


def _entry_probe_with_baseline(
    slot: str, item: Any, header: Any, **fields: Any
) -> tuple[dict[str, Any] | None, web.Response | None]:
    """Build a fit probe, mapping legacy redaction collisions to HTTP 400."""
    try:
        return _entry_with_baseline(slot, item, header, **fields), None
    except crew_log_emit.WorkFieldError as exc:
        return None, _refuse_400(work_ledger.CODE_INVALID_VALUE, str(exc))


def _baseline_overflows(
    slot: str, item: Any, header: Any, *, skip: tuple[str, ...], **fields: Any
) -> bool:
    """Whether the committed item itself, not the request, is what cannot fit.

    An entry about an item the record has never held whole carries the whole item
    (:func:`_baseline_fields`). When such an entry does not fit one log line, two
    things can be too big: the request's own text, which its sender can shorten,
    or the committed item, which only its conductor can shrink. Telling the two
    apart is the difference between a refusal that helps and one that strands the
    worker: with the fields this write sets removed from the baseline, a probe
    that still does not fit is the item's own size. The acceptance is the only
    field without a cap of its own, so it is what overflows in practice.
    """
    if item is None or not _baseline_fields(item, header):
        return False
    base = _baseline_fields(item, header)
    for name in skip:
        base.pop(name, None)
    return not crew_log_emit.work_entry_fits(_work_entry(slot, **{**base, **fields}))


def _refuse_item_too_large(key: str, operation: str, item_id: str) -> web.Response:
    _audit(key, operation, "denied", resources=item_id, error="work_item_too_large")
    return _refuse_400(
        "work_item_too_large",
        f"item {item_id} predates the record, so its first recorded write must carry "
        "the whole item, and the committed item does not fit one crew-log line (its "
        "acceptance is the only field without a cap); no shorter write about it can "
        f"land. The conductor shrinks it first: work_ledger_record action=accept "
        f"item_id={item_id} with a smaller acceptance records the item whole, and "
        "writes about it land from then on.",
        field="acceptance",
    )


def _baseline_fields(item: Any, header: Any) -> dict[str, Any]:
    """The whole committed item, for an entry about an item the record never held.

    An item from before the projection (or whose recorded create was undone) has
    no create entry, so a delta about it could never rebuild it. Until the log has
    held it whole once (`recorded_at`), every entry about it carries all of it.
    """
    if getattr(item, "recorded_at", ""):
        return {}
    return {
        "baseline": True,
        # The board's own lineage and goal ride along: a nested board whose
        # header the record never saw would otherwise rebuild as a top-level one.
        "depth": getattr(header, "depth", 0),
        "parent_item": getattr(header, "parent_item", None) or None,
        "goal": getattr(header, "goal", "") or None,
        "board_round": getattr(header, "round", 0),
        "board_created_at": getattr(header, "created_at", "") or None,
        "title": item.title,
        "acceptance": item.acceptance,
        "state": item.state,
        "verdict": item.verdict,
        "decision": item.decision or None,
        "worker_session_key": item.worker_session_key or None,
        "round": item.round,
        "fails": item.fails,
        "status": item.status,
        "reason": item.reason,
        "summary": item.summary or None,
        "artifacts": item.artifacts,
        "pr": item.pr,
        "created_at": item.created_at or None,
        "last_report_at": item.last_report_at or None,
        "closed_at": item.closed_at or None,
        "decided_at": getattr(item, "decided_at", None) or None,
        "pending_since": getattr(item, "pending_since", None) or None,
    }


#: One asyncio lock per board, held across a write's commit and its crew-log
#: append and across a rebuild, so a rebuild can never fold between the two and
#: write the cache back to before a mutation the log then records. Every writer
#: and every rebuild runs on this gateway's one loop, which is what makes an
#: in-process lock the complete answer. The registry is ``office_slides``'s
#: ``_KeyedLocks``: a board's entry lives only while someone holds or waits for
#: its lock, so it is bounded by the boards in use.
_BOARD_LOCKS = _KeyedLocks()


def _board_lock(slot: str) -> contextlib.AbstractAsyncContextManager[None]:
    return _BOARD_LOCKS.hold(slot)


async def _drain_before_cancelling(coro: Any) -> Any:
    """Finish *coro* before propagating this caller's cancellation.

    Repeated shutdown cancellation is absorbed only while the task drains. The
    transaction is bounded by the crew-log append timeout and finite cache writes,
    so this cannot keep shutdown alive indefinitely.
    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            if cancelled:
                raise asyncio.CancelledError
            return result
        except asyncio.CancelledError:
            cancelled = True
            if not task.done():
                continue
            raise


async def _refuse_if_dirty(slot: str, key: str, operation: str) -> web.Response | None:
    """409 while the board's cache is flagged dirty: only a rebuild may touch it.

    The flag is set when an undo failed, so the cache may hold a mutation the
    record never saw or half of a rebuild. Serving or writing over that would
    pass the damage on; the refusal names the cure.
    """
    reason = await asyncio.to_thread(work_ledger.cache_dirty, slot)
    if reason is None:
        return None
    _audit(key, operation, "denied", error=work_ledger.CODE_CACHE_DIRTY)
    return _refuse_409(
        work_ledger.CODE_CACHE_DIRTY,
        f"the board's cache is flagged dirty ({reason}); run work_ledger_rebuild to "
        "rebuild it from the record, or, to keep the cache as it stands, remove the "
        f"{work_ledger.DIRTY_FILE!r} marker in the board's work-ledger directory",
    )


async def _board_header(slot: str, header: Any) -> "tuple[bool, Any]":
    """*slot*'s conductor record for an entry, and whether it could be established.

    The generation an entry carries comes off this record, and a MISSING one is two
    different facts: a board minted before the stamp existed carries none and folds
    by its own generationless entries, while a header read that faulted also carries
    none. They cannot be treated alike here, because the fold omits a generationless
    entry once a stamped board is live -- so appending one on a fault loses a write
    this route has already committed, and the next rebuild is where it disappears.

    A header the commit handed back is the board's own and is taken as it is,
    generation or not. Only an ABSENT one is re-read, strictly: that is the contract
    the header writers use for the same reason, raising on an I/O fault and
    answering None only for a ledger that is really gone. Either of those leaves the
    generation unestablished, and the caller refuses the write rather than appending
    an entry the fold would drop. The record also carries the baseline's board
    fields, so resolving it once serves both.
    """
    if header is not None:
        return True, header
    try:
        record = await asyncio.to_thread(
            functools.partial(work_ledger.read_conductor, slot, strict=True)
        )
    except OSError:
        logger.warning("could not read the conductor record for %s", slot, exc_info=True)
        return False, None
    return record is not None, record


async def _refuse_unrecorded(
    key: str,
    operation: str,
    item_id: str | None,
    slot: str,
    snapshot: dict[str, bytes | None],
    *,
    created: str | None = None,
    worker_session_key: str | None = None,
) -> web.Response:
    """The store committed but the crew log did not confirm the entry.

    The log is the record, so the cache must not keep a mutation the log never
    saw: every file the write touched is put back from the *snapshot* the route
    took before the write, and an item the write created is removed. That undo
    needs no fold, so it is exact for a board from before the projection as much
    as for a fresh one; it runs off the loop, and it takes the store's own file
    locks for the files it puts back -- the board lock this caller holds is an
    in-process one, so it does not exclude a second gateway on the same store.
    When even that fails the refusal stands and a rebuild converges.
    Answered as a failure so the caller never believes the mutation is recorded;
    the committed `item_id` is returned so a `create` is not repeated on retry.
    """
    logger.error("work ledger %s for %s committed without a crew-log record", operation, key)
    _audit(key, operation, "error", resources=item_id or "", error="crew_log_unrecorded")
    # What this write left in those files, read before the undo touches them: the
    # undo puts back only a file still holding it, so a write a second gateway
    # committed in between survives and flags the cache instead of being replaced.
    after = await asyncio.to_thread(work_ledger.current_bytes, snapshot)
    try:
        await asyncio.to_thread(
            functools.partial(
                work_ledger.restore_snapshot,
                slot,
                snapshot,
                created_item=created,
                item_id=item_id,
                worker_session_key=worker_session_key,
                expected=after,
            )
        )
    except Exception:  # noqa: BLE001 - the refusal is the answer either way
        logger.warning("could not undo the unrecorded write on %s", slot, exc_info=True)
        try:
            await asyncio.to_thread(
                work_ledger.mark_cache_dirty, slot, "an unrecorded write could not be undone"
            )
        except Exception:  # noqa: BLE001 - nothing left to do but say so
            logger.error("could not flag the cache dirty for %s", slot, exc_info=True)
    return web.json_response(
        {
            "ok": False,
            "error": "the write was stored but its crew-log record was not confirmed",
            "code": "crew_log_unrecorded",
            "item_id": item_id,
        },
        status=503,
    )


def _work_entry(slot: str, **fields: Any) -> dict[str, Any]:
    """One ``work/recorded`` payload: the board's slot plus the fields this write set.

    ``None`` values are dropped, because an omitted field means "unchanged" to
    every reader of the entry, and the type's own validation would refuse a null.
    """
    payload: dict[str, Any] = {"slot": slot}
    payload.update({name: value for name, value in fields.items() if value is not None})
    return payload


def _acting_unit(
    state: DashboardState, key: str, operation: str, resources: str
) -> tuple[str | None, web.Response | None]:
    """The crew-log unit *key* is serving, or the refusal that stops the write.

    The work ledger is a projection of the crew log: every write appends one
    ``work/recorded`` entry to the acting session's log, and the store files are
    only a cache rebuilt from those entries. A write with nowhere to append is
    therefore refused up front -- with the emitter off, or with a caller whose
    unit the registry cannot name -- rather than leaving the cache holding a
    mutation the log never saw.
    """
    if not crew_log_emit.enabled():
        _audit(key, operation, "denied", resources=resources, error="crew_log_off")
        return None, _refuse_409(
            "crew_log_off",
            "the crew log is off and the work ledger records only into it; "
            f"{crew_log_emit.CREW_LOG_ENV} is set to 0, false, no, off or an unrecognised value; "
            f"unset it (or remove it from {env_file_display()}) and restart the gateway",
        )
    unit = unit_for_session_key(getattr(state, "sessions", None), key)
    if not unit or unit == UNKNOWN:
        _audit(key, operation, "denied", resources=resources, error="crew_log_unit_unknown")
        return None, _refuse_409(
            "crew_log_unit_unknown",
            "this session's crew log unit is not known; there is nothing to record into",
        )
    return unit, None


def _conductor_unit(state: DashboardState, conductor_key: str) -> str:
    """The unit a board's own copy of a worker's report is written into, or ``""``.

    The conductor's live unit when the session registry names one; otherwise the
    conductor slot's newest unit (``session_ledger.newest_work_unit``), whether or not
    that session is running now or has recorded anything yet.
    """
    unit = unit_for_session_key(getattr(state, "sessions", None), conductor_key)
    if unit and unit != UNKNOWN:
        return unit
    from kiro_crew import session_ledger

    return session_ledger.newest_work_unit(conductor_key)


def _crew_store(conductor_slot_key: str) -> str:
    """The crew-log store a conductor's dispatch record belongs to, or ``""``.

    A crew log belongs to a CREW, and the crew a conductor slot names is the member
    whose DM thread it is -- keyed ``member-<slug>``, so the store is that slug,
    read through the members module's own derivation so this cannot drift from the
    slot layer.

    ``""`` for every other slot, and that is a refusal rather than a gap: a board
    driven from an ordinary chat slot has no crew to own the record, and spelling a
    unit id out of a slot key would attribute the work to a unit whose header
    names a crew no reader can resolve. The board's own authority is unaffected --
    it is the ``work/recorded`` entry in the acting session's log, which every
    write already refuses to proceed without.
    """
    from kiro_crew.eventlog_hooks import member_slug_for_slot

    return member_slug_for_slot(conductor_slot_key) or ""

"""The worker half of the work ledger: the brief and the report.

``GET /api/work-ledger/brief`` returns the one item a worker session was dispatched
for, and ``POST /api/work-ledger/report`` files its status against that item, refused
before the commit when a ``done`` carries no evidence or the record would not fit.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from aiohttp import web

from kiro_crew import work_ledger, work_preview
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers.work_ledger_caller import (
    _caller_key,
    _contained_channel_caller,
    _worker_binding,
)
from kiro_crew.dashboard.handlers.work_ledger_recording import (
    _WIDEST_COUNTER,
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_STAMP,
    _acting_unit,
    _baseline_overflows,
    _board_header,
    _board_lock,
    _conductor_unit,
    _crew_store,
    _current_item_and_board,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _entry_with_baseline,
    _mark_recorded,
    _refuse_if_dirty,
    _refuse_item_too_large,
    _refuse_unrecorded,
)
from kiro_crew.dashboard.handlers.work_ledger_refusals import (
    _audit,
    _drop_nulls,
    _json_object,
    _refuse_400,
    _refuse_403,
    _refuse_404,
    _refuse_503,
    _refuse_store_error,
    _validation_code,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.metrics import work as work_metrics
from kiro_crew.validation import (
    WORK_REPORT_REMOTE_DISPATCH_SCHEMA,
    WORK_REPORT_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.work_ledger import WorkLedgerError

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


# ── worker half ───────────────────────────────────────────────────────────


async def api_work_brief(request: web.Request) -> web.Response:
    """GET /api/work-ledger/brief — the ONE item this session was dispatched for.

    Title, acceptance, round, the conductor's latest decision, and this worker's
    own last status/summary. Deliberately not the conductor's goal and not a
    sibling's anything: a worker has no reason to see its peers.
    """
    key, refusal = await _caller_key(request, "work_brief")
    if refusal is not None:
        return refusal
    assert key is not None
    binding, brefusal = await _worker_binding(key, "work_brief")
    if brefusal is not None:
        return brefusal
    assert binding is not None
    conductor_key, item_id = binding
    # Under the board lock, like every write: a write commits to the cache and
    # then appends to the record, undoing the commit if the append fails, and a
    # rebuild rewrites the cache in place. A read between those steps would serve
    # a state the record never holds. The lock makes the read see either side.
    async with _board_lock(conductor_key):
        dirty = await _refuse_if_dirty(conductor_key, key, "work_brief")
        if dirty is not None:
            return dirty
        brief = await asyncio.to_thread(work_ledger.read_work_brief, conductor_key, item_id)
    if why := _contained_channel_caller(request, request.headers.get("X-Session-Key", "")):
        # Re-checked AFTER the await, immediately before the data would be
        # returned. An outbound mirror can be added or retargeted at any moment,
        # so containment decided on entry says nothing about containment now — the
        # same reason ``session_control`` applies ``_refuse_ineligible_creator``
        # twice, once on entry and again just before it allocates. A brief carries
        # a private dispatch's acceptance bar, and a mirrored reply publishes it.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            "work_brief",
            "denied",
            resources="channel_agent_block_post_read",
        )
        return _refuse_403(
            "channel_session",
            "This session stopped being a private surface while the brief was being "
            f"read, so it is no longer one to return it to: {why}.",
        )
    if brief is None:
        # The binding names an item that is gone or unreadable. 404 on the ITEM,
        # not 403 on the binding: the caller IS bound, and telling it otherwise
        # would send a worker looking for a grant it already has.
        _audit(key, "work_brief", "denied", resources=item_id, error=work_ledger.CODE_UNKNOWN_ITEM)
        return _refuse_404(
            work_ledger.CODE_UNKNOWN_ITEM,
            f"the item this session is bound to ({item_id}) is not readable",
        )
    _audit(key, "work_brief", "ok", resources=item_id)
    # The call got through, so an earlier undelivered one stops being reported.
    await asyncio.to_thread(work_ledger.clear_undelivered, key)
    return web.json_response({"brief": brief})


#: A route refusal rather than a store code: the store never sees the report.
_CODE_DONE_WITHOUT_EVIDENCE = "done_without_evidence"


async def api_work_report(request: web.Request) -> web.Response:
    """POST /api/work-ledger/report — this worker's status against its own item.

    ``status`` and ``summary`` are required; ``artifacts``, ``pr`` and ``reason``
    are optional. There is no ``item_id``, no ``session``, no ``acceptance``, no
    ``verdict`` and no ``state``, so no round trip through here can write a
    conductor-owned field.
    """
    key, refusal = await _caller_key(request, "work_report")
    if refusal is not None:
        return refusal
    assert key is not None
    binding, brefusal = await _worker_binding(key, "work_report")
    if brefusal is not None:
        return brefusal
    assert binding is not None
    conductor_key, item_id = binding
    dirty = await _refuse_if_dirty(conductor_key, key, "work_report")
    if dirty is not None:
        return dirty

    state: DashboardState = request.app["state"]
    unit, urefusal = _acting_unit(state, key, "work_report", item_id)
    if urefusal is not None:
        return urefusal
    assert unit is not None
    # The closure below reads these; a nested function does not inherit the
    # ``is not None`` narrowing, so it takes them as ``str`` bindings.
    caller: str = key
    acting_unit: str = unit

    body, bad = await _json_object(request)
    if bad is not None:
        return bad
    assert body is not None
    # ``reply`` is a status only under the remote-dispatch preview; with it off the
    # schema, and so the refusal, is the one from before the status existed.
    remote_dispatch = await asyncio.to_thread(work_preview.remote_dispatch)
    schema = WORK_REPORT_REMOTE_DISPATCH_SCHEMA if remote_dispatch else WORK_REPORT_SCHEMA
    try:
        cleaned = validate_tool_args(_drop_nulls(body), schema)
    except ValidationError as exc:
        return _refuse_400(_validation_code(exc), str(exc))
    try:
        cleaned = crew_log_emit.safe_work_fields(cleaned)
    except crew_log_emit.WorkFieldError as exc:
        return _refuse_400(work_ledger.CODE_INVALID_VALUE, str(exc))
    if cleaned.get("status") == "done" and not cleaned.get("artifacts") and not cleaned.get("pr"):
        # A ``done`` names what the conductor should check. One pointer is enough,
        # and a check that could not run is still a pointer when it says so.
        _audit(
            key,
            "work_report",
            "denied",
            resources=item_id,
            error=_CODE_DONE_WITHOUT_EVIDENCE,
        )
        return _refuse_400(
            _CODE_DONE_WITHOUT_EVIDENCE,
            "a 'done' report must carry its evidence in this report: at least one "
            "artifacts pointer (commit, test command and exit code, path) or a pr. "
            "If a check could not run, say so in an artifact, e.g. "
            '{"tests": "not run: harness unavailable"}',
            field="artifacts",
        )

    # The record is refused BEFORE the cache commits: the widest entry this report
    # can produce (the committed `pr` may be an earlier report's) must fit a line.
    # It is built the way the entry is -- over the baseline an unstamped item is
    # owed, with every store-generated field at its widest -- so a write the probe
    # passes cannot then commit and be undone with a 503 where a 400 was promised.
    probe_item, probe_board = await asyncio.to_thread(
        _current_item_and_board, conductor_key, item_id
    )
    probe, invalid = _entry_probe_with_baseline(
        conductor_key,
        probe_item,
        probe_board,
        actor="worker",
        by=caller,
        action="report",
        item_id=item_id,
        generation=_WIDEST_HEX_ID,
        round=_WIDEST_COUNTER,
        status=cleaned.get("status"),
        reason=cleaned.get("reason"),
        summary=cleaned.get("summary"),
        artifacts=cleaned.get("artifacts") or {},
        pr=work_ledger.MAX_PR,
        last_report_at=_WIDEST_STAMP,
        event=_WIDEST_EVENT_TEXT,
        event_kind="report",
        event_id=_WIDEST_HEX_ID,
        event_ts=_WIDEST_STAMP,
        # Both copies' marks, so the probe is as wide as either copy.
        board_copy=True,
        mirror=True,
    )
    if invalid is not None:
        return invalid
    assert probe is not None
    if not crew_log_emit.work_entry_fits(probe):
        if _baseline_overflows(
            conductor_key,
            probe_item,
            probe_board,
            skip=_WORKER_REPORT_FIELDS,
            actor="worker",
            by=caller,
            action="report",
            item_id=item_id,
            generation=_WIDEST_HEX_ID,
            round=_WIDEST_COUNTER,
            pr=work_ledger.MAX_PR,
            last_report_at=_WIDEST_STAMP,
            event=_WIDEST_EVENT_TEXT,
            event_kind="report",
            event_id=_WIDEST_HEX_ID,
            event_ts=_WIDEST_STAMP,
            board_copy=True,
            mirror=True,
        ):
            return _refuse_item_too_large(key, "work_report", item_id)
        _audit(key, "work_report", "denied", resources=item_id, error="work_entry_too_large")
        return _refuse_400(
            "work_entry_too_large",
            "the report's crew-log record does not fit one log line; shorten it",
        )

    async def _under_board() -> web.Response:
        # Re-checked under the lock: a write ahead of this one may have marked the
        # cache dirty (its undo failed) after this one's early check passed, and a
        # commit over that would build on the damage.
        dirty = await _refuse_if_dirty(conductor_key, caller, "work_report")
        if dirty is not None:
            return dirty
        snapshot = await asyncio.to_thread(
            functools.partial(work_ledger.snapshot_for_write, conductor_key, item_id=item_id)
        )

        async def _commit_and_record() -> web.Response:
            try:
                result = await asyncio.to_thread(
                    _report,
                    conductor_key,
                    item_id,
                    cleaned.get("status"),
                    cleaned.get("summary"),
                    cleaned.get("artifacts"),
                    cleaned.get("pr"),
                    cleaned.get("reason"),
                )
            except WorkLedgerError as exc:
                _audit(caller, "work_report", "denied", resources=item_id, error=exc.code)
                return _refuse_store_error(exc)
            except OSError:
                logger.warning("work ledger report failed for %s", item_id, exc_info=True)
                return _refuse_503("ledger_write_failed", "ledger write failed; try again")

            item = result["item"]
            board = await asyncio.to_thread(work_ledger.read_conductor, conductor_key)
            established, board = await _board_header(conductor_key, board)
            if not established:
                return await _refuse_unrecorded(
                    caller, "work_report", item.item_id, conductor_key, snapshot
                )
            # The entry carries what the COMMIT holds, not what the request said: an
            # omitted `artifacts` clears the map, and the fold reads an absent field as
            # unchanged, so only the committed value keeps the rebuild equal to the cache.
            entry = _entry_with_baseline(
                conductor_key,
                item,
                board,
                actor="worker",
                by=caller,
                action="report",
                item_id=item.item_id,
                generation=getattr(board, "generation", None) or None,
                round=item.round,
                status=item.status,
                reason=item.reason,
                summary=item.summary,
                artifacts=item.artifacts,
                pr=item.pr,
                last_report_at=item.last_report_at,
                event=getattr(result.get("event"), "text", None),
                event_kind="report",
                event_id=getattr(result.get("event"), "id", None) or None,
                event_ts=getattr(result.get("event"), "ts", None) or None,
            )
            # The board's own copy, in the CONDUCTOR's log, FIRST: the board's folds read
            # the conductor's units and no worker's, so this is the copy the board is
            # rebuilt from, and the report has happened exactly when it lands. The gateway
            # is that log's one writer, as it is for a bind or a decide.
            parent_unit = await asyncio.to_thread(_conductor_unit, state, conductor_key)
            if not parent_unit:
                # The board has no unit of its own to record into (its conductor's logs
                # are gone). The worker's copy is then the only record, as it always was.
                landed = await asyncio.to_thread(crew_log_emit.on_work_recorded, acting_unit, entry)
            else:
                # Marked as the board's copy: the operator's rebuild also joins worker
                # logs, and the mark is how it tells this authoritative copy from a
                # worker-log one (a mirror, or a report from before board copies).
                board_copy = {**entry, "board_copy": True}
                landed = await asyncio.to_thread(
                    crew_log_emit.on_work_recorded, parent_unit, board_copy
                )
                if not landed:
                    # The live unit may have no log yet (a session just switched agents,
                    # before its first turn); the slot's newest unit that has one takes it.
                    from kiro_crew import session_ledger

                    fallback = await asyncio.to_thread(
                        session_ledger.newest_work_unit, conductor_key
                    )
                    if fallback and fallback != parent_unit:
                        parent_unit = fallback
                        landed = await asyncio.to_thread(
                            crew_log_emit.on_work_recorded, parent_unit, board_copy
                        )
                    elif not fallback:
                        # No unit of the board's own has a log: the worker's copy is the
                        # record, exactly as when no conductor unit is named at all.
                        parent_unit = ""
                        landed = await asyncio.to_thread(
                            crew_log_emit.on_work_recorded, acting_unit, entry
                        )
            if landed and parent_unit and parent_unit != acting_unit:
                # The worker's own copy, marked as a mirror of the board's. Nothing reads
                # it to rebuild the board, so a failure here is logged and the report
                # stands.
                mirrored = await asyncio.to_thread(
                    crew_log_emit.on_work_recorded, acting_unit, {**entry, "mirror": True}
                )
                if not mirrored:
                    logger.warning(
                        "work report for %s landed on the board; the worker's copy did not",
                        item.item_id,
                    )
            if not landed:
                return await _refuse_unrecorded(
                    caller, "work_report", item.item_id, conductor_key, snapshot
                )
            await asyncio.to_thread(_mark_recorded, conductor_key, item.item_id)
            # Counted from the COMMITTED item, after its record landed: a refused
            # report -- an unknown or closed item, a ``done`` with no evidence, a
            # status outside the worker's vocabulary -- returned long before here.
            work_metrics.record_report(item.status, item.reason)
            from kiro_crew.metrics import crew_mode as crew_mode_metrics

            if crew_mode_metrics.board_is_crew_mode(state, conductor_key):
                crew_mode_metrics.record_worker_report(item.status, item.reason)
            crew_store = _crew_store(conductor_key)
            if crew_store and item.status:
                # The answer to the dispatch, in the crew's own log: the emitter
                # threads it onto that dispatch's seq and builds the required
                # ``ref`` from this worker's unit, so the claim carries its
                # evidence. An anchor it cannot resolve lands the report
                # unthreaded rather than dropping it, so a dispatch entry that
                # never got written cannot silence this item's whole history.
                # Written after the work entry landed, best-effort.
                await asyncio.to_thread(
                    crew_log_emit.on_crew_report,
                    crew_store,
                    {
                        "item": item.item_id,
                        "status": item.status,
                        **({"summary": item.summary} if item.summary else {}),
                    },
                    cite_unit=acting_unit,
                )
            _audit(caller, "work_report", "ok", resources=f"{item_id} status={item.status}")
            await asyncio.to_thread(work_ledger.clear_undelivered, caller)
            return web.json_response(
                {"ok": True, "item_id": item.item_id, "status": item.status, "round": item.round}
            )

        # Shielding alone detaches this transaction when the handler is cancelled.
        # Drain it instead: after the cache commit, either the crew-log append lands
        # or the snapshot is restored before the cancellation is re-raised.
        return await _drain_before_cancelling(_commit_and_record())

    async with _board_lock(conductor_key):
        return await _under_board()


#: The fields a worker's report sets; removed from a baseline to ask whether the
#: rest -- what the worker cannot change -- is what fails to fit.
_WORKER_REPORT_FIELDS: tuple[str, ...] = (
    "status",
    "reason",
    "summary",
    "artifacts",
    "pr",
    "round",
    "last_report_at",
)


def _report(
    conductor_key: str,
    item_id: str,
    status: Any,
    summary: Any,
    artifacts: Any,
    pr: Any,
    reason: Any = None,
) -> dict[str, Any]:
    return work_ledger.apply_worker_report(
        conductor_key,
        item_id,
        status=status,
        summary=summary,
        artifacts=artifacts,
        pr=pr,
        reason=reason,
    )


# ── the person's steer ────────────────────────────────────────────────────


#: Why a person's steer wrote no ledger event (the route's ``ledger_error``).
STEER_CREW_LOG_OFF = "crew_log_off"
STEER_NO_UNIT = "crew_log_unit_unknown"
STEER_NOT_RECORDED = "not_recorded"
#: The worker's item is already accepted or closed: nothing to record.
STEER_ITEM_CLOSED = "item_closed"
#: No such item on that ledger, or the item is one the record cannot hold whole.
STEER_NO_ITEM = "no_item"
STEER_ITEM_TOO_LARGE = "work_item_too_large"


def _steer_unit(state: Any, keys: tuple[str, ...], board_key: str) -> str:
    """The crew-log unit a person's steer appends to, or ``""``. BLOCKING (a listing).

    The live unit of the first of *keys* that has one (the steered worker first, the
    way a worker's own report records into the worker's unit), then the board's
    live unit, then the board's newest recorded work unit: a root with no live
    session (reset by the migration, reaped while idle, before its first turn after
    a restart) still holds its ledger, and the fold reads every unit recorded
    under the board, so the steer lands where the conductor will read it.
    """
    from kiro_crew import session_ledger
    from kiro_crew.crew_log.resolve import UNKNOWN, unit_for_session_key

    sessions = getattr(state, "sessions", None)
    for key in (*keys, board_key):
        unit = unit_for_session_key(sessions, key)
        if unit and unit != UNKNOWN:
            return unit
    try:
        recorded = session_ledger.work_crew_log_units(board_key)
    except Exception:
        recorded = ()
    return recorded[-1] if recorded else ""


async def record_person_steer_result(
    state: Any,
    board_key: str,
    item_id: str,
    text: str,
    *,
    unit_keys: tuple[str, ...] = (),
) -> tuple[str, str]:
    """One ``steer`` event on *item_id* of *board_key*'s ledger, as the person wrote it.

    The one person-steer transaction: the Crew Mode worker steer route and the
    worker's own composer (``POST /api/chat`` on a worker) both call it, through
    ``crew_workers.person_steer``, after the text was delivered. Identity is
    server-resolved there: *board_key* is the ledger that holds the worker's item
    (its root's, or a sub-conductor's under that root), and *item_id* is that item.
    *text* is already redacted; the store caps it. *unit_keys* are the sessions whose
    live crew-log unit is preferred (the worker); see :func:`_steer_unit`.

    The write takes the board lock, commits the event (no item field moves),
    appends one ``work/recorded`` entry (actor ``person``, action ``steer``,
    ``by`` the board), and undoes the event when that entry does not land, as every
    ledger write does. Returns ``(item id, "")`` or ``("", why)``.
    """
    if not crew_log_emit.enabled():
        _audit(board_key, "crew_mode.steer", "denied", resources=item_id, error="crew_log_off")
        return "", STEER_CREW_LOG_OFF
    unit = await asyncio.to_thread(_steer_unit, state, unit_keys, board_key)
    if not unit:
        _audit(board_key, "crew_mode.steer", "denied", resources=item_id, error=STEER_NO_UNIT)
        return "", STEER_NO_UNIT
    async with _board_lock(board_key):
        if await _refuse_if_dirty(board_key, board_key, "crew_mode.steer") is not None:
            return "", STEER_NOT_RECORDED
        # Refused BEFORE the cache commits, as every ledger write is: the widest
        # entry this steer can produce (the text at its cap, over the baseline an
        # unstamped item is owed) must fit one crew-log line, or the event would be
        # committed and then undone where a plain refusal was owed.
        probe_item, probe_board = await asyncio.to_thread(
            _current_item_and_board, board_key, item_id
        )
        if probe_item is None:
            _audit(board_key, "crew_mode.steer", "denied", resources=item_id, error=STEER_NO_ITEM)
            return "", STEER_NO_ITEM
        probe, invalid = _entry_probe_with_baseline(
            board_key,
            probe_item,
            probe_board,
            actor="person",
            by=board_key,
            action="steer",
            item_id=item_id,
            generation=_WIDEST_HEX_ID,
            event=_WIDEST_EVENT_TEXT,
            event_kind="steer",
            event_id=_WIDEST_HEX_ID,
            event_ts=_WIDEST_STAMP,
        )
        if invalid is not None or probe is None or not crew_log_emit.work_entry_fits(probe):
            _audit(
                board_key,
                "crew_mode.steer",
                "denied",
                resources=item_id,
                error=STEER_ITEM_TOO_LARGE,
            )
            return "", STEER_ITEM_TOO_LARGE
        snapshot = await asyncio.to_thread(
            functools.partial(work_ledger.snapshot_for_write, board_key, item_id=item_id)
        )
        try:
            result = await asyncio.to_thread(
                work_ledger.apply_person_steer, board_key, item_id, text
            )
        except WorkLedgerError:
            _audit(board_key, "crew_mode.steer", "denied", resources=item_id, error=STEER_NO_ITEM)
            return "", STEER_NO_ITEM
        if result is None:
            return "", STEER_ITEM_CLOSED
        item, event = result["item"], result["event"]
        board = await asyncio.to_thread(work_ledger.read_conductor, board_key)
        established, board = await _board_header(board_key, board)
        landed = established and await asyncio.to_thread(
            crew_log_emit.on_work_recorded,
            unit,
            _entry_with_baseline(
                board_key,
                item,
                board,
                actor="person",
                by=board_key,
                action="steer",
                item_id=item.item_id,
                generation=getattr(board, "generation", None) or None,
                event=event.text,
                event_kind="steer",
                event_id=event.id or None,
                event_ts=event.ts or None,
            ),
        )
        if not landed:
            await _refuse_unrecorded(
                board_key, "crew_mode.steer", item.item_id, board_key, snapshot
            )
            return "", STEER_NOT_RECORDED
        await asyncio.to_thread(_mark_recorded, board_key, item.item_id)
        _audit(board_key, "crew_mode.steer", "ok", resources=item.item_id)
    return item.item_id, ""

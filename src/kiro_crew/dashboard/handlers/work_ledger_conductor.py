"""The conductor's writes: ``POST /api/work-ledger/record`` and ``/rebuild``.

The one probe, commit and record path every conductor action takes, the ledger it
bootstraps, the store call each action routes to, the counter a committed write is
owed, the ownership checks a ``bind`` must pass, the patrol a bind arms, the rebuild
from the crew log, and whether a session holds a ledger at all.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from kiro_crew import conductor_patrol, session_ledger, work_ledger
from kiro_crew.constants import env_file_display
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.dashboard import session_control
from kiro_crew.dashboard.handlers.work_ledger_caller import _caller_key, _own_ledger
from kiro_crew.dashboard.handlers.work_ledger_read import worker_holds_open_item
from kiro_crew.dashboard.handlers.work_ledger_recording import (
    _WIDEST_COUNTER,
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_ITEM_ID,
    _WIDEST_STAMP,
    _acting_unit,
    _baseline_overflows,
    _board_header,
    _board_lock,
    _crew_store,
    _current_item_and_board,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _entry_with_baseline,
    _mark_goal_recorded,
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
    _refuse_409,
    _refuse_503,
    _refuse_store_error,
    _validation_code,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.metrics import work as work_metrics
from kiro_crew.validation import WORK_LEDGER_RECORD_SCHEMA, ValidationError, validate_tool_args
from kiro_crew.work_ledger import WorkLedgerError
from kiro_crew.work_vocab import WORK_CONDUCTOR_FIELDS, WORK_MAX_DEPTH_MAX

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


#: The actions ``work_ledger_record`` accepts. A SUPERSET of the store's
#: :data:`work_ledger.CONDUCTOR_ACTIONS`: ``accept`` promotes a worker's claimed
#: ``pr`` into the item's ``acceptance`` and is served by
#: :func:`work_ledger.apply_acceptance_update`, which is deliberately not a
#: seventh member of that frozenset (see its docstring).
RECORD_ACTIONS: frozenset[str] = work_ledger.CONDUCTOR_ACTIONS | {"accept"}


def _count_recorded(action: str, item: Any, header: Any) -> None:
    """The ``kirocrew.work.*`` counter one committed conductor write is owed.

    Four of the six actions carry a number worth counting; ``decide`` and
    ``verdict`` carry none of their own -- a decision's text and a verdict's
    ruling are already visible in the close they lead to, and an item's fail
    count is read off the close rather than off each verdict that raised it.

    Every value comes from the COMMITTED record the caller holds, never from the
    request, so a field the store defaulted or clamped is counted as the store
    holds it. ``goal`` counts only the write that OPENED the board: the committed
    header's ``goal_version`` is 1 on exactly that write and higher on every
    reword, round bump, cap raise and ``new_goal`` after it.
    """
    depth = getattr(header, "depth", None)
    if action == "goal":
        if getattr(header, "goal_version", 0) == 1:
            work_metrics.record_goal_created(depth)
    elif action == "create":
        work_metrics.record_item_created(depth)
    elif action == "close":
        work_metrics.record_item_closed(
            getattr(item, "state", None),
            getattr(item, "fails", None),
            getattr(item, "created_at", None),
            getattr(item, "closed_at", None),
        )
    elif action == "bind":
        # ``session`` is the kind the dispatch this bind records always names; see
        # the ``crew_log_emit.on_crew_dispatch`` call below, which builds the
        # target object, and ``metrics/work.py::record_dispatch``.
        work_metrics.record_dispatch("session")


async def _count_crew_mode_run(state: DashboardState, key: str, item: Any) -> None:
    """A Crew Mode chat's run finished: count how it ended and the steers it took.

    Only for a board a Crew Mode root owns (``metrics.crew_mode.board_is_crew_mode``);
    the steer count is the item's own ``steer`` events. Never raises.
    """
    from kiro_crew.metrics import crew_mode as crew_mode_metrics

    if not crew_mode_metrics.board_is_crew_mode(state, key):
        return
    try:
        events = await asyncio.to_thread(work_ledger.read_events, key, item.item_id)
        steers = sum(1 for event in events if getattr(event, "kind", "") == "steer")
    except Exception:
        steers = None
    crew_mode_metrics.record_run_finished(getattr(item, "state", None), steers)


async def _record_conductor_write(
    state: DashboardState,
    key: str,
    unit: str,
    action: str,
    cleaned: dict[str, Any],
    *,
    operation: str,
    before_write: Callable[[], Awaitable[web.Response | None]] | None = None,
    dispatch: bool = False,
) -> web.Response:
    """Probe, commit and record one conductor-owned write on *key*'s board.

    The one commit-and-record path for every conductor action: the record route
    and the ``session_send`` hand-back (:func:`stamp_send_handback`) both write
    through it. *cleaned* is already validated and redacted. *operation* names
    the write in the audit trail. *before_write*, when given, runs under the
    board lock before the snapshot; a response from it stops the write and is
    returned as is. *dispatch* marks a ``work_dispatch`` create: it never opens a
    ledger, so it is judged under the board lock like every write on an existing
    one, and refused ``no_ledger`` when the caller owns none.
    """
    # Refused BEFORE the ledger is even bootstrapped: the widest entry this action
    # can produce,
    # with the store-generated fields at their widest, must fit one log line. The
    # request's acceptance IS the committed one (the store keeps it verbatim).
    probe_item, probe_header = await asyncio.to_thread(
        _current_item_and_board, key, cleaned.get("item_id")
    )
    # ``close`` logs the item's decision whether or not this request set one, so
    # the probe carries the committed value when the request carries none.
    probe_decision = cleaned.get("decision")
    if probe_decision is None and "decision" in WORK_CONDUCTOR_FIELDS.get(action, ()):
        probe_decision = getattr(probe_item, "decision", None)
    probe, invalid = _entry_probe_with_baseline(
        key,
        probe_item if action != "create" else None,
        probe_header,
        actor="conductor",
        by=key,
        action=action,
        item_id=cleaned.get("item_id") or _WIDEST_ITEM_ID,
        generation=_WIDEST_HEX_ID,
        goal=cleaned.get("goal"),
        round=_WIDEST_COUNTER,
        depth=WORK_MAX_DEPTH_MAX,
        parent_item=_WIDEST_ITEM_ID,
        title=cleaned.get("title"),
        acceptance=cleaned.get("acceptance"),
        worker_session_key=cleaned.get("worker_session_key"),
        worker_instance_id=cleaned.get("worker_instance_id"),
        decision=probe_decision,
        verdict=cleaned.get("verdict"),
        state="abandoned",
        fails=_WIDEST_COUNTER,
        event=_WIDEST_EVENT_TEXT,
        event_kind="decision",
        event_id=_WIDEST_HEX_ID,
        event_ts=_WIDEST_STAMP,
        created_at=_WIDEST_STAMP,
        closed_at=_WIDEST_STAMP,
        decided_at=_WIDEST_STAMP,
        pending_since=_WIDEST_STAMP,
    )
    if invalid is not None:
        return invalid
    assert probe is not None
    if not crew_log_emit.work_entry_fits(probe):
        if action != "create" and _baseline_overflows(
            key,
            probe_item,
            probe_header,
            skip=WORK_CONDUCTOR_FIELDS.get(action, ()),
            actor="conductor",
            by=key,
            action=action,
            item_id=cleaned.get("item_id") or _WIDEST_ITEM_ID,
            generation=_WIDEST_HEX_ID,
            round=_WIDEST_COUNTER,
            depth=WORK_MAX_DEPTH_MAX,
            parent_item=_WIDEST_ITEM_ID,
            fails=_WIDEST_COUNTER,
            event=_WIDEST_EVENT_TEXT,
            event_kind="decision",
            event_id=_WIDEST_HEX_ID,
            event_ts=_WIDEST_STAMP,
            created_at=_WIDEST_STAMP,
            closed_at=_WIDEST_STAMP,
            decided_at=_WIDEST_STAMP,
            pending_since=_WIDEST_STAMP,
        ):
            return _refuse_item_too_large(key, operation, str(cleaned.get("item_id")))
        _audit(key, operation, "denied", resources=action, error="work_entry_too_large")
        return _refuse_400(
            "work_entry_too_large",
            "the action's crew-log record does not fit one log line; shorten it",
        )

    async def _under_board() -> web.Response:
        # Re-checked under the lock, for the same reason the report route does.
        dirty = await _refuse_if_dirty(key, key, operation)
        if dirty is not None:
            return dirty
        if before_write is not None:
            stop = await before_write()
            if stop is not None:
                return stop
        # The worker key is folded through ``session_ledger.ledger_key`` before the
        # store writes the binding (see the ``handlers.work_ledger`` note); the snapshot names the
        # same path, or a failed bind's binding would escape the undo.
        raw_worker = cleaned.get("worker_session_key")
        folded_worker = (
            session_ledger.ledger_key(raw_worker)
            if isinstance(raw_worker, str) and raw_worker and not cleaned.get("worker_instance_id")
            else None
        )
        snapshot = await asyncio.to_thread(
            functools.partial(
                work_ledger.snapshot_for_write,
                key,
                item_id=cleaned.get("item_id"),
                worker_session_key=folded_worker,
            )
        )
        if dispatch:
            # Under the lock, so a purge cannot land between this read and the
            # write; the store's own header check refuses one that lands later.
            drefusal = await _refuse_dispatch_without_ledger(key)
            if drefusal is not None:
                return drefusal
        elif action in ("goal", "create"):
            bootstrapped = await _bootstrap(key)
            if isinstance(bootstrapped, web.Response):
                return bootstrapped
        else:
            _record, lrefusal = await _own_ledger(key, operation)
            if lrefusal is not None:
                return lrefusal

        try:
            result = await asyncio.to_thread(_write, key, action, cleaned)
        except WorkLedgerError as exc:
            _audit(key, operation, "denied", resources=action, error=exc.code)
            return _refuse_store_error(exc)
        except OSError:
            logger.warning("work ledger write failed for %s", key, exc_info=True)
            return _refuse_503("ledger_write_failed", "ledger write failed; try again")

        item = result.get("item")
        header = result.get("conductor")
        event = result.get("event")
        established, header = await _board_header(key, header)
        if not established:
            return await _refuse_unrecorded(
                key,
                operation,
                getattr(item, "item_id", None),
                key,
                snapshot,
                created=getattr(item, "item_id", None) if action == "create" else None,
                worker_session_key=folded_worker,
            )
        holder = item if item is not None else header
        # Each field comes from the COMMITTED record (item or header), never from the
        # request, so the fold rebuilds what the cache holds; each action logs the
        # fields it sets -- the same table the fold applies (`WORK_CONDUCTOR_FIELDS`).
        sets = WORK_CONDUCTOR_FIELDS.get(action, ())
        landed = await asyncio.to_thread(
            crew_log_emit.on_work_recorded,
            unit,
            _entry_with_baseline(
                key,
                item if action != "create" else None,
                header,
                actor="conductor",
                by=key,
                action=action,
                item_id=getattr(item, "item_id", None),
                generation=getattr(header, "generation", None) or None,
                goal=getattr(header, "goal", None) if action == "goal" else None,
                round=(
                    getattr(holder, "round", None) if action == "goal" or "round" in sets else None
                ),
                goal_version=getattr(header, "goal_version", None) if action == "goal" else None,
                # Lineage is board identity, not an action's delta: every conductor
                # entry carries it, so a board whose goal was never recorded, or
                # whose header is lost, still rebuilds at its depth.
                depth=getattr(header, "depth", None),
                parent_item=getattr(header, "parent_item", None),
                title=getattr(item, "title", None) if "title" in sets else None,
                acceptance=getattr(item, "acceptance", None) if "acceptance" in sets else None,
                worker_session_key=(
                    getattr(item, "worker_session_key", None)
                    if "worker_session_key" in sets
                    else None
                ),
                worker_instance_id=(
                    getattr(item, "worker_instance_id", None)
                    if "worker_instance_id" in sets
                    else None
                ),
                decision=getattr(item, "decision", None) if "decision" in sets else None,
                verdict=getattr(item, "verdict", None) if "verdict" in sets else None,
                state=getattr(item, "state", None) if "state" in sets else None,
                fails=getattr(item, "fails", None) if "fails" in sets else None,
                event=getattr(event, "text", None),
                event_kind=getattr(event, "kind", None),
                event_id=getattr(event, "id", None) or None,
                event_ts=getattr(event, "ts", None) or None,
                created_at=getattr(item, "created_at", None) if action == "create" else None,
                closed_at=getattr(item, "closed_at", None) if action == "close" else None,
                decided_at=getattr(item, "decided_at", None) if action == "decide" else None,
                pending_since=(
                    getattr(item, "pending_since", None) if action == "verdict" else None
                ),
            ),
        )
        if not landed:
            return await _refuse_unrecorded(
                key,
                operation,
                getattr(item, "item_id", None),
                key,
                snapshot,
                created=getattr(item, "item_id", None) if action == "create" else None,
                worker_session_key=folded_worker,
            )
        if item is not None:
            await asyncio.to_thread(_mark_recorded, key, item.item_id)
        if action == "close" and item is not None and item.worker_session_key:
            # A closed item reads no undelivered flag, so its record is dead. Dropped
            # only now the close is recorded: an unrecorded close is undone above and
            # its item, still open, keeps its flag.
            await asyncio.to_thread(
                work_ledger.forget_undelivered_for_item,
                item.worker_session_key,
                key,
                item.item_id,
            )
        if action == "goal":
            await asyncio.to_thread(_mark_goal_recorded, key)
        # Counted here and not a line earlier: every refusal above returns, and a
        # write whose record did not land is undone above, so this point is
        # reached once per write the route reports as successful.
        _count_recorded(action, item, header)
        if action == "close" and item is not None:
            await _count_crew_mode_run(state, key, item)
        # A remote bind names a PEER's slot, which no local crew record can resolve.
        remote = bool(getattr(item, "worker_instance_id", None))
        crew_store = _crew_store(key) if action == "bind" and not remote else ""
        worker_slot = getattr(item, "worker_session_key", "") or ""
        if item is not None and crew_store and worker_slot:
            # The crew-side record of the fact the entry above recorded: a dispatch
            # is a crew handing one item to a target, and ``bind`` is the action
            # that names the target. Written AFTER the work entry landed, so the
            # crew's log cannot claim a dispatch the board does not hold, and
            # best-effort, so a crew log that cannot be written does not fail a
            # ledger write that succeeded.
            await asyncio.to_thread(
                crew_log_emit.on_crew_dispatch,
                crew_store,
                {"item": item.item_id, "target": {"kind": "session", "slot": worker_slot}},
            )
        _audit(
            key,
            operation,
            "ok",
            resources=f"{action} {getattr(item, 'item_id', '') or ''}".strip(),
        )
        payload: dict[str, Any] = {"ok": True, "action": action}
        if item is not None:
            payload["item"] = item.to_dict()
        if action == "bind":
            # A dispatched worker must have a patrol reading its reports. After the
            # bind committed, arm the default one when this conductor has no
            # active loop; never raises, never displaces an active loop or a
            # person's stop, and a refusal does not undo the bind
            # (``conductor_patrol`` module docstring).
            patrol, reason = await conductor_patrol.ensure_patrol(state, key)
            payload["patrol"] = patrol
            payload["patrol_reason"] = reason
            note = conductor_patrol.patrol_note(state, key, reason)
            if note:
                payload["patrol_note"] = note
        if action == "close" and item is not None:
            warning = _close_warning(state, item.worker_session_key or "")
            if warning:
                payload["warning"] = warning
        conductor = result.get("conductor")
        if conductor is not None:
            payload["conductor"] = conductor.to_dict()
        return web.json_response(payload)

    # Shielding alone detaches this transaction when the handler is cancelled.
    # Drain it instead, as the report route does: the cache commit and the
    # crew-log append both finish -- or the snapshot is restored -- before the
    # cancellation is re-raised and the board lock is released. Without this a
    # cancelled request leaves a committed cache write the log never saw, and
    # the lock goes back with that divergence visible to the next reader.
    async with _board_lock(key):
        return await _drain_before_cancelling(_under_board())


async def api_work_ledger_record(request: web.Request) -> web.Response:
    """POST /api/work-ledger/record — one conductor-owned write.

    An ``action`` selects the operation because the field sets are disjoint and
    one flat schema would accept nonsense combinations. ``goal`` and ``create``
    also BOOTSTRAP the ledger, which is the only way one comes into existence;
    every other action answers ``no_ledger`` until then, so a conductor cannot
    bind or decide against a ledger that was never opened.
    """
    key, refusal = await _caller_key(request, "work_ledger_record")
    if refusal is not None:
        return refusal
    assert key is not None

    body, bad = await _json_object(request)
    if bad is not None:
        return bad
    assert body is not None
    # ``work_dispatch`` marks its create; the field never reaches the schema, which
    # is also the ``work_ledger_record`` tool's input schema. The mark can only add
    # a refusal, so ANY presence of the key counts as one (rfc-work-dispatch.md,
    # record-route amendment).
    dispatch = DISPATCH_FIELD in body
    body = {k: v for k, v in body.items() if k != DISPATCH_FIELD}
    try:
        cleaned = validate_tool_args(_drop_nulls(body), WORK_LEDGER_RECORD_SCHEMA)
    except ValidationError as exc:
        return _refuse_400(_validation_code(exc), str(exc))
    try:
        cleaned = crew_log_emit.safe_work_fields(cleaned)
    except crew_log_emit.WorkFieldError as exc:
        return _refuse_400(work_ledger.CODE_INVALID_VALUE, str(exc))

    action = str(cleaned.get("action") or "")
    if action not in RECORD_ACTIONS:
        return _refuse_400(
            work_ledger.CODE_INVALID_ACTION,
            f"unknown action {action!r}; expected one of: {', '.join(sorted(RECORD_ACTIONS))}",
        )

    state: DashboardState = request.app["state"]
    unit, urefusal = _acting_unit(state, key, "work_ledger_record", action)
    if urefusal is not None:
        return urefusal
    dirty = await _refuse_if_dirty(key, key, "work_ledger_record")
    if dirty is not None:
        return dirty
    assert unit is not None

    if action == "bind":
        refusal = _refuse_unowned_worker(request, key, cleaned.get("worker_session_key"))
        if refusal is None:
            refusal = await _refuse_dispatched_worker(key, cleaned.get("worker_session_key"))
        if refusal is not None:
            return refusal

    # A dispatch never opens a ledger: only ``kirocrew-work``'s own
    # ``work_ledger_record`` does, so a session without that server is refused,
    # whatever its backend.
    resp = await _record_conductor_write(
        state, key, unit, action, cleaned, operation="work_ledger_record", dispatch=dispatch
    )
    if action == "decide" and resp.status == 200:
        resp = await _forward_remote_decide(state, key, cleaned, resp)
    return resp


async def _forward_remote_decide(
    state: DashboardState, key: str, cleaned: dict[str, Any], resp: web.Response
) -> web.Response:
    """A recorded ``decide`` on a REMOTE item also reaches its peer copy (RFC §2).

    Only with ``work_ledger.remote_dispatch`` on and for an item bound to a crew;
    every other decide returns *resp* untouched. The PATCH runs after the board
    lock is released. Its outcome is added to the reply as ``remote_decide``; a
    failure leaves the local write standing, and the next decide carries the
    decision again.
    """
    import json

    from kiro_crew.dashboard.handlers.work_ledger_read import _remote_dispatch_on

    item_id = str(cleaned.get("item_id") or "")
    if not await asyncio.to_thread(_remote_dispatch_on):
        return resp
    from kiro_crew.dashboard.handlers import remote_ingest

    item = await remote_ingest.settled_item(key, item_id)
    if item is None or not item.worker_instance_id:
        return resp

    outcome = await remote_ingest.forward_decide(state, key, item_id)
    _audit(
        key,
        "work_ledger_record.remote_decide",
        "ok" if outcome.get("ok") else "failure",
        resources=item_id,
        error=str(outcome.get("code") or ""),
    )
    try:
        payload = json.loads(resp.body) if isinstance(resp.body, bytes | str) else None
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return resp
    payload["remote_decide"] = outcome
    # Reached only for a recorded decide, whose reply is a 200.
    return web.json_response(payload)


#: The body field ``work_dispatch`` marks its step-1 create with.
DISPATCH_FIELD = "dispatch"


async def _refuse_dispatch_without_ledger(key: str) -> web.Response | None:
    """``no_ledger`` when *key* owns no ledger, else ``None``.

    The one remedy is ``action=goal``: unlike ``_own_ledger``'s text, it does not
    suggest ``action=create``, which ``work_dispatch`` would send marked again.
    """
    record = await asyncio.to_thread(work_ledger.read_conductor, key)
    if record is not None:
        return None
    _audit(key, "work_dispatch", "denied", error=work_ledger.CODE_NO_LEDGER)
    return _refuse_404(
        work_ledger.CODE_NO_LEDGER,
        "work_dispatch never opens a work ledger, and this session owns none. Open "
        "it with work_ledger_record action=goal, then dispatch again. Nothing was "
        "created.",
    )


async def holds_ledger(session_key: str) -> bool:
    """Whether *session_key* holds a readable work ledger.

    For the session-control create route, which arms a work-ledger patrol only
    on a caller that has a ledger to watch. The key is the caller's own,
    resolved from its ``X-Session-Key`` by that route; this answers a yes/no
    and returns nothing from the record.
    """
    try:
        return await asyncio.to_thread(work_ledger.read_conductor, session_key) is not None
    except Exception:  # noqa: BLE001 - an unreadable ledger is no ledger to watch
        return False


async def api_work_ledger_rebuild(request: web.Request) -> web.Response:
    """POST /api/work-ledger/rebuild -- re-materialise this conductor's board.

    The cache under ``work-ledger/<conductor>/`` is rewritten from the crew log's
    ``work`` fold over this session's own slot, so a board whose files were lost
    or damaged comes back from the record. The caller can only rebuild its own
    board: the slot is the caller's key, never a parameter.
    """
    key, refusal = await _caller_key(request, "work_ledger_rebuild")
    if refusal is not None:
        return refusal
    assert key is not None
    if not crew_log_emit.enabled():
        _audit(key, "work_ledger_rebuild", "denied", error="crew_log_off")
        return _refuse_409(
            "crew_log_off",
            "the crew log is off, so there is no record to rebuild from; "
            f"{crew_log_emit.CREW_LOG_ENV} is set to 0, false, no, off or an unrecognised value; "
            f"unset it (or remove it from {env_file_display()}) and restart the gateway",
        )
    try:
        async with _board_lock(key):
            # Drained for the same reason as a write: a rebuild re-materialises
            # every file, so a cancellation that released the board lock mid-way
            # would publish a board half old and half rebuilt.
            counts = await _drain_before_cancelling(
                asyncio.to_thread(work_ledger.rebuild_from_projection, key)
            )
    except CrewLogError as exc:
        _audit(key, "work_ledger_rebuild", "denied", error="crew_log_unreadable")
        return _refuse_409("crew_log_unreadable", f"the crew log could not be read: {exc}")
    except WorkLedgerError as exc:
        _audit(key, "work_ledger_rebuild", "denied", error=exc.code)
        return _refuse_store_error(exc)
    except OSError:
        logger.warning("work ledger rebuild failed for %s", key, exc_info=True)
        return _refuse_503("ledger_write_failed", "ledger write failed; try again")
    _audit(key, "work_ledger_rebuild", "ok", resources=f"items={counts['items']}")
    return web.json_response({"ok": True, **counts})


def _worker_slot(state: DashboardState, worker_session_key: str) -> Any:
    """The open slot for *worker_session_key* under any spelling it is filed as."""
    folded = session_ledger.ledger_key(worker_session_key)
    for candidate in dict.fromkeys((worker_session_key, folded, f"dashboard_{folded}")):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot-table read must not 500
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            slot = None
        if slot is not None:
            return slot
    return None


def _close_warning(state: DashboardState, worker_session_key: str) -> str:
    """The note a ``close`` reply carries when its worker session is still open.

    Closing the item cuts that worker's only upward channel: its ``work_report``
    is refused ``item_closed``, and a session cannot ``session_send`` to the
    session that created it. Said at close time because nothing else tells the
    conductor, and a worker cut off this way goes silent rather than failing loudly.
    """
    if not worker_session_key or _worker_slot(state, worker_session_key) is None:
        return ""
    return (
        f"Worker session {worker_session_key} is still open, and closing its item cut "
        "its report channel: its work_report is now refused item_closed. If it still "
        "has work, create a new item and bind it to that same session; otherwise "
        "session_close it."
    )


def _refuse_unowned_worker(
    request: web.Request, conductor_key: str, worker_session_key: Any
) -> web.Response | None:
    """Refuse a ``bind`` whose worker session this conductor does not own.

    The store checks only that the ITEM is unbound and that the worker does not
    already hold an open item — neither of which says the worker is *this*
    conductor's. Without this, conductor A can bind conductor B's idle worker
    session to A's item, and B's worker then reads A's brief and A's ``decision``
    field, which is the one field a worker treats as an instruction. That is
    cross-session control, and it is the same class the strict identity resolver
    exists to prevent from the other direction.

    Ownership is ``session_create``'s own attribution: it stamps ``_created_by``
    with the calling session's key inside the synchronous window after the mint,
    and it is the only entry point that does — a person's own tab stays
    unattributed. So a bind is admitted only for a live slot this conductor
    created, in the conductor's own workspace (the memory boundary
    ``authorize_target`` already refuses across).

    Returns a refusal, or ``None`` when the bind may proceed.
    """
    if not isinstance(worker_session_key, str) or not worker_session_key:
        return None  # the store's own invalid_value refusal is the better message
    state: DashboardState = request.app["state"]
    slot = _worker_slot(state, worker_session_key)
    if slot is None:
        _audit(
            conductor_key,
            "work_ledger_record",
            "denied",
            resources="bind",
            error="unknown_worker_session",
        )
        return _refuse_404(
            "unknown_worker_session",
            "That worker session is not open, so it cannot be bound. Create the "
            "session first and bind the key session_create returned — binding "
            "before the session exists is what leaves a worker running unbound.",
        )
    # ``session_create`` stamps ``_created_by`` with the creator's SLOT key, while
    # this route addresses the conductor by its SESSION key. For a dashboard
    # session both fold to one spelling (``chat-X``), but a channel-born conductor
    # -- an owner's own DM -- is keyed ``discord:…:genN`` while its slot is that key
    # folded to the filename charset, so a string comparison would refuse every
    # worker such a conductor ever created. Resolve the conductor's slot the way
    # session control identifies every caller (``caller_slot_key``), and fall back
    # to the ledger spelling only when no open slot answers to the key, which is
    # what a dashboard conductor without a live tab has always compared as.
    conductor_slot_key = session_control.caller_slot_key(state, conductor_key) or conductor_key
    creator = session_ledger.ledger_key(str(getattr(slot, "_created_by", "") or ""))
    if creator != session_ledger.ledger_key(conductor_slot_key):
        _audit(
            conductor_key,
            "work_ledger_record",
            "denied",
            resources="bind",
            error="worker_not_owned",
        )
        return _refuse_403(
            "worker_not_owned",
            "That worker session was not created by this conductor, so it cannot "
            "be bound to one of its items. A conductor binds only the sessions it "
            "dispatched.",
        )
    conductor_slot = None
    for candidate in (conductor_slot_key, conductor_key, f"dashboard_{conductor_key}"):
        try:
            conductor_slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover
            conductor_slot = None
        if conductor_slot is not None:
            break
    if conductor_slot is not None:
        mine = str(getattr(conductor_slot, "workspace", "default") or "default")
        theirs = str(getattr(slot, "workspace", "default") or "default")
        if mine != theirs:
            _audit(
                conductor_key,
                "work_ledger_record",
                "denied",
                resources="bind",
                error="worker_cross_workspace",
            )
            return _refuse_403(
                "worker_cross_workspace",
                "That worker session is in a different workspace. Workspace is the "
                "memory boundary, and a conductor cannot bind across it.",
            )
    return None


async def _refuse_dispatched_worker(
    conductor_key: str, worker_session_key: Any
) -> web.Response | None:
    """Refuse a ``bind`` whose worker session is still bound to an OPEN item.

    Asked after :func:`_refuse_unowned_worker`, so another conductor's session is
    refused as not-owned whatever its binding says. The binding and item reads run
    in a worker thread: they are store reads, and on the loop thread the store's
    retry on a transient read error is skipped, so a valid rebind would be refused.
    """
    if not isinstance(worker_session_key, str) or not worker_session_key:
        return None
    folded = session_ledger.ledger_key(worker_session_key)
    if await asyncio.to_thread(worker_holds_open_item, folded):
        # ONE OPEN item per worker session. A worker has a single binding file, so a
        # second bind would repoint its only report channel away from an item that
        # is still waiting on it. Once that item is terminal its channel is already
        # dead (reports are refused ``item_closed``), so a rebind loses nothing and
        # is the only way to give a still-running worker its channel back. The
        # predicate is the one slot retention asks, and it fails closed: an
        # unreadable binding or item counts as open. The store re-checks under the
        # worker's binding lock.
        #
        # A report the worker composed before the rebind lands on the new item, because a report
        # resolves its binding when it lands; that is the point of a rebind -- the
        # conductor is handing this worker's next words to the new item.
        _audit(
            conductor_key,
            "work_ledger_record",
            "denied",
            resources="bind",
            error="worker_already_dispatched",
        )
        return _refuse_409(
            "worker_already_dispatched",
            "That worker session is still bound to an open item. A session holds one "
            "open item at a time: close that item first (the session can then be bound "
            "to this one), or create a new session for this item.",
        )
    return None


async def _bootstrap(key: str) -> None | web.Response:
    """Open this session's ledger if it has none, deriving depth from its parent.

    A session that is ITSELF bound to an item is a second-level conductor, so its
    depth is one past its parent's and its ``parent_item`` is the item it was
    dispatched for. An existing ledger is returned untouched. For a new one,
    :func:`work_ledger.child_depth` refuses past the cap, which
    is how a grandchild is stopped from conducting — the refusal surfaces as
    ``depth_exceeded`` (409) at the moment the child tries to open a ledger,
    rather than later when it tries to create an item.
    """
    # A ledger that already exists keeps working whatever the cap has become since it
    # opened: lowering ``work_ledger.max_depth`` refuses NEW ledgers and new items past
    # the cap (``child_depth`` below, ``create`` in the store), never a ``goal`` update
    # of a ledger that is already open. An unreadable header reads as absent here, so it
    # falls through to the normal path and its strict re-read.
    if await asyncio.to_thread(work_ledger.read_conductor, key) is not None:
        return None
    depth = 0
    parent_item: str | None = None
    binding = await asyncio.to_thread(work_ledger.read_binding, key)
    if binding is not None:
        parent_key, parent_item = binding
        parent = await asyncio.to_thread(work_ledger.read_conductor, parent_key)
        if parent is None:
            # REFUSE rather than assume depth 0. ``read_conductor`` answers ``None``
            # for an absent record AND for a torn or unreadable one, and treating
            # that as "no parent" would compute ``child_depth(0) == 1`` for a child
            # whose parent is really at depth 1 — then PERSIST that 1, granting one
            # extra generation that no later read corrects. A cap that fails open on
            # an unreadable input is not a cap, so this is the one place the
            # ledger's read-as-absent convention must not be inherited.
            _audit(
                key,
                "work_ledger_record",
                "denied",
                resources="bootstrap",
                error="parent_unreadable",
            )
            return _refuse_409(
                "parent_unreadable",
                "This session is bound to a work item, but its conductor's own "
                "ledger is not readable, so the nesting depth of a ledger opened "
                "here cannot be established. Retry once the parent's record is "
                "readable.",
            )
        try:
            # Off the loop: the cap comes from the config file.
            depth = await asyncio.to_thread(work_ledger.child_depth, parent.depth)
        except WorkLedgerError as exc:
            _audit(key, "work_ledger_record", "denied", resources="bootstrap", error=exc.code)
            return _refuse_store_error(exc)
    try:
        await asyncio.to_thread(_ensure, key, depth, parent_item if binding is not None else None)
    except WorkLedgerError as exc:
        _audit(key, "work_ledger_record", "denied", resources="bootstrap", error=exc.code)
        return _refuse_store_error(exc)
    except OSError:
        logger.warning("work ledger bootstrap failed for %s", key, exc_info=True)
        return _refuse_503("ledger_write_failed", "ledger write failed; try again")
    return None


def _ensure(key: str, depth: int, parent_item: str | None) -> work_ledger.ConductorRecord:
    return work_ledger.ensure_conductor(key, depth=depth, parent_item=parent_item)


def _write(key: str, action: str, cleaned: dict[str, Any]) -> dict[str, Any]:
    """Route one validated action to the store call that owns it."""
    if action == "bind" and cleaned.get("worker_instance_id"):
        # Reached only from the gateway's own remote create, never from the record
        # route: its schema has no ``worker_instance_id``. The peer's slot key is
        # not folded, since no local binding file is written for it.
        return work_ledger.apply_remote_bind(
            key,
            str(cleaned.get("item_id") or ""),
            worker_session_key=cleaned.get("worker_session_key"),
            worker_instance_id=cleaned.get("worker_instance_id"),
        )
    if action == "accept":
        return work_ledger.apply_acceptance_update(
            key,
            str(cleaned.get("item_id") or ""),
            acceptance=cleaned.get("acceptance"),
        )
    # ``worker_session_key`` is folded exactly as the caller's own key is, so the
    # digest the conductor binds is the digest the worker resolves to.
    worker_key = cleaned.get("worker_session_key")
    if isinstance(worker_key, str) and worker_key:
        worker_key = session_ledger.ledger_key(worker_key)
    return work_ledger.apply_conductor_action(
        key,
        action,
        item_id=cleaned.get("item_id") or None,
        title=cleaned.get("title"),
        acceptance=cleaned.get("acceptance"),
        worker_session_key=worker_key,
        decision=cleaned.get("decision"),
        verdict=cleaned.get("verdict"),
        state=cleaned.get("state"),
        goal=cleaned.get("goal"),
        round_number=cleaned.get("round"),
        fails=cleaned.get("fails"),
        item_cap=cleaned.get("item_cap"),
        concurrency_cap=cleaned.get("concurrency_cap"),
        new_goal=cleaned.get("new_goal"),
    )

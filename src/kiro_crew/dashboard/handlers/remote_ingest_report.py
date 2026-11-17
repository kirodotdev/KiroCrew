"""How a pulled peer snapshot becomes a filed report on the hub.

An owner of :mod:`kiro_crew.dashboard.handlers.remote_ingest`: callers and tests import
and patch these names through that facade, which mirrors a patch into every module of
the family that holds the name. Owners never import the facade.

* the snapshot's retry key (:func:`_snapshot_key`) and the crew-log retry window
  (:func:`_unrecorded_hold`);
* stamps and the current binding's report floor (:func:`_stamp`, :func:`_later`,
  :func:`_report_floor`);
* the peer item read as one report and cleaned through the report route's own schema
  (:func:`_snapshot_report`, :func:`_clean_report`);
* the filing itself, under the board lock (:func:`_ingest_report_locked`,
  :func:`_file_one`).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew import work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import trace_binding
from kiro_crew.dashboard.handlers import remote_ingest_peer as peer_ingest
from kiro_crew.dashboard.handlers.work_ledger import (
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_STAMP,
    _audit,
    _board_header,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _entry_with_baseline,
    _mark_recorded,
    _refuse_if_dirty,
    _refuse_unrecorded,
)
from kiro_crew.tracing import work as trace_work
from kiro_crew.validation import (
    WORK_REPORT_REMOTE_DISPATCH_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.work_vocab import WORK_REASON_STATUSES

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState


def _is_remote_open(item: work_ledger.WorkItem) -> bool:
    return bool(item.worker_instance_id) and not item.is_terminal and bool(item.worker_session_key)


def _already_filed(item: work_ledger.WorkItem, cleaned: dict[str, Any]) -> bool:
    """Whether *item* already holds exactly this report as its newest one, every
    worker field alike.

    The item is the authoritative record (rebuilt from the crew log), so this is
    the whole dedupe: a repeat read of an unchanged peer files nothing.
    """
    return (
        item.last_report_at == cleaned.get("reported_at")
        and item.status == cleaned.get("status")
        and item.summary == (cleaned.get("summary") or "")
        and item.reason == cleaned.get("reason")
        and item.artifacts == (cleaned.get("artifacts") or {})
        # A report without a pr leaves the item's pr as it was.
        and (cleaned.get("pr") is None or item.pr == cleaned.get("pr"))
    )


#: Seconds before a pull retries a peer stamp whose filing the crew log refused.
UNRECORDED_RETRY_SECS = 600.0


#: The peer item fields a snapshot's retry key covers: its stamp and every worker field.
_SNAPSHOT_FIELDS = ("last_report_at", "status", "summary", "reason", "artifacts", "pr")


def _snapshot_key(payload: dict[str, Any]) -> str | None:
    """A short digest of the peer worker's latest snapshot, or ``None`` with no stamp.

    Two reports in the same second share a stamp, so the stamp alone cannot tell a
    changed snapshot from a repeat; the digest covers every worker field too.
    """
    peer = payload.get("work_item")
    if not isinstance(peer, dict):
        return None
    stamp = peer.get("last_report_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    fields = {name: peer.get(name) for name in _SNAPSHOT_FIELDS}
    raw = json.dumps(fields, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _unrecorded_hold(record: work_ledger.RemoteState, payload: dict[str, Any]) -> bool:
    """Whether this pull skips filing: the same peer snapshot failed to record within
    :data:`UNRECORDED_RETRY_SECS`. A changed snapshot is always filed."""
    key = _snapshot_key(payload)
    if not key or key != record.unrecorded_key or not record.unrecorded_at:
        return False
    since = _stamp(record.unrecorded_at)
    if since is None:
        return False
    age = (datetime.now(timezone.utc) - since).total_seconds()
    return 0 <= age < UNRECORDED_RETRY_SECS


def _stamp(value: Any) -> Any:
    return work_ledger.parse_stamp(value) if isinstance(value, str) and value else None


def _later(a: str | None, b: str | None) -> bool:
    """Whether stamp *a* is strictly after *b*; an absent *b* is before everything."""
    moment = _stamp(a)
    if moment is None:
        return False
    floor = _stamp(b)
    return floor is None or moment > floor


def _report_floor(record: work_ledger.RemoteState, item: work_ledger.WorkItem) -> str | None:
    """The current binding's report floor: a peer snapshot no newer is not news.

    The floor belongs to the binding (crew + slot), never to the item. A record
    that holds it answers it. Without one, an item that was never retired falls
    back to its ``last_report_at`` (a record from before the per-binding floor);
    a retired item has no floor, since its ``last_report_at`` is a retired
    worker's stamp from another crew's clock. Only the current pair is ever
    pulled, so no floor lets a retired worker's report through.
    """
    if record.report_floor_known:
        return record.report_floor
    return None if item.retired_bindings else item.last_report_at


def _snapshot_report(
    item: work_ledger.WorkItem, payload: dict[str, Any], floor: str | None = None
) -> dict[str, Any] | None:
    """The peer worker's LATEST report as report fields plus its stamp, or ``None``.

    Worker fields are latest-value (RFC §2): the hub files the peer item's current
    ``status``, ``summary``, ``artifacts``, ``pr`` and ``reason``, stamped with the
    peer's ``last_report_at``, and never the event history. A snapshot older than
    the hub's own ``last_report_at`` is not news. Statuses the worker passed through
    between two hub reads are not kept.
    """
    peer = payload.get("work_item")
    if not isinstance(peer, dict) or peer.get("status") is None:
        return None
    stamp = peer.get("last_report_at")
    if not isinstance(stamp, str) or not stamp or _later(floor, stamp):
        return None
    row: dict[str, Any] = {
        "status": peer.get("status"),
        "summary": peer.get("summary") if isinstance(peer.get("summary"), str) else "",
        "reported_at": stamp,
    }
    for name in ("artifacts", "pr"):
        if peer.get(name) is not None:
            row[name] = peer.get(name)
    if peer.get("reason") is not None and peer.get("status") in WORK_REASON_STATUSES:
        # A reason rides only on blocked / question, as a local report's does.
        row["reason"] = peer.get("reason")
    return row


def _clean_report(row: dict[str, Any]) -> dict[str, Any] | None:
    """*row* through the report route's own schema and redaction, or ``None``."""
    fields = {k: row[k] for k in ("status", "summary", "artifacts", "pr", "reason") if k in row}
    if not fields.get("artifacts"):
        fields.pop("artifacts", None)
    try:
        # The peer's own report schema with the preview on, which ingest always is:
        # a ``reply`` the worker could file must be one the hub can file too.
        cleaned = validate_tool_args(fields, WORK_REPORT_REMOTE_DISPATCH_SCHEMA)
        cleaned = crew_log_emit.safe_work_fields(cleaned)
    except (ValidationError, crew_log_emit.WorkFieldError):
        return None
    cleaned["reported_at"] = row.get("reported_at")
    return cleaned


#: A filing the crew log did not record; the ledger write was undone.
CODE_LOG_UNRECORDED = "crew_log_unrecorded"

#: Filing failures that may clear on their own: the pull stops there, files no
#: later report, and the next pull files the same one again. Any other failure can
#: never file, so that report is passed over and counted (``peer_skipped``).
_RETRY_LATER = frozenset({CODE_LOG_UNRECORDED, "ledger_write_failed"})


async def _ingest_report_locked(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    crew: str,
    slot: str,
    payload: dict[str, Any],
    floor: str | None,
    limit: int,
    *,
    record_floor_known: bool = True,
) -> str | None:
    """File every peer report since the last pull on *item_id*, oldest first. HOLD
    THE BOARD LOCK.

    *floor* is the current binding's newest filed peer stamp (``RemoteState``),
    *record_floor_known* whether the record holds it, and *limit* the number of
    peer events the payload was asked for.

    ``None`` or an error code. Where ingest stands is folded from the crew log
    (``remote_ingest_peer``): each report filed carries the peer's own event id,
    and a report whose id is filed is never filed again. A failure in
    :data:`_RETRY_LATER` stops the pull, so nothing after it (the newest report,
    with the peer's latest fields, included) files before it does. A report that
    can never file is passed over and counted on the next filing that lands, as is
    a peer log that rolled past where ingest stood, so a retry never counts twice.
    """
    if await _refuse_if_dirty(board, board, "work_ledger_read.ingest") is not None:
        return work_ledger.CODE_CACHE_DIRTY
    try:
        # Strict: an I/O fault must not read as "no item" and end the pull as a success.
        current = await asyncio.to_thread(
            functools.partial(work_ledger.read_work_item, board, item_id, strict=True)
        )
    except OSError:
        return "ledger_write_failed"
    if (
        current is None
        or not _is_remote_open(current)
        or current.worker_session_key != slot
        or current.worker_instance_id != crew
    ):
        # The item is not bound to the crew and slot this pull read.
        return None
    try:
        folded = await asyncio.to_thread(peer_ingest.read_folded_peer, board, item_id)
    except Exception:  # noqa: BLE001 - without the record nothing says where ingest stands
        logging.getLogger(__name__).warning(
            "work fold unreadable for remote ingest on %s", item_id, exc_info=True
        )
        return peer_ingest.CODE_FOLD_UNREADABLE
    if not record_floor_known and folded.item is not None:
        # A record with no floor of its own falls back to the item's stamp: the
        # crew log's, never the cache's, which may hold a report the log refused.
        floor = _report_floor(work_ledger.RemoteState(), folded.item)
    # A binding that has filed by peer id picks its reports by id, never by stamp:
    # the stamp floor would hide reports a peer filed after its clock stepped back.
    row = _snapshot_report(current, payload, None if folded.ids else floor)
    if row is None:
        return None
    stamp = row["reported_at"]
    if work_ledger.parse_stamp(stamp) is None:
        # ``apply_remote_report`` refuses the same stamp; refusing it here names why.
        return "crew_report_malformed"
    # ``None`` when the latest fields fail the schema: the events still file. The
    # latest fields filed alone (no event names them) still never go back past the
    # floor: a delayed copy of an older snapshot is not news.
    snapshot = _clean_report(row)
    stale = _later(floor, stamp)
    events = payload.get("events")
    # In the peer log's order, whatever their stamps say (a clock may step back).
    reports = peer_ingest.report_events(payload)
    if limit < work_ledger.MAX_EVENTS_PER_ITEM and peer_ingest.needs_page(
        payload, folded.ids, limit
    ):
        # The pull did not page because the fold could not be read then: this short
        # tail may stop before where ingest stands, so nothing is filed from it.
        return peer_ingest.CODE_FOLD_UNREADABLE
    if not reports:
        # A peer that sent no report event: its latest fields alone, as before ids.
        if snapshot is None:
            return "crew_report_malformed"
        if stale or _already_filed(current, snapshot):
            return None
        return await _drain_before_cancelling(
            _file_one(state, board, unit, item_id, slot, snapshot)
        )
    fresh, gap = peer_ingest.new_reports(
        reports, folded.ids, floor, peer_ingest.rolled(events, limit)
    )
    # Recorded with the next filing that lands: the ids a pre-id binding passes over
    # and those of reports that can never file, so neither is taken for news again.
    seen = peer_ingest.legacy_seen(reports, folded.ids, floor)
    skipped = int(gap)
    refused: str | None = None
    filed_any = False
    for index, report in enumerate(fresh):
        cleaned = _peer_report_fields(report, row, snapshot, newest=index == len(fresh) - 1)
        if cleaned is None:
            skipped, refused = skipped + 1, refused or "crew_report_malformed"
            seen = [*seen, report.id]
            continue
        if not folded.ids and floor is not None and _already_filed(folded.item or current, cleaned):
            # A binding from before peer ids: its newest report is already filed.
            continue
        error = await _drain_before_cancelling(
            _file_one(
                state,
                board,
                unit,
                item_id,
                slot,
                cleaned,
                peer_event_id=report.id,
                skipped=skipped,
                seen=seen,
            )
        )
        if error in _RETRY_LATER:
            return error
        if error is None:
            skipped, filed_any, seen = 0, True, []
        else:
            skipped, refused = skipped + 1, refused or error
            seen = [*seen, report.id]
    if snapshot is None or stale:
        return refused
    if filed_any:
        try:
            now = await asyncio.to_thread(
                functools.partial(work_ledger.read_work_item, board, item_id, strict=True)
            )
        except OSError:
            return "ledger_write_failed"
    else:
        now = current
    if now is not None and not _already_filed(now, snapshot):
        # The item does not hold the peer's latest fields: the peer repeated its
        # newest report (same event id, so no new event) with a new reason,
        # artifacts or pr, or its newest event was not filed with them. Filed as the
        # latest fields alone, deduped against the item, as before peer ids.
        error = await _drain_before_cancelling(
            # No skip count here: a passed-over report stays before the cursor and
            # is counted by the peer-id filing that lands after it.
            _file_one(state, board, unit, item_id, slot, snapshot)
        )
        return error or refused
    return refused


def _peer_report_fields(
    report: peer_ingest.PeerReport,
    latest: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    newest: bool,
) -> dict[str, Any] | None:
    """*report* as cleaned report fields, or ``None`` when it fails the report route's
    schema or redaction.

    *latest* is the peer item's raw report fields (:func:`_snapshot_report`) and
    *snapshot* the same cleaned. The newest report, when it says what the peer item
    says, is filed with the item's fields (reason, artifacts, pr and stamp); an
    event carries only a status and a summary. An event is stamped no later than
    the peer item: the peer stamps its item a moment before the event, and nothing
    it filed is newer.
    """
    if (
        newest
        and snapshot is not None
        and report.status == latest["status"]
        and report.summary == latest["summary"]
    ):
        return snapshot
    stamp = latest["reported_at"]
    reported_at = stamp if _later(report.ts, stamp) else report.ts
    return _clean_report(
        {"status": report.status, "summary": report.summary, "reported_at": reported_at}
    )


async def _file_one(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    slot: str,
    cleaned: dict[str, Any],
    *,
    peer_event_id: str | None = None,
    skipped: int = 0,
    seen: list[str] | None = None,
) -> str | None:
    """One ingested report: probe, commit, record (board copy). HOLD THE BOARD LOCK.

    *peer_event_id* is the peer's own id for the report, and the hub event's id;
    *skipped* the places before it the hub could not file, and *seen* the peer ids
    before it a binding from before peer ids passes over. All ride in the crew-log
    entry, which the work fold reads them back from."""
    peer = _peer_fields(peer_event_id, skipped, seen or [])
    probe_item, probe_board = await asyncio.to_thread(
        work_ledger.read_work_item, board, item_id
    ), await asyncio.to_thread(work_ledger.read_conductor, board)
    probe, invalid = _entry_probe_with_baseline(
        board,
        probe_item,
        probe_board,
        actor="worker",
        by=slot,
        action="report",
        item_id=item_id,
        generation=_WIDEST_HEX_ID,
        round=getattr(probe_item, "round", 0),
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
        board_copy=True,
        **peer,
    )
    if invalid is not None or probe is None or not crew_log_emit.work_entry_fits(probe):
        return "work_entry_too_large"
    snapshot = await asyncio.to_thread(
        functools.partial(work_ledger.snapshot_for_write, board, item_id=item_id)
    )
    try:
        result = await asyncio.to_thread(
            functools.partial(
                work_ledger.apply_remote_report,
                board,
                item_id,
                status=cleaned.get("status"),
                summary=cleaned.get("summary"),
                artifacts=cleaned.get("artifacts"),
                pr=cleaned.get("pr"),
                reason=cleaned.get("reason"),
                reported_at=cleaned.get("reported_at"),
                peer_event_id=peer_event_id,
            )
        )
    except work_ledger.WorkLedgerError as exc:
        return exc.code or "crew_report_refused"
    except OSError:
        return "ledger_write_failed"
    item, event = result["item"], result["event"]
    header = await asyncio.to_thread(work_ledger.read_conductor, board)
    established, header = await _board_header(board, header)
    landed = established and await asyncio.to_thread(
        crew_log_emit.on_work_recorded,
        unit,
        {
            **_entry_with_baseline(
                board,
                item,
                header,
                actor="worker",
                by=slot,
                action="report",
                item_id=item.item_id,
                generation=getattr(header, "generation", None) or None,
                round=item.round,
                status=item.status,
                reason=item.reason,
                summary=item.summary,
                artifacts=item.artifacts,
                pr=item.pr,
                last_report_at=item.last_report_at,
                event=event.text,
                event_kind="report",
                event_id=event.id or None,
                event_ts=event.ts or None,
            ),
            "board_copy": True,
            **peer,
        },
    )
    if not landed:
        await _refuse_unrecorded(board, "work_ledger_read.ingest", item.item_id, board, snapshot)
        return "crew_log_unrecorded"
    await asyncio.to_thread(_mark_recorded, board, item.item_id)
    _audit(board, "work_ledger_read.ingest", "ok", resources=f"{item_id} status={item.status}")
    # A peer's worker reported: no turn here made it, so it sits under its item.
    await trace_work.recorded(
        "",
        board,
        "report",
        actor="worker",
        item=item,
        header=header,
        event_id=event.id,
        root_kind=lambda: trace_binding.root_kind(state, board),
    )
    return None


def _peer_fields(peer_event_id: str | None, skipped: int, seen: list[str]) -> dict[str, Any]:
    """The crew-log entry's peer fields: only those that are set."""
    out: dict[str, Any] = {}
    if peer_event_id:
        out["peer_event_id"] = peer_event_id
        if seen:
            out["peer_seen"] = seen
    if skipped:
        out["peer_skipped"] = skipped
    return out

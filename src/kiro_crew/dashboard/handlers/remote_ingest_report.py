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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew import work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import trace_binding
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


async def _ingest_report_locked(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    crew: str,
    slot: str,
    payload: dict[str, Any],
    floor: str | None,
) -> str | None:
    """File the peer worker's latest report on *item_id*. HOLD THE BOARD LOCK.

    *floor* is the current binding's newest filed peer stamp (``RemoteState``).

    ``None`` or an error code. The report is filed unless the hub item already
    holds exactly those worker fields and that stamp, so a repeat read files
    nothing and a changed snapshot is filed once. Nothing else is remembered
    between reads: the item is the record.
    """
    if await _refuse_if_dirty(board, board, "work_ledger_read.ingest") is not None:
        return work_ledger.CODE_CACHE_DIRTY
    current = await asyncio.to_thread(work_ledger.read_work_item, board, item_id)
    if (
        current is None
        or not _is_remote_open(current)
        or current.worker_session_key != slot
        or current.worker_instance_id != crew
    ):
        # The item is not bound to the crew and slot this pull read.
        return None
    row = _snapshot_report(current, payload, floor)
    if row is None:
        return None
    cleaned = _clean_report(row)
    if cleaned is None:
        return "crew_report_malformed"
    stamp = cleaned.get("reported_at")
    if not isinstance(stamp, str) or work_ledger.parse_stamp(stamp) is None:
        # ``apply_remote_report`` refuses the same stamp; refusing it here names why.
        return "crew_report_malformed"
    if _already_filed(current, cleaned):
        return None
    return await _drain_before_cancelling(_file_one(state, board, unit, item_id, slot, cleaned))


async def _file_one(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    slot: str,
    cleaned: dict[str, Any],
) -> str | None:
    """One ingested report: probe, commit, record (board copy). HOLD THE BOARD LOCK."""
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

"""Which peer reports a pull files, keyed on the peer's own event ids.

The crew log is the only record of where remote ingest stands. Each report the hub
files from a peer carries the peer's event id (``peer_event_id``), and the work fold
holds the ids filed on the item's current binding (``projection_work._work_peer_report``).
The pull cursor is the newest of them, read from the fold each pull, never stored
anywhere else, so a kill, a refused append or a ledger rebuild leaves it exactly
where the crew log says.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from kiro_crew import work_ledger

#: The work fold could not be read, so the pull filed nothing.
CODE_FOLD_UNREADABLE = "work_fold_unreadable"


@dataclass(frozen=True)
class FoldedPeer:
    """What the crew log says of one item's remote ingest (:func:`read_folded_peer`)."""

    item: work_ledger.WorkItem | None
    #: Peer event ids filed on the current binding, oldest first.
    ids: tuple[str, ...]
    #: Places in the peer's reports the hub could not file, summed.
    skipped: int


def _read_work_state(board: str) -> dict[str, Any]:
    """The board's work fold state, internal keys included. BLOCKING; raises when
    the fold cannot be read. Never cold: a board with no warm cell asks the
    background prime for one and raises, so the pull files nothing and the next
    one reads the warm cell."""
    from kiro_crew.crew_log import projection, slot_prime
    from kiro_crew.crew_log.projection_slot_memo import SlotFoldNotReady
    from kiro_crew.work_vocab import WORK_FOLD_NAME

    try:
        checkpoint, _revision = projection.read_slot_state(board, WORK_FOLD_NAME, cold=False)
    except SlotFoldNotReady:
        slot_prime.schedule(board, WORK_FOLD_NAME, for_reader=True)
        raise
    state: dict[str, Any] = checkpoint.state
    return state


def read_folded_peer(board: str, item_id: str) -> FoldedPeer:
    """*item_id* as the board's work fold holds it, with the peer event ids filed on
    its current binding. BLOCKING; raises when the fold cannot be read. An item the
    fold does not hold reads as no item and no ids."""
    raw = _read_work_state(board).get("items", {}).get(item_id)
    if not isinstance(raw, dict):
        return FoldedPeer(None, (), 0)
    ids = raw.get("_peer_ids")
    skipped = raw.get("_peer_skipped")
    item = work_ledger.WorkItem.from_dict(
        {key: value for key, value in raw.items() if key != "events" and not key.startswith("_")}
    )
    return FoldedPeer(
        item,
        tuple(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else (),
        skipped if isinstance(skipped, int) and not isinstance(skipped, bool) else 0,
    )


def whole_log(events: Any, limit: int) -> bool:
    """Whether a reply asked for *limit* events holds the peer's whole retained log:
    fewer came back than were asked for, or the ask was the store's own cap."""
    return limit >= work_ledger.MAX_EVENTS_PER_ITEM or (
        isinstance(events, list) and len(events) < limit
    )


def needs_page(payload: Any, ids: tuple[str, ...] | None, limit: int) -> bool:
    """Whether a reply of *limit* events may stop short of where ingest stands: it
    is not the whole log and holds none of the filed *ids* (none yet, on a first
    pull). ``None`` *ids* (the fold could not be read) never pages: that pull files
    nothing anyway."""
    if ids is None or not isinstance(payload, dict):
        return False
    events = payload.get("events")
    if not isinstance(events, list) or whole_log(events, limit):
        return False
    filed = set(ids)
    return not any(
        isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] in filed for e in events
    )


@dataclass(frozen=True)
class PeerReport:
    """One report event from the peer's log, its id checked."""

    id: str
    status: Any
    summary: Any
    ts: str


def report_events(payload: dict[str, Any]) -> list[PeerReport]:
    """The peer reply's report events, oldest first. Anything that is not a report
    event with a work event id and a readable stamp is not a peer report at all and
    is left out. Only the store's cap of events is read, whatever the peer sent."""
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    out: list[PeerReport] = []
    for event in events[-work_ledger.MAX_EVENTS_PER_ITEM :]:
        if not isinstance(event, dict) or event.get("kind") != "report":
            continue
        event_id, ts = event.get("id"), event.get("ts")
        if not isinstance(event_id, str) or not work_ledger.is_peer_event_id(event_id):
            continue
        if not isinstance(ts, str) or work_ledger.parse_stamp(ts) is None:
            continue
        out.append(PeerReport(event_id, event.get("status"), event.get("text"), ts))
    return out


def rolled(events: Any, limit: int) -> bool:
    """Whether the peer's log may have dropped events off its back: the reply is
    the whole log and the log is at the store's cap."""
    return (
        whole_log(events, limit)
        and isinstance(events, list)
        and len(events) >= work_ledger.MAX_EVENTS_PER_ITEM
    )


def new_reports(
    reports: list[PeerReport], ids: tuple[str, ...], floor: str | None, gone: bool
) -> tuple[list[PeerReport], bool]:
    """``(fresh, gap)``: the peer reports after where ingest stands, in the peer
    log's order, and whether the peer's log rolled past it.

    Where ingest stands is the newest report in the peer's log whose id is filed:
    the cursor (the newest filed id) itself or, when the peer coalesced the
    cursor's ``progress`` into the next one, the filed report before it. Only ids
    decide it, never stamps: a peer clock that steps back reorders nothing. With
    no id filed on this binding, a binding that has filed nothing (no *floor*)
    takes every report; one whose reports were filed before peer ids were kept
    takes only the newest, as the hub did then, and records the ids before it as
    seen (:func:`legacy_seen`). With ids filed but none in the peer's log, every
    report in it is new; when the log is at its cap (*gone*, :func:`rolled`)
    reports may have rolled off its back, and that gap is said.
    """
    filed = set(ids)
    at = next((i for i in range(len(reports) - 1, -1, -1) if reports[i].id in filed), None)
    if at is not None:
        return [r for r in reports[at + 1 :] if r.id not in filed], False
    if not ids:
        # shortcut: a binding from before peer ids misses reports between its last
        # pre-upgrade pull and the first one after it; it files only the newest.
        return (reports if floor is None else reports[-1:]), False
    return reports, gone


def legacy_seen(reports: list[PeerReport], ids: tuple[str, ...], floor: str | None) -> list[str]:
    """The peer ids a binding from before peer ids passes over on its first id
    filing: every retained report before the newest, already filed (or passed
    over) before the upgrade. Recorded in the crew log with that filing
    (``peer_seen``), so a later coalesce of the newest never makes them news."""
    if ids or floor is None:
        return []
    return [r.id for r in reports[:-1]]


def folded_skipped(board: str, item_id: str) -> int | None:
    """The row's ``remote_reports_skipped``; ``None`` when the fold cannot be read.
    BLOCKING, never raises."""
    try:
        return read_folded_peer(board, item_id).skipped
    except Exception:  # noqa: BLE001 - a row field must never fail the read
        return None


#: The worker-owned fields a copy's digest covers (``remote_work_items_pull``).
_DIGEST_FIELDS = ("status", "summary", "reason", "artifacts", "pr")


def worker_digest(fields: Mapping[str, Any]) -> str:
    """A short digest of a copy's worker-owned fields, for the batch cursor.

    Two reports in one second with the same status and summary share a stamp and
    an event id, so only this tells a changed reason, artifacts or pr apart. The
    hub sends it over its own item; the peer compares it with its copy. A field
    the hub stored redacted or cleaned digests differently, which only costs the
    copy being sent again.
    """
    raw = json.dumps(
        {name: fields.get(name) or None for name in _DIGEST_FIELDS}, sort_keys=True, default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


#: Per mailbox board on this crew, the events of the hub waits held open on it
#: (``remote_work_items_pull.api_chat_work_items_wait``). Loop-confined: set and
#: read on the dashboard's event loop only.
_WAITERS: dict[str, set[asyncio.Event]] = {}


def note_report(board: str) -> None:
    """A worker report landed on *board*: release every hub wait held on it. ON THE
    LOOP. Costs one dict read when nobody waits, so the report route calls it for
    every board."""
    for event in _WAITERS.get(board, ()):
        event.set()


def waiting_count() -> int:
    """How many hub waits this crew holds open right now, over every board."""
    return sum(len(events) for events in _WAITERS.values())


@contextlib.contextmanager
def report_waiter(board: str) -> Iterator[asyncio.Event]:
    """An event :func:`note_report` sets for *board*, registered for the block. ON
    THE LOOP. Registered before the caller's first read, so a report landing between
    that read and the wait is never missed."""
    event = asyncio.Event()
    _WAITERS.setdefault(board, set()).add(event)
    try:
        yield event
    finally:
        events = _WAITERS.get(board)
        if events is not None:
            events.discard(event)
            if not events:
                del _WAITERS[board]


__all__ = [
    "CODE_FOLD_UNREADABLE",
    "FoldedPeer",
    "PeerReport",
    "folded_skipped",
    "note_report",
    "report_waiter",
    "waiting_count",
    "needs_page",
    "legacy_seen",
    "new_reports",
    "read_folded_peer",
    "report_events",
    "rolled",
    "whole_log",
    "worker_digest",
]

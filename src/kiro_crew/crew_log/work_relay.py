"""Relay a bound worker's charges, and a sub-board's records, into the parent board's log.

A board's folds (``work``, ``workstreams``) read the board's OWN units and nothing else,
so what they need from other sessions has to be written there. Three things are:

- a worker's report: the report route writes it into the conductor's unit itself
  (``api_work_report``), synchronously, because the caller is told whether it landed;
- a worker's CHARGE: each turn, subagent or background closer that bills credits or
  measures time in a session bound to a work item is copied as one ``work/charged``
  entry into the binding board's newest unit (:func:`relay`);
- a SUB-BOARD's records: a board whose conductor is itself bound to a parent board has
  its ``work/recorded`` and ``work/charged`` entries copied up into the parent's unit,
  so the parent's project report can show and cost the nested board.

Every copy is written by this process's one crew-log writer, through the same queue as
the entry it copies, so a board's log still has a single writer. The append job fixes the
board (the binding in force when the entry committed) and queues the rest for the relay's
consumer thread (``work_relay_feed``), which finds the board's newest unit off the
writer and hands the copies back, one ``append_many`` per board unit. Copies are best-effort:
a relay that cannot resolve its target, or whose append fails, costs the parent board a
charge or a record and never the entry being copied. A charge is identified by its
closer (``<unit>:<seq>``) and a relayed record by its origin, so a reader counts each
once however many copies reach it, and a chain of bindings that loops is cut by the
same identity (:data:`_RECENT`).
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import OrderedDict
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.crew_log.store import CrewLog
    from kiro_crew.crew_log.work_relay_feed import Wake

from kiro_crew.crew_log.writer_groups import UNIT_SLOTS_LIMIT

logger = logging.getLogger(__name__)

#: The closers that carry a charge, and the ``source`` each is relayed as.
CHARGE_SOURCES: Final[dict[str, str]] = {
    "turn/completed": "turn",
    "subagent/completed": "subagent",
    "subagent/failed": "subagent",
    "background/completed": "background",
}

_WORK_TYPE: Final[str] = "work/recorded"
_CHARGE_TYPE: Final[str] = "work/charged"

#: Relays this process already sent, as (origin, target board), newest last. What stops a
#: binding cycle (A bound under B, B under A) from relaying one entry around it forever,
#: and a retried append from being relayed twice. Bounded; an evicted pair costs at most
#: one duplicate copy, which every reader dedupes by the same identity.
_RECENT: "OrderedDict[tuple[str, str], None]" = OrderedDict()
_RECENT_LIMIT: Final[int] = 4096
_recent_guard = threading.Lock()

#: Each unit's own slot, from its header, which is written once at create.
_UNIT_SLOTS: "OrderedDict[str, str]" = OrderedDict()
#: The writer's unit -> slot keys bound (``writer_groups``): one population, one bound.
_UNIT_SLOTS_LIMIT: Final[int] = UNIT_SLOTS_LIMIT

#: Each worker's binding as last read, keyed by the binding file's ``stat``. Every
#: closer asks for its slot's binding on the writer thread, and a binding changes only
#: when the file is rewritten, so an unchanged file costs a ``stat`` instead of a read.
_BINDINGS: "OrderedDict[str, tuple[tuple[int, int, int] | None, str]]" = OrderedDict()
_BINDINGS_LIMIT: Final[int] = 4096


def relay(entry: Any, entry_type: str, session_id: str, data: Mapping[str, Any]) -> None:
    """Copy *entry*, just appended to *session_id*'s log, to the board above it. Never raises.

    Called from the append job right after the append returned, like the eager-fold
    notice beside it. Types this module does not relay return on the first line. Reads
    only what fixes the board at commit, then queues the decision off the writer.
    """
    if entry_type not in CHARGE_SOURCES and entry_type not in (_WORK_TYPE, _CHARGE_TYPE):
        return
    try:
        _capture(entry, entry_type, session_id, data)
    except Exception:  # pragma: no cover - a relay must not cost the entry it copies
        # Rendered text, never ``exc_info``: this runs inside the append job, whose frames
        # bind the live ``CrewLog`` and its write lease (see ``emit_state._note_eager``).
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "crew log: relaying %s from %s failed:\n%s",
                entry_type,
                session_id,
                traceback.format_exc(),
            )


def forget_for_tests() -> None:
    """Drop the relay memory, so one test's relays cannot hide another's."""
    from kiro_crew.crew_log import work_relay_feed

    work_relay_feed.forget_for_tests()
    with _recent_guard:
        _RECENT.clear()
        _UNIT_SLOTS.clear()
        _BINDINGS.clear()


def _capture(entry: Any, entry_type: str, session_id: str, data: Mapping[str, Any]) -> None:
    """On the writer: the unit's slot and the board it is bound to now, then queue."""
    from kiro_crew.crew_log import work_relay_feed

    seq = int(getattr(entry, "seq", 0) or 0)
    if seq <= 0:
        return
    own = _writer_slot(session_id)
    if not own:
        return
    parent = _bound_board(own)
    if not parent or parent == own:
        return
    work_relay_feed.note(work_relay_feed.Wake(session_id, entry_type, seq, data, own, parent))


def _writer_slot(unit_id: str) -> str:
    """*unit_id*'s slot as a work-ledger key: the one the writer noted when it opened the
    unit, else from the header (a unit this process did not open; read once, cached)."""
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import writer_groups

    raw = writer_groups.slot_of(unit_id)
    return session_ledger.ledger_key(raw) if raw else _unit_slot(unit_id)


class Copy(NamedTuple):
    """One decided copy, waiting for :func:`hand_over` to queue it for its board's unit."""

    board: str
    unit: str
    entry_type: str
    payload: "dict[str, Any]"


#: Copies one follow-on job writes with one ``append_many``.
COPIES_PER_JOB: Final[int] = 64


def decide(wake: "Wake", out: "list[Copy]") -> None:
    """On the relay consumer: build *wake*'s copy, if any, into *out*."""
    entry_type, session_id, seq, data = wake.entry_type, wake.unit, wake.seq, wake.data
    own, parent = wake.own, wake.board
    if entry_type in CHARGE_SOURCES:
        charge = _charge_of(data)
        if charge is None:
            return
        credits, duration_ms = charge
        payload: dict[str, Any] = {
            "slot": parent,
            "by": own,
            "source": CHARGE_SOURCES[entry_type],
            "closer": f"{session_id}:{seq}",
        }
        if credits is not None:
            payload["credits"] = credits
        if duration_ms:
            payload["duration_ms"] = duration_ms
        _send(out, parent, _CHARGE_TYPE, payload, origin=payload["closer"])
        return
    if entry_type == _CHARGE_TYPE:
        # A charge that reached THIS board's log goes up once more when this board is
        # itself a bound worker's: the parent's report costs the nested board's tasks.
        if str(data.get("slot") or "") != own:
            return
        if str(data.get("by") or "") == parent:
            # The parent's own charge, relayed down a binding cycle (A under B, B under
            # A): sent back, the parent would bill it twice.
            return
        closer = str(data.get("closer") or "")
        if not closer:
            return
        _send(out, parent, _CHARGE_TYPE, {**dict(data), "slot": parent}, origin=closer)
        return
    # A work record of THIS board (its own conductor entry, a report written into it, or
    # a copy relayed up from below) goes up to the board it is bound under. A worker's own
    # copy of its report names the parent's slot, not its own, and stays where it is.
    origin = str(data.get("relay") or "")
    if str(data.get("slot") or "") != own and not origin:
        return
    if str(data.get("slot") or "") == parent:
        # A board's own record never goes back into that board's log: in a binding
        # cycle (A under B, B under A) it would come home and fold a second time.
        return
    origin = origin or f"{session_id}:{seq}"
    _send(out, parent, _WORK_TYPE, {**dict(data), "relay": origin}, origin=origin)


def _send(
    out: "list[Copy]", board: str, entry_type: str, payload: "dict[str, Any]", *, origin: str
) -> None:
    unit = _target(board, origin)
    if unit:
        out.append(Copy(board, unit, entry_type, payload))


def hand_over(copies: "list[Copy]") -> None:
    """Queue *copies* for the writer, one follow-on job per target unit and run.

    Copies into one unit keep the order they were decided in. Each job writes its run
    with one ``append_many`` and publishes the board's unit order once, so the writer
    pays one lock, one tail read, one ``write()`` and one ``fsync`` for the run.
    """
    runs: "dict[str, list[Copy]]" = {}
    for copy in copies:
        runs.setdefault(copy.unit, []).append(copy)
    for unit, run in runs.items():
        for at in range(0, len(run), COPIES_PER_JOB):
            _queue_run(unit, run[at : at + COPIES_PER_JOB])


def _queue_run(unit: str, run: "list[Copy]") -> None:
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import writer_groups
    from kiro_crew.crew_log.emit_runtime import _hand_over
    from kiro_crew.crew_log.writer import WriteJob

    board = run[0].board
    # Read here, off the writer, so the job does not read the unit's header again.
    canonical = writer_groups.slot_of(unit) or session_ledger.canonical_slot(board, unit)

    def _job() -> None:
        from kiro_crew.crew_log.emit_runtime import _handle
        from kiro_crew.crew_log.emit_state import _note_eager

        log = _handle(unit)
        if log is None:
            return
        entries = _append_run(log, run)
        # The board's folds read its units in this order, so a copy landing in a unit the
        # board has not recorded into yet (a restarted conductor's new unit) must move
        # that unit last, or the predecessor's older entries fold over it.
        session_ledger.note_work_unit_recorded(board, unit, canonical=canonical)
        for copy, entry in zip(run, entries):
            if entry is None:
                continue
            # A relayed record keeps the sub-board's ``slot``, but it lands in THIS
            # board's log, so this board's folds are the ones to advance.
            board_of = copy.board if copy.entry_type == _WORK_TYPE else None
            _note_eager(entry, copy.entry_type, unit, copy.payload, board=board_of)

    what = f"relaying {len(run)} board cop{'y' if len(run) == 1 else 'ies'}"
    _hand_over(unit, WriteJob.follow_on(_job, what))


def _append_run(log: "CrewLog", run: "list[Copy]") -> "list[Any]":
    """Append *run* as one group; on a refusal, one at a time so only the bad one is lost.

    A refusal is decided before any byte is written, so the one-at-a-time pass writes
    nothing twice. When every copy is refused the last refusal is raised, so the writer's
    own rule (one loss marker, never retried) decides it.
    """
    from kiro_crew.crew_log.emit_state import _SRC_GATEWAY
    from kiro_crew.crew_log.errors import CrewLogError

    items = [_item(copy) for copy in run]
    try:
        return list(log.append_many(items, src=_SRC_GATEWAY))
    except CrewLogError:
        if len(run) == 1:
            raise
    entries: "list[Any]" = []
    refusal: "CrewLogError | None" = None
    # shortcut: a non-refusal failure part way through this pass raises, and the
    # writer's retry of the job writes the copies before it again; every reader counts a
    # copy once by its closer or origin. Track per-copy outcomes if that ever matters.
    for copy in run:
        try:
            entries.append(log.append(copy.entry_type, copy.payload, src=_SRC_GATEWAY))
        except CrewLogError as exc:
            refusal = exc
            entries.append(None)
    if refusal is not None and all(entry is None for entry in entries):
        raise refusal
    return entries


def _item(copy: Copy) -> "dict[str, Any]":
    """*copy* as an ``append_many`` member, its type spelled as a literal for the writer
    vocabulary check (``test_crew_log_types``)."""
    if copy.entry_type == _CHARGE_TYPE:
        return {"type": "work/charged", "data": copy.payload, "ignorable": False}
    return {"type": "work/recorded", "data": copy.payload, "ignorable": False}


def _target(board: str, origin: str) -> str:
    """*board*'s newest unit, the first time (origin, board) is relayed; else ``""``."""
    key = (origin, board)
    with _recent_guard:
        if key in _RECENT:
            return ""
    from kiro_crew import session_ledger

    unit = session_ledger.newest_work_unit(board)
    _note_hold_key(board, unit)
    if not unit:
        # Not remembered: a later copy of the same origin may find the board's unit.
        logger.debug("crew log: board %s has no work unit to relay %s into", board, origin)
        return ""
    with _recent_guard:
        if key in _RECENT:
            return ""
        _RECENT[key] = None
        while len(_RECENT) > _RECENT_LIMIT:
            _RECENT.popitem(last=False)
    return unit


def _note_hold_key(board: str, unit: str) -> None:
    """Give the writer *unit*'s slot, so it is never written beside a sibling unit.

    A board unit this process did not open has no key yet. Read once per unit, here, in
    a job and off the writer's lock, with the spelling the order file is published under.
    """
    # Imported here: the writer modules stay off the gateway boot path.
    from kiro_crew.crew_log import writer_groups

    if not unit or writer_groups.known(unit):
        return
    from kiro_crew import session_ledger

    writer_groups.note_unit_slot(unit, session_ledger.canonical_slot(board, unit))


def _unit_slot(unit_id: str) -> str:
    """*unit_id*'s own slot as a work-ledger key, from its header; ``""`` when unknown."""
    with _recent_guard:
        held = _UNIT_SLOTS.get(unit_id)
    if held is not None:
        return held
    from kiro_crew import session_ledger
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import unit_header_slot

    raw = unit_header_slot(KIND_SESSION, unit_id) or ""
    slot = session_ledger.ledger_key(raw) if raw else ""
    if slot:
        with _recent_guard:
            _UNIT_SLOTS[unit_id] = slot
            while len(_UNIT_SLOTS) > _UNIT_SLOTS_LIMIT:
                _UNIT_SLOTS.popitem(last=False)
    return slot


def _bound_board(worker: str) -> str:
    """The board *worker* is bound to, or ``""``; re-read only when the file changed."""
    from kiro_crew import work_ledger

    try:
        stat = work_ledger.binding_path(worker).stat()
        mark: "tuple[int, int, int] | None" = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        mark = None
    except (OSError, work_ledger.WorkLedgerError):
        return ""
    with _recent_guard:
        held = _BINDINGS.get(worker)
    if held is not None and held[0] == mark:
        return held[1]
    try:
        binding = work_ledger.read_binding(worker, strict=True) if mark is not None else None
    except OSError:
        # A read that failed is not an answer: nothing is cached, so the next closer
        # reads the file again.
        return ""
    board = str(binding[0]) if binding else ""
    with _recent_guard:
        _BINDINGS.pop(worker, None)
        _BINDINGS[worker] = (mark, board)
        while len(_BINDINGS) > _BINDINGS_LIMIT:
            _BINDINGS.popitem(last=False)
    return board


def _charge_of(data: Mapping[str, Any]) -> "tuple[float | None, int] | None":
    """A closer's (credits, duration) as the project report reads them, or ``None``."""
    from kiro_crew.crew_log.projection_usage import _credit_charge
    from kiro_crew.crew_log.projection_values import _as_int

    credits = _credit_charge(data.get("credits"))
    measured = _as_int(data.get("duration_ms")) or _as_int(data.get("ms"))
    if credits is None and not measured:
        return None
    return credits, measured

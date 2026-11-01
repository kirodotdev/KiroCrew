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
the entry it copies, so a board's log still has a single writer. Copies are best-effort:
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
from typing import Any, Final

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
_UNIT_SLOTS_LIMIT: Final[int] = 4096

#: Each worker's binding as last read, keyed by the binding file's ``stat``. Every
#: closer asks for its slot's binding on the writer thread, and a binding changes only
#: when the file is rewritten, so an unchanged file costs a ``stat`` instead of a read.
_BINDINGS: "OrderedDict[str, tuple[tuple[int, int, int] | None, str]]" = OrderedDict()
_BINDINGS_LIMIT: Final[int] = 4096


def relay(entry: Any, entry_type: str, session_id: str, data: Mapping[str, Any]) -> None:
    """Copy *entry*, just appended to *session_id*'s log, to the board above it. Never raises.

    Called from the append job right after the append returned, like the eager-fold
    notice beside it. Types this module does not relay return on the first line.
    """
    if entry_type not in CHARGE_SOURCES and entry_type not in (_WORK_TYPE, _CHARGE_TYPE):
        return
    try:
        _relay(entry, entry_type, session_id, data)
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
    with _recent_guard:
        _RECENT.clear()
        _UNIT_SLOTS.clear()
        _BINDINGS.clear()


def _relay(entry: Any, entry_type: str, session_id: str, data: Mapping[str, Any]) -> None:
    seq = int(getattr(entry, "seq", 0) or 0)
    own = _unit_slot(session_id)
    if seq <= 0 or not own:
        return
    parent = _bound_board(own)
    if not parent or parent == own:
        return
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
        _send_charge(parent, payload, origin=payload["closer"])
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
        _send_charge(parent, {**dict(data), "slot": parent}, origin=closer)
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
    _send_record(parent, {**dict(data), "relay": origin}, origin=origin)


def _send_charge(board: str, payload: dict[str, Any], *, origin: str) -> None:
    """Queue one ``work/charged`` copy into *board*'s newest unit."""
    unit = _target(board, origin)
    if not unit:
        return
    from kiro_crew.crew_log.emit_runtime import _handle
    from kiro_crew.crew_log.emit_state import _SRC_GATEWAY, _note_eager

    def _job() -> None:
        log = _handle(unit)
        if log is None:
            return
        entry = log.append("work/charged", payload, src=_SRC_GATEWAY)
        _publish_order(board, unit)
        _note_eager(entry, "work/charged", unit, payload)

    _follow_on(unit, _job, "relaying a worker charge")


def _send_record(board: str, payload: dict[str, Any], *, origin: str) -> None:
    """Queue one relayed ``work/recorded`` copy into *board*'s newest unit."""
    unit = _target(board, origin)
    if not unit:
        return
    from kiro_crew.crew_log.emit_runtime import _handle
    from kiro_crew.crew_log.emit_state import _SRC_GATEWAY, _note_eager

    def _job() -> None:
        log = _handle(unit)
        if log is None:
            return
        entry = log.append("work/recorded", payload, src=_SRC_GATEWAY)
        _publish_order(board, unit)
        # The copy keeps the sub-board's ``slot``, but it lands in THIS board's log, so
        # this board's folds are the ones to advance.
        _note_eager(entry, "work/recorded", unit, payload, board=board)

    _follow_on(unit, _job, "relaying a sub-board record")


def _publish_order(board: str, unit: str) -> None:
    """Record that *unit* holds *board*'s newest work entry, as a local append does.

    The board's folds read its units in this order, so a copy landing in a unit the
    board has not recorded into yet (a restarted conductor's new unit) must move that
    unit last, or the predecessor's older entries fold over it.
    """
    from kiro_crew import session_ledger

    session_ledger.note_work_unit_recorded(board, unit)


def _follow_on(unit: str, job: "Any", what: str) -> None:
    """Hand *job* to the writer as a follow-on for *unit*.

    This runs on the writer thread, inside the job that appended the entry being
    copied. A follow-on is the writer's policy for exactly that: queued behind what
    the unit already owes and never run inline, where it would wait for the pass it is
    inside to finish.
    """
    from kiro_crew.crew_log.emit_runtime import _hand_over
    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(unit, WriteJob.follow_on(job, what))


def _target(board: str, origin: str) -> str:
    """*board*'s newest unit, the first time (origin, board) is relayed; else ``""``."""
    key = (origin, board)
    with _recent_guard:
        if key in _RECENT:
            return ""
    from kiro_crew import session_ledger

    unit = session_ledger.newest_work_unit(board)
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

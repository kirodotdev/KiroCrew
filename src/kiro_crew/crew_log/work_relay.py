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

Every copy is written by this process's one crew-log writer, so a board's log still has
a single writer. The DECISION to copy is not made on the writer: the append job only
hands the committed entry to :mod:`~kiro_crew.crew_log.work_relay_feed`, whose own thread
reads the unit header, the binding and the board's newest unit, then queues the copy. A
copy is still best-effort for the entry it copies -- a relay that cannot resolve its
target costs the parent board a charge or a record and never the entry -- but one the
consumer fell behind on, or a crash cut off, is caught up from its durable cursor. A
charge is identified by its closer (``<unit>:<seq>``) and a relayed record by its origin,
so a reader counts each once however many copies reach it, and a chain of bindings that
loops is cut by the same identity (:data:`_RECENT`).
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.crew_log.store import CrewLog

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


#: What the consumer hands each copy so it can learn the copy's outcome: called once when
#: the copy is queued, it returns the hook the copy's job calls with True once landed, or
#: False when the writer gave up on it. ``None`` (a direct caller) tracks nothing.
Ticket = Callable[[], Callable[[bool], None]]

#: The origins already in a board's log, for a catch-up that must not copy one twice.
Seen = Callable[[str], "frozenset[str]"]


def relayable(entry_type: str) -> bool:
    """Whether an entry of *entry_type* is ever copied to a board above its unit."""
    return entry_type in CHARGE_SOURCES or entry_type in (_WORK_TYPE, _CHARGE_TYPE)


def relay(entry: Any, entry_type: str, session_id: str, data: Mapping[str, Any]) -> None:
    """Hand *entry*, just appended to *session_id*'s log, to the relay. Never raises.

    Called from the append job right after the append returned, like the eager-fold
    notice beside it, so this is writer-thread time: a membership test and one queue
    put (:func:`kiro_crew.crew_log.work_relay_feed.note`). Resolving the board and
    queuing the copy happen on the relay's own thread.
    """
    if not relayable(entry_type):
        return
    seq = int(getattr(entry, "seq", 0) or 0)
    if seq <= 0:
        return
    try:
        from kiro_crew.crew_log import work_relay_feed

        work_relay_feed.note(session_id, entry_type, seq, data, int(getattr(entry, "time", 0) or 0))
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


def resolve(
    seq: int,
    entry_type: str,
    session_id: str,
    data: Mapping[str, Any],
    *,
    out: "list[Copy]",
    ticket: "Ticket | None" = None,
    seen: "Seen | None" = None,
    time_ms: int = 0,
) -> bool:
    """Decide whether entry *seq* of *session_id* is copied up; put the copy on *out*.

    The relay consumer's step, off the writer thread; :func:`hand_over` queues what it
    collected. True when the unit's slot is bound to a board above it, whether or not
    this entry needed a copy: only such a unit has a cursor worth keeping. Raises what a
    read raised: the consumer reads an exception as "not settled", so the entry is tried
    again. *time_ms* is the entry's own ``time``: an entry older than the binding file
    needs no copy.
    """
    own = _unit_slot(session_id)
    if seq <= 0 or not own:
        return False
    parent = _bound_board(own)
    if not parent or parent == own:
        return False
    if time_ms and time_ms < _bound_since(own):
        # Committed before this binding was written: the work is not this board's.
        return True
    if entry_type in CHARGE_SOURCES:
        charge = _charge_of(data)
        if charge is None:
            return True
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
        _send(
            parent,
            _CHARGE_TYPE,
            payload,
            origin=payload["closer"],
            ticket=ticket,
            seen=seen,
            out=out,
        )
        return True
    if entry_type == _CHARGE_TYPE:
        # A charge that reached THIS board's log goes up once more when this board is
        # itself a bound worker's: the parent's report costs the nested board's tasks.
        if str(data.get("slot") or "") != own:
            return True
        if str(data.get("by") or "") == parent:
            # The parent's own charge, relayed down a binding cycle (A under B, B under
            # A): sent back, the parent would bill it twice.
            return True
        closer = str(data.get("closer") or "")
        if not closer:
            return True
        payload = {**dict(data), "slot": parent}
        _send(parent, _CHARGE_TYPE, payload, origin=closer, ticket=ticket, seen=seen, out=out)
        return True
    # A work record of THIS board (its own conductor entry, a report written into it, or
    # a copy relayed up from below) goes up to the board it is bound under. A worker's own
    # copy of its report names the parent's slot, not its own, and stays where it is.
    origin = str(data.get("relay") or "")
    if str(data.get("slot") or "") != own and not origin:
        return True
    if str(data.get("slot") or "") == parent:
        # A board's own record never goes back into that board's log: in a binding
        # cycle (A under B, B under A) it would come home and fold a second time.
        return True
    origin = origin or f"{session_id}:{seq}"
    payload = {**dict(data), "relay": origin}
    _send(parent, _WORK_TYPE, payload, origin=origin, ticket=ticket, seen=seen, out=out)
    return True


class Copy(NamedTuple):
    """One decided copy, waiting for :func:`hand_over` to queue it for its board's unit."""

    board: str
    unit: str
    entry_type: str
    payload: dict[str, Any]
    settle: "Callable[[bool], None] | None"
    origin: str = ""


_LANDED: Final[str] = "landed"
_REFUSED: Final[str] = "refused"

#: Copies one follow-on job writes at most: one ``append_many``, one order publish.
COPIES_PER_JOB: Final[int] = 64


def _send(
    board: str,
    entry_type: str,
    payload: dict[str, Any],
    *,
    origin: str,
    ticket: "Ticket | None",
    seen: "Seen | None",
    out: "list[Copy]",
) -> None:
    """Decide one copy of type *entry_type* into *board*'s newest unit, onto *out*."""
    if seen is not None and origin in seen(board):
        return
    unit = _target(board, origin)
    if not unit:
        return
    settle = ticket() if ticket is not None else None
    out.append(Copy(board, unit, entry_type, payload, settle, origin))


def hand_over(copies: "list[Copy]") -> None:
    """Queue *copies* for the writer, one follow-on job per target unit and run.

    Called by the relay consumer at the end of a pass. Copies into one unit keep the
    order they were decided in. Each job writes its run with one ``append_many`` and
    publishes the board's unit order once, so the writer pays one lock, one tail read,
    one ``write()``, one ``fsync`` and one order publish for the run rather than per copy.
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
    #: Per copy: ``_LANDED``, ``_REFUSED`` (decided before any byte, never retried), or
    #: ``""`` (not written; a retryable failure, so the consumer tries it again).
    state = [""] * len(run)

    def _job() -> None:
        from kiro_crew.crew_log.emit_runtime import _handle
        from kiro_crew.crew_log.emit_state import _note_eager
        from kiro_crew.crew_log.errors import CrewLogError

        try:
            log = _handle(unit)
        except CrewLogError:
            state[:] = [_REFUSED] * len(run)
            raise
        if log is None:
            # No log for the board's unit is a policy no-op, never retried.
            state[:] = [_LANDED] * len(run)
            return
        entries = _append_run(log, run, state)
        # The order publish is the step PR 2's slot hold keeps beside the append: this
        # job runs on the lane that holds the board's slot.
        session_ledger.note_work_unit_recorded(board, unit, canonical=canonical)
        for copy, entry in zip(run, entries):
            if entry is None:
                continue
            # A relayed record keeps the sub-board's ``slot``, but it lands in THIS
            # board's log, so this board's folds are the ones to advance. A charge has
            # no board field, so its unit's header names the fold.
            board_of = copy.board if copy.entry_type == _WORK_TYPE else None
            _note_eager(entry, copy.entry_type, unit, copy.payload, board=board_of)

    def _after() -> None:
        for copy, outcome in zip(run, state):
            if not outcome:
                # Not sent after all: the consumer's retry may copy this origin again.
                _forget_sent(copy.origin, copy.board)
            # A refusal is settled as decided: the writer has dropped it under its own
            # loss-marker rule, and a retry would only be refused and marked again.
            if copy.settle is not None:
                copy.settle(bool(outcome))

    what = f"relaying {len(run)} board cop{'y' if len(run) == 1 else 'ies'}"
    _hand_over(unit, WriteJob.follow_on(_job, what, after=_after))


def _append_run(log: "CrewLog", run: "list[Copy]", state: "list[str]") -> "list[Any]":
    """Append *run* as one group; on a refusal, one at a time so only the bad one fails.

    Returns one entry (``None`` for a copy not written) per copy and marks each copy's
    outcome in *state*. When no copy landed it raises the refusal, so the writer's own
    rule decides it: a refusal is never retried.
    """
    from kiro_crew.crew_log.emit_state import _SRC_GATEWAY
    from kiro_crew.crew_log.errors import CrewLogError

    items = [_item(copy) for copy in run]
    try:
        entries: "list[Any]" = list(log.append_many(items, src=_SRC_GATEWAY))
    except CrewLogError:
        if len(run) == 1:
            state[0] = _REFUSED
            raise
        entries = []
        refusal: "CrewLogError | None" = None
        for copy in run:
            try:
                entries.append(log.append(copy.entry_type, copy.payload, src=_SRC_GATEWAY))
            except CrewLogError as exc:
                refusal = exc
                state[len(entries)] = _REFUSED
                entries.append(None)
            except Exception:
                # A retryable failure: retried whole while nothing has landed; after a
                # copy landed a retry would write it twice. So this copy and every one
                # after it are let go, which keeps the run in order, and the consumer's
                # cursor holds below them until its retry copies them again.
                if all(entry is None for entry in entries):
                    raise
                entries.extend([None] * (len(run) - len(entries)))
                break
        if refusal is not None and all(entry is None for entry in entries):
            raise refusal from None
    for at, entry in enumerate(entries):
        if entry is not None:
            state[at] = _LANDED
    return entries


def _item(copy: Copy) -> "dict[str, Any]":
    """*copy* as an ``append_many`` member, its type spelled as a literal for the writer
    vocabulary check (``test_crew_log_types``)."""
    if copy.entry_type == _CHARGE_TYPE:
        return {"type": "work/charged", "data": copy.payload, "ignorable": False}
    return {"type": "work/recorded", "data": copy.payload, "ignorable": False}


def bound_workers() -> "dict[str, int]":
    """Every bound worker's slot key, with its binding file's mtime in ms.

    For the relay consumer's catch-up, which relays what a worker committed while bound.
    """
    import json

    from kiro_crew import work_ledger

    out: "dict[str, int]" = {}
    try:
        files = list(work_ledger.bindings_dir().glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            mtime_ms = path.stat().st_mtime_ns // 1_000_000
        except (OSError, ValueError):
            continue
        worker = raw.get("worker_slot_key") if isinstance(raw, dict) else None
        if isinstance(worker, str) and worker:
            out[worker] = mtime_ms
    return out


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


def _forget_sent(origin: str, board: str) -> None:
    """Drop (origin, board) from :data:`_RECENT`, for a copy that never landed."""
    with _recent_guard:
        _RECENT.pop((origin, board), None)


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
    """*unit_id*'s own slot as a work-ledger key, from its header; ``""`` when it has none.

    A header that cannot be read or proved raises ``OSError``: the relay consumer holds
    its cursor below that entry and reads it again, where ``""`` would pass it.
    """
    with _recent_guard:
        held = _UNIT_SLOTS.get(unit_id)
    if held is not None:
        return held
    from kiro_crew import session_ledger
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store_units import _unit_header_object

    parsed = _unit_header_object(KIND_SESSION, unit_id)
    if parsed is None:
        raise OSError(f"crew log header of {unit_id!r} cannot be proved")
    raw = parsed.get("slot") if isinstance(parsed.get("slot"), str) else ""
    slot = session_ledger.ledger_key(raw) if raw else ""
    if slot:
        with _recent_guard:
            _UNIT_SLOTS[unit_id] = slot
            while len(_UNIT_SLOTS) > _UNIT_SLOTS_LIMIT:
                _UNIT_SLOTS.popitem(last=False)
    return slot


def _bound_board(worker: str) -> str:
    """The board *worker* is bound to, or ``""``; re-read only when the file changed.

    A read that FAILED raises ``OSError`` rather than answering ``""``: the relay
    consumer holds its cursor below an entry it could not decide and reads it again,
    where "unbound" would let the cursor pass it and lose the copy.
    """
    from kiro_crew import work_ledger

    try:
        stat = work_ledger.binding_path(worker).stat()
        mark: "tuple[int, int, int] | None" = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        mark = None
    except work_ledger.WorkLedgerError:
        return ""
    with _recent_guard:
        held = _BINDINGS.get(worker)
    if held is not None and held[0] == mark:
        return held[1]
    # A read that fails raises past the cache below, so nothing is cached for it.
    binding = work_ledger.read_binding(worker, strict=True) if mark is not None else None
    board = str(binding[0]) if binding else ""
    with _recent_guard:
        _BINDINGS.pop(worker, None)
        _BINDINGS[worker] = (mark, board)
        while len(_BINDINGS) > _BINDINGS_LIMIT:
            _BINDINGS.popitem(last=False)
    return board


def _bound_since(worker: str) -> int:
    """When *worker*'s binding file was last written, in ms; 0 when not cached."""
    with _recent_guard:
        held = _BINDINGS.get(worker)
    mark = held[0] if held is not None else None
    return mark[1] // 1_000_000 if mark is not None else 0


def _charge_of(data: Mapping[str, Any]) -> "tuple[float | None, int] | None":
    """A closer's (credits, duration) as the project report reads them, or ``None``."""
    from kiro_crew.crew_log.projection_usage import _credit_charge
    from kiro_crew.crew_log.projection_values import _as_int

    credits = _credit_charge(data.get("credits"))
    measured = _as_int(data.get("duration_ms")) or _as_int(data.get("ms"))
    if credits is None and not measured:
        return None
    return credits, measured

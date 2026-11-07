"""The board relay's consumer: its own thread, a bounded queue and a durable cursor.

WHY IT EXISTS. Deciding whether an entry is copied to a board above its unit reads the
unit's header, the worker's binding file and the board's newest unit, and the first such
read in a process lists every unit header in the store. Every one of those syscalls
re-takes the GIL, and on a loaded host each re-take waits behind every busy thread in
the gateway. Inside the append job, on a writer lane, that is latency for every entry
queued behind it on the lane. So the append job only calls :func:`note` -- a membership
test and one deque append -- and this module's thread does the rest
(:func:`kiro_crew.crew_log.work_relay.resolve`).

WHAT IT KEEPS. The copy is written by the writer, as a follow-on of the board's unit, so
every log keeps one writer, and the copy publishes the board's ``work-unit-order`` from
the lane that holds the board's slot (``writer_groups``). Copies of one source unit are
queued in the order its entries committed, because one thread consumes the queue in
order.

THE CURSOR. Per source unit, the highest seq such that every entry at or below it is
SETTLED: decided as needing no copy, or copied and the copy landed. A copy the writer
gave up on, or a decision that raised, holds the cursor below it. The cursor is
persisted (``crew-log/relay/cursor.json``, inside the fenced crew-log tree, outside
every unit) at most every :data:`_PERSIST_SECONDS`, and only ever moves forward.

CATCH-UP. Entries this thread never decided are found again from the cursor:

* when the thread starts -- the first relayable entry a process commits, so a restart
  is caught up as soon as anything is relayed again, with no work on the boot path --
  and at the first wake in a different data home: for every unit whose slot has a
  binding file, from its cursor, or, for a unit with none, from the binding file's
  mtime (what was committed while bound);
* in process, for a unit whose wake did not fit in the queue: that unit's later wakes
  are not queued either, so its entries stay in seq order, and it is caught up from its
  cursor once the queue has room.

A catch-up copy is checked against the origins already in the board's own units first,
so one copy that landed before the cursor was persisted is not written twice. Readers
count a closer and a relayed record once anyway (``projection_workstreams``).

A FAILURE holds the cursor: an entry whose decision raised (a header, binding or log
read that failed), or a copy the writer could not write, keeps its unit's cursor below
it, and that unit is read again from there after :data:`_RETRY_SECONDS`. A copy the
store REFUSED is not a failure: the writer has dropped it under its loss-marker rule,
so it is settled as decided and never retried. An entry older than its worker's binding
file is never copied, whichever path decides it. Its later entries are not
decided meanwhile, so its copies stay in seq order -- except copies already handed to
the writer when an earlier copy's failure comes back, which land ahead of the retried
one. That needs the writer to have given up on a board unit (its attempt budget spent);
folds count each copy once either way.

shortcut: catch-up reads the CURRENT binding; a worker rebound between a crash and the
restart has its unrelayed entries copied to the new board. Record the board in the
cursor if that ever matters. Likewise a unit that has a cursor and was unbound for a
while is read from its cursor, so a closer it committed unbound and never decided (a
crash in that gap) is copied to the board it is bound to now.

THE FILE IS ADVISORY. A missing or unreadable cursor reads as empty, and a unit's cursor
past that unit's end is dropped: either way the unit's bound span is rescanned, and
every copy is checked against the origins already in the board, so a replay writes
nothing twice. The file is replaced whole (temp file, fsync, rename). One process per
data home writes it: the one holding ``crew-log/relay/relay.lock``. Another process
logs a warning and never writes the cursor; its copies go through the writer lease on
each board unit, which refuses a process that does not own that unit.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from typing import Any, Final, NamedTuple

logger = logging.getLogger(__name__)

#: Wakes held between the writer and this thread. Past it a unit falls BEHIND and is
#: caught up from its cursor, so a full queue costs lag, never a copy.
QUEUE_LIMIT: Final[int] = 4096

#: Longest a settled cursor waits in memory before it is written.
_PERSIST_SECONDS: Final[float] = 0.5

#: Source units the cursor file remembers, least recently moved forgotten first.
_CURSOR_LIMIT: Final[int] = 4096

#: How long an entry that could not be decided, or a copy that did not land, waits
#: before this thread reads its unit again from the cursor. shortcut: fixed, no backoff;
#: a wedged disk retries every interval until it recovers.
_RETRY_SECONDS: Final[float] = 5.0

#: Copies a catch-up decides before it hands them to the writer.
_CATCH_UP_BATCH: Final[int] = 256

_CURSOR_LEAF: Final[tuple[str, str]] = ("relay", "cursor.json")
_LOCK_LEAF: Final[tuple[str, str]] = ("relay", "relay.lock")


class _Wake(NamedTuple):
    home: str
    unit: str
    entry_type: str
    seq: int
    data: Mapping[str, Any]
    time_ms: int = 0


_cond = threading.Condition()
_queue: "deque[_Wake]" = deque()
_thread: "threading.Thread | None" = None
#: Units whose wakes overflowed, with the home they belong to.
_behind: "dict[str, str]" = {}
_queued = 0
_done = 0
_busy = False
_overflowed = 0

#: Everything below belongs to ONE data home (``_state_home``) and is reset on a change.
_state_home = ""
_persisted: "OrderedDict[str, int]" = OrderedDict()
_high: "dict[str, int]" = {}
_pending: "dict[str, set[int]]" = {}
_held: "dict[str, int]" = {}
_dirty = False
_last_persist = 0.0
#: Units holding the cursor below an entry, and when to read them again.
_retry_at: "dict[str, float]" = {}
#: Serializes snapshot-and-write, so an older snapshot is never written last.
_persist_lock = threading.Lock()
#: The open ``relay.lock`` this process holds for ``_state_home``, or ``None`` when
#: another process holds it (then this one never writes the cursor).
_relayer_fd: "int | None" = None


def note(unit: str, entry_type: str, seq: int, data: Mapping[str, Any], time_ms: int = 0) -> None:
    """Queue one committed relayable entry. Writer-thread time: never blocks or reads."""
    global _queued, _overflowed
    from kiro_crew.config.paths import data_home

    home = str(data_home())
    with _cond:
        if unit in _behind or len(_queue) >= QUEUE_LIMIT:
            _behind[unit] = home
            _overflowed += 1
        else:
            _queue.append(_Wake(home, unit, entry_type, seq, data, time_ms))
            _queued += 1
        _ensure_thread_locked()
        _cond.notify()


def overflowed() -> int:
    """Wakes that did not fit in the queue and were caught up from the cursor instead."""
    with _cond:
        return _overflowed


def idle() -> bool:
    """Whether nothing is queued, behind or being decided right now."""
    with _cond:
        return not (_queue or _behind or _busy) and _done >= _queued


def drain(timeout: float = 5.0) -> bool:
    """Wait until every queued wake is decided and every copy handed to the writer."""
    deadline = time.monotonic() + timeout
    with _cond:
        _cond.notify()
        while _queue or _behind or _busy or _done < _queued:
            left = deadline - time.monotonic()
            if left <= 0 or _thread is None:
                return not (_queue or _behind or _busy)
            _cond.wait(min(left, 0.05))
    return True


def persist() -> None:
    """Write the settled cursor now. The shutdown drain calls this after the writer's."""
    with _persist_lock:
        with _cond:
            snapshot = _cursor_snapshot_locked()
            home = _state_home
        if home:
            _write_cursor(home, snapshot)


def forget_for_tests() -> None:
    """Settle what is queued, then drop all state, so one test's relays cannot leak."""
    global _state_home, _dirty, _queued, _done, _overflowed
    drain(2.0)
    _release_relayer()
    with _cond:
        _queue.clear()
        _behind.clear()
        _queued = _done = _overflowed = 0
        _state_home = ""
        _persisted.clear()
        _high.clear()
        _pending.clear()
        _held.clear()
        _retry_at.clear()
        _dirty = False


# --------------------------------------------------------------------------- #
# The thread
# --------------------------------------------------------------------------- #


def _ensure_thread_locked() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_run, name="crew-log-relay", daemon=True)
    _thread.start()


def _run() -> None:
    global _busy, _done
    from kiro_crew.config.paths import data_home

    with _cond:
        _busy = True
    startup: "list[Any]" = []
    try:
        _enter_home(str(data_home()), startup)
    except Exception:  # pragma: no cover - a failed catch-up must not stop the consumer
        _log_exc("crew log relay catch-up failed")
    finally:
        _hand_over(startup)
        with _cond:
            _busy = False
            _cond.notify_all()
    while True:
        with _cond:
            while not (_queue or _behind or _persist_due_locked() or _retry_due_locked()):
                _cond.wait(_PERSIST_SECONDS)
            wakes = list(_queue)
            _queue.clear()
            behind = dict(_behind)
            _behind.clear()
            retries = _take_retries_locked()
            _busy = True
        out: "list[Any]" = []
        try:
            for wake in wakes:
                _enter_home(wake.home, out)
                if wake.home == _state_home and not _is_held(wake.unit):
                    # A held unit's later entries wait for its retry, which reads
                    # from the hold, so its copies stay in seq order.
                    _decide(
                        wake.unit, wake.entry_type, wake.seq, wake.data, out, time_ms=wake.time_ms
                    )
            origins: "dict[str, frozenset[str]]" = {}
            for unit, home in behind.items():
                _enter_home(home, out)
                if home == _state_home:
                    _catch_up([unit], since=None, out=out, origins=origins)
            if retries:
                _catch_up(list(retries), since=None, out=out, origins=origins, starts=retries)
        except Exception:  # pragma: no cover - the consumer must outlive one bad entry
            _log_exc("crew log relay pass failed")
        finally:
            _hand_over(out)
            _persist_if_due()
            with _cond:
                _busy = False
                _done += len(wakes)
                _cond.notify_all()


def _enter_home(home: str, out: "list[Any]") -> None:
    """Make *home* the one this thread's state belongs to; catch up when it is new."""
    global _state_home, _dirty
    if home == _state_home:
        return
    _hand_over(out)
    _claim_relayer(home)
    with _cond:
        _state_home = home
        _persisted.clear()
        _persisted.update(_read_cursor(home))
        _high.clear()
        _pending.clear()
        _held.clear()
        _retry_at.clear()
        _dirty = False
    _catch_up(None, since=_bindings(), out=out)


def _hand_over(out: "list[Any]") -> None:
    """Queue the copies a pass decided, and settle each one the writer would not take."""
    from kiro_crew.crew_log import work_relay

    if not out:
        return
    try:
        work_relay.hand_over(out)
    except Exception:
        _log_exc("crew log relay copies not handed to the writer")
        for copy in out:
            if copy.settle is not None:
                copy.settle(False)
    out.clear()


# --------------------------------------------------------------------------- #
# Deciding one entry, and what that does to the cursor
# --------------------------------------------------------------------------- #


def _decide(
    unit: str,
    entry_type: str,
    seq: int,
    data: Mapping[str, Any],
    out: "list[Any]",
    seen: "Any" = None,
    *,
    time_ms: int = 0,
) -> None:
    from kiro_crew.crew_log import work_relay

    try:
        bound = work_relay.resolve(
            seq,
            entry_type,
            unit,
            data,
            out=out,
            ticket=lambda: _ticket(unit, seq),
            seen=seen,
            time_ms=time_ms,
        )
        failed = False
    except Exception:
        _log_exc(f"crew log relay of {entry_type} {unit}:{seq} not decided")
        bound = failed = True
    with _cond:
        if not bound and unit not in _high:
            # An unbound unit's entries need no copy and no cursor: a catch-up only
            # reads bound units, and one bound later starts at its binding's mtime.
            return
        _high[unit] = max(_high.get(unit, 0), seq)
        if failed:
            _hold_locked(unit, seq)
        _mark_dirty_locked()


def _ticket(unit: str, seq: int) -> "Any":
    """Hold *unit*'s cursor below *seq* until its copy's outcome is known."""
    with _cond:
        _pending.setdefault(unit, set()).add(seq)

    def settle(landed: bool) -> None:
        with _cond:
            owed = _pending.get(unit)
            if owed is not None:
                owed.discard(seq)
                if not owed:
                    _pending.pop(unit, None)
            if not landed:
                _hold_locked(unit, seq)
            _mark_dirty_locked()

    return settle


def _is_held(unit: str) -> bool:
    with _cond:
        return unit in _held


def _hold_locked(unit: str, seq: int) -> None:
    """Hold *unit*'s cursor below *seq*, and read the unit again after a while."""
    _held[unit] = min(_held.get(unit, seq), seq)
    _retry_at.setdefault(unit, time.monotonic() + _RETRY_SECONDS)


def _retry_due_locked() -> bool:
    now = time.monotonic()
    return any(at <= now for at in _retry_at.values())


def _take_retries_locked() -> "dict[str, int]":
    """Due retries as {unit: the seq to read after}, their holds released.

    The start is read WITH the hold, so the entry that held it is read again; the hold
    goes, so a retry that succeeds lets the cursor move on.
    """
    now = time.monotonic()
    due = [unit for unit, at in _retry_at.items() if at <= now]
    starts: "dict[str, int]" = {}
    for unit in due:
        _retry_at.pop(unit, None)
        starts[unit] = _settled_locked(unit)
        _held.pop(unit, None)
    return starts


def _settled_locked(unit: str) -> int:
    """*unit*'s cursor as this process knows it: every entry at or below is settled."""
    point = _high.get(unit, 0)
    owed = _pending.get(unit)
    if owed:
        point = min(point, min(owed) - 1)
    if unit in _held:
        point = min(point, _held[unit] - 1)
    return max(point, _persisted.get(unit, 0))


def _cursor_snapshot_locked() -> "dict[str, int]":
    for unit in list(_high):
        value = _settled_locked(unit)
        if value > _persisted.get(unit, 0):
            _persisted.pop(unit, None)
            _persisted[unit] = value
    while len(_persisted) > _CURSOR_LIMIT:
        _persisted.popitem(last=False)
    return dict(_persisted)


def _mark_dirty_locked() -> None:
    global _dirty
    _dirty = True


def _persist_due_locked() -> bool:
    return _dirty and time.monotonic() - _last_persist >= _PERSIST_SECONDS


def _persist_if_due() -> None:
    global _dirty, _last_persist
    with _persist_lock:
        with _cond:
            if not _persist_due_locked():
                return
            snapshot = _cursor_snapshot_locked()
            home = _state_home
            _dirty = False
            _last_persist = time.monotonic()
        _write_cursor(home, snapshot)


# --------------------------------------------------------------------------- #
# Catch-up
# --------------------------------------------------------------------------- #


def _catch_up(
    units: "Iterable[str] | None",
    *,
    since: "dict[str, int] | None",
    out: "list[Any]",
    origins: "dict[str, frozenset[str]] | None" = None,
    starts: "Mapping[str, int] | None" = None,
) -> None:
    """Decide again what *units* committed past their cursor; ``None`` is every bound unit.

    *since* maps a bound slot to its binding's mtime in ms: a unit with no cursor starts
    at the first entry written after it. ``None`` reads the bindings when needed.
    *origins* caches each board's copy origins across calls of one pass; *starts*
    overrides the cursor a unit is read after (a retry, whose hold is already released).
    """
    from kiro_crew import session_ledger
    from kiro_crew.crew_log import work_relay
    from kiro_crew.crew_log.projection_session_log import open_session_log

    bound = since if since is not None else _bindings()
    if units is None:
        if not bound:
            return
        from kiro_crew.crew_log.store import session_units_by_slot

        units = [
            unit
            for slot, members in session_units_by_slot().items()
            if session_ledger.ledger_key(slot) in bound
            for unit in members
        ]
    cache: "dict[str, frozenset[str]]" = {} if origins is None else origins

    def seen(board: str) -> "frozenset[str]":
        if board not in cache:
            cache[board] = _origins_in(board)
        return cache[board]

    for unit in units:
        if (starts is None or unit not in starts) and _is_held(unit):
            continue  # its retry reads it from the hold
        with _cond:
            known = unit in _high or unit in _persisted or (starts is not None and unit in starts)
            start = _settled_locked(unit) if known else 0
            if starts is not None and unit in starts:
                start = starts[unit]
        floor = 0
        if not known:
            try:
                slot = work_relay._unit_slot(unit)
            except OSError:
                _log_exc(f"crew log relay catch-up could not read the header of {unit}")
                with _cond:
                    _hold_locked(unit, 1)
                    _mark_dirty_locked()
                continue
            if slot not in bound:
                continue
            floor = bound[slot]
        last = start
        try:
            log = open_session_log(unit)
            if log is not None and start > log.last_seq:
                # A cursor past the unit's end is stale (the unit was replaced): it is
                # advisory, so drop it and rescan the bound span, deduped by origin.
                logger.warning("crew log relay cursor for %s is past its end; rescanning", unit)
                with _cond:
                    _persisted.pop(unit, None)
                    _high.pop(unit, None)
                start = last = 0
                slot = work_relay._unit_slot(unit)
                floor = bound.get(slot, 0)
            # Streamed, not listed: a long worker log is read once, never held whole.
            for entry in [] if log is None else log.iter_from(start + 1):
                last = entry.seq
                if not work_relay.relayable(entry.type) or int(entry.time) < floor:
                    continue
                _decide(unit, entry.type, entry.seq, entry.data, out, seen, time_ms=int(entry.time))
                if _is_held(unit):
                    break
                if len(out) >= _CATCH_UP_BATCH:
                    _hand_over(out)
        except Exception:
            _log_exc(f"crew log relay catch-up could not read {unit}")
            # What was not read stays undecided: hold the cursor at it and read again.
            with _cond:
                _hold_locked(unit, last + 1)
                _mark_dirty_locked()
            continue
        if last > start:
            with _cond:
                _high[unit] = max(_high.get(unit, 0), last)
                _mark_dirty_locked()


def _origins_in(board: str) -> "frozenset[str]":
    """Every copy origin already in *board*'s units: charge closers and relayed records."""
    from kiro_crew.crew_log.projection_session_log import open_session_log
    from kiro_crew.crew_log.store import session_units_for_slot

    found: set[str] = set()
    for unit in session_units_for_slot(board):
        log = open_session_log(unit)
        if log is None:
            continue
        for entry in log.iter_from(1):
            if entry.type == "work/charged":
                found.add(str(entry.data.get("closer") or ""))
            elif entry.type == "work/recorded" and entry.data.get("relay"):
                found.add(str(entry.data.get("relay")))
    found.discard("")
    return frozenset(found)


def _bindings() -> "dict[str, int]":
    from kiro_crew.crew_log import work_relay

    return work_relay.bound_workers()


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #


def _claim_relayer(home: str) -> None:
    """Take ``relay.lock`` for *home*, or log that another process relays here.

    One relayer per data home. Its copies already go through the writer lease on
    each board unit, so a second process's copies are refused there; this lock is
    what keeps the second one from rewriting the first one's cursor. A process that
    does not get it still decides and queues (the lease refuses what it may not
    write) but never writes the cursor, and says so once.
    """
    global _relayer_fd
    import os

    from kiro_crew.platform_lock_compat import try_acquire_lock_or_raise

    _release_relayer()
    path = _cursor_path(home).with_name(_LOCK_LEAF[1])
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        _log_exc("crew log relay lock not opened; the cursor will not be written")
        return
    try:
        held = try_acquire_lock_or_raise(fd, exclusive=True)
    except OSError:
        held = False
    if not held:
        os.close(fd)
        logger.warning(
            "crew log relay: another process holds %s, so this process does not "
            "write the relay cursor; one relaying process per data home is expected",
            path,
        )
        return
    _relayer_fd = fd


def _release_relayer() -> None:
    global _relayer_fd
    import os

    from kiro_crew.platform_lock_compat import release_lock

    fd, _relayer_fd = _relayer_fd, None
    if fd is None:
        return
    try:
        release_lock(fd)
    except OSError:
        pass
    os.close(fd)


def _cursor_path(home: str) -> Any:
    from pathlib import Path

    from kiro_crew.crew_log.store_paths import _ROOT_LEAF

    return Path(home, _ROOT_LEAF, *_CURSOR_LEAF)


def _read_cursor(home: str) -> "dict[str, int]":
    try:
        raw = json.loads(_cursor_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    units = raw.get("units") if isinstance(raw, dict) else None
    if not isinstance(units, dict):
        return {}
    return {
        str(unit): seq
        for unit, seq in units.items()
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0
    }


def _write_cursor(home: str, units: "dict[str, int]") -> None:
    """Replace the cursor file whole: temp file, fsync, rename (``atomic_write``)."""
    from kiro_crew.atomic_write import atomic_write

    if _relayer_fd is None:
        return  # another process is the relayer for this home: its cursor stands
    try:
        atomic_write(
            _cursor_path(home),
            json.dumps({"schema": 1, "units": units}, separators=(",", ":")),
            fsync=True,
            restrict_to_owner=True,
        )
    except Exception:
        # Any failure, not only ``OSError``: this runs in the consumer's ``finally``,
        # and a cursor not written costs a longer catch-up, never the thread.
        _log_exc("crew log relay cursor not written")


def _log_exc(message: str) -> None:
    """Log with the traceback rendered to text, never kept: see ``eager._log_exc``."""
    try:
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(logger, logging.DEBUG, message)
    except Exception:  # pragma: no cover - logging must never be the failure
        logger.debug(message)

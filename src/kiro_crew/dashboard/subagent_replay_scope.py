"""What the Subagents panel's durable replay reads, and how often it may run.

The durable half of the ``subscribe_subagents`` replay folds crew-log units. Three
rules keep that work proportional to what a socket shows instead of to the size of
the data home:

1. **Scope.** Only the slots a socket names are replayed: the ``slots`` list a
   client sends with ``subscribe_subagents`` or ``subagent_replay``, plus each slot
   it later focuses. Each slot is replayed at most once per subscription.
2. **Skip units with no subagent entry.** :class:`SubagentUnitIndex` answers "can
   this unit hold a subagent row" from the raw bytes, without a fold. A unit is read
   whole once per process; after that only the bytes appended since are read, so an
   unchanged unit costs a directory listing and a stat per segment.
3. **One replay per socket at a time.** :class:`SubagentReplayQueue` runs the
   replays for one socket on one background task, so the socket's receive loop
   never waits on a fold and a re-subscribe or a focus change while a replay runs
   is queued behind it instead of starting a second one beside it.

This module owns no permission decision. Every frame the replay produces still
goes through the socket's ownership and scope gates in ``ws.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Every subagent entry type starts with this, and the serialized record carries
#: its type as a JSON string, so a unit whose bytes never contain the needle holds
#: no subagent entry. A match elsewhere (a task text quoting it) only costs a fold.
NEEDLE = b'"subagent/'

#: The most slots one request may name. A dashboard names the slot it shows plus
#: a few split panes or a session's children; anything past this is ignored.
MAX_REQUEST_SLOTS = 256

#: Upper bound on remembered units, so a process that sees many short-lived
#: sessions keeps a bounded table. Evicted units are simply read again once.
_MAX_UNITS = 16_384

_READ_CHUNK = 1 << 20

#: Bytes kept from the end of what was read of a segment. A segment only grows, but
#: a tail repair truncates a damaged last line and the next append can carry the
#: file past its old size on the same inode. A mismatch in these bytes marks that
#: case, and the segment is then read whole again.
_TAIL_BYTES = 64

#: Most slots one socket's subscription remembers as replayed; past it, new names
#: are ignored until the next subscribe.
MAX_SUBSCRIPTION_SLOTS = 4096

#: How long an "owner unreadable" answer is reused for an unchanged unit before the
#: class is read again. The answer is usually permanent (a log that never recorded a
#: class), but the same ``None`` also covers a read that failed once, so it is not
#: kept for good: an unchanged unit then costs at most one fold per this many seconds.
UNOWNED_RETRY_SECS = 300.0


@dataclass
class _UnitState:
    """What has been read of one unit: per segment, its inode, bytes read, and the
    last :data:`_TAIL_BYTES` of them (so a rewrite in place is noticed)."""

    segments: dict[str, tuple[int, int, bytes]] = field(default_factory=dict)
    hit: bool = False


def _tail(path: Path, end: int) -> bytes:
    """The last :data:`_TAIL_BYTES` bytes of *path* before offset *end*."""
    start = max(0, end - _TAIL_BYTES)
    with open(path, "rb") as handle:
        handle.seek(start)
        return handle.read(end - start)


def _contains_needle(path: Path, start: int, end: int) -> bool:
    """Whether bytes ``[start, end)`` of *path* contain :data:`NEEDLE`.

    Read in chunks with an overlap of ``len(NEEDLE) - 1`` bytes, so a needle split
    across two chunks is still found.
    """
    overlap = len(NEEDLE) - 1
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = end - start
        carry = b""
        while remaining > 0:
            block = handle.read(min(_READ_CHUNK, remaining))
            if not block:
                break
            remaining -= len(block)
            window = carry + block
            if NEEDLE in window:
                return True
            carry = window[-overlap:]
    return False


class SubagentUnitIndex:
    """Per-process answer to "can this crew-log unit hold a subagent row".

    :meth:`peek` never reads a whole unit. It answers from what an earlier read of
    the unit found plus the bytes appended since, and returns ``None`` for a unit it
    has not read yet. :meth:`scan` reads a unit whole, and does it at most once per
    unit per process: a second caller for the same unit waits for the first one's
    answer instead of reading again.

    A positive answer is kept for good. A segment is append-only and retention only
    deletes whole segments off the front, and a unit whose front is gone answers
    ``False`` anyway: the replay's class read refuses a log that does not hold its
    own beginning, so no row of it could be drawn.
    """

    def __init__(self, *, max_units: int = _MAX_UNITS) -> None:
        self._lock = threading.Lock()
        self._units: OrderedDict[str, _UnitState] = OrderedDict()
        self._scanning: dict[str, threading.Event] = {}
        self._max_units = max_units
        #: Whole-unit reads done, for tests and benchmarks.
        self.scans = 0

    def clear(self) -> None:
        with self._lock:
            self._units.clear()
            self.scans = 0

    @staticmethod
    def _segments(unit_id: str) -> list[Path] | None:
        """The unit's segments, or ``None`` when its log does not start at seq 1."""
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store_paths import LOG_FILE, segment_paths

        paths = segment_paths(KIND_SESSION, unit_id)
        if not paths or paths[0].name != LOG_FILE:
            return None
        return paths

    @staticmethod
    def _key(unit_id: str) -> str:
        """The unit's directory: unit ids repeat across data homes, paths do not."""
        from kiro_crew.crew_log.schema import KIND_SESSION
        from kiro_crew.crew_log.store_paths import crew_log_dir

        return str(crew_log_dir(KIND_SESSION, unit_id))

    def _remember(self, unit_id: str, state: _UnitState) -> None:
        key = self._key(unit_id)
        with self._lock:
            self._units[key] = state
            self._units.move_to_end(key)
            while len(self._units) > self._max_units:
                self._units.popitem(last=False)

    def peek(self, unit_id: str) -> bool | None:
        """``True``/``False`` without reading the whole unit, or ``None`` if unread.

        Reads only bytes appended since the last read. A segment that was replaced
        (new inode), shrank, or whose last-read bytes changed is treated as unread,
        so it goes back to :meth:`scan`.
        Blocking; call it off the event loop.
        """
        paths = self._segments(unit_id)
        if paths is None:
            return False
        with self._lock:
            prior = self._units.get(self._key(unit_id))
        if prior is None:
            return None
        if prior.hit:
            return True
        segments = dict(prior.segments)
        hit = False
        for path in paths:
            stat = path.stat()
            seen = segments.get(path.name)
            if seen is not None and (seen[0] != stat.st_ino or stat.st_size < seen[1]):
                return None
            # The tail is checked before the "same size" shortcut: a rollback can
            # truncate a segment and regrow it to exactly its old size.
            if seen is not None and _tail(path, seen[1]) != seen[2]:
                return None
            if seen is not None and stat.st_size == seen[1]:
                continue
            start = 0 if seen is None else max(0, seen[1] - (len(NEEDLE) - 1))
            if stat.st_size > start and _contains_needle(path, start, stat.st_size):
                hit = True
            segments[path.name] = (stat.st_ino, stat.st_size, _tail(path, stat.st_size))
            if hit:
                break
        self._remember(unit_id, _UnitState(segments=segments, hit=hit))
        return hit

    def scan(self, unit_id: str) -> bool:
        """Read the unit whole, once per process, and remember what it holds.

        A concurrent caller for the same unit waits for the running read and then
        answers from it. Blocking; call it off the event loop.
        """
        key = self._key(unit_id)
        while True:
            with self._lock:
                running = self._scanning.get(key)
                if running is None:
                    done = threading.Event()
                    self._scanning[key] = done
                    break
            running.wait()
            answer = self.peek(unit_id)
            if answer is not None:
                return answer
        try:
            answer = self.peek(unit_id)
            if answer is not None:
                return answer
            paths = self._segments(unit_id) or []
            segments: dict[str, tuple[int, int, bytes]] = {}
            hit = False
            for path in paths:
                stat = path.stat()
                segments[path.name] = (stat.st_ino, stat.st_size, _tail(path, stat.st_size))
                if not hit and stat.st_size and _contains_needle(path, 0, stat.st_size):
                    hit = True
            with self._lock:
                self.scans += 1
            self._remember(unit_id, _UnitState(segments=segments, hit=hit))
            return hit
        finally:
            with self._lock:
                self._scanning.pop(key, None)
            done.set()

    def holding_subagents(self, pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        """The ``(slot, unit)`` pairs whose unit can hold a subagent row.

        A unit that cannot be looked at is KEPT: the fold that follows decides it the
        way it always did, so an unreadable directory never hides a card.
        """
        kept: list[tuple[str, str]] = []
        for slot, unit_id in pairs:
            if not unit_id:
                continue
            try:
                answer = self.peek(unit_id)
                if answer is None:
                    answer = self.scan(unit_id)
            except OSError:
                logger.debug("subagent replay: could not read %s; folding it", unit_id)
                answer = True
            if answer:
                kept.append((slot, unit_id))
        return kept


#: The process's one index, shared by every socket.
UNIT_INDEX = SubagentUnitIndex()


def _unit_identity(unit_id: str) -> tuple[str, tuple | None]:
    """``(directory, identity)`` for one unit, from stat calls alone.

    The identity is the crew log's own (``_unit_log_identity``: header creation
    stamp, device and inode, summed segment size and newest mtime), so this cache
    and the session fold agree on what "the same log" means. ``None`` is unknown
    and is never cached.
    """
    from kiro_crew.crew_log.projection_session_log import _unit_log_identity
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store_paths import crew_log_dir

    directory = str(crew_log_dir(KIND_SESSION, unit_id))
    identity = _unit_log_identity(KIND_SESSION, unit_id)
    return directory, (identity if identity[0] is not None else None)


class UnitRowsCache:
    """The ``subagents`` rows and recorded owner of a unit, cached by its bytes.

    The two folds the replay needs per unit (``recorded_class`` and the
    ``subagents`` projection) are pure functions of the unit's log, so their answer
    is kept under :func:`_unit_identity`. A reconnect over an unchanged unit then
    costs a listing and a stat instead of a fold.

    Shared by every socket: a caller that finds another thread folding the same
    unit waits for that fold and uses its answer, so N sockets reconnecting at once
    fold each unit once.

    An unreadable owner is cached for :data:`UNOWNED_RETRY_SECS` only.
    ``recorded_class`` answers ``None`` both for a log that will never say who owns
    it and for a read that failed this once; kept for good, a one-off failure would
    hide the unit's cards until it next changed, and not kept at all, a log with no
    class record would be folded on every connect.
    """

    def __init__(self, *, max_units: int = _MAX_UNITS) -> None:
        self._lock = threading.Lock()
        self._rows: OrderedDict[str, tuple[tuple, str | None, dict, float]] = OrderedDict()
        self._folding: dict[str, threading.Event] = {}
        self._max_units = max_units
        #: Folds actually run, for tests and benchmarks.
        self.folds = 0

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
            self.folds = 0

    def _cached(self, directory: str, identity: tuple) -> tuple[str | None, dict] | None:
        with self._lock:
            held = self._rows.get(directory)
            if held is None or held[0] != identity:
                return None
            if held[1] is None and time.monotonic() - held[3] >= UNOWNED_RETRY_SECS:
                return None
            self._rows.move_to_end(directory)
            return held[1], held[2]

    def _fold(self, unit_id: str) -> tuple[str | None, dict]:
        """The owner and ``subagents`` rows, folded now."""
        from kiro_crew.crew_log import projection as _projection
        from kiro_crew.crew_log import read as _crew_log_read

        recorded = _crew_log_read.recorded_class(unit_id)
        rows = (
            _projection.fold_session(unit_id, ("subagents",))
            .projection("subagents")
            .value.get("by_id")
            or {}
        )
        with self._lock:
            self.folds += 1
        return (None if recorded is None else str(recorded.get("app") or "")), rows

    def rows(self, unit_id: str) -> tuple[str | None, dict]:
        """``(owner app or None, rows by agent id)`` for *unit_id*.

        ``None`` for the owner means the log's class history cannot be trusted; the
        rows are still returned for the dismissal backfill, which does not route a
        card. A unit whose identity cannot be read is folded and not cached. Raises
        what the folds raise. Blocking; call it off the event loop.
        """
        directory, identity = _unit_identity(unit_id)
        while True:
            if identity is None:
                return self._fold(unit_id)
            hit = self._cached(directory, identity)
            if hit is not None:
                return hit
            with self._lock:
                running = self._folding.get(directory)
                if running is None:
                    done = threading.Event()
                    self._folding[directory] = done
                    break
            running.wait()
            directory, identity = _unit_identity(unit_id)
        try:
            app, rows = self._fold(unit_id)
            with self._lock:
                self._rows[directory] = (identity, app, rows, time.monotonic())
                self._rows.move_to_end(directory)
                while len(self._rows) > self._max_units:
                    self._rows.popitem(last=False)
            return app, rows
        finally:
            with self._lock:
                self._folding.pop(directory, None)
            done.set()


#: The process's one row cache, shared by every socket.
UNIT_ROWS = UnitRowsCache()


def live_subagent_ids(state: Any) -> set[str]:
    """Ids of the subagents gateway memory holds: native slot runs and managed runs.

    Read on the event loop. A durable replay excludes these, so a card drawn from
    memory is never replaced by a folded one.
    """
    ids: set[str] = set()
    try:
        for native in state.native_subagent_snapshots():
            if isinstance(native, dict) and native.get("id"):
                ids.add(str(native["id"]))
    except Exception:
        logger.debug("subagent replay: native snapshot read failed", exc_info=True)
    manager = getattr(state, "subagents", None)
    if manager:
        ids.update(str(agent.id) for agent in manager.all_agents)
    return ids


def requested_slots(value: object) -> list[str] | None:
    """The slot names a client frame asked for, or ``None`` when it named none.

    Only non-empty strings of a sane length count, deduplicated in order, and at
    most :data:`MAX_REQUEST_SLOTS` of them.
    """
    if not isinstance(value, list):
        return None
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 512 or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= MAX_REQUEST_SLOTS:
            break
    return out


class SubagentReplayQueue:
    """Runs one socket's durable replays one at a time, on one background task.

    :meth:`request` adds the slots not yet replayed in this subscription and starts
    the task if it is idle. A request that arrives while a replay runs is merged
    into the next batch, so at most one replay per socket is ever in flight.
    """

    def __init__(
        self,
        run: Callable[[list[str]], Awaitable[None]],
        admit: Callable[[str], bool] = lambda _slot: True,
    ) -> None:
        self._run = run
        #: Whether a slot name may be kept at all: the caller passes "is an open
        #: slot this socket may see", so a client cannot make the queue hold names
        #: that are not real slots.
        self._admit = admit
        #: Names refused because the subscription already holds
        #: :data:`MAX_SUBSCRIPTION_SLOTS`, for the operator and for tests.
        self.dropped = 0
        self._done: set[str] = set()
        self._pending: list[str] = []
        self._task: asyncio.Task | None = None
        self._active = True
        #: Batches started, for tests.
        self.batches = 0

    def reset(self, run: Callable[[list[str]], Awaitable[None]]) -> None:
        """A new subscription: new runner, and every slot may be replayed again.

        A replay already running finishes first; the next batch uses *run*.
        """
        self._run = run
        self._active = True
        self.dropped = 0
        self._done.clear()
        self._pending.clear()

    @property
    def active(self) -> bool:
        """Whether the subscription this queue serves is still on."""
        return self._active

    def forget(self, slots: Iterable[str]) -> None:
        """Let *slots* be requested again: their replay found nothing to read yet
        (the slot was not open) or failed, which is not a replay done."""
        for slot in slots:
            self._done.discard(slot)

    def clear(self) -> None:
        """Drop queued slots and take no more (the client unsubscribed).

        A running batch finishes. :meth:`reset` makes the queue take requests again.
        """
        self._active = False
        self._pending.clear()

    def request(self, slots: Iterable[str]) -> None:
        if not self._active:
            return
        for slot in slots:
            if not slot or slot in self._done or not self._admit(slot):
                continue
            if len(self._done) >= MAX_SUBSCRIPTION_SLOTS:
                if not self.dropped:
                    logger.warning(
                        "subagent replay: a subscription named more than %d slots; "
                        "the rest are not replayed until it subscribes again",
                        MAX_SUBSCRIPTION_SLOTS,
                    )
                self.dropped += 1
                continue
            self._done.add(slot)
            self._pending.append(slot)
        if self._pending and (self._task is None or self._task.done()):
            self._task = asyncio.ensure_future(self._drain())

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _drain(self) -> None:
        while self._pending:
            batch, self._pending = self._pending, []
            self.batches += 1
            try:
                await self._run(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("subagent replay batch failed", exc_info=True)
                self.forget(batch)

    async def wait_idle(self) -> None:
        """Wait for the queued batches to finish (tests)."""
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)

    def cancel(self) -> None:
        self._pending.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()

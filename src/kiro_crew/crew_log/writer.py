"""The durable writer behind the session crew log: buffer, order, retry, admit loss.

See ``docs/system-specs/modules/crew-log-emitter.md`` for the contract. The emitter
(:mod:`kiro_crew.crew_log.emit`) decides WHAT is recorded and builds each append as a
job; this module decides HOW it reaches the file, and nothing here knows an entry type
other than the ``write/dropped`` loss marker it authors itself.

Interface -- a caller learns these and nothing else:

* :class:`CrewLogWriter` ``(open_unit, *, executor, clock, sleep, limits, retry_delay,
  on_grew, warnings, logger)``, with :meth:`~CrewLogWriter.submit`,
  :meth:`~CrewLogWriter.flush`, :meth:`~CrewLogWriter.drain_for_shutdown`,
  :meth:`~CrewLogWriter.stats`, :meth:`~CrewLogWriter.owes`,
  :meth:`~CrewLogWriter.forget` and :meth:`~CrewLogWriter.close`.
* :class:`WriteJob`, built only through its named constructors -- ``append``,
  ``opening``, ``follow_on`` -- each of which selects one row of the policy table
  below, so a caller names what a job IS rather than which flags it needs.
* :class:`WarningBudget`, the rate-limited failure report the writer and the emitter
  share.

Two rules govern the writer, and neither gives way to the other: a lifecycle record is
not dropped merely for crossing the BACKPRESSURE high-water mark, because a hole in an
append-only log is permanent and silent, and the event loop is never BLOCKED, because a
turn must not wait on a disk. So a producer appends to an in-memory buffer and returns,
and one worker drains it in batches. The buffer has hard count and byte ceilings to
prevent an OOM from losing every session's debt; records refused at those ceilings are
counted separately (``WriterStats.overflowed``) and handed BACK to the caller, which
settles them -- :meth:`CrewLogWriter.submit` returns ``False``. Fail-soft governs
ERRORS, not backpressure.

A failed append is RETAINED, not swallowed. The batch goes back to the front of its
unit's bucket, every later write for that unit queues behind it, and the writer
retries with a short doubling backoff. Bounded, because entries live in memory until
they are written: past ``max_write_attempts`` consecutive failed passes the batch is
dropped and counted (``WriterStats.dropped``), so a wedged disk becomes a reported loss
instead of a wait no bounded caller can finish. A REFUSAL -- a ``CrewLogError``,
decided before any byte is written -- is not retried at all: it would be refused
identically every time, and retrying it would hold that unit's whole log behind one
entry that can never land.

Every loss is admitted before that unit appends again: the writer owes the unit a
``write/dropped`` marker and writes it at the head of the unit's next batch, through
``open_unit``. No entry is ever appended after a loss until a marker naming that loss
has been appended -- with one exception, a refusal on the inline path, which is counted
but owes no marker (:data:`_INLINE_REFUSAL_OWES_MARKER`).

With no event loop running the write happens inline on the calling thread -- which is
what makes a synchronous caller, and the test suite, deterministic. :meth:`flush` waits
for the buffers to drain when a caller needs the file on disk before it looks, and
:meth:`drain_for_shutdown` is the quiescence barrier a restart needs: buffered entries
are in memory, so an exit that skips it loses the last thing each unit did.

Adapters at the two seams:

* ``executor`` -- production passes a factory for the one-worker crew-log pool
  (``executors.crew_log_executor``); ``None`` runs every pass on the caller's thread
  inside :meth:`flush`, paced by the injected ``clock`` and ``sleep``, so a test drives
  the whole retry schedule without waiting on a real one.
* ``open_unit`` -- production passes the emitter's handle cache; a test passes an opener
  backed by a real ``CrewLog`` on a temporary data home, or one that injects faults.

Imported lazily by the emitter, never at its import: the emitter is reachable from the
gateway boot path, and a launch with the crew log switched off must not load this.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover -- typing only; the store stays unloaded at import
    from kiro_crew.crew_log.store import CrewLog

_logger = logging.getLogger(__name__)

#: The ``src`` a loss marker is written under: the gateway decided it, the ACP stream
#: did not report it.
_SRC_GATEWAY: Final[str] = "gateway"


@dataclass(frozen=True)
class WriterLimits:
    """Every bound and pace the writer runs on. One frozen value per writer.

    ``batch_deadline_seconds``: how long the writer pauses before a drain pass, so one
    pass takes a turn's burst rather than waking per entry. Fixed, so the worst case a
    producer can impose on the file is a constant.

    ``pending_high_water``: buffered appends past which the backlog is reported once.
    NOT a cap: nothing is dropped for crossing it. It exists so a filesystem that has
    stopped keeping up shows up in the log as memory pressure instead of silently
    accumulating.

    ``max_pending_count`` / ``max_pending_bytes``: the hard ceilings the buffer is
    bounded by -- a count of appends and a byte total of their bodies, either of which
    caps memory. They sit far above the high-water mark so a gateway keeping up under
    real load never reaches them; crossing one means the writer has fallen so far behind
    that the backlog is a memory-exhaustion risk to the whole process, and losing the
    newest entries of one overwhelmed buffer is the smaller failure than an OOM that
    takes every unit's unwritten entries with it, uncounted. The overflow is rejected at
    the buffer's tail rather than shed from its head: the entries already queued keep
    their order and their prefix of the log intact, and the loss is the log's tail
    stopping at a named, counted point instead of its middle silently disagreeing with
    causality.

    ``write_stall_secs``: how long one write may take before the writer says so. A job
    past this is not failed -- it may still land -- but nothing else for that unit can
    proceed, so silence here is what a stuck filesystem looks like from the outside.

    ``inline_order_seconds``: how long a synchronous caller waits for an in-flight
    writer batch before it hands its job over instead of writing inline. Bounded because
    the caller is a real thread doing real work, and generous because the alternative --
    writing beside a claimed batch -- reorders the file rather than merely delaying it.

    ``min_shutdown_retry_gap``: floor on the gap between shutdown retry passes. Without
    it a budget already nearly spent would spin the remaining attempts away in
    microseconds, which is how a filesystem that needed a moment becomes a permanent
    hole in the log.

    ``second_chance_drain_seconds``: budget for the second inline attempt, after the
    timed wait on the writer has already spent the caller's timeout. Short on purpose:
    exit must not be delayed twice over for the same batch.

    ``retry_backoff_seconds`` / ``retry_backoff_max_seconds``: how long the writer waits
    before retrying a batch whose append raised, and the ceiling that wait doubles to.
    The errors worth retrying are the transient ones -- an ENOSPC a rotation clears, an
    EIO on a network mount -- which resolve on a timescale a short backoff covers. The
    wait is per UNIT, so one wedged crew log paces only itself and another unit's
    entries keep flowing.

    ``max_write_attempts``: how many consecutive failed passes a unit's owed batch
    survives before it is DROPPED and counted. There has to be such a number: entries
    live in memory until they are written, so a filesystem that never answers would
    otherwise hold them forever and every bounded caller -- :meth:`CrewLogWriter.flush`,
    :meth:`CrewLogWriter.drain_for_shutdown` -- would time out instead of returning. A
    loss that is counted and named can be investigated; a hang cannot.
    """

    batch_deadline_seconds: float = 0.02
    pending_high_water: int = 4096
    max_pending_count: int = 100_000
    max_pending_bytes: int = 256 * 1024 * 1024
    write_stall_secs: float = 30.0
    inline_order_seconds: float = 5.0
    min_shutdown_retry_gap: float = 0.01
    second_chance_drain_seconds: float = 0.5
    retry_backoff_seconds: float = 0.05
    retry_backoff_max_seconds: float = 2.0
    max_write_attempts: int = 6

    def retry_delay(self, attempts: int) -> float:
        """How long to wait before retry number *attempts* + 1. Seconds.

        The SCHEDULE is one named thing: a retained batch's next attempt is due this
        long after its last one failed, doubling from ``retry_backoff_seconds`` to
        ``retry_backoff_max_seconds``.
        """
        return min(
            self.retry_backoff_seconds * 2 ** (max(1, attempts) - 1),
            self.retry_backoff_max_seconds,
        )


DEFAULT_LIMITS: Final[WriterLimits] = WriterLimits()

#: Whether a REFUSAL on the inline path owes the unit a ``write/dropped`` marker. The
#: batch path's refusal does, and the emitter spec states that this one should too;
#: today it is counted as dropped and owes no marker. One named switch, so the two
#: paths' treatment of the same loss is decided in one place.
_INLINE_REFUSAL_OWES_MARKER: Final[bool] = False


class JobKind(Enum):
    """What a :class:`WriteJob` is, which is what decides how the writer treats it."""

    #: An ordinary entry: bounded by the ceilings, inline when the caller allows it.
    APPEND = "append"
    #: The record that CREATES a unit's log. Never refused at the memory ceiling.
    OPENING = "opening"
    #: Work queued from INSIDE a job, ordered behind everything the unit already owes.
    FOLLOW_ON = "follow-on"
    #: A FOLLOW_ON submitted once more after its batch was dropped. Built here only.
    REQUEUED = "requeued"
    #: The writer's own ``write/dropped`` marker. Built here, never by a caller.
    LOSS_MARKER = "loss-marker"


@dataclass(frozen=True)
class _JobPolicy:
    """One row of the policy table. See :data:`_POLICY` for why each row is what it is."""

    #: Never refused at the memory ceiling.
    exempt_ceiling: bool
    #: Never written inline on the submitting thread.
    queue_only: bool
    #: Submitted once more if its batch is dropped, and not counted as lost.
    requeue_on_drop: bool


#: The whole policy, one row per kind.
#:
#: The ceiling bounds the memory held by PAYLOAD entries; the exempt kinds (every kind
#: but APPEND) are O(1) per unit and must not be refusable by it. Refusing the file-creating OPENING
#: record means the crew log never exists, so every later entry for that unit --
#: including the marker that would report the damage -- is discarded. A LOSS_MARKER
#: reports that the ceiling fired, so the ceiling refusing it would discard the account
#: of the very pressure that rejected it. A FOLLOW_ON is exempt because the opening entry
#: that queues it is: refusing only the follow-on creates the successor's log and
#: abandons the work it was owed, and nothing re-queues a one-shot job for an id that is
#: never resumed. An exempt job still counts toward the buffer's bookkeeping once
#: admitted; it is only never turned away.
#:
#: A FOLLOW_ON is queued from INSIDE a job, on the writer thread, so the inline path's
#: ordering wait would wait for the pass it is inside: it cannot be satisfied, and the
#: entry reaches the buffer anyway once the wait expires, having held the one writer
#: thread for that whole time. Declining the inline path only ever declines an
#: optimisation, so it cannot make an entry land in a weaker order than it would have
#: otherwise.
#:
#: A FOLLOW_ON waits in its unit's bucket, and waiting there is also how it is LOST: a
#: batch the filesystem refuses is retained with everything behind it, and once the
#: attempt budget is spent the WHOLE batch is dropped -- the owed append and this job
#: with it. So it is submitted once more on that drop, behind the loss marker. The
#: re-submission is a REQUEUED copy, the same policy minus the requeue, and that is what bounds
#: it at one extra attempt: the order the first submission was waiting for is gone, since
#: nothing ahead of it will be written now, and a copy that re-armed itself would follow
#: a wedged disk around its retry budget for as long as the disk stayed wedged.
_POLICY: Final[Mapping[JobKind, _JobPolicy]] = {
    JobKind.APPEND: _JobPolicy(exempt_ceiling=False, queue_only=False, requeue_on_drop=False),
    JobKind.OPENING: _JobPolicy(exempt_ceiling=True, queue_only=False, requeue_on_drop=False),
    JobKind.FOLLOW_ON: _JobPolicy(exempt_ceiling=True, queue_only=True, requeue_on_drop=True),
    JobKind.REQUEUED: _JobPolicy(exempt_ceiling=True, queue_only=True, requeue_on_drop=False),
    JobKind.LOSS_MARKER: _JobPolicy(exempt_ceiling=True, queue_only=True, requeue_on_drop=False),
}


@dataclass(frozen=True)
class WriteJob:
    """One unit of work for the writer: what to run, and what is owed once it resolves.

    Built through :meth:`append`, :meth:`opening` or :meth:`follow_on`, never by naming
    the fields, so the kind is what a caller decides and :data:`_POLICY` is what turns it
    into behaviour.

    ``run`` is called with no arguments on the writer thread (or inline on the caller's,
    see :meth:`CrewLogWriter.submit`). Returning is success. A ``CrewLogError`` is a
    REFUSAL and is dropped at once; any other exception is a failure and is retained.

    ``what`` names the job in the writer's own failure reports.

    ``nbytes`` is an O(1) size hint the producer already had -- the body length for a
    body-bearing entry, 0 otherwise -- carried so a RETAINED batch keeps its
    contribution to the byte ceiling instead of being re-measured, which nothing here
    can do after the fact.

    ``after`` runs once the job lands or the writer definitively drops it. A retryable
    failure leaves it attached to the retained job.

    ``on_drop`` runs once, ahead of ``after``, when the job is given up on permanently --
    a refusal, or its attempt budget spent. Never raises into the writer.
    """

    run: Callable[[], None]
    what: str
    kind: JobKind = JobKind.APPEND
    nbytes: int = 0
    after: Callable[[], None] | None = None
    on_drop: Callable[[], None] | None = None

    @classmethod
    def append(
        cls,
        run: Callable[[], None],
        what: str,
        *,
        nbytes: int = 0,
        after: Callable[[], None] | None = None,
        on_drop: Callable[[], None] | None = None,
    ) -> "WriteJob":
        """An ordinary entry. Bounded by the ceilings; written inline when it may be."""
        return cls(run, what, JobKind.APPEND, nbytes, after, on_drop)

    @classmethod
    def opening(
        cls,
        run: Callable[[], None],
        what: str,
        *,
        on_drop: Callable[[], None] | None = None,
    ) -> "WriteJob":
        """The record that creates a unit's log. Never refused at the memory ceiling."""
        return cls(run, what, JobKind.OPENING, 0, None, on_drop)

    @classmethod
    def follow_on(cls, run: Callable[[], None], what: str) -> "WriteJob":
        """Work a job queues for a unit from the writer thread. See :data:`_POLICY`."""
        return cls(run, what, JobKind.FOLLOW_ON, 0, None, None)


@dataclass
class _Pending:
    """One admitted job and the bookkeeping that travels with it until it resolves."""

    run: Callable[[], None]
    what: str
    kind: JobKind
    nbytes: int = 0
    after: Callable[[], None] | None = None
    on_drop: Callable[[], None] | None = None
    loss: "_PendingLoss | None" = None
    #: True while this job's ``nbytes`` are included in the process byte total. The
    #: inline path runs a job without ever buffering it, so a job can leave through
    #: the drop path having never been added; subtracting it anyway would drive the
    #: total negative and loosen the ceiling it exists to enforce.
    counted: bool = False

    @property
    def policy(self) -> _JobPolicy:
        return _POLICY[self.kind]


@dataclass
class _RetryState:
    """A unit's retained batch: how many passes have failed, and when to retry.

    ``attempts`` counts CONSECUTIVE failed passes over this unit's owed entries, and any
    append that lands resets it. Counting it that way is what makes the retry terminate:
    a reset costs a real append, so the buffer strictly shrinks between resets and an
    alternating failure cannot retry forever. It also means the budget bounds a WEDGED
    writer rather than a slow one -- a pass that wrote something is not wedged, however
    much is still owed.

    ``not_before`` is the clock instant the next pass may claim the bucket. It is per
    unit, so a wedged crew log's backoff paces only its own entries.
    """

    attempts: int = 0
    not_before: float = 0.0


@dataclass
class _PendingLoss:
    """Loss debt that must be written before this unit can append again."""

    dropped_count: int = 0
    dropped_bytes: int = 0

    def merge(self, other: "_PendingLoss") -> None:
        """Fold *other*'s debt into this marker."""
        self.dropped_count += other.dropped_count
        self.dropped_bytes += other.dropped_bytes

    def data(self) -> dict[str, Any]:
        """The frozen ``write/dropped`` data shape."""
        return {
            "dropped_count": self.dropped_count,
            "dropped_bytes": self.dropped_bytes,
        }


@dataclass(frozen=True)
class WriterStats:
    """What the writer holds and has lost, read in one short lock hold.

    ``dropped`` counts appends given up on after a storage refusal or a spent attempt
    budget -- the only way an append is abandoned. ``overflowed`` counts appends the
    buffer rejected at a hard memory ceiling, a DIFFERENT cause (one unit's share:
    :meth:`CrewLogWriter.overflowed_for`). Reading the two apart is how an operator
    tells a stuck disk from a saturated one.

    Every field is a counter the writer keeps, so the read is O(1): producers read these
    around each record, on the event loop, while the writer may be holding a large
    backlog. How many units are owed a ``write/dropped`` marker is NOT here -- answering
    it scans every buffered job, so it is its own read
    (:meth:`CrewLogWriter.loss_markers_owed`) and part of :class:`DrainReport`.
    """

    buffered: int
    peak_buffered: int
    dropped: int
    overflowed: int
    batch_in_flight: bool


@dataclass(frozen=True)
class DrainReport:
    """What :meth:`CrewLogWriter.drain_for_shutdown` achieved.

    ``drained`` is True only when the buffers reached empty AND no batch is still in
    flight. Both, because a claimed batch is not in the buffer any more: the writer takes
    a batch out before it writes it, so an empty buffer on its own says nothing about
    whether those entries reached the file. The other three fields are read after the
    drain finished, for the report a caller exits on.
    """

    drained: bool
    buffered: int
    loss_markers_owed: int
    batch_in_flight: bool


class WarningBudget:
    """Report a failure at warning level once per KIND, then stay quiet for a window.

    One slot per kind of failure, not one per process: :meth:`kind` builds the key from
    the operation and the exception, so a store refused for ENOSPC is named at default
    level even though an unrelated listener error spent a slot hours before. *op* names
    the operation in a fixed string and is the only part of the call that reaches the
    key; *what* carries the store, unit or entry type for the reader and is kept out of
    it.

    Repetition of a kind already named is swallowed and COUNTED, and the count rides on
    the next warning for that kind once ``rearm_seconds`` has passed -- a budget that ran
    out has to say so, since a silently spent one hides exactly the ongoing failure it was
    meant to surface, and naming every repeat would flood the log instead.

    Both records carry the failure as TEXT -- the warning's ``%s`` argument, and on the
    debug line the traceback RENDERED to a string while the exception is live, rather
    than ``exc_info``. An exception object handed to a log call, or the ``exc_info``
    triple, rides on the record with its ``__traceback__`` and ``__context__``, and a
    handler that keeps records (pytest's per-test capture, a ``MemoryHandler``) would keep
    the job frames -- and the ``CrewLog`` handle in them -- for as long as it keeps the
    record. A pre-rendered string holds no frames. See :meth:`CrewLogWriter._run_job` for
    why that handle must not outlive the pass.

    Shared: the writer reports its own failures through the instance it is given, and the
    emitter reports its other failures through the same one, so one budget governs the
    whole crew log path. ``max_kinds`` bounds the map, oldest kind evicted first; the key
    parts are program and OS constants, so that is a guard on the map rather than a limit
    routine traffic reaches.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        rearm_seconds: float = 300.0,
        max_kinds: int = 64,
    ) -> None:
        self._log = logger if logger is not None else _logger
        self._clock = clock
        self._rearm_seconds = rearm_seconds
        self._max_kinds = max_kinds
        self._lock = threading.Lock()
        #: Failure kind -> (clock instant it last warned, failures swallowed since).
        self._spent: "OrderedDict[tuple[str, str, str], tuple[float, int]]" = OrderedDict()

    @staticmethod
    def kind(op: str, exc: BaseException) -> "tuple[str, str, str]":
        """What KIND of failure this is, for the budget to spend a slot on.

        Three stable parts: the operation that failed, the exception's class, and the
        code the operating system or the store put on it. So a disk that is full and a
        disk that is failing arrive as two kinds out of one ``OSError``, and a lost write
        lease is not filed under an unrelated listener's ``RuntimeError``.

        What is deliberately absent is the UNIT -- no store name, no session id, no entry
        type. Those live in ``what`` for the message and never in the key, because many
        units failing at once is one cause repeating, and a key holding the unit would
        hand each of them its own warning and flood the log this budget exists to
        protect. Every part is a program or OS constant, so the map is bounded with no
        list of kinds for anyone to maintain: a new call site gets its own slot by naming
        its own ``op``.
        """
        code = getattr(exc, "code", "") or getattr(exc, "errno", "")
        return (op, type(exc).__qualname__, str(code or ""))

    def report(self, what: str, exc: BaseException, *, op: str) -> None:
        """Name *exc* at warning level unless its kind already spoke this window."""
        kind = self.kind(op, exc)
        now = self._clock()
        with self._lock:
            held = self._spent.get(kind)
            if held is None:
                speak, swallowed = True, 0
            else:
                warned_at, swallowed = held
                speak = now - warned_at >= self._rearm_seconds
            if speak:
                self._spent[kind] = (now, 0)
                self._spent.move_to_end(kind)
                while len(self._spent) > self._max_kinds:
                    self._spent.popitem(last=False)
            else:
                self._spent[kind] = (warned_at, swallowed + 1)
        if speak:
            self._log.warning(
                "session log writes are failing (%s: %s%s)%s; further failures of "
                "this kind are logged at debug only for the next %.0fs",
                what,
                str(exc),
                f", code={getattr(exc, 'code', '')}" if getattr(exc, "code", "") else "",
                (
                    f", and {swallowed} more went unreported since it was last named"
                    if swallowed
                    else ""
                ),
                self._rearm_seconds,
            )
        elif self._log.isEnabledFor(logging.DEBUG):
            # The traceback rendered to text while the exception is live: full
            # diagnostics on the record, and a string holds no frames.
            self._log.debug(
                "session log %s failed:\n%s",
                what,
                "".join(traceback.format_exception(exc)).rstrip(),
            )


def _is_refusal(failure: type[BaseException]) -> bool:
    """Whether retrying a *failure* of this type is pointless: it will be refused again.

    A ``CrewLogError`` is a REFUSAL, not a failure: the storage layer declines the entry
    before any byte is written, so the file is byte-identical and nothing about this
    process's next attempt is different. Either the entry does not fit the format, in
    which case the same verdict comes back every time, or ANOTHER PROCESS owns that
    unit's log -- ownership held for the life of the owning process, which no retry
    budget outlasts. Retaining either one would spend the whole budget on a verdict that
    will not change, and hold every later entry of that unit behind it while doing so --
    turning one refused entry into a stall for the log it is the only casualty of. So a
    refusal is a loss immediately, counted and named in the log; only an error that MIGHT
    clear -- an ENOSPC, an EIO, a filesystem that stopped answering, which is what this
    retention exists for -- is retried.

    A second gateway claiming a session whose writer is alive therefore loses its own
    entries, visibly, and writes nothing into the owner's file. That is the intended
    trade: the entries this process cannot write are counted, while the log keeps ONE
    writer's account of the turn instead of two interleaved ones.

    The errors module is imported here, on the first failure, rather than at the top:
    it is light, but the package it lives in is the storage layer this module keeps off
    the boot path.
    """
    from kiro_crew.crew_log.errors import CrewLogError

    return issubclass(failure, CrewLogError)


def _on_event_loop() -> bool:
    """True when this thread is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class CrewLogWriter:
    """Buffer, order, retry and admit the loss of crew log appends, per unit.

    Invariants every caller may rely on:

    * **Per-unit FIFO.** A unit's jobs run in submission order. A retained batch goes
      back to the FRONT of its bucket, and a later submission queues behind it, so a
      log is never written in an order that did not happen. Units are independent: one
      unit's backoff paces only that unit.
    * **A refusal is dropped, never retried** (a ``CrewLogError``; see
      :func:`_is_refusal`), and the pass continues past it.
    * **A failure is retained** with doubling backoff up to ``max_write_attempts``
      consecutive failed passes, then counted as lost.
    * **Every loss is admitted.** A loss of any cause owes the unit a ``write/dropped``
      marker, written at the head of its next batch before anything else for it --
      except a refusal on the inline path, which is counted but owes none
      (:data:`_INLINE_REFUSAL_OWES_MARKER`).
    * **The ceilings count overflow rather than block.** :meth:`submit` never raises and
      never blocks an event loop; only a caller with no loop running writes inline.

    Error modes: :meth:`submit`, :meth:`flush` and :meth:`drain_for_shutdown` never
    raise. A job's own exception is the job's outcome; a hook that raises is reported
    through the warning budget and does not stop the writer.

    Ordering constraints: the writer's own lock is a LEAF -- no job, hook, opener,
    listener or log call runs while it is held -- so a hook may submit again. The two
    callables it does call under that lock, ``clock`` and ``retry_delay``, must be pure.

    Required configuration: ``open_unit(unit)`` returns the open log the writer appends
    its ``write/dropped`` markers to, or ``None`` when the unit has no log; its exception
    is a failed marker append, retained like any other.
    """

    def __init__(
        self,
        open_unit: "Callable[[str], CrewLog | None]",
        *,
        executor: "Callable[[], Executor] | None" = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        limits: WriterLimits = DEFAULT_LIMITS,
        retry_delay: Callable[[int], float] | None = None,
        on_grew: Callable[[str], None] | None = None,
        warnings: WarningBudget | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._open_unit = open_unit
        self._executor = executor
        self._clock = clock
        self._sleep = sleep
        self.limits = limits
        self._retry_delay = retry_delay if retry_delay is not None else limits.retry_delay
        self._on_grew = on_grew
        self._log = logger if logger is not None else _logger
        self._warnings = warnings if warnings is not None else WarningBudget(self._log, clock=clock)
        self._lock = threading.Lock()
        #: Held while a caller runs passes itself (no executor), so two callers never
        #: drain beside each other.
        self._passes = threading.Lock()
        #: Signals a change to the writer's state. It has its OWN lock, deliberately
        #: NOT ``_lock``: a condition sharing that lock makes every waiter and notifier
        #: hold the same non-reentrant mutex that guards the maps, so one helper called
        #: from inside a ``with`` block on it self-deadlocks. Here the two concerns are
        #: separate -- state is mutated under ``_lock`` and the wake is delivered outside
        #: it, by :meth:`_notify` -- and a waiter's predicate takes ``_lock`` for itself.
        #: There is no lost wakeup: a notifier must acquire this lock to signal, and
        #: ``wait_for`` holds it across both the predicate and the wait.
        self._drained = threading.Condition()
        #: unit -> the appends waiting to be written, in the order they were made.
        #: Producers only ever append here; the writer prepends a batch it could not
        #: write. Keyed per unit because each unit is a separate file, so one slow crew
        #: log cannot reorder another's entries -- and because a retained batch has to
        #: hold back exactly the entries that belong AFTER it, which is that unit's
        #: bucket and nothing else. A bucket is popped whole and is never left empty.
        self._pending: "OrderedDict[str, list[_Pending]]" = OrderedDict()
        self._pending_count = 0
        #: Approximate serialized size of every unwritten entry in the process,
        #: including jobs claimed by the writer. Released only when a job lands or is
        #: dropped.
        self._pending_total_bytes = 0
        #: The most ever buffered at once, for a caller that wants to see the backlog.
        self._pending_high_water = 0
        #: Approximate serialized size of each unit's unwritten entries.
        self._pending_bytes: "dict[str, int]" = {}
        #: unit -> its retained batch's state, present only while one is owed. Its
        #: presence is also what disables the inline fast path for that unit, so a later
        #: write cannot overtake the entries it is queued behind.
        self._retry: "dict[str, _RetryState]" = {}
        #: Losses not yet admitted into the unit's own file. This debt is separate from
        #: ordinary jobs so crossing a memory ceiling cannot reject its marker too.
        self._pending_loss: "dict[str, _PendingLoss]" = {}
        #: Units a synchronous caller is writing inline for right now. The writer does
        #: not claim a bucket in here: ``flock`` serializes two appends but does not
        #: order them, so claiming beside an inline write is the reordering the inline
        #: gate exists to prevent.
        self._inline: "set[str]" = set()
        #: How many CLAIMED entries each unit still owes: the ones the writer took out of
        #: ``_pending`` and has not attempted yet. A claimed batch is absent from
        #: ``_pending``, so a caller asking whether this writer still owes a unit
        #: anything cannot learn it there. The entry being attempted right NOW is not
        #: counted, because the only caller that asks is a job of this batch asking about
        #: its own unit, and a job that counted itself would answer that it must wait for
        #: itself. Every exit from :meth:`_write_batch` releases what it did not attempt.
        self._claimed: "dict[str, int]" = {}
        self._dropped_count = 0
        #: Units whose loss has already been reported, so a wedged disk is named once
        #: rather than once per batch. Cleared by that unit's next successful append,
        #: which is what makes the recovery its own single line.
        self._dropped_reported: "set[str]" = set()
        self._overflow_count = 0
        #: The same count per unit, for a caller that must tell ITS OWN rejection apart
        #: from another unit's -- a global delta cannot say whose append was refused.
        self._overflow_by_unit: "dict[str, int]" = {}
        #: Units whose overflow has already been reported, so a saturated buffer is
        #: named once rather than once per rejected entry. Cleared by that unit's next
        #: successful append.
        self._overflow_reported: "set[str]" = set()
        self._warned_high_water = False
        #: The write currently in progress: when it started, and what it was. A job that
        #: HANGS never returns, so the writer cannot report on itself -- producers read
        #: this and say so instead. 0.0 means no write is in flight.
        self._inflight_since = 0.0
        self._inflight_what = ""
        self._stall_reported = False
        #: True while a drain pass is scheduled or running, so a producer wakes the
        #: writer once rather than per entry. Never read directly: read
        #: :meth:`_busy_locked`, which also answers the case where the pass this flag
        #: was set for can never run.
        self._draining = False
        #: The future :meth:`_start_drain` submitted for the current pass, or None.
        #: Held because the flag alone cannot say whether the pass is still coming:
        #: shutting the pool down CANCELS a queued future, and the flag would then stay
        #: set for the life of the process -- making every ``flush`` and
        #: ``drain_for_shutdown`` return False and, worse, making the exit path's inline
        #: fallback refuse to write on the belief that a batch is claimed. That loses
        #: exactly the entries the drain exists to save.
        self._drain_future: "Future[None] | None" = None
        #: True once shutdown has asked for quiescence: the writer stops pausing to
        #: batch and writes what it has as fast as it can.
        self._draining_for_shutdown = False
        #: Set to make the writer thread abandon its current inter-pass wait and
        #: recompute it. Without this the pause is a plain sleep: a writer that parked
        #: for the length of a retry backoff cannot learn that a shutdown has since asked
        #: for quiescence, so clamping the backoff would never reach the thread that
        #: honours it.
        self._wake = threading.Event()
        #: When a bounded shutdown drain is running, the clock instant it gives up at. A
        #: retained batch parked BEYOND it is retried just before it instead of never: a
        #: retry schedule outliving its process does not defer a write, it loses it.
        self._shutdown_deadline = 0.0
        #: When that drain began. The pair defines the budget, and the retry instants are
        #: absolute points inside it -- a rolling "now + slice" is always in the future
        #: and would never become claimable at all.
        self._shutdown_started = 0.0

    # -- the interface ---------------------------------------------------------------

    def submit(self, unit: str, job: WriteJob) -> bool:
        """Hand *job* to the writer for *unit*.

        On a running event loop it returns without waiting on the disk; with no loop
        running it may write inline, waiting at most ``inline_order_seconds`` for the
        unit's earlier entries first (see below).

        Returns ``False`` only when the job was REJECTED at a hard memory ceiling. A
        rejected job was never admitted: it is counted (``WriterStats.overflowed``),
        folded into the unit's ``write/dropped`` debt, and named once per unit -- but
        NONE of its hooks run. Its settlement is the caller's, because only the caller
        knows what else hangs off that entry, and it can act on the verdict at once;
        every other outcome (landed, retained, dropped) resolves through the job's own
        hooks and returns ``True``.

        Never raises, and never blocks an event loop. A lifecycle record is never dropped merely
        for crossing the BACKPRESSURE high-water mark: crossing it is reported rather
        than acted on, and only the hard ceilings far above it ever reject an entry.

        With no event loop running the write happens inline. That is not a fallback on
        the loop path -- there is no loop to protect, the caller is a thread that asked
        for this, and it keeps a synchronous caller and the test suite deterministic. It
        is ORDERED against the writer in both directions. ``flock`` stops two appends
        from interleaving but does not decide which lands first, so writing here while
        the writer holds a claimed batch could put this entry at a lower seq than one
        submitted before it -- and a log whose seq disagrees with causality is worse than
        a short one, because a fold cannot detect it. So the inline path is taken only
        for a unit that owes nothing: no retained batch, no buffered entry, no other
        inline write. And an inline write that FAILS is retained like any other, never
        swallowed -- otherwise a synchronous caller's entry would be the one kind this
        writer silently loses. A ``FOLLOW_ON`` job never takes the inline path (see
        :data:`_POLICY`).
        """
        pending = _Pending(
            run=job.run,
            what=job.what,
            kind=job.kind,
            nbytes=job.nbytes,
            after=job.after,
            on_drop=job.on_drop,
        )
        # The only thread that can notice a write which stopped answering is a
        # PRODUCER: the one that would notice is blocked inside the call. Checked before
        # this entry is handed over, so a stuck write is named while the backlog behind
        # it is still growing rather than after it clears.
        self._note_stall_if_any()
        if not pending.policy.queue_only and not _on_event_loop():
            if self.owes(unit):
                # This unit already owes entries this one belongs after, so there is
                # nothing to wait for: queueing behind them keeps the order and costs the
                # caller nothing, while waiting for the debt to clear would put a real
                # thread behind the filesystem for as long as the debt lasts.
                return self._buffer(unit, pending)
            with self._drained:
                self._drained.wait_for(
                    lambda: self._inline_ready(unit), timeout=self.limits.inline_order_seconds
                )
            if self._claim_inline(unit):
                try:
                    failure = self._run_job(pending.run, pending.what)
                    if failure is None:
                        self._finish(pending)
                        self._note_progress(unit)
                    elif _is_refusal(failure):
                        self._drop(unit, [pending], mark=_INLINE_REFUSAL_OWES_MARKER)
                    else:
                        self._retain(unit, [pending])
                finally:
                    self._release_inline(unit)
                return True
            # The writer is mid-batch. Hand the job over rather than race it: the entry
            # then waits, which is visible in the buffer count and recoverable at
            # shutdown, instead of landing out of order, which is neither.
        return self._buffer(unit, pending)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until nothing is owed. True when the writer is quiet within *timeout*.

        For a caller that must read the file it just wrote -- a test, a shutdown path --
        since :meth:`submit` returns as soon as the work is HANDED to the writer. Never
        called from the turn path: waiting there would reintroduce the block this queue
        exists to remove.

        Terminates even against a filesystem that never answers: a batch the writer
        cannot write is retried a bounded number of times and then dropped, so the
        buffer reaches empty rather than holding entries no wait could ever satisfy.

        With no executor this RUNS the passes on the calling thread: each pass writes
        what is due on the writer's clock, and the gap to the next due retry is spent in
        the injected ``sleep``. So a test that injects a fake clock and a sleep that
        advances it drives the whole retry schedule without waiting on a real one.
        """
        if self._executor is None:
            return self._run_passes_here(self._clock() + timeout)
        with self._drained:
            return self._drained.wait_for(self._quiet, timeout=timeout)

    def drain_for_shutdown(self, timeout: float) -> DrainReport:
        """Write out everything buffered, then stop accepting batching pauses.

        The quiescence barrier a restart needs: entries live in memory until the writer
        takes them, so a process that exits without this loses whatever had not been
        written yet -- exactly the records a crash-time log is wanted for.

        Blocking here is correct where blocking a producer is not: this runs on the
        shutdown path, which has nothing left to keep responsive. Bounded all the same, by
        *timeout*, so a wedged filesystem delays exit rather than hanging it.

        The inline fallback runs at most one pass, and only when NO writer batch is
        claimed. Two threads appending to one file is not merely a race for the lock:
        ``flock`` serializes the writes but does not order them, so the batch claimed
        second can reach the file first and a ``turn/started`` can land at a lower seq
        than the ``session/opened`` before it. A log whose seq disagrees with causality
        is worse than a short one, because a fold cannot detect it. The timed wait
        already waits on the writer being idle, so reaching the fallback with a batch
        still claimed means the writer is wedged holding it -- and then the only safe
        answer is to write nothing.

        Residual, in two shapes. A batch the wedged writer holds cannot be finished from
        this thread. And when that batch is still claimed, the entries buffered BEHIND it
        are left alone too, so they are lost at exit rather than written out of order. A
        report that is not ``drained`` means exactly that: the log's tail is short, and
        the warning says how short.

        A retained batch is retried rather than waited out. Its backoff is a schedule in
        seconds and this process is leaving, so honouring it here does not defer the
        write, it loses it. The shutdown therefore CLAMPS the backoff to just inside its
        deadline (see :meth:`_backoff_ready_at_locked`) -- and PACES the attempts across
        the remaining budget, because a batch has few attempts before it is dropped and
        spending them in microseconds turns a filesystem that needed a moment into a
        permanent hole. What bounds this path is still *timeout*; a batch still owed when
        it expires is reported by the warning, and one that ran out of attempts is
        counted as dropped.
        """
        budget = timeout
        with self._lock:
            self._draining_for_shutdown = True
            self._shutdown_started = self._clock()
            # Published before anything is claimed, so the writer thread's own pass sees
            # the same deadline this call will give up at.
            self._shutdown_deadline = self._shutdown_started + budget
            pending = bool(self._pending or self._pending_loss)
            running = self._busy_locked()
        # Outside the lock, and BEFORE the wait below: a writer already parked for the
        # length of a retry backoff has to recompute that pause against the deadline,
        # or the clamp above changes a value nothing rereads.
        self._wake.set()
        if not pending and not running:
            return self._drain_report(True)
        try:
            if not running:
                # Nothing is in flight, so this thread may write without racing anyone
                # -- and it does, rather than asking the pool for a pass. The pool is
                # routinely already GONE by the time this runs (the executor's own exit
                # hook runs first), and asking would then build a fresh one during
                # interpreter shutdown: new non-daemon threads, plus an ``atexit``
                # registered while ``atexit`` is already draining.
                drained = self._drain_inline_until(self._clock() + budget)
            else:
                with self._drained:
                    drained = self._drained.wait_for(self._quiet, timeout=budget)
        except Exception as exc:
            self._warnings.report("draining the session log for shutdown", exc, op="shutdown-drain")
            drained = False
        if not drained:
            with self._lock:
                # Re-read under the lock: the wait may have failed on either condition,
                # and only one of them permits an inline write.
                claimed = self._busy_locked()
            if not claimed:
                # The writer is gone rather than mid-batch, so nothing else will touch
                # the file. Finish what is still buffered here instead of losing it. A
                # short second budget: the first wait already spent *timeout*, and exit
                # cannot be delayed twice over for the same batch.
                try:
                    drained = self._drain_inline_until(
                        self._clock() + self.limits.second_chance_drain_seconds
                    )
                except Exception as exc:
                    self._warnings.report(
                        "draining the session log for shutdown", exc, op="shutdown-drain"
                    )
                    drained = False
        report = self._drain_report(drained)
        if not drained:
            self._log.warning(
                "session log did not finish writing within %.1fs of shutdown; "
                "%d append(s) buffered, %d loss marker(s) owed, batch in flight=%s",
                budget,
                report.buffered,
                report.loss_markers_owed,
                report.batch_in_flight,
            )
        return report

    def stats(self) -> WriterStats:
        """The writer's counters, read together. O(1), whatever the backlog."""
        with self._lock:
            return WriterStats(
                buffered=self._pending_count,
                peak_buffered=self._pending_high_water,
                dropped=self._dropped_count,
                overflowed=self._overflow_count,
                batch_in_flight=self._busy_locked(),
            )

    def overflowed_for(self, unit: str) -> int:
        """How many of *unit*'s appends the ceilings rejected since it was last forgotten.

        Per unit, so a caller judging its own append can tell its rejection apart from
        another unit's. O(1).
        """
        with self._lock:
            return self._overflow_by_unit.get(unit, 0)

    def loss_markers_owed(self) -> int:
        """How many units are still owed a ``write/dropped`` marker.

        A diagnostic read, not a counter: it scans every buffered job (see
        :meth:`_owed_loss_markers_locked`), so it is kept off the producer paths that
        :meth:`stats` serves, and the shutdown drain reports it in :class:`DrainReport`.
        """
        with self._lock:
            return self._owed_loss_markers_locked()

    def owes(self, unit: str) -> bool:
        """Whether *unit* has entries queued, claimed or retained, or a marker owed.

        The claimed count is the one a reader would not think to ask for: the writer pops
        a unit's bucket before it writes it, so a batch in flight is in none of the other
        three while its entries are still owed. It counts what has not been ATTEMPTED, so
        the job running right now -- the one doing the asking -- is not among them.
        """
        with self._lock:
            return (
                unit in self._pending
                or unit in self._retry
                or unit in self._pending_loss
                or unit in self._claimed
            )

    def forget(self, unit: str) -> None:
        """Drop *unit*'s overflow tally: the unit has closed.

        The tally is per UNIT and only ever read while that unit is writing, so it dies
        with the unit. Left behind, a writer that runs for weeks keeps one entry per unit
        that ever overflowed, and a successor reusing the id would inherit a count it did
        not earn.
        """
        with self._lock:
            self._overflow_by_unit.pop(unit, None)

    def close(self, timeout: float = 2.0) -> bool:
        """Retire this writer: wait up to *timeout* for its work, then discard the rest.

        Whatever could not be written in time is DISCARDED rather than carried anywhere:
        none of those jobs' hooks run and none is counted. Reaching that point means the
        writer is wedged, and a retiring writer must not go on retrying entries for a
        caller that has stopped believing in its handles -- its drain thread, if it ever
        returns, finds nothing left and stops. Clearing the claim matters as much as
        clearing the buffer: a claim nothing will ever release makes every later barrier
        report a batch in flight. A successful wait makes the discard a no-op.

        Bounded well below a shutdown's budget: this wait exists so a job holding a stale
        handle cannot write after the retirement, and it returns the instant the writer
        is quiet. When the writer is GONE -- the pool shut down with work still owed -- no
        job can run at all, so waiting longer would stall the caller for nothing.
        """
        drained = self.flush(timeout=timeout)
        with self._lock:
            self._pending.clear()
            self._pending_loss.clear()
            self._pending_count = 0
            self._pending_total_bytes = 0
            self._pending_bytes.clear()
            self._retry.clear()
            self._claimed.clear()
            self._inline.clear()
            self._draining = False
            self._drain_future = None
        self._notify()
        return drained

    # -- buffering -------------------------------------------------------------------

    def _buffer(self, unit: str, pending: _Pending) -> bool:
        """Append one job to its unit's bucket and wake the writer. False = rejected.

        Backpressure alone sheds nothing until the buffer reaches a hard memory ceiling.
        This writer does not create a hole in a log for falling behind: the record is
        what several subsystems read to decide what happened, and a missing entry is
        indistinguishable from a fact that never occurred. A crash-shaped loss is
        different in kind -- the repair machinery names and closes what a kill left
        behind -- but a discard chosen while the process is healthy has nothing that can
        recover it and no reader that can detect it. So a filesystem that stops answering
        costs MEMORY, reported through the stats and the high-water warning, and a write
        in flight too long is named by :meth:`_note_stall_if_any`.

        At a ceiling this entry is REJECTED at the tail rather than an older one shed
        from the head: the queued prefix keeps its order and stays a faithful run of the
        log, and the loss is that log's tail stopping at a counted point rather than its
        middle disagreeing with causality. The rejection is counted, folded into the
        unit's loss debt and named once per unit; the job itself is handed back
        unsettled (see :meth:`submit`).
        """
        overflow = False
        with self._lock:
            would_count = self._pending_count + 1
            would_bytes = self._pending_bytes.get(unit, 0) + pending.nbytes
            would_total_bytes = self._pending_total_bytes + pending.nbytes
            if not pending.policy.exempt_ceiling and (
                would_count > self.limits.max_pending_count
                or would_bytes > self.limits.max_pending_bytes
                or would_total_bytes > self.limits.max_pending_bytes
            ):
                self._overflow_count += 1
                self._overflow_by_unit[unit] = self._overflow_by_unit.get(unit, 0) + 1
                self._record_loss_locked(unit, [pending], True)
                first_overflow = unit not in self._overflow_reported
                self._overflow_reported.add(unit)
                overflow_total = self._overflow_count
                overflow = True
            else:
                held = self._pending.setdefault(unit, [])
                held.append(pending)
                pending.counted = True
                self._pending_bytes[unit] = would_bytes
                self._pending_count = would_count
                self._pending_total_bytes = would_total_bytes
                if self._pending_count > self._pending_high_water:
                    self._pending_high_water = self._pending_count
                over = (
                    self._pending_count >= self.limits.pending_high_water
                    and not self._warned_high_water
                )
                start_writer = not self._busy_locked()
                if start_writer:
                    self._mark_draining_locked()
        if overflow:
            if first_overflow:
                self._report_overflow(unit, overflow_total)
            self._notify()
            return False
        if over:
            self._report_high_water()
        if start_writer:
            self._start_drain()
        return True

    def _retain(self, unit: str, jobs: "list[_Pending]") -> None:
        """Put a failed batch back at the FRONT of its unit's bucket.

        The front, and the whole remainder rather than the one job that failed, because
        the entries after a failure belong AFTER it in the file: writing them while the
        failed one waits would put this unit's log on disk in an order that never
        happened, which a fold reads as fact. Producers append to the same bucket behind
        them, so a later write queues behind the retained batch by construction -- there
        is no second structure for it to overtake.

        Spending the last attempt drops the batch instead. Nothing else can: entries live
        in memory until they are written, so a filesystem that never answers would hold
        them forever and turn every bounded caller into a timeout.
        """
        with self._lock:
            state = self._retry.setdefault(unit, _RetryState())
            state.attempts += 1
            spent = state.attempts >= self.limits.max_write_attempts
            if spent:
                self._retry.pop(unit, None)
                start_writer = False
            else:
                state.not_before = self._clock() + self._retry_delay(state.attempts)
                self._pending.setdefault(unit, [])[:0] = list(jobs)
                self._pending_count += len(jobs)
                self._pending_bytes[unit] = self._pending_bytes.get(unit, 0) + sum(
                    job.nbytes for job in jobs
                )
                # An inline job reaches this without ever passing `_buffer`, so it is
                # entering the process total for the first time. A claimed job is
                # already in it and must not be added twice.
                self._pending_total_bytes += sum(job.nbytes for job in jobs if not job.counted)
                for job in jobs:
                    job.counted = True
                if self._pending_count > self._pending_high_water:
                    self._pending_high_water = self._pending_count
                start_writer = not self._draining_for_shutdown and not self._busy_locked()
                if start_writer:
                    self._mark_draining_locked()
        if start_writer:
            self._start_drain()
        if spent:
            self._drop(unit, jobs, mark=True)

    def _retain_without_failure(self, unit: str, jobs: "list[_Pending]") -> None:
        """Put *jobs* back at the head without spending their retry budget."""
        with self._lock:
            self._pending.setdefault(unit, [])[:0] = list(jobs)
            self._pending_count += len(jobs)
            self._pending_total_bytes += sum(job.nbytes for job in jobs if not job.counted)
            for job in jobs:
                job.counted = True
            self._pending_bytes[unit] = self._pending_bytes.get(unit, 0) + sum(
                job.nbytes for job in jobs
            )
            if self._pending_count > self._pending_high_water:
                self._pending_high_water = self._pending_count

    def _drop(self, unit: str, jobs: "list[_Pending]", *, mark: bool = False) -> None:
        """Give up on *jobs* and count them. The one place a record is lost.

        Reported once per unit rather than once per batch, so a wedged disk names itself
        and then stops talking; the recovery is its own single line, written by
        :meth:`_note_progress` when a write for that unit lands again. Between the two,
        the dropped count is exact.

        A ``FOLLOW_ON`` job in the dropped batch is submitted once more instead of being
        lost (see :data:`_POLICY`), and the count is corrected FIRST: a marker is authored
        by a LATER pass of the one writer thread, and this runs inside the pass that
        recorded the debt, so no reader can have seen the count yet. Only the count moves:
        a follow-on carries no body, so the marker's byte total was already exact.
        """
        with self._lock:
            self._pending_total_bytes -= sum(job.nbytes for job in jobs if job.counted)
            for job in jobs:
                job.counted = False
            self._dropped_count += len(jobs)
            self._record_loss_locked(unit, jobs, mark)
            total = self._dropped_count
            first = unit not in self._dropped_reported
            self._dropped_reported.add(unit)
        if first:
            self._log.warning(
                "session log gave up on %d append(s) for session %s after %d failed "
                "attempts; %d dropped in total. That log's tail is short by them. "
                "Further losses for this session are logged at debug until one lands",
                len(jobs),
                unit,
                self.limits.max_write_attempts,
                total,
            )
        else:
            self._log.debug(
                "session log dropped %d more append(s) for session %s",
                len(jobs),
                unit,
            )
        for job in jobs:
            if job.policy.requeue_on_drop:
                self._uncount_one_requeued_drop(unit)
                self.submit(unit, WriteJob(job.run, job.what, JobKind.REQUEUED))
            elif job.on_drop is not None:
                hook = job.on_drop
                job.on_drop = None
                try:
                    hook()
                except Exception as exc:
                    self._warnings.report(
                        f"flagging a permanent drop of {job.what}", exc, op="flag-permanent-drop"
                    )
            self._finish(job)
        self._notify()

    def _record_loss_locked(self, unit: str, jobs: "list[_Pending]", mark: bool) -> None:
        """Merge lost *jobs* into one per-unit marker. ``_lock`` held.

        The marker carries a COUNT and a SIZE, no reason code. The only site that knows a
        cause cannot separate the ones that differ: :func:`_is_refusal` collapses a
        malformed entry and an entry refused because another process owns the log into a
        single boolean. A reason a reader cannot trust is worse than none, so what the
        marker states is that facts are missing and how many.

        *mark* is False for a loss the log cannot admit anyway, and a dropped marker's own
        debt still travels through ``job.loss`` regardless.
        """
        if not mark and not any(job.loss is not None for job in jobs):
            return
        loss = self._pending_loss.setdefault(unit, _PendingLoss())
        for job in jobs:
            if job.loss is not None:
                loss.merge(job.loss)
                continue
            if not mark:
                continue
            loss.dropped_count += 1
            loss.dropped_bytes += max(0, job.nbytes)

    def _uncount_one_requeued_drop(self, unit: str) -> None:
        """Take one append back out of a loss that has NOT been written yet.

        Debt that falls to nothing is REMOVED rather than left at zero, so a unit whose
        only dropped entry is coming back does not append a marker announcing that
        nothing is missing.
        """
        with self._lock:
            if self._dropped_count > 0:
                self._dropped_count -= 1
            loss = self._pending_loss.get(unit)
            if loss is None:
                return
            if loss.dropped_count > 0:
                loss.dropped_count -= 1
            if loss.dropped_count == 0 and loss.dropped_bytes == 0:
                self._pending_loss.pop(unit, None)

    def _owed_loss_markers_locked(self) -> int:
        """How many units are owed a ``write/dropped`` marker. ``_lock`` held.

        A unit's loss debt lives in one of two places and a count that names it has to
        read both. It sits in ``_pending_loss`` while no marker job exists for it. Once one
        is built it TRAVELS IN THE JOB: :meth:`_loss_marker_job` takes the debt out of the
        map to serialize it, and an append that raises hands the job -- debt and all -- to
        :meth:`_retain`, which puts it at the front of that unit's bucket in
        ``_pending``.

        So a marker waiting to be retried is owed while the map is empty, and reading only
        the map reports nothing owed at exactly that moment. That moment is not an edge
        case for a bounded shutdown: a marker whose filesystem is still failing is
        retained by every attempt the budget allows, and the budget is spent only if
        enough paced attempts fit inside the caller's timeout.

        Counted per UNIT, because one marker covers a unit's whole interval -- the same
        grain the map's own length carries.
        """
        owed = set(self._pending_loss)
        owed.update(
            unit
            for unit, jobs in self._pending.items()
            if any(job.loss is not None for job in jobs)
        )
        return len(owed)

    def _loss_marker_job(self, unit: str, loss: _PendingLoss) -> _Pending:
        """Build the marker that must lead this unit's next drain."""

        def _job() -> None:
            # Loss can continue while a retained marker waits. Claim and merge all debt
            # immediately before serializing it, so one marker names the whole interval
            # known before this append starts.
            with self._lock:
                newer = self._pending_loss.pop(unit, None)
                if newer is not None:
                    loss.merge(newer)
            log = self._open_unit(unit)
            if log is None:
                return
            log.append("write/dropped", loss.data(), src=_SRC_GATEWAY)

        return _Pending(
            run=_job,
            what="appending write/dropped",
            kind=JobKind.LOSS_MARKER,
            loss=loss,
        )

    def _finish(self, job: _Pending) -> None:
        """Run one append's terminal cleanup once. Never raises."""
        after = job.after
        job.after = None
        if after is None:
            return
        try:
            after()
        except Exception as exc:
            self._warnings.report(f"finishing {job.what}", exc, op="finish-write")

    def _note_progress(self, unit: str) -> None:
        """An append landed: clear the retry state, and say so if it had been failing.

        Clearing on progress is what makes the retry terminate. The budget counts
        CONSECUTIVE failed passes, and a reset costs a real append -- so the buffer
        strictly shrinks between resets and an intermittent failure cannot retry without
        bound.
        """
        with self._lock:
            had_retry = self._retry.pop(unit, None) is not None
            recovered = unit in self._dropped_reported
            self._dropped_reported.discard(unit)
            self._overflow_reported.discard(unit)
        if recovered:
            self._log.warning(
                "session log writes for session %s are landing again; the "
                "write/dropped marker was appended before later entries",
                unit,
            )
        if had_retry or recovered:
            self._notify()

    def _report_high_water(self) -> None:
        """Report the buffer growing past its mark once, then stay quiet."""
        with self._lock:
            first = not self._warned_high_water
            self._warned_high_water = True
            held = self._pending_count
        if first:
            self._log.warning(
                "session log has %d appends buffered, past the %d mark: the writer "
                "is not keeping up and the backlog is held in memory rather than "
                "dropped; further growth is not reported",
                held,
                self.limits.pending_high_water,
            )

    def _report_overflow(self, unit: str, total: int) -> None:
        """Name a unit's first buffer overflow once, at error level.

        Called with the first rejection for a unit; later rejections for it stay silent
        until an append lands, which clears the flag through :meth:`_note_progress`.
        Error level rather than warning because a rejected lifecycle record is a real
        hole in that log, not the mere memory pressure the high-water mark reports.
        """
        self._log.error(
            "session log buffer full for session %s: the writer is too far behind "
            "to hold more, so its newest appends are being rejected and counted; %d "
            "rejected in total. That log's tail is short by them until writes catch "
            "up. Further overflow for this session is not reported until one lands",
            unit,
            total,
        )

    # -- the inline path -------------------------------------------------------------

    def _inline_ready(self, unit: str) -> bool:
        """Whether an inline write for *unit* may go ahead. Takes ``_lock``."""
        with self._lock:
            return self._inline_claimable_locked(unit)

    def _inline_claimable_locked(self, unit: str) -> bool:
        """The inline path's whole precondition. ``_lock`` held.

        Every clause is an ordering rule. A busy writer covers a batch it may already
        hold, for any unit, because a claimed batch is absent from ``_pending`` and cannot
        be seen there. ``_pending``, ``_retry`` and ``_pending_loss`` cover this unit's
        own owed entries, which this one belongs after. ``_inline`` covers another
        synchronous caller already writing for it.
        """
        return (
            not self._busy_locked()
            and unit not in self._pending
            and unit not in self._retry
            and unit not in self._pending_loss
            and unit not in self._inline
        )

    def _claim_inline(self, unit: str) -> bool:
        """Take the inline slot for *unit*, re-checking under one lock hold.

        The wait and the claim cannot be one operation -- the predicate reads ``_lock``
        while the wait holds the condition's own lock -- so the decision is made again
        here, atomically, and the claim is published in the same hold. Without that
        re-read a producer could start the writer in the window between the two.
        """
        with self._lock:
            if not self._inline_claimable_locked(unit):
                return False
            self._inline.add(unit)
            return True

    def _release_inline(self, unit: str) -> None:
        """Give the inline slot back and wake the writer, which may have been waiting."""
        with self._lock:
            self._inline.discard(unit)
        self._notify()

    # -- running a job ---------------------------------------------------------------

    def _run_job(self, run: Callable[[], None], what: str) -> type[BaseException] | None:
        """Run one storage job. Never raises. Returns the failure's TYPE, or None.

        The type is returned rather than a bare False because the caller's next decision
        depends on WHICH failure it was: a refusal is a loss now, and anything else is
        retried. It is already reported by the time it comes back.

        The type and not the exception: the caller binds the return to a local while it
        decides, and an exception object reaches frames three ways -- its own
        ``__traceback__``, and the ``__context__`` / ``__cause__`` of whatever it was
        raised while handling, each with a traceback of its own. Those frames include this
        one (whose ``f_back`` is the caller's frame) and the job's, whose locals hold the
        ``CrewLog`` handle it was appending through: a reference cycle through the handle,
        and a handle's lease is released by a finalizer when the handle is dropped, so the
        lease would stay held until the cyclic collector's next pass rather than when the
        pass that failed returned. Stripping the tracebacks one by one leaves the next link
        to find; a class object has no frames at all. :func:`_is_refusal` needs only the
        type, and the report above has already used the exception.
        """
        with self._lock:
            self._inflight_since = self._clock()
            self._inflight_what = what
        try:
            run()
        except Exception as exc:
            self._warnings.report(what, exc, op="queued-write")
            return type(exc)
        finally:
            with self._lock:
                self._inflight_since = 0.0
                self._inflight_what = ""
                # Armed again, so a SECOND stall is reported rather than swallowed as a
                # repeat of the first.
                self._stall_reported = False
        return None

    def _note_stall_if_any(self) -> None:
        """Say once that the write in flight has been running too long.

        Called by producers, because the thread that would notice is the one blocked in
        the call. A stalled job is not a failed one -- it may still land, and its retry
        budget has not moved, since nothing raised -- so this reports and does nothing
        else. With no ceiling to shed against, this line is the ONLY outward sign of a
        hung write besides memory climbing: nothing else about it is visible, because it
        neither returns nor raises.
        """
        with self._lock:
            since = self._inflight_since
            what = self._inflight_what
            if not since or self._stall_reported:
                return
            if self._clock() - since < self.limits.write_stall_secs:
                return
            self._stall_reported = True
            held = self._pending_count
        self._log.error(
            "session log writer stalled: %s has been in progress for over %.0fs and "
            "has neither returned nor failed; %d append(s) are waiting behind it",
            what,
            self.limits.write_stall_secs,
            held,
        )

    # -- the drain -------------------------------------------------------------------

    def _notify(self) -> None:
        """Wake every waiter on the writer's state. Called WITHOUT ``_lock`` held."""
        with self._drained:
            self._drained.notify_all()

    def _busy_locked(self) -> bool:
        """Whether a drain pass is really coming or running. ``_lock`` held.

        ``_draining`` says a pass was SCHEDULED, which is not the same claim. Shutting the
        writer pool down cancels a queued future, and the drain loop that would have
        cleared the flag then never runs -- so the flag alone reports a batch in flight
        forever. Deriving the answer from the future instead makes the state
        self-healing: the next producer starts a fresh pass, and until then every barrier
        reads the truth.

        Pure on purpose. Clearing the flag here would make a waiter's predicate mutate
        shared state, and a second waiter would still sit until its own timeout because
        nothing notified it. Reporting instead leaves the correction to
        :meth:`_mark_draining_locked`, which assigns a new flag AND a new future together.
        """
        if not self._draining:
            return False
        return self._drain_future is None or not self._drain_future.done()

    def _quiet(self) -> bool:
        """True when nothing is owed and no batch is in flight. Takes ``_lock``."""
        with self._lock:
            return not self._pending and not self._pending_loss and not self._busy_locked()

    def _drain_report(self, drained: bool) -> DrainReport:
        with self._lock:
            return DrainReport(
                drained=drained,
                buffered=self._pending_count,
                loss_markers_owed=self._owed_loss_markers_locked(),
                batch_in_flight=self._busy_locked(),
            )

    def _mark_draining_locked(self) -> None:
        """Claim the next pass. ``_lock`` held.

        The future is cleared HERE rather than left pointing at the previous pass, so
        :meth:`_busy_locked` reports busy for the window before :meth:`_start_drain`
        publishes the new one -- a pass that is about to be submitted is coming, and a
        stale done-future would read as "nothing coming" and let a second producer
        submit a duplicate.

        With no executor no pass is ever coming, so nothing is claimed: the passes run
        inside :meth:`flush`, on the caller's thread.
        """
        if self._executor is None:
            return
        self._draining = True
        self._drain_future = None

    def _start_drain(self) -> None:
        """Ask the writer pool for one drain pass. Never raises."""
        if self._executor is None:
            return
        try:
            future = self._executor().submit(self._drain_loop)
        except Exception as exc:  # pool shut down, or thread creation refused
            with self._lock:
                self._draining = False
                self._drain_future = None
            self._notify()
            self._warnings.report("scheduling the crew log writer", exc, op="schedule-writer")
            return
        with self._lock:
            # Published so the barriers can tell a pass that is still coming from one that
            # was CANCELLED by a pool shutdown and will never run.
            self._drain_future = future
        self._notify()

    def _drain_loop(self) -> None:
        """Drain the pending buffers in batches until they are empty. Writer thread.

        The pause before each pass is what makes a batch: a turn emits several entries in
        a burst, and waiting a fixed moment lets one pass take all of them rather than
        waking per entry. It is a fixed deadline rather than an adaptive one so the worst
        case a producer can impose on the file is a constant.

        A unit inside its retry backoff is not claimable, so when nothing is claimable
        this waits exactly as long as the soonest backoff has left rather than spinning
        on a bucket it may not touch. Loss-only debt receives one marker attempt per drain
        invocation; debt folded forward by a failed marker waits for the next invocation
        rather than re-entering this one forever.
        """
        deferred_loss: "set[str]" = set()
        try:
            while True:
                with self._lock:
                    loss_owed = any(unit not in deferred_loss for unit in self._pending_loss)
                    if not self._pending and not loss_owed:
                        self._draining = False
                        idle = True
                        delay = 0.0
                    else:
                        idle = False
                        delay = self._next_pass_delay_locked(deferred_loss)
                if idle:
                    self._notify()
                    return
                if delay:
                    # Interruptible: a shutdown that clamps the backoff sets this so the
                    # pause is recomputed against the new deadline rather than slept out.
                    self._wake.wait(delay)
                    self._wake.clear()
                deferred_loss.update(self._drain_once(deferred_loss))
        except BaseException:
            with self._lock:
                self._draining = False
            self._notify()
            raise

    def _run_passes_here(self, deadline: float) -> bool:
        """Run drain passes on THIS thread until quiet or *deadline*. No executor only.

        The same passes the writer thread runs, with the same per-unit backoff honoured
        -- a retained batch is claimed only once its retry is due -- and with the wait
        between passes spent in the injected ``sleep`` rather than a real pause, so the
        schedule is driven entirely by the writer's clock. No batching pause: there is no
        burst to gather when the caller is the one draining.

        Returns True when the buffers reached empty. A retry due after *deadline* is not
        waited for; the writer is reported not quiet and the batch stays owed for the
        next call.
        """
        with self._passes:
            with self._lock:
                # Claimed for the duration, so a synchronous submitter on another thread
                # queues behind these passes instead of writing beside them.
                self._draining = True
            try:
                return self._run_claimed_passes(deadline)
            finally:
                with self._lock:
                    self._draining = False
                self._notify()

    def _run_claimed_passes(self, deadline: float) -> bool:
        deferred_loss: "set[str]" = set()
        while True:
            deferred_loss.update(self._drain_once(deferred_loss))
            with self._lock:
                owed = bool(self._pending) or any(
                    unit not in deferred_loss for unit in self._pending_loss
                )
                delay = self._next_pass_delay_locked(deferred_loss, batching=False) if owed else 0.0
            if not owed:
                with self._lock:
                    return not self._pending and not self._pending_loss
            if self._clock() + delay > deadline:
                return False
            if delay:
                before = self._clock()
                self._sleep(delay)
                if self._clock() <= before:
                    # A sleep that does not move the clock would spin here forever.
                    return False

    def _next_pass_delay_locked(
        self, deferred_loss: "set[str] | None" = None, *, batching: bool = True
    ) -> float:
        """How long to wait before the next pass. ``_lock`` held, work is owed.

        The batching pause when a unit is claimable now -- skipped once shutdown has asked
        for quiescence, which is the only thing that pause gives up. Nothing claimable
        means every owed bucket is inside a backoff or held by an inline write, so the
        wait is exactly what the soonest of those has left: the writer sleeps instead of
        spinning, and one wedged crew log paces only its own entries.

        A shutdown does not COLLAPSE the backoff -- burning every attempt in no time at
        all against a filesystem that needed a moment converts a delay into a guaranteed
        loss, at the one point where the entries matter most. It CLAMPS it instead, to
        just inside the drain's own deadline, so a schedule longer than the budget still
        gets one attempt rather than none. See :meth:`_backoff_ready_at_locked`.
        """
        now = self._clock()
        soonest: float | None = None
        deferred_loss = deferred_loss or set()
        units = list(self._pending)
        units.extend(
            unit
            for unit in self._pending_loss
            if unit not in self._pending and unit not in deferred_loss
        )
        for unit in units:
            jobs = self._pending.get(unit)
            if not jobs and unit not in self._pending_loss:
                continue
            if self._claimable_locked(unit, now):
                if not batching or self._draining_for_shutdown:
                    return 0.0
                return self.limits.batch_deadline_seconds
            state = self._retry.get(unit)
            if state is None:
                continue  # held by an inline write rather than by a backoff
            ready_at = self._backoff_ready_at_locked(state)
            if soonest is None or ready_at < soonest:
                soonest = ready_at
        if soonest is None:
            return self.limits.batch_deadline_seconds
        return max(0.0, soonest - now)

    def _claimable_locked(self, unit: str, now: float) -> bool:
        """Whether the writer may take this unit's bucket. ``_lock`` held.

        Two reasons to leave it alone. Its retained batch is inside its backoff, and
        claiming early would burn an attempt against a filesystem that has not been given
        time to recover. Or a synchronous caller is writing inline for it, and claiming
        beside that is the reordering the inline gate exists to prevent.

        A bounded shutdown CLAMPS the first reason without waiving it -- see
        :meth:`_backoff_ready_at_locked`.
        """
        if unit in self._inline:
            return False
        state = self._retry.get(unit)
        return state is None or self._backoff_ready_at_locked(state) <= now

    def _backoff_ready_at_locked(self, state: _RetryState) -> float:
        """When this retained batch may next be attempted. ``_lock`` held.

        Its own schedule, except that a bounded shutdown drain pulls a time BEYOND the
        deadline back to just inside it. Waiting out a backoff that outlasts the process
        does not defer the write, it loses it -- and these are the last entries each unit
        produced.

        Clamped rather than collapsed. Zeroing the schedule would spend every remaining
        attempt in microseconds against a filesystem that needed a moment, converting a
        delay into a guaranteed drop at exactly the wrong time.

        The budget is divided into one slice per attempt, and attempt N is allowed at
        ``started + N * slice``. Waiting until the deadline would leave room for a single
        attempt; a slice each leaves room for all of them, which is strictly better for
        the case the backoff exists for -- several chances spread across the budget beat
        one chance at its last moment. The instants are ABSOLUTE for a reason: a rolling
        ``now + slice`` recomputes into the future on every check and the batch would
        never become claimable at all.
        """
        if not self._draining_for_shutdown or self._shutdown_deadline <= 0.0:
            return state.not_before
        budget = self._shutdown_deadline - self._shutdown_started
        if budget <= 0.0:
            return self._shutdown_started
        slice_secs = max(
            self.limits.min_shutdown_retry_gap, budget / self.limits.max_write_attempts
        )
        return min(state.not_before, self._shutdown_started + state.attempts * slice_secs)

    def _drain_once(self, deferred_loss: "set[str] | None" = None) -> "set[str]":
        """Write every claimable unit's owed entries, in per-unit order.

        Returns loss-only units whose marker was attempted but is still owed. The caller
        defers those until its next drain so a permanently failing marker cannot re-enter
        the same pass forever.
        """
        now = self._clock()
        deferred_loss = deferred_loss or set()
        with self._lock:
            claimed: "list[tuple[str, list[_Pending]]]" = []
            units = list(self._pending)
            units.extend(
                unit
                for unit in self._pending_loss
                if unit not in self._pending and unit not in deferred_loss
            )
            for unit in units:
                if not self._claimable_locked(unit, now):
                    continue
                jobs = self._pending.pop(unit, [])
                self._pending_count -= len(jobs)
                self._pending_bytes.pop(unit, None)
                loss = self._pending_loss.pop(unit, None)
                if loss is not None:
                    if jobs and jobs[0].loss is not None:
                        jobs[0].loss.merge(loss)
                    else:
                        jobs.insert(0, self._loss_marker_job(unit, loss))
                claimed.append((unit, jobs))
                self._claimed[unit] = self._claimed.get(unit, 0) + len(jobs)
        defer_until_next_drain: "set[str]" = set()
        for unit, jobs in claimed:
            marker_attempted = bool(jobs and jobs[0].loss is not None)
            marker_landed = self._write_batch(unit, jobs)
            if marker_attempted and not marker_landed:
                with self._lock:
                    if unit in self._pending_loss and unit not in self._pending:
                        defer_until_next_drain.add(unit)
        self._notify()
        return defer_until_next_drain

    def _write_batch(self, unit: str, jobs: "list[_Pending]") -> bool:
        """Write one unit's owed entries in order, stopping at the first FAILURE.

        Stopping rather than skipping ahead: the entries after a failed append belong
        after it in the file, so the remainder -- the failed entry and everything behind
        it -- goes to :meth:`_retain`, which puts it back at the front of this unit's
        bucket. Writing past it would put the log on disk in an order that never
        happened, which a fold reads as fact.

        A REFUSAL is different and the pass continues past it. A refused entry leaves the
        file byte-identical and will be refused again, so it is simply gone -- and the
        entries behind it would be waiting for something that is never going to land.

        A pass that wrote something before it failed calls :meth:`_note_progress` first,
        so the attempt budget starts over. That is what makes the retry terminate: the
        budget bounds a WEDGED writer, and a pass that shortened the buffer is not wedged.

        Returns whether this batch's loss marker landed, so a failed marker can be
        deferred without also deferring new loss created after a successful marker.
        """
        landed = False
        loss_marker_landed = False
        # Each job releases its own claim as it is ATTEMPTED, so a job asking what its
        # unit still owes is never told to wait for itself, and whatever this pass does
        # not reach is released on the way out -- retained, dropped-and-marked or
        # requeued by then, and countable there.
        unattempted = len(jobs)
        # One notification per pass, on whichever way this returns. The four exits each
        # mean something different to the buffer and nothing different to a reader, whose
        # only question is whether there is anything new on disk -- so the signal belongs
        # where every exit passes through rather than repeated at each of them, where an
        # exit added later would silently miss it.
        try:
            for index, job in enumerate(jobs):
                unattempted -= 1
                self._release_claim(unit, 1)
                if job.loss is None:
                    with self._lock:
                        loss_waiting = unit in self._pending_loss
                    if loss_waiting:
                        if landed:
                            self._note_progress(unit)
                        self._retain_without_failure(unit, jobs[index:])
                        return loss_marker_landed
                failure = self._run_job(job.run, job.what)
                if failure is None:
                    with self._lock:
                        if job.counted:
                            self._pending_total_bytes -= job.nbytes
                            job.counted = False
                    self._finish(job)
                    landed = True
                    loss_marker_landed = loss_marker_landed or job.loss is not None
                    continue
                if _is_refusal(failure):
                    if job.loss is not None:
                        self._drop(unit, jobs[index:], mark=True)
                        return False
                    # A permanent refusal owes a marker like any other loss. The refusal
                    # check cannot split a CrewLogError into its two causes -- a malformed
                    # entry the format rejected, or a well-formed entry refused because
                    # another process owns this log -- and the second is a genuine hole.
                    # Marking unconditionally is what makes the distinction unnecessary.
                    self._drop(unit, [job], mark=True)
                    continue
                if landed:
                    self._note_progress(unit)
                self._retain(unit, jobs[index:])
                return loss_marker_landed
            self._note_progress(unit)
            return loss_marker_landed
        finally:
            # The release goes first so this pass finishes its own bookkeeping before
            # handing the thread to a listener, which runs inline. The order is not
            # load-bearing today: whatever this pass did not reach is in `_pending` or
            # `_pending_loss` by the time it returns, and `owes` reads those too, so a
            # listener asking what the unit owes gets the same answer either way. It is
            # the order that stays correct if a listener ever reads the claim count
            # itself.
            self._release_claim(unit, unattempted)
            if landed and self._on_grew is not None:
                try:
                    self._on_grew(unit)
                except Exception as exc:
                    self._warnings.report("growth listener", exc, op="growth-listener")

    def _release_claim(self, unit: str, count: int) -> None:
        """Stop counting *count* of *unit*'s claimed entries. Takes ``_lock``.

        Called for a job as it is attempted and for whatever a pass never reached. By then
        each one is written, dropped with a loss marker, or back in the queue, so releasing
        it here removes a count that another source now carries.
        """
        if count <= 0:
            return
        with self._lock:
            held = self._claimed.get(unit, 0) - count
            if held > 0:
                self._claimed[unit] = held
            else:
                self._claimed.pop(unit, None)

    def _drain_inline_until(self, deadline: float) -> bool:
        """Write buffered entries from THIS thread until quiet or *deadline*.

        Used only by :meth:`drain_for_shutdown`, and only when no writer batch is claimed,
        so there is no second appender to race.

        Retries a batch that is inside its retry backoff, because the schedule cannot
        outlive the process: honouring it here means the entries are never written at
        all. But the attempts are PACED across the remaining budget rather than spent at
        once -- a batch has a small, fixed number of attempts before it is dropped, and
        burning them in microseconds would turn a filesystem that needed a moment into a
        permanent hole. So each pass is followed by a wait sized to leave one attempt per
        slice of what is left.

        Returns True when the buffers reached empty with nothing in flight.
        """
        deferred_loss: "set[str]" = set()
        while True:
            deferred_loss.update(self._drain_once(deferred_loss))
            if self._quiet():
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            with self._lock:
                owed = bool(self._pending) or any(
                    unit not in deferred_loss for unit in self._pending_loss
                )
            if not owed:
                # Nothing left to attempt; anything short is a claimed batch or a marker
                # deferred to the next drain, neither of which this pass may touch again.
                return self._quiet()
            self._sleep(
                min(
                    remaining,
                    max(
                        self.limits.min_shutdown_retry_gap,
                        remaining / self.limits.max_write_attempts,
                    ),
                )
            )


__all__ = [
    "DEFAULT_LIMITS",
    "CrewLogWriter",
    "DrainReport",
    "JobKind",
    "WarningBudget",
    "WriteJob",
    "WriterLimits",
    "WriterStats",
]

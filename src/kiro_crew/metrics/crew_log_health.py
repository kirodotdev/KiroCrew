"""Crew log health instruments -- is the log keeping up, and is it losing anything.

This module holds both batches of RFC section 6.5's crew log health table.
The first batch counts what the log loses and how its eager folds run
(``write.lost``, ``fold.apply``, ``fold.start``). The second watches the
log's backlog, its savepoints and its bus (``savepoint.write``,
``eager.queue_depth``, ``writer.buffered``, ``bus.subscriber_error``). The
two share the duration ladder (:data:`BUCKETS`) and the ``other`` fallback;
each section below states the constraints its own instruments are shaped by.

Second batch: backlog, savepoints and the bus
=============================================

The crew log is the durable record every fold, every dashboard read and every
resumed session is built from, and main has no metric for it at all. An operator
can see that a session's board looks stale and cannot see why: whether the writer
is holding a backlog, whether savepoints are being written or refused, whether
the eager fold queue is backed up, or whether a subscriber is quietly raising on
every published fold. The in-process ``WriterStats`` and a handful of warning
logs are the only evidence today, and neither is sampled over time.

The second batch:

====================================================  =========  ======================
name                                                  type       attributes
====================================================  =========  ======================
``kirocrew.crew_log.savepoint.write``                 counter    ``kind``, ``outcome``,
                                                                 ``bucket``
``kirocrew.crew_log.eager.queue_depth``               gauge      --
``kirocrew.crew_log.writer.buffered``                 gauge      --
``kirocrew.crew_log.bus.subscriber_error``            counter    ``kind``
====================================================  =========  ======================

NOT ONE LINE OF THE WRITE PATH IS TOUCHED, and that is the constraint that
decided the shape of this module -- including what is missing from it. An append
reaches disk on the write-behind's batch pass
(:meth:`~kiro_crew.crew_log.writer.CrewLogWriter._write_batch`) or on its inline
fast path, and both are on the latency path of a turn that commits an entry. So
``crew_log/writer.py`` and ``crew_log/store_segment_writes.py`` gain nothing from
these four instruments. (The first batch edits one writer method,
``CrewLogWriter._drop``, so that the dropped count it already keeps never
publishes a value it is about to correct; that adds no work to the pass.)

THREE INSTRUMENTS OF RFC 6.5's SECOND BATCH ARE ABSENT, each because the only way
to produce it is work on that path:

* ``append.batch`` (write + fsync time). Nothing keeps per-batch timing today and
  the only source is a ``time.monotonic()`` pair on the batch pass.
* ``append.bytes``. Every available source is either work on that path or wrong.
  Counting at the write is a ``counter.add`` or an integer field on the pass
  itself. Deriving it on the reader thread from what the write-behind already
  keeps does not work either: the only byte figures it holds are the LIVE pending
  totals (``_pending_total_bytes``, ``_pending_bytes``), and recovering "written"
  from them needs a cumulative accepted total that does not exist, so it would
  take a new field on the enqueue side -- itself on the turn path -- and would
  still be inexact three ways. The inline fast path runs a job without ever
  buffering it, so its bytes never enter the pending total at all; there is no
  cumulative dropped-byte figure to subtract, since a unit's loss debt is
  consumed when its marker is written; and the underlying ``nbytes`` is the
  producer's size hint, which is a CHARACTER count carried by only the two
  body-bearing entry types, so a non-ASCII message would be under-reported by
  roughly a third under a metric whose unit says bytes and every other entry
  type would contribute zero.
* ``fold.lag``, which the RFC itself defers for the same reason.

A metric that is absent costs a question nobody can answer yet. A metric that is
present and wrong costs more, and an operator cannot tell which one they are
reading -- so these three wait for a source that does not sit on the write path.

THE ONE PLACE A METRIC CALL CAN REACH A WRITE THREAD is the bus failure branch,
and it is worth being exact about rather than claiming otherwise.
``bus.publish(TREE_ADVANCED)`` is reached from the session-tree projection, which
a ``session/opened`` or ``session/spawned`` job advances while the write-behind
is running that job -- so a subscriber of that kind runs on the write thread, as
``dashboard/handlers/crew_log.py`` says in as many words. A subscriber that
RAISES there therefore reaches :func:`bus_subscriber_error`. Three things bound
it: the normal path (a subscriber that returns) adds nothing at all; the raising
path already formats and emits a WARNING log, which costs more than the counter
beside it; and :func:`_loaded` means the counter is a no-op until something else
has imported this module, so a write thread can never trigger the first import
of the metrics facade. ``FOLD_ADVANCED`` is published only from the eager fold
worker and does not reach a write thread at all.

The two gauges follow rule 5 of the RFC ("how many right now" is read from live
state, never an up/down counter a crash can leave drifting): both are observable
callbacks that read the live queue and the live writer when a reader collects.

A reader that cannot answer yields NO observation rather than a zero, the rule
``process_gauges`` and ``inventory_gauges`` state at length: "no write-behind was
ever built in this process" and "the write-behind is holding nothing" are
different facts and a fake zero would merge them. Nothing here brings a subsystem
into being either, and not merely as an object: each reader resolves its source
from ``sys.modules`` (:func:`_loaded`) and answers ``None`` when the module is not
loaded, so a collection every export interval cannot be the thing that pulls the
crew log into a process that has it switched off.

HOW EACH COUNTER REACHES THIS MODULE differs between the two, and the difference
is deliberate rather than an inconsistency. ``checkpoint`` IMPORTS
:func:`savepoint_write`, because it is off the write thread and an import there
costs nothing that matters -- so no savepoint point is ever dropped. ``bus``
reaches :func:`bus_subscriber_error` through ``sys.modules``, because it can run
on a write thread and must never be what first loads the metrics facade. The
price of that is a real window: this module enters ``sys.modules`` when
``provider._build_recorder()`` registers the gauges, that build is lazy, and a
subscriber raising before it is logged at WARNING but not counted. The window
closes at the first ``get_recorder()`` from anywhere, which the gateway reaches
at boot.

The first batch puts this module in ``sys.modules`` one other way: the eager
fold worker imports it when it claims its thread
(:func:`claim_fold_worker_thread`). That import runs on the fold thread, never
on a write thread, and the same worker's first pass calls ``get_recorder()``
right after it. So the only new case is a subscriber raising on a write thread
between that claim and that first pass, in a process with telemetry on and no
recorder yet: that point is counted, and the count is what builds the recorder.

Consent: every instrument rides ``telemetry.enabled``. The observable ones are
registered on ``provider._build_recorder()``'s live path, which runs only behind
the consent gate; the two synchronous counters go through
``metrics.events.emit_counter`` -> ``get_recorder()``, which is a no-op recorder
when telemetry is off.

Cardinality: four closed enums and nothing else. :data:`SAVEPOINT_KINDS` (3,
counting ``other``), :data:`OUTCOMES` (2) and :data:`BUCKETS` (4), so
``savepoint.write`` is at most 3 x 2 x 4 = 24 series; :data:`BUS_EVENT_KINDS` (3)
makes ``bus.subscriber_error`` at most 3. No unit id, slot key, session id, fold
name, path or exception text is ever an attribute: a slot key and a session id
are unbounded, and the fold name belongs to the first batch's ``fold.apply``, not
here. An unknown value maps to ``other`` rather than passing through, which is
why ``other`` is a member of the enum rather than a value outside it.

First batch: losses and folds
=============================

Three instruments, and all three exist because the crew log is the record every
dashboard, conductor and work ledger reads, and nothing today reports whether
that record is complete or how expensive it is to rebuild:

``kirocrew.crew_log.write.lost``
    Entries the log never got, split by ``cause``. A non-zero reading is a real
    hole: a storage refusal the writer gave up on (``dropped``), an append the
    buffer rejected at its memory ceiling (``overflowed``), or a fold wake the
    eager queue could not hold (``wake_dropped`` -- the folds stay correct, the
    dashboard just sees the value later).
``kirocrew.crew_log.fold.apply``
    One point per fold applied over one batch, with the pass duration as a
    closed ``bucket``. Answers "is folding getting slower, and which fold".
``kirocrew.crew_log.fold.start``
    One point per fold start, by where the state came from: a warm in-memory
    cell, a savepoint on disk, or a cold rebuild from the first record. A rising
    ``cold`` share is the signal that savepoints are not being kept.

THE APPEND THREAD GAINS NOTHING, which is the constraint this module is shaped
by rather than a property it happens to have. The crew log's append path is one
``put_nowait`` plus a set membership test (``eager.note_commit``), and a turn
waits on it, so a counter call there would be new work on the one path that must
stay cheap. Two mechanisms keep it out:

*   Every ``write.lost`` cause is detected ON the append thread or on a
    producer's own thread, and each one is ALREADY counted by the subsystem that
    detects it -- ``WriterStats.dropped`` / ``.overflowed`` for the writer,
    ``eager.dropped_wakes()`` for the fold queue. So this module adds no call at
    those sites at all: it registers ONE observable gauge whose callback runs
    on the metrics reader's own ticker thread, reads those three numbers (each an
    O(1) counter read, no disk, no lock held across anything) and reports what
    has accrued since the last collection. ``test_crew_log_health_threads.py``
    pins that the append path records nothing.
*   ``fold.apply`` and ``fold.start`` are recorded only on the eager fold
    worker thread. The worker CLAIMS its thread once at start
    (:func:`claim_fold_worker_thread`), and every record site begins with a
    thread-local lookup returning immediately on every other thread -- so the
    same fold-start branches, reached from a dashboard read or from a CLI,
    record nothing. That is also what keeps the metric a count of EAGER folding
    rather than of reads, which is the question section 6.5 asks.

NOTHING IS RECORDED FROM INSIDE A FOLD, and that is the second half of the same
idea. Two of the start branches run while holding a per-cell fold lock a
dashboard read can be waiting on, and a FIRST ``get_recorder()`` builds the whole
telemetry provider synchronously. So a branch only leaves a thread-local mark
(:func:`note_fold_origin`, :func:`note_fold_idle`), and the eager worker turns the
marks into points afterwards, outside every lock, with :func:`note_fold_pass`.
The marks are keyed by scope because a pass can nest: resolving which units a
slot fold covers can fold a session log.

WHAT ``fold.apply`` MEANS, exactly, because it is narrower than "a fold ran". It
counts a fold THE EAGER WORKER'S OWN PASS ADVANCED. A pass an unchanged live cell
answered advanced nothing and records a start with no apply, as does one that was
refused or that raised -- so ``fold.start`` above ``fold.apply`` is the share of
eager folding that produced no value. One consequence is worth knowing before
reading the ratio: when a dashboard READ reaches a growing cell first, the read
does the folding and the worker's pass behind it finds nothing changed, so that
fold is counted as a start and not as an apply. The two series together say how
much work eager folding is doing, not how much folding the process did.

The duration bucket measures the pass end to end, INCLUDING any wait for the
per-cell fold lock. A pass held up behind a reader is slow eager folding, which
is the thing being measured.

CARDINALITY. Every attribute value is a closed enum: three causes, two scopes,
three origins, four buckets, and the fold catalogue
(:data:`~kiro_crew.crew_log.projection_catalogue.FOLD_NAMES`, 18 names). The
catalogue is read straight from its one owner, so a fold the crew log gains is a
fold this metric labels. Nothing here raises at IMPORT, which matters because the
fold surface imports this module at module scope: a second literal copy of that
catalogue, with a check that raised when the two drifted, would make a forgotten
label edit take down every dashboard read and session fold rather than costing a
metric.

TEMPORALITY, and why ``write.lost`` is a GAUGE carrying a total. ``fold.apply``
and ``fold.start`` are synchronous counters and export DELTA like every other
counter (``metrics/temporality.py``). ``write.lost`` reads a total it does not
own, which can only be an asynchronous instrument -- and an observable COUNTER
would export CUMULATIVE, which this project allows nowhere: one cumulative series
forces every consumer into stateful whole-hour aggregation, and
``test_no_instrument_exports_cumulative`` asserts it over every exported
instrument. Declaring DELTA for it instead is the move ``temporality.py`` records
as tried and reverted, because the delta baseline lives in the provider and the
provider is rebuilt whenever telemetry consent changes, so the first collection
after a rebuild would re-emit a whole process total as one delta. So this follows
the shape the process CPU and GC readings already took for the same reason: an
observable GAUGE whose reading IS the process-lifetime total. A gauge carries no
temporality, merges one datapoint at a time, and the loss RATE is the consumer's
difference between two readings.

The value reported is a total this module accumulates itself from the growth it
reads, so it is monotonic even across a counter that is replaced by a fresh one,
and it starts at zero for the process rather than at whatever the writer had
accrued before telemetry was enabled. :func:`write_lost_totals` reads and accrues
under ONE lock hold because more than one reader collects, and :func:`_accrue`
accrues only growth because a reading can go down for two unrelated reasons; both
are explained there.

Everything is behind ``telemetry.enabled``: ``get_recorder()`` is a no-op
recorder when telemetry is off, and the observable gauge is registered only on
``provider._build_recorder()``'s live path.

The fold labels come from ``crew_log/projection_catalogue.py``, imported at
module scope. That loads the crew log's entry-type catalogue and nothing that
holds state: not the writer, not the store, not the eager queue, all of which
the readers above still resolve only through ``sys.modules``.

OSS-CLEAN: opentelemetry (Apache-2.0) + stdlib + first-party helpers only.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import TYPE_CHECKING, Collection, Final, Iterable, Optional

from kiro_crew.crew_log.projection_catalogue import FOLD_NAMES
from kiro_crew.metrics.schema import validate_name

if TYPE_CHECKING:  # annotation-only; never imported at runtime
    from types import ModuleType

    from opentelemetry.metrics import CallbackOptions, Meter, Observation

    # The recorder's own attribute type, so the value union has ONE owner. A
    # ``dict[str, str]`` is not assignable to it (``Dict`` is invariant), and
    # restating the union here would be a second copy to drift. Annotation-only:
    # this module is imported by the fold surface and must not pull the
    # recorder, and through it the OTEL metrics SDK, onto that path.
    from kiro_crew.metrics.recorder import Attributes

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

COUNTER_SAVEPOINT_WRITE: Final[str] = "kirocrew.crew_log.savepoint.write"
GAUGE_EAGER_QUEUE_DEPTH: Final[str] = "kirocrew.crew_log.eager.queue_depth"
GAUGE_WRITER_BUFFERED: Final[str] = "kirocrew.crew_log.writer.buffered"
COUNTER_BUS_SUBSCRIBER_ERROR: Final[str] = "kirocrew.crew_log.bus.subscriber_error"

#: Every name the SECOND batch registers or emits, validated at its registration.
#: The first batch's three are :data:`WRITE_LOST`, :data:`FOLD_APPLY` and
#: :data:`FOLD_START` below; ``write.lost`` is validated where it is registered.
ALL_METRIC_NAMES: Final[tuple[str, ...]] = (
    COUNTER_SAVEPOINT_WRITE,
    GAUGE_EAGER_QUEUE_DEPTH,
    GAUGE_WRITER_BUFFERED,
    COUNTER_BUS_SUBSCRIBER_ERROR,
)

# --------------------------------------------------------------------------- #
# Closed enums
# --------------------------------------------------------------------------- #

#: Which savepoint was written: one session's folds, or one slot fold across the
#: units a slot ran under. The two have different write paths and different
#: failure modes, and reading them apart is the point of the attribute.
#: ``other`` is a member because that is where an unrecognised value is mapped,
#: and an enum that does not list its own fallback understates its series count.
SAVEPOINT_KINDS: Final[tuple[str, ...]] = ("session", "slot", "other")

#: Whether the savepoint reached its file. ``failed`` covers a store that refused
#: the payload and a raise on the way through. A fold that was SKIPPED because the
#: pass's witness certifies another boundary is not a failure and is not counted.
OUTCOMES: Final[tuple[str, ...]] = ("written", "failed")

#: Duration bands, in the order the edges are applied, shared by
#: ``savepoint.write`` and ``fold.apply`` through :func:`bucket_for`. Stated here
#: and in ``docs/system-specs/modules/metrics.md``; a histogram is deliberately
#: not used (RFC rule 2), because these four bands are the whole question and a
#: backend that drops explicit histogram buckets would lose them. Each bound is
#: EXCLUSIVE, so exactly 10.0ms is ``lt_100ms``.
BUCKETS: Final[tuple[str, ...]] = ("lt_10ms", "lt_100ms", "lt_1s", "ge_1s")

#: The bus event kinds a subscriber can raise on, plus ``other`` for a kind this
#: enum has not been taught (the bus carries whatever a publisher sends).
BUS_EVENT_KINDS: Final[tuple[str, ...]] = ("fold_advanced", "tree_advanced", "other")


def bucket_for(elapsed_ms: float) -> str:
    """Which of :data:`BUCKETS` *elapsed_ms* falls in.

    A negative or non-finite reading lands in the fastest band rather than
    inventing a value: the only way to produce one is a clock that went
    backwards, and a savepoint write is not the place to argue about it.
    """
    try:
        value = float(elapsed_ms)
    except (TypeError, ValueError):
        return BUCKETS[0]
    if not value == value:  # NaN
        return BUCKETS[0]
    if value < 10.0:
        return BUCKETS[0]
    if value < 100.0:
        return BUCKETS[1]
    if value < 1000.0:
        return BUCKETS[2]
    return BUCKETS[3]


def _closed(value: str, allowed: "Collection[str]", fallback: str) -> str:
    """*value* when the enum holds it, else *fallback*. RFC rule 3."""
    return value if value in allowed else fallback


# --------------------------------------------------------------------------- #
# Synchronous counters -- both on paths that are not the append path
# --------------------------------------------------------------------------- #


def savepoint_write(*, kind: str, written: bool, elapsed_ms: float) -> None:
    """Count one savepoint write attempt.

    Called once per attempt that reached the savepoint store, from
    ``checkpoint.save`` (``kind="session"``) and ``checkpoint.save_slot``
    (``kind="slot"``). A call that returned before attempting a write -- no write
    earned yet, no witness, the unit gone, the lease contended -- records
    nothing, because nothing was written and a point there would report refusals
    as failures.

    Both call sites are on the fold and read path, never on a write thread: a
    savepoint is written by whoever just folded, roughly once per 256 entries.

    Raises nothing an ``Exception`` handler would catch -- ``emit_counter``
    swallows its own failures -- and the caller
    (``checkpoint._count_savepoint_write``) guards it as well, which is what
    covers the rest.
    """
    from kiro_crew.metrics.events import emit_counter

    emit_counter(
        COUNTER_SAVEPOINT_WRITE,
        {
            "kind": _closed(kind, SAVEPOINT_KINDS, "other"),
            "outcome": OUTCOMES[0] if written else OUTCOMES[1],
            "bucket": bucket_for(elapsed_ms),
        },
    )


def bus_subscriber_error(kind: str) -> None:
    """Count one crew log bus subscriber that raised.

    A failure path only: the bus has no queue (an event with no subscriber is
    dropped by design), so there is no backlog to measure and a subscriber's
    exception is the single thing worth counting. The publisher is told nothing
    about it, by design -- it has already done the fold -- so the count is the
    only thing that makes a subscriber failing on every event visible. The log
    line beside it is at WARNING, which a default install does collect, but a log
    line carries no rate and no total, which is the question an operator asks.

    This is the one call in this module that a crew log WRITE thread can reach;
    the module docstring states the path and what bounds it. Its caller
    (``bus._count_subscriber_error``) therefore reaches this function through
    ``sys.modules`` rather than importing it, which costs a point when telemetry
    is on and no recorder has been built yet -- that caller documents the window.
    :func:`savepoint_write` is off that thread and has no such gap.

    Raises nothing an ``Exception`` handler would catch, and the caller guards it
    as well.
    """
    from kiro_crew.metrics.events import emit_counter

    emit_counter(
        COUNTER_BUS_SUBSCRIBER_ERROR,
        {"kind": _closed(kind, BUS_EVENT_KINDS, "other")},
    )


# --------------------------------------------------------------------------- #
# Live-state readers for the observable instruments
# --------------------------------------------------------------------------- #


def _loaded(name: str) -> "ModuleType | None":
    """The module *name* if something has ALREADY imported it, else ``None``.

    Never imports. A reader here must not be the thing that pulls the crew log
    into a process: ``card_lifecycle`` states that the package is not loaded at
    all when the feature is off, and an ordinary ``import`` inside a callback
    that runs every export interval would load the storage layer on exactly the
    installs that disabled it. If the module is absent then so is the state it
    holds, which is the same answer either way.
    """
    return sys.modules.get(name)


def read_eager_queue_depth() -> Optional[int]:
    """How many fold wakes are waiting right now, or ``None``.

    ``None`` when no eager queue exists in this process: a gateway that has
    committed no entry has no fold thread and no queue, and that is a different
    fact from an empty queue. Never creates the queue and never imports its
    module.
    """
    try:
        eager = _loaded("kiro_crew.crew_log.eager")
        if eager is None:
            return None
        return eager.queue_depth()
    except Exception:  # noqa: BLE001 - a probe must never break the exporter
        logger.debug("crew log eager queue depth unreadable", exc_info=True)
        return None


def read_writer_buffered() -> Optional[int]:
    """How many entries the writer is holding right now, or ``None``.

    ``None`` when no writer exists in this process, for the reason
    :func:`read_eager_queue_depth` gives: "no writer" and "an empty writer" are
    different facts.

    Reads ``emit_runtime._writer_instance`` rather than calling ``_writer()``:
    the accessor BUILDS the writer on first use, and a telemetry collection must
    never bring a subsystem into being (the rule ``inventory_gauges`` states for
    the knowledge database).
    """
    try:
        emit_runtime = _loaded("kiro_crew.crew_log.emit_runtime")
        if emit_runtime is None:
            return None
        writer = emit_runtime._writer_instance
        if writer is None:
            return None
        return int(writer.stats().buffered)
    except Exception:  # noqa: BLE001 - a probe must never break the exporter
        logger.debug("crew log writer buffered unreadable", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def register_crew_log_health_instruments(meter: "Meter") -> None:
    """Register the observable crew log health instruments on *meter*.

    Called once per live ``MeterProvider`` build, from
    ``provider._build_recorder()``'s consent-gated path, alongside the process
    and inventory gauges. Best-effort: a failure here loses these gauges and
    never telemetry as a whole.

    The ``write.lost`` gauge registered last here leaves its BASELINES AND TOTALS
    alone. Registering is not the start of the stream: the recorder is rebuilt
    in-process whenever telemetry consent changes, and
    ``provider._consent_worker`` can build a recorder it then DISCARDS because a
    newer consent superseded it. A registration that cleared this module's state
    would let that discarded build zero the totals of the provider still serving,
    which both loses the losses already accrued and makes the series go
    backwards. The baseline is taken instead by the first COLLECTION of the
    process, which is what keeps losses accrued before telemetry was enabled out
    of the stream. Its callback runs only when a reader collects, on that
    reader's own ticker thread -- no sampler of ours, no work at rest, and
    nothing on the crew log's append thread.

    The two synchronous counters (:func:`savepoint_write`,
    :func:`bus_subscriber_error`) are NOT registered here -- the recorder facade
    creates their instruments on first emit, as every other counter's does.

    The callback wrapper comes from ``process_gauges``: it is the module that
    states the no-observation-rather-than-a-zero contract at length, and these
    gauges answer ``None`` for exactly that reason, so sharing it keeps one
    definition of what a probe returning nothing means. It is imported here
    rather than at module scope because it reaches for the OTEL SDK.
    """
    try:
        from kiro_crew.metrics.process_gauges import _observations

        for name in ALL_METRIC_NAMES:
            validate_name(name)
        validate_name(WRITE_LOST)

        meter.create_observable_gauge(
            GAUGE_EAGER_QUEUE_DEPTH,
            callbacks=[_observations(read_eager_queue_depth)],
            unit="1",
            description=(
                "Crew log fold wakes waiting to be folded, read live at collection. "
                "Absent when this process has committed no entry"
            ),
        )
        meter.create_observable_gauge(
            GAUGE_WRITER_BUFFERED,
            callbacks=[_observations(read_writer_buffered)],
            unit="1",
            description=(
                "Crew log entries the writer is holding, read live at collection. "
                "Absent when this process built no writer"
            ),
        )
        meter.create_observable_gauge(
            name=WRITE_LOST,
            unit="1",
            description=_DESCRIPTIONS[WRITE_LOST],
            callbacks=[_write_lost_observations],
        )
    except Exception as exc:  # noqa: BLE001 - telemetry must never break boot
        logger.warning("crew log health instrument registration failed: %s", exc)


# --------------------------------------------------------------------------- #
# First batch: write.lost, fold.apply, fold.start
# --------------------------------------------------------------------------- #

WRITE_LOST: Final[str] = "kirocrew.crew_log.write.lost"
FOLD_APPLY: Final[str] = "kirocrew.crew_log.fold.apply"
FOLD_START: Final[str] = "kirocrew.crew_log.fold.start"

#: Why an entry or a wake was lost. ``dropped`` and ``overflowed`` are holes in
#: the log itself and are different failures (a stuck disk vs a saturated one);
#: ``wake_dropped`` is not a hole -- the next read carries the fold forward -- so
#: it is a separate cause rather than folded into the other two.
CAUSE_DROPPED: Final[str] = "dropped"
CAUSE_OVERFLOWED: Final[str] = "overflowed"
CAUSE_WAKE_DROPPED: Final[str] = "wake_dropped"
CAUSES: Final[tuple[str, ...]] = (CAUSE_DROPPED, CAUSE_OVERFLOWED, CAUSE_WAKE_DROPPED)

#: What a fold is keyed by. A slot fold joins every unit of one board; a session
#: fold reads one unit's file.
SCOPE_SLOT: Final[str] = "slot"
SCOPE_SESSION: Final[str] = "session"
SCOPES: Final[tuple[str, ...]] = (SCOPE_SLOT, SCOPE_SESSION)

#: Where a fold's starting state came from.
ORIGIN_WARM: Final[str] = "warm"
ORIGIN_SAVEPOINT: Final[str] = "savepoint"
ORIGIN_COLD: Final[str] = "cold"
ORIGINS: Final[tuple[str, ...]] = (ORIGIN_WARM, ORIGIN_SAVEPOINT, ORIGIN_COLD)

#: The fold names this metric may carry: the crew log's own catalogue, resolved
#: ONCE at import rather than per record. One owner, so there is no second copy
#: to drift -- a fold the crew log gains is a fold this metric labels, and the
#: series count stays bounded because the catalogue is closed (18 names today,
#: listed in ``docs/system-specs/modules/metrics.md`` for a reader).
FOLD_LABELS: Final[frozenset[str]] = frozenset(FOLD_NAMES)

#: Collapse target for a value outside its enum (RFC section 4 rule 3).
OTHER: Final[str] = "other"

_DESCRIPTIONS: Final[dict[str, str]] = {
    WRITE_LOST: "Crew log entries or fold wakes lost, by cause",
    FOLD_APPLY: "Crew log folds applied over one eager batch",
    FOLD_START: "Crew log fold starts, by where the state came from",
}


# --------------------------------------------------------------------------- #
# The clock, and the thread that may record a fold
# --------------------------------------------------------------------------- #

#: Indirection so a test can drive the bucket edges with values it chooses.
#: Sleeping to cross an edge would make the test both slow and flaky, and the
#: 1s edge is not reachable by sleeping at all within a sane test budget.
_now = time.monotonic

#: The eager fold worker's claim on its own thread, and the stamps a fold pass
#: leaves on it. A ``threading.local`` rather than the worker's thread object, so
#: this module holds no reference to the crew log's state and the check costs one
#: attribute lookup.
_claim = threading.local()


def claim_fold_worker_thread() -> None:
    """Mark the CALLING thread as the eager fold worker.

    Called once by the worker as it starts. Everything in this module that
    records a fold refuses to record on any other thread, which is what makes
    ``fold.apply`` and ``fold.start`` counts of eager folding rather than of
    every read that happens to fold the same cell.
    """
    _claim.fold_worker = True


def on_fold_worker_thread() -> bool:
    """Whether the calling thread is the one allowed to record a fold."""
    return getattr(_claim, "fold_worker", False) is True


# --------------------------------------------------------------------------- #
# fold.start / fold.apply -- eager worker thread only
# --------------------------------------------------------------------------- #


def begin_fold_pass() -> float:
    """Open a fold pass on this thread: clear its stamps, answer the start instant.

    The stamps are cleared HERE rather than only when they are taken, because a
    fold that raises before it reaches a start branch leaves whatever the
    previous pass stamped. Clearing at the open means a pass can only ever
    report its own.
    """
    _claim.origin = {}
    _claim.idle = set()
    return _now()


def note_fold_origin(scope: str, origin: str) -> None:
    """Stamp where the *scope* pass on this thread got its starting state.

    RECORDS NOTHING. This is called from inside the fold's own branches, and two
    of those hold a per-cell lock: a first ``get_recorder()`` there can build the
    whole telemetry provider synchronously and would do it while holding a lock a
    dashboard read is waiting on. So the branch leaves a thread-local mark and
    :func:`note_fold_pass` turns it into a point afterwards, outside every lock.

    KEYED BY SCOPE because a pass can nest: resolving which units a slot fold
    covers can fold a session log, and an unkeyed mark would let the inner fold's
    origin be reported as the outer one's.

    A NO-OP on every thread but the eager fold worker's, and the check is the
    first thing it does -- so the start branches stay free for a dashboard read,
    a CLI and the append path alike.
    """
    if not on_fold_worker_thread():
        return
    marks = getattr(_claim, "origin", None)
    if marks is None:
        marks = _claim.origin = {}
    # LAST ONE WINS for a given scope. ``fold_session`` folds twice when the file
    # changed identity under the first pass, and the origin that describes what
    # the caller is actually served is the one the retry folded from.
    marks[scope] = origin


def note_fold_idle(scope: str) -> None:
    """Stamp that the *scope* pass on this thread advanced nothing.

    RECORDS NOTHING, for the reason :func:`note_fold_origin` gives. Without this
    a pass that an unchanged live cell answered would count as an apply, and
    ``fold.start`` above ``fold.apply`` would stop meaning "folding that produced
    no value".
    """
    if not on_fold_worker_thread():
        return
    idle = getattr(_claim, "idle", None)
    if idle is None:
        idle = _claim.idle = set()
    idle.add(scope)


def note_fold_pass(scope: str, folds: "Iterable[str]", started: float) -> None:
    """Close the *scope* pass: one ``fold.start``, and one ``fold.apply`` per fold.

    Called by the eager fold worker once per pass, OUTSIDE any fold lock. Takes
    and clears this thread's stamps for *scope*, so a later pass cannot report
    them again.

    No start was stamped means the fold raised before it chose one, and nothing
    is recorded: there was no pass to describe. A pass stamped IDLE advanced
    nothing, so it records its start and no apply.

    The clock is read ONCE, here, before any point is recorded. Every fold the
    pass applied shares that one duration, so they land in the same bucket, and
    the first ``get_recorder()`` call -- which can build the whole telemetry
    provider -- is not charged to the fold.
    """
    if not on_fold_worker_thread():
        return
    elapsed_ms = (_now() - started) * 1000.0
    marks = getattr(_claim, "origin", None) or {}
    idle = getattr(_claim, "idle", None) or set()
    origin = marks.pop(scope, None)
    was_idle = scope in idle
    idle.discard(scope)
    if origin is None:
        return
    _count(
        FOLD_START,
        {"scope": _closed(scope, SCOPES, OTHER), "origin": _closed(origin, ORIGINS, OTHER)},
    )
    if was_idle:
        return
    for fold in folds:
        _count_apply(scope, fold, elapsed_ms)


def note_fold_apply(scope: str, fold: str, started: float) -> None:
    """Count one application of *fold* over one batch, timed from *started*.

    *started* comes from :func:`begin_fold_pass`. Only a fold that APPLIED is
    counted: a pass the read path refused, one that raised, and one that found
    nothing to move all record nothing, so ``fold.start`` above ``fold.apply``
    is how much folding is starting without producing a value.

    A NO-OP on every thread but the eager fold worker's, as :func:`note_fold_origin`
    is and for the same reason. :func:`note_fold_pass` does not call this: it
    reads the clock once for the whole pass and records through
    :func:`_count_apply` directly.
    """
    if not on_fold_worker_thread():
        return
    _count_apply(scope, fold, (_now() - started) * 1000.0)


def _count_apply(scope: str, fold: str, elapsed_ms: float) -> None:
    """Record one ``fold.apply`` point for a duration already measured."""
    _count(
        FOLD_APPLY,
        {
            "scope": _closed(scope, SCOPES, OTHER),
            "fold": _closed(fold, FOLD_LABELS, OTHER),
            "bucket": bucket_for(elapsed_ms),
        },
    )


def _count(name: str, attrs: "Attributes") -> None:
    """Add one to the counter *name*. Never raises.

    The provider import is function-local: this module is reached from the crew
    log's fold surface, and a module-scope import would put the OpenTelemetry
    metrics SDK's import chain behind it.
    """
    try:
        from kiro_crew.metrics.provider import get_recorder

        get_recorder().counter(name, 1, unit="1", attrs=attrs, description=_DESCRIPTIONS[name])
    except Exception:  # noqa: BLE001 - telemetry must never cost a fold
        logger.debug("crew log health counter %r failed", name, exc_info=True)


# --------------------------------------------------------------------------- #
# write.lost -- the metrics reader thread only
# --------------------------------------------------------------------------- #

#: Guards the two tables below. The callback runs on the reader's ticker thread
#: and a second reader (the local sink plus an OTLP destination) has its own, so
#: two collections can overlap.
_lost_guard = threading.Lock()
#: Per cause, the last raw reading taken from the owning subsystem. ``None``
#: until the first collection, which is the BASELINE: losses a process accrued
#: before telemetry was enabled are not this metric's to report.
_lost_seen: "dict[str, int]" = {}
#: Per cause, the monotonic total this module has accumulated from the deltas it
#: read. In memory only, so a restart starts again at zero and no loss counted
#: before it is counted again.
_lost_total: "dict[str, int]" = {}


def reset_for_tests() -> None:
    """Forget the ``write.lost`` baselines and totals.

    A test seam, named as one. The tables are process-wide, so one case's
    readings would otherwise become the next case's baseline.
    """
    with _lost_guard:
        _lost_seen.clear()
        _lost_total.clear()


def _read_raw_losses() -> "dict[str, int]":
    """The three loss counts their owning subsystems already keep.

    Read through ``sys.modules`` rather than by importing, and that is not a
    micro-optimisation: this runs on the metrics reader's ticker thread, and
    importing the crew log's writer or fold queue FROM a telemetry callback would
    put that whole import chain on a thread no caller is waiting on, in a process
    that may never have appended anything. A module that was never imported has
    nothing to report, which is exactly ``0``.
    """
    counts = {cause: 0 for cause in CAUSES}
    emit = sys.modules.get("kiro_crew.crew_log.emit")
    if emit is not None:
        counts[CAUSE_DROPPED] = int(emit.dropped_writes())
        counts[CAUSE_OVERFLOWED] = int(emit.overflow_writes())
    eager = sys.modules.get("kiro_crew.crew_log.eager")
    if eager is not None:
        counts[CAUSE_WAKE_DROPPED] = int(eager.dropped_wakes())
    return counts


def _accrue(cause: str, current: int) -> int:
    """Fold *current* into *cause*'s monotonic total and return it.

    ONLY GROWTH IS ACCRUED, and a reading lower than the last one moves the
    watermark down without adding anything. That rule has to hold both ways round
    for two different reasons:

    *   A counter can be REPLACED by a fresh one. A writer rebuilt mid-process
        starts again at zero, and lowering the watermark means its next loss is
        counted once rather than its whole new total being added to a total that
        already stands.
    *   A counter can momentarily read HIGH. ``CrewLogWriter._drop`` adds a whole
        failed batch to its dropped count and then takes the follow-on jobs back
        out, so a reader landing between the two sees a count that is about to be
        corrected. Treating that dip as a fresh counter (adding the whole new
        reading) turned one requeued job into an inflated total that never came
        back down -- it is the defect this rule exists to prevent.

    A corrected reading cannot be un-counted, because the reported total must
    not go backwards; the source is fixed instead, so the
    transient is never published (see ``CrewLogWriter._drop``).
    """
    seen = _lost_seen.get(cause)
    _lost_seen[cause] = current
    if seen is None:
        # First collection: adopt the reading as the baseline. Losses from before
        # telemetry was enabled are not reported -- see the module docstring.
        return _lost_total.setdefault(cause, 0)
    total = _lost_total.get(cause, 0) + max(current - seen, 0)
    _lost_total[cause] = total
    return total


def write_lost_totals() -> "dict[str, int]":
    """Each cause's monotonic total, after folding in the current readings.

    THE READ IS INSIDE THE LOCK, which is the whole correctness of the function
    rather than a precaution. Kiro Crew attaches more than one reader to one
    meter provider -- the local JSONL sink always, plus a reader per OTLP
    destination -- and each has its own ticker thread, so two collections can
    overlap. Reading outside the lock lets the slower thread carry a STALE
    reading into the accrual behind the fresher one, where it reads as a counter
    that went backwards and then forwards again: the same loss is then added
    twice. The read is three O(1) counter reads, so holding the lock across it
    costs nothing, and no path takes this lock while holding the writer's or the
    fold queue's, so it cannot deadlock against them.

    Public so a test can assert the accrual without an OTEL reader, and so the
    callback below is one line over it.
    """
    with _lost_guard:
        raw = _read_raw_losses()
        return {cause: _accrue(cause, raw[cause]) for cause in CAUSES}


def _write_lost_observations(
    options: "CallbackOptions",
) -> "Iterable[Observation]":
    """One observation per cause, for the observable ``write.lost`` gauge."""
    from opentelemetry.metrics import Observation

    try:
        totals = write_lost_totals()
    except Exception:  # noqa: BLE001 - a telemetry callback must never raise
        logger.debug("crew log write.lost read failed", exc_info=True)
        return ()
    return [
        Observation(total, {"cause": _closed(cause, CAUSES, OTHER)})
        for cause, total in totals.items()
    ]


__all__ = [
    "ALL_METRIC_NAMES",
    "BUCKETS",
    "BUS_EVENT_KINDS",
    "COUNTER_BUS_SUBSCRIBER_ERROR",
    "COUNTER_SAVEPOINT_WRITE",
    "GAUGE_EAGER_QUEUE_DEPTH",
    "GAUGE_WRITER_BUFFERED",
    "OUTCOMES",
    "SAVEPOINT_KINDS",
    "SCOPE_SESSION",
    "SCOPE_SLOT",
    "begin_fold_pass",
    "bucket_for",
    "bus_subscriber_error",
    "claim_fold_worker_thread",
    "note_fold_idle",
    "note_fold_origin",
    "note_fold_pass",
    "read_eager_queue_depth",
    "read_writer_buffered",
    "register_crew_log_health_instruments",
    "savepoint_write",
]

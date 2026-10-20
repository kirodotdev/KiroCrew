"""Crew log health instruments -- is the log keeping up, and is it losing anything.

The crew log is the durable record every fold, every dashboard read and every
resumed session is built from, and main has no metric for it at all. An operator
can see that a session's board looks stale and cannot see why: whether the writer
is holding a backlog, whether savepoints are being written or refused, whether
the eager fold queue is backed up, or whether a subscriber is quietly raising on
every published fold. The in-process ``WriterStats`` and a handful of warning
logs are the only evidence today, and neither is sampled over time.

This module covers the second batch of RFC section 6.5:

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
``crew_log/writer.py`` and ``crew_log/store_segment_writes.py`` are unchanged,
byte for byte, by this work.

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

OSS-CLEAN: opentelemetry (Apache-2.0) + stdlib + first-party helpers only.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Final, Optional

if TYPE_CHECKING:  # annotation-only; never imported at runtime
    from types import ModuleType

    from opentelemetry.metrics import Meter

from kiro_crew.metrics.schema import validate_name

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #

COUNTER_SAVEPOINT_WRITE: Final[str] = "kirocrew.crew_log.savepoint.write"
GAUGE_EAGER_QUEUE_DEPTH: Final[str] = "kirocrew.crew_log.eager.queue_depth"
GAUGE_WRITER_BUFFERED: Final[str] = "kirocrew.crew_log.writer.buffered"
COUNTER_BUS_SUBSCRIBER_ERROR: Final[str] = "kirocrew.crew_log.bus.subscriber_error"

#: Every name this module registers or emits, validated at registration.
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

#: Duration bands, in the order the edges are applied. Stated here and in
#: ``docs/system-specs/modules/metrics.md``; a histogram is deliberately not used
#: (RFC rule 2), because these four bands are the whole question and a backend
#: that drops explicit histogram buckets would lose them.
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


def _closed(value: str, allowed: "tuple[str, ...]", fallback: str) -> str:
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
    and inventory gauges. Best-effort: a failure here loses these two gauges and
    never telemetry as a whole.

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
    except Exception as exc:  # noqa: BLE001 - telemetry must never break boot
        logger.warning("crew log health instrument registration failed: %s", exc)


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
    "bucket_for",
    "bus_subscriber_error",
    "read_eager_queue_depth",
    "read_writer_buffered",
    "register_crew_log_health_instruments",
    "savepoint_write",
]

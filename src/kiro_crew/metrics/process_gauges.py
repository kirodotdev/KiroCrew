"""Process-resource observable instruments — threads, FDs, GC, memory, CPU.

The gateway had no continuous view of its own resource state: thread counts
were only visible in stall dumps (``dashboard.loop_watchdog``), file
descriptors and GC not at all, and memory only as a point-in-time dashboard
read. Leaks therefore surfaced as outages instead of trends — a native
thread-pool leak, for example, is invisible to ``threading.active_count()``
(non-Python threads) and only shows in the OS thread count.

These instruments close that gap using OTEL *observable* (asynchronous)
instruments: each callback runs only when the ``PeriodicExportingMetricReader``
collects, on the reader's own ticker thread. No sampler thread of our own, no
work at rest, and nothing at all unless telemetry is enabled — registration
happens on ``provider._build_recorder()``'s live path only, so the
``telemetry.enabled`` consent gate covers these gauges too.

Instruments (all in the core ``kirocrew.`` namespace, validated against
``schema.validate_name`` at registration):

===============================================  ==========  ====================
name                                             kind        source
===============================================  ==========  ====================
``kirocrew.process.threads.python``              gauge       ``threading.active_count()``
``kirocrew.process.threads.os``                  gauge       ``/proc/self/task`` (Linux only)
``kirocrew.process.open_fds``                    gauge       ``/proc/self/fd`` / ``/dev/fd``
``kirocrew.process.memory.rss_bytes``            gauge       ``platform_compat.proc_rss_bytes``
``kirocrew.process.memory.peak_rss_bytes``       gauge       ``platform_compat.proc_peak_rss_bytes``
``kirocrew.process.cpu.seconds``                 gauge       ``platform_compat.proc_cpu_seconds``
``kirocrew.process.gc.collections``              gauge       ``gc.get_stats()`` per generation
``kirocrew.process.gc.collected``                gauge       ``gc.get_stats()`` per generation
``kirocrew.process.gc.uncollectable``            gauge       ``gc.get_stats()`` per generation
===============================================  ==========  ====================

The last four report a monotonic PROCESS-LIFETIME total and were observable
COUNTERS until they were the only cumulative series Kiro Crew exported. One
cumulative series is enough to force every consumer into stateful whole-hour
aggregation — a cumulative counter's hourly increment is last-minus-first across
the entire hour, per host and per process lifetime — while every other instrument
here is additive (delta sums, histograms) or associative (gauge min/max/avg) and
merges one datapoint at a time. Registering them as observable GAUGES keeps the
data (the value is still the lifetime total; the per-interval increment is
recovered by differencing consecutive samples) and removes the last cumulative
series. The route NOT taken is observable counters with DELTA temporality: that
was tried and reverted, because an observable callback reads an external
lifetime total, so the first collection after a provider rebuild re-emits the
whole total as one giant delta (see :mod:`kiro_crew.metrics.temporality`).

Gauge is also the safer encoding for these four specifically. A retried export
double-counts a delta, while min/max/avg are idempotent; and a gauge has no
baseline, so a provider rebuild on a telemetry-consent change cannot manufacture
a step. :data:`LIFETIME_TOTAL_METRICS` names them for the one consumer that must
know the difference — the dashboard aggregator reduces them window-relative
instead of reporting the newest lifetime total as a reading, which also keeps its
series continuous across shards written before the switch.

Current vs peak RSS are separate metrics because they answer different
questions: a leak-vs-plateau diagnosis needs the current value (which falls
when memory is released), while a transient spike that the live reading has
already forgotten shows only in the high-water mark. ``platform_compat``
provides exactly this pair cross-platform — ``proc_rss_bytes`` (current) and
``proc_peak_rss_bytes`` (peak) — so both gauges delegate to it.

A callback that raises would otherwise surface as SDK-level error logs every
export cycle, so every reader is wrapped: on failure it yields no observation
for that cycle (a gap in the series, never a crash and never a fake zero).

Cardinality: all names are constants and the only attribute is ``generation``
with three values — well inside the recorder's low-cardinality contract.

GC pause *durations* are deliberately out of scope: they would need a
``gc.callbacks`` hook timing every collection (overhead on each GC, telemetry
on or off), whereas everything here is free until the reader collects.

OSS-CLEAN: opentelemetry (Apache-2.0) + stdlib + first-party helpers only.
The opentelemetry import is deferred into :func:`register_process_gauges` so
this module stays importable when the SDK is absent (env-closure drift) —
mirroring ``recorder.py``'s convention.
"""

from __future__ import annotations

import gc
import logging
import os
import threading
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from kiro_crew import platform_compat
from kiro_crew.metrics.schema import validate_name

if TYPE_CHECKING:  # annotation-only; never imported at runtime
    from opentelemetry.metrics import CallbackOptions, Meter, Observation

logger = logging.getLogger(__name__)

GAUGE_THREADS_PYTHON = "kirocrew.process.threads.python"
GAUGE_THREADS_OS = "kirocrew.process.threads.os"
GAUGE_OPEN_FDS = "kirocrew.process.open_fds"
GAUGE_RSS = "kirocrew.process.memory.rss_bytes"
GAUGE_PEAK_RSS = "kirocrew.process.memory.peak_rss_bytes"
GAUGE_CPU_SECONDS = "kirocrew.process.cpu.seconds"
GAUGE_GC_COLLECTIONS = "kirocrew.process.gc.collections"
GAUGE_GC_COLLECTED = "kirocrew.process.gc.collected"
GAUGE_GC_UNCOLLECTABLE = "kirocrew.process.gc.uncollectable"

ALL_METRIC_NAMES = (
    GAUGE_THREADS_PYTHON,
    GAUGE_THREADS_OS,
    GAUGE_OPEN_FDS,
    GAUGE_RSS,
    GAUGE_PEAK_RSS,
    GAUGE_CPU_SECONDS,
    GAUGE_GC_COLLECTIONS,
    GAUGE_GC_COLLECTED,
    GAUGE_GC_UNCOLLECTABLE,
)

#: Gauges whose reading is a monotonic PROCESS-LIFETIME total rather than a
#: point-in-time state. The metric NAME is the contract here, so a consumer can
#: recognise them without knowing this module: the dashboard aggregator reads
#: this tuple to reduce them window-relative (newest minus first-in-window)
#: instead of reporting a lifetime total as a current reading, which is also
#: what keeps its series continuous across the shards written while these four
#: were still observable counters. Every other name above is a true gauge whose
#: newest sample IS the answer.
LIFETIME_TOTAL_METRICS = (
    GAUGE_CPU_SECONDS,
    GAUGE_GC_COLLECTIONS,
    GAUGE_GC_COLLECTED,
    GAUGE_GC_UNCOLLECTABLE,
)


# ---------------------------------------------------------------------------
# Raw readers — plain callables returning values (or None for "unavailable"),
# kept SDK-free so they are unit-testable without an OTEL pipeline.
# ---------------------------------------------------------------------------


def read_python_threads() -> int:
    """Count of live Python ``threading`` threads."""
    return threading.active_count()


def read_os_threads() -> int | None:
    """OS-level thread count via ``platform_compat``, or None off-Linux.

    This is the count that catches native-library thread pools (ggml, grpc,
    tokenizers), which never register with ``threading`` and are invisible to
    :func:`read_python_threads`.
    """
    return platform_compat.process_thread_count(os.getpid())


def read_open_fds() -> int | None:
    """Count of open file descriptors, or None when the platform has no probe.

    Thin delegate to :func:`platform_compat.count_open_fds`, the one shared
    per-platform probe (also behind gatewayd's zombie-diagnostic ``fd_count``
    field). POSIX counts fd-directory entries minus the enumeration fd itself;
    Windows reports the kernel handle count — platform-dependent semantics,
    but coverage this gauge previously lacked.
    """
    return platform_compat.count_open_fds()


def _gc_stats() -> list[dict[str, int]]:
    """Per-generation GC stats; empty list when the runtime lacks them."""
    try:
        return list(gc.get_stats())
    except Exception:  # noqa: BLE001 — telemetry must never break collection
        return []


# ---------------------------------------------------------------------------
# OTEL registration
# ---------------------------------------------------------------------------


def _observations(
    reader: Callable[[], "int | float | None"],
    attrs: "dict[str, str] | None" = None,
) -> "Callable[[CallbackOptions], Iterable[Observation]]":
    """Wrap a raw reader as an OTEL observable callback.

    A None (or raising) reader yields no observation for the cycle — a gap in
    the series is honest, a zero would be a lie, and an exception would spam
    SDK error logs every export interval.
    """
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        try:
            value = reader()
        except Exception:  # noqa: BLE001 — never let a probe break the exporter
            logger.debug("process gauge reader failed", exc_info=True)
            return
        if value is None:
            return
        yield Observation(value, attributes=attrs or {})

    return _callback


def _gc_observations(
    key: str,
) -> "Callable[[CallbackOptions], Iterable[Observation]]":
    """Observable-gauge callback for one ``gc.get_stats()`` key, all gens."""
    from opentelemetry.metrics import Observation

    def _callback(options: "CallbackOptions") -> "Iterator[Observation]":
        for gen, stats in enumerate(_gc_stats()):
            value = stats.get(key)
            if value is None:
                continue
            yield Observation(value, attributes={"generation": str(gen)})

    return _callback


def register_process_gauges(meter: "Meter") -> None:
    """Register all process-resource instruments on *meter*.

    Called once per live ``MeterProvider`` build (instruments die with their
    provider, so a consent-driven rebuild re-registers on the new meter — that
    is per-provider construction, not duplication). Best-effort: a failure
    disables these gauges, never telemetry as a whole.
    """
    try:
        for name in ALL_METRIC_NAMES:
            validate_name(name)

        meter.create_observable_gauge(
            GAUGE_THREADS_PYTHON,
            callbacks=[_observations(read_python_threads)],
            unit="1",
            description="Live Python threading threads",
        )
        meter.create_observable_gauge(
            GAUGE_THREADS_OS,
            callbacks=[_observations(read_os_threads)],
            unit="1",
            description="OS-level threads incl. native pools (Linux)",
        )
        meter.create_observable_gauge(
            GAUGE_OPEN_FDS,
            callbacks=[_observations(read_open_fds)],
            unit="1",
            description="Open file descriptors",
        )
        # Current and peak delegate to platform_compat's cross-platform pair;
        # 0 means "could not read" there, so map it to None (gap, not a fake
        # zero sample).
        meter.create_observable_gauge(
            GAUGE_RSS,
            callbacks=[_observations(lambda: platform_compat.proc_rss_bytes() or None)],
            unit="By",
            description="Current resident set size",
        )
        meter.create_observable_gauge(
            GAUGE_PEAK_RSS,
            callbacks=[_observations(lambda: platform_compat.proc_peak_rss_bytes() or None)],
            unit="By",
            description="Peak resident set size (high-water mark)",
        )
        meter.create_observable_gauge(
            GAUGE_CPU_SECONDS,
            # proc_cpu_seconds returns 0.0 on probe failure. Publishing that
            # would read as the process having burned no CPU since it started —
            # a false reading the window-relative reducer then takes as a new
            # baseline. Map the failure sentinel to None: gap, never a fake zero.
            callbacks=[_observations(lambda: platform_compat.proc_cpu_seconds() or None)],
            unit="s",
            description="Process-lifetime user+system CPU time",
        )
        meter.create_observable_gauge(
            GAUGE_GC_COLLECTIONS,
            callbacks=[_gc_observations("collections")],
            unit="1",
            description="Process-lifetime GC runs per generation",
        )
        meter.create_observable_gauge(
            GAUGE_GC_COLLECTED,
            callbacks=[_gc_observations("collected")],
            unit="1",
            description="Process-lifetime objects collected per generation",
        )
        meter.create_observable_gauge(
            GAUGE_GC_UNCOLLECTABLE,
            callbacks=[_gc_observations("uncollectable")],
            unit="1",
            description="Process-lifetime uncollectable objects per generation",
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break boot
        logger.warning("process gauge registration failed: %s", exc)

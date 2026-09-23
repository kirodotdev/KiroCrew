"""Signals the adaptive controller reads, and the pure pressure classifier.

A :class:`Sample` is one periodic observation of the host and of the two
admission points (the gateway's subagent cap and the daemon's spawn gate).
:func:`classify` turns a sample into a :class:`PressureReport` -- WHICH
signals fired, whether they corroborate each other, whether the pressure is
severe enough to pause dispatch, and which provider scopes are throttled.

Two things the classifier deliberately keeps apart:

* **Host pressure** (loop lag, memory, fd/proc counts, start latency,
  attributable timeouts, completion rate) drives the host-wide caps.
* **Provider throttling** (per-provider 429s) is scoped to that provider's
  dependency channel. It is reported, never counted as a host signal: one
  provider's rate limit must not halve the concurrency every other provider
  still has (SPEC 二.3, addendum §4).

Errors that are not congestion -- permission denied, invalid params, context
length, deny-rule refusals -- are the caller's job to exclude before they
reach ``attributable_timeout_rate``; this module only reads the rate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

# Signal names. Closed set: they appear in Decision.reason, the resource_status
# summary and the audit line, never as free-form text.
SIGNAL_LOOP_LAG = "loop_lag"
SIGNAL_MEMORY = "memory"
SIGNAL_FDS = "fds"
SIGNAL_PROCS = "procs"
SIGNAL_START_LATENCY = "start_latency"
SIGNAL_TIMEOUTS = "timeouts"
SIGNAL_COMPLETION = "completion_rate"
SIGNAL_SLOW_KEYS = "slow_keys"
SIGNAL_GATE_FAILURES = "gate_failures"

#: Signals that are individually sufficient for a decrease. Everything else
#: needs corroboration (>= 2 distinct signals in one sample).
SUFFICIENT_ALONE = frozenset({SIGNAL_LOOP_LAG, SIGNAL_MEMORY})


@dataclass(frozen=True)
class Thresholds:
    """Every knob the classifier reads. Defaults are RFC §5.2 starting values.

    Memory thresholds are in MB and default to the ``resource_pressure_gb`` /
    ``resource_critical_gb`` pair the ``[RESOURCES]`` advisory already uses,
    so the controller and the advisory never disagree about "tight".
    """

    lag_decrease_ms: float = 250.0
    lag_increase_ms: float = 100.0
    lag_severe_ms: float = 2000.0
    mem_pressure_mb: float = 4096.0
    mem_critical_mb: float = 2048.0
    #: Fraction of the fd / proc budget above which the signal fires.
    fd_ratio: float = 0.8
    proc_ratio: float = 0.9
    #: Attributable start-timeout / stall rate over the window (0..1).
    timeout_rate: float = 0.2
    #: p95 session-start latency that counts as a slow start.
    start_p95_ms: float = 30_000.0
    #: Completion rate (done / admitted over the window) below which the host
    #: is not finishing what it admits. Only read once the window has admitted
    #: enough work to be meaningful (see ``completion_min_admitted``).
    completion_rate: float = 0.5
    completion_min_admitted: int = 5
    #: Distinct PoolKeys with slow or failing starts in the window that count
    #: as correlated (one key failing is that server, not the host).
    slow_keys: int = 2
    #: Spawn-gate init failures in the window that corroborate other signals
    #: (read from ``Sample.gate_failures_in_window``, a per-window delta).
    gate_failures: int = 2
    #: Length of that window: the daemon reports lifetime counters, so the
    #: policy counts the failures that landed in the last this-many seconds.
    gate_failure_window_secs: float = 60.0
    #: Consecutive severe samples before dispatch pauses.
    severe_samples: int = 2


@dataclass(frozen=True)
class SpawnGateStats:
    """The daemon gate's ``stats.admission.spawn_gate`` snapshot, typed."""

    capacity: int = 0
    floor: int = 1
    ceiling: int = 8
    in_flight: int = 0
    queued: int = 0
    #: LIFETIME outcome counters as the daemon reports them (``outcomes.*``
    #: in the stats frame are never reset while the daemon lives). The policy
    #: diffs them; nothing reads them as a per-window count.
    successes: int = 0
    failures: int = 0
    neutral: int = 0

    @classmethod
    def from_snapshot(cls, snap: Mapping[str, object] | None) -> "SpawnGateStats":
        if not snap:
            return cls()
        outcomes = snap.get("outcomes")
        out = outcomes if isinstance(outcomes, Mapping) else {}
        return cls(
            capacity=_as_int(snap.get("capacity")),
            floor=_as_int(snap.get("floor"), 1),
            ceiling=_as_int(snap.get("ceiling"), 8),
            in_flight=_as_int(snap.get("in_flight")),
            queued=_as_int(snap.get("queued")),
            successes=_as_int(out.get("success")),
            failures=_as_int(out.get("failure")),
            neutral=_as_int(out.get("neutral")),
        )


@dataclass(frozen=True)
class Sample:
    """One controller observation. Every field has a "not measured" value."""

    t: float
    loop_lag_ms: float = 0.0
    rss_mb: float = -1.0
    free_mem_mb: float = -1.0
    fd_count: int = -1
    fd_limit: int = 0
    proc_count: int = -1
    proc_limit: int = 0
    start_latency_p50_ms: float = 0.0
    start_latency_p95_ms: float = 0.0
    #: Attributable start timeouts / stalls over the window, as a rate 0..1.
    attributable_timeout_rate: float = 0.0
    #: done / admitted over the window (1.0 when nothing was admitted).
    completion_rate: float = 1.0
    admitted_in_window: int = 0
    #: Cumulative completions (successful runs) seen so far. The policy diffs it.
    completions: int = 0
    #: Distinct PoolKeys with slow or failing starts in the window.
    slow_or_failing_keys: int = 0
    #: Per-provider 429 counts in the window. Provider ids are scopes, not
    #: host signals.
    per_provider_429: Mapping[str, int] = field(default_factory=dict)
    spawn_gate: SpawnGateStats = field(default_factory=SpawnGateStats)
    #: Spawn-gate init failures INSIDE the evidence window. Derived by the
    #: policy from the cumulative ``spawn_gate.failures`` (see
    #: ``AdaptivePolicy._windowed_gate_failures``); ``classify`` reads only
    #: this, never the lifetime counter, so two failures a daemon saw an hour
    #: ago cannot keep the cap pinned.
    gate_failures_in_window: int = 0
    #: Gateway-side demand: running + queued subagent spawns.
    running: int = 0
    queued: int = 0
    #: In-flight starts that are still healthy (not timed out). A decrease
    #: never targets below this: natural shrink cannot free what is working.
    healthy_in_flight: int = 0
    #: Running sessions with new stream activity since the previous observation.
    #: Unlike healthy_in_flight, merely occupying a slot is not evidence here.
    progressing: int = 0

    @property
    def demand(self) -> int:
        return self.running + self.queued


@dataclass(frozen=True)
class PressureReport:
    """What one sample says about the host."""

    signals: frozenset[str]
    #: Enough evidence to decrease: a signal from SUFFICIENT_ALONE, or >= 2
    #: distinct signals in the same sample.
    corroborated: bool
    #: Memory below critical or loop lag beyond the severe line.
    severe: bool
    #: Nothing fired AND the hysteresis "increase" side holds (lag well below
    #: the decrease line, memory above the pressure line).
    clear_for_increase: bool
    #: Provider scopes throttled in this sample. Reported, not a host signal.
    throttled_providers: frozenset[str]

    @property
    def any(self) -> bool:
        return bool(self.signals)


def classify(sample: Sample, th: Thresholds) -> PressureReport:
    """Pure: one sample + thresholds -> :class:`PressureReport`."""
    signals: set[str] = set()
    severe = False

    if sample.loop_lag_ms >= th.lag_decrease_ms:
        signals.add(SIGNAL_LOOP_LAG)
    if sample.loop_lag_ms >= th.lag_severe_ms:
        severe = True

    if sample.free_mem_mb >= 0 and th.mem_critical_mb > 0:
        if sample.free_mem_mb <= th.mem_critical_mb:
            signals.add(SIGNAL_MEMORY)
            severe = True

    if sample.fd_limit > 0 and sample.fd_count >= 0:
        if sample.fd_count >= th.fd_ratio * sample.fd_limit:
            signals.add(SIGNAL_FDS)
    if sample.proc_limit > 0 and sample.proc_count >= 0:
        if sample.proc_count >= th.proc_ratio * sample.proc_limit:
            signals.add(SIGNAL_PROCS)

    if sample.start_latency_p95_ms >= th.start_p95_ms > 0:
        signals.add(SIGNAL_START_LATENCY)
    if sample.attributable_timeout_rate >= th.timeout_rate > 0:
        signals.add(SIGNAL_TIMEOUTS)
    if (
        sample.admitted_in_window >= th.completion_min_admitted
        and sample.completion_rate < th.completion_rate
    ):
        signals.add(SIGNAL_COMPLETION)
    if sample.slow_or_failing_keys >= th.slow_keys > 0:
        signals.add(SIGNAL_SLOW_KEYS)
    if sample.gate_failures_in_window >= th.gate_failures > 0:
        signals.add(SIGNAL_GATE_FAILURES)

    corroborated = bool(signals & SUFFICIENT_ALONE) or len(signals) >= 2

    mem_ok = (
        sample.free_mem_mb < 0
        or th.mem_pressure_mb <= 0
        or (sample.free_mem_mb >= th.mem_pressure_mb)
    )
    clear = not signals and sample.loop_lag_ms < th.lag_increase_ms and mem_ok

    throttled = frozenset(
        str(scope) for scope, count in sample.per_provider_429.items() if _as_int(count) > 0
    )
    return PressureReport(
        signals=frozenset(signals),
        corroborated=corroborated,
        severe=severe,
        clear_for_increase=clear,
        throttled_providers=throttled,
    )


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default

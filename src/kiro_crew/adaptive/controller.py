"""``AdaptiveController``: samples the host, runs the policy, drives the actuators.

One asyncio task on the gateway event loop. Every ``controller_sample_secs``
(5 s) it

1. measures event-loop lag (how late its own timer fired), reads host memory,
   RSS and open fds off the loop (``asyncio.to_thread``), and asks the MCP
   gateway daemon for its ``stats`` frame (spawn gate + host budget) -- also
   off the loop, over a fresh control connection with the manager's own
   timeout;
2. folds the evidence the hooks below recorded since the last tick (session
   start latencies, attributable timeouts, provider throttles, completions)
   into one :class:`~.signals.Sample`;
3. hands it to :class:`~.policy.AdaptivePolicy` and applies the
   :class:`~.policy.Decision`: the execution cap through
   ``SubagentManager.set_effective_cap`` (natural shrink -- in-flight work
   finishes, nothing is killed) and the spawn-gate capacity through
   ``GatewayManager.set_spawn_capacity`` (the daemon clamps to its own
   floor/ceiling and lets in-flight spawns finish).

The controller never blocks the loop and never raises out of its task: a
failed sample is logged and the previous caps stand. Its state is a plain dict
(:meth:`AdaptiveController.state`) that ``resource_status`` renders, and every
decision that changes something is a bounded counter
(:data:`~kiro_crew.metrics.events.ADAPTIVE_DECISIONS`).

Hooks for the evidence the sampler cannot see on its own live on the
controller: ``record_start`` (session/backend start latency and whether it
timed out for a congestion reason), ``record_provider_throttle`` (a typed 429
from the ACP stream, scoped to its provider), ``note_gate_outcome`` (the
``SpawnGate(on_settle=...)`` seam when the gate is in-process). Completions are
observed by diffing the subagent manager's run table each tick, so no hook in
the run loop is required.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Optional, Protocol

from kiro_crew.metrics.events import (
    ADAPTIVE_DECISIONS,
    LOOP_LAG_MS,
    NON_MS_HISTOGRAM_UNITS,
    PROCESS_CPU_UTILIZATION,
    PROCESS_RSS_SAMPLED,
    emit_counter,
    emit_histogram,
)
from kiro_crew.metrics.process_gauges import cpu_utilization, read_logical_cores

from .policy import (
    ACTION_DECREASE,
    ACTION_FIXED,
    ACTION_HOLD,
    ACTION_PAUSE,
    ACTION_RESUME,
    AdaptivePolicy,
    Decision,
    PolicyParams,
    params_from_config,
)
from .signals import Sample, SpawnGateStats

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_SECS = 5.0
#: Ring of recent samples kept for the state snapshot (RFC §5.1: 60 x 5 s).
SAMPLE_RING = 60
#: Evidence window the per-tick rates are computed over.
WINDOW_SECS = 60.0
#: Cap-changing decisions kept for the state snapshot, newest last.
RECENT_DECISIONS = 32

#: Substrings of a run's ``error`` that mark a CONGESTION failure: a start or
#: turn that timed out, a stall, a backend that never initialised. Anything
#: else -- permission, invalid params, context length, deny rules, turn limits,
#: cancellation -- is non-congestion and never feeds the controller.
ATTRIBUTABLE_MARKERS: tuple[str, ...] = (
    "timed out",
    "timeout",
    "stall",
    "startup",
    "did not start",
    "initialize",
)

OUTCOME_SUCCESS = "success"
OUTCOME_ATTRIBUTABLE = "attributable"
OUTCOME_NON_CONGESTION = "non_congestion"


def classify_run_outcome(info: Any) -> str:
    """Bucket a finished run for the controller. Conservative by default."""
    error = str(getattr(info, "error", "") or "")
    if not error:
        return OUTCOME_SUCCESS
    lowered = error.lower()
    if lowered.startswith("cancel"):
        return OUTCOME_NON_CONGESTION
    if any(marker in lowered for marker in ATTRIBUTABLE_MARKERS):
        return OUTCOME_ATTRIBUTABLE
    return OUTCOME_NON_CONGESTION


class ExecActuator(Protocol):
    """What the controller needs from ``SubagentManager``."""

    @property
    def user_max_concurrent(self) -> int: ...

    @property
    def running_count(self) -> int: ...

    def set_effective_cap(self, cap: Optional[int]) -> int: ...


GateSetter = Callable[[int], Awaitable[Optional[int]]]
StatsReader = Callable[[], Awaitable[dict[str, Any]]]

# The ``process`` attribute every sample this controller publishes carries. One
# spelling for all three series (loop lag, resident set, CPU share): a dashboard
# that splits on this attribute would otherwise report them as separate
# processes, and they are readings of the same one.
_PROCESS = "gateway"


@dataclass
class HostSample:
    """Blocking host reads, taken off the loop. ``-1`` = not measured."""

    free_mem_mb: float = -1.0
    rss_mb: float = -1.0
    fd_count: int = -1
    fd_limit: int = 0
    #: Process-lifetime CPU seconds, a monotonic total rather than a rate. Read
    #: here so the share-of-machine figure can be differenced between two ticks
    #: without a second probe on a different cadence.
    cpu_seconds: float = -1.0
    #: The monotonic instant ``cpu_seconds`` was read at, taken in the same probe
    #: so the two travel together. The share divides one difference by the other,
    #: and an instant taken after the worker thread resumes belongs to a later
    #: moment than the total it would be paired with: the resumption delay varies
    #: from tick to tick, so it does not cancel, and at the one-second floor of
    #: ``controller_sample_secs`` a few hundred milliseconds of it is a
    #: double-digit-percent error published as a measured value.
    cpu_clock: float = -1.0


def probe_host() -> HostSample:
    """Read memory, RSS and open fds.

    Never raises; runs in a worker thread, so the blocking ``/proc`` read (or
    its platform equivalent) stays off the event loop.
    """
    out = HostSample()
    try:
        from kiro_crew.resource_status import _read_available_gb

        gb = _read_available_gb()
        out.free_mem_mb = gb * 1024.0 if gb >= 0 else -1.0
    except Exception:
        logger.debug("adaptive: memory probe failed", exc_info=True)
    try:
        from kiro_crew import platform_compat

        out.rss_mb = platform_compat.proc_rss_bytes() / (1024.0 * 1024.0)
    except Exception:
        logger.debug("adaptive: rss probe failed", exc_info=True)
    try:
        from kiro_crew import platform_compat

        out.cpu_seconds = platform_compat.proc_cpu_seconds()
        out.cpu_clock = time.monotonic()
    except Exception:
        logger.debug("adaptive: cpu seconds probe failed", exc_info=True)
    try:
        from kiro_crew.mcp_gateway.host_budget import _nofile_soft_limit

        out.fd_limit = _nofile_soft_limit()
    except Exception:
        logger.debug("adaptive: fd limit probe failed", exc_info=True)
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            out.fd_count = len(os.listdir(fd_dir))
            break
        except OSError:
            continue
    return out


@dataclass
class _StartRecord:
    t: float
    duration_ms: float
    ok: bool
    attributable: bool
    key: str


@dataclass
class _Evidence:
    """Everything the hooks recorded, windowed at read time."""

    starts: deque[_StartRecord] = field(default_factory=lambda: deque(maxlen=4096))
    throttles: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=4096))
    completions_total: int = 0
    completed_at: deque[tuple[float, str]] = field(default_factory=lambda: deque(maxlen=4096))
    gate_outcomes: dict[str, int] = field(
        default_factory=lambda: {"success": 0, "failure": 0, "neutral": 0}
    )


class AdaptiveController:
    """The gateway-side controller. See the module docstring."""

    #: Live config paths that re-parameterise the policy without a restart.
    LIVE_CONFIG_PATHS: tuple[str, ...] = (
        "agent.adaptive_concurrency",
        "agent.adaptive_concurrency_mode",
        "agent.adaptive_floor",
        "agent.adaptive_initial",
        "agent.adaptive_slow_start",
        "agent.controller_sample_secs",
        "agent.resource_pressure_gb",
        "agent.resource_critical_gb",
    )

    def __init__(
        self,
        manager: ExecActuator,
        *,
        cfg: object,
        set_gate_capacity: Optional[GateSetter] = None,
        read_gate_stats: Optional[StatsReader] = None,
        host_probe: Optional[Callable[[], HostSample]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        gate_initial: int = 4,
        gate_floor: int = 1,
        gate_ceiling: int = 8,
        on_provider_throttle: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self._manager = manager
        self._set_gate_capacity = set_gate_capacity
        self._read_gate_stats = read_gate_stats
        self._host_probe = host_probe
        self._clock = clock
        self._sleep = sleep
        self._gate_bounds = (gate_initial, gate_floor, gate_ceiling)
        # The run loops are the SOURCE of provider throttles, not this hook:
        # a typed 429 is reported to ``record_provider_throttle`` by the
        # sub-agent run (``_yield_for_dependency``) and the main chat
        # (``_shared_dependency_delay``) at the same moment they park on the
        # DependencyCoordinator's per-scope schedule, so the coordinator needs
        # no subscription here. The listener stays for an observer (a metrics
        # or notification sink) that wants each throttle as it lands.
        self._on_provider_throttle = on_provider_throttle
        self._enabled = True
        self._sample_secs = DEFAULT_SAMPLE_SECS
        self._policy = AdaptivePolicy(self._params_for(cfg))
        self._apply_enabled_from(cfg)
        self._evidence = _Evidence()
        self._seen_done: dict[str, bool] = {}
        self._seen_activity: dict[str, float] = {}
        self._samples: deque[Sample] = deque(maxlen=SAMPLE_RING)
        self._task: Optional[asyncio.Task[None]] = None
        self._applied_exec: Optional[int] = None
        self._applied_gate: Optional[int] = None
        self._gate_pending: Optional[int] = None
        self._last_error: str = ""
        self._ticks = 0
        # Previous CPU-seconds reading and the clock time it was taken at. A
        # share-of-machine figure is a RATE, and the probe returns a
        # process-lifetime total, so it takes two readings to make one sample --
        # which is why the first tick of a process publishes no utilization.
        # ``-1.0`` distinguishes "no predecessor yet" from a measured 0.0.
        self._prev_cpu_seconds: float = -1.0
        self._prev_cpu_clock: float = -1.0
        # Logical core count, resolved once: it is the denominator of every
        # utilization sample and does not change while the process runs.
        self._cores: Optional[int] = None
        self._counts = {"decrease": 0, "increase": 0, "pause": 0, "probe": 0, "resume": 0}
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT_DECISIONS)
        self._config_sub: Any = None
        try:
            from kiro_crew.config import live

            self._config_sub = live.watch_object(
                self, *self.LIVE_CONFIG_PATHS, name="AdaptiveController"
            )
        except Exception:
            logger.debug("AdaptiveController could not subscribe to live config", exc_info=True)
        # Fresh process: the exec cap starts at min(user_max, initial) and
        # earns its way up. Applied synchronously so the first spawn already
        # sees it; the gate capacity follows on the first tick (the daemon may
        # not be up yet).
        self._apply_exec(self._policy.exec_cap if self._enabled else None)

    # -- configuration -------------------------------------------------------

    def _params_for(self, cfg: object) -> PolicyParams:
        gate_initial, gate_floor, gate_ceiling = self._gate_bounds
        return params_from_config(
            cfg,
            exec_ceiling=max(1, int(self._manager.user_max_concurrent)),
            gate_initial=gate_initial,
            gate_floor=gate_floor,
            gate_ceiling=gate_ceiling,
        )

    def _apply_enabled_from(self, cfg: object) -> None:
        agent = getattr(cfg, "agent", None)
        enabled = getattr(agent, "adaptive_concurrency", True)
        self._enabled = enabled if isinstance(enabled, bool) else True
        try:
            secs = float(getattr(agent, "controller_sample_secs", DEFAULT_SAMPLE_SECS))
        except (TypeError, ValueError):
            secs = DEFAULT_SAMPLE_SECS
        self._sample_secs = secs if secs > 0 else DEFAULT_SAMPLE_SECS

    async def reconfigure(self, cfg: object) -> None:
        """Live-config applier for :attr:`LIVE_CONFIG_PATHS`."""
        self.apply_config(cfg)

    def apply_config(self, cfg: object) -> None:
        was_enabled = self._enabled
        self._apply_enabled_from(cfg)
        self._policy.update_params(self._params_for(cfg))
        if not self._enabled:
            # Off: the user's ceiling is the only bound again; the gate goes
            # back to its configured initial on the next tick.
            self._apply_exec(None)
            self._gate_pending = self._gate_bounds[0]
        elif not was_enabled:
            self._apply_exec(self._policy.exec_cap)
            self._gate_pending = self._policy.gate_cap
        logger.info(
            "AdaptiveController reconfigured: enabled=%s mode=%s exec_cap=%d gate_cap=%d",
            self._enabled,
            self._policy.params.mode,
            self._policy.exec_cap,
            self._policy.gate_cap,
        )

    # -- evidence hooks ------------------------------------------------------

    def record_start(
        self, duration_ms: float, *, ok: bool, attributable_timeout: bool = False, key: str = ""
    ) -> None:
        """A session/backend start finished: how long it took and whether it
        timed out for a congestion reason. ``key`` groups starts by PoolKey so
        the classifier can tell one slow server from a slow host."""
        self._evidence.starts.append(
            _StartRecord(self._clock(), float(duration_ms), ok, attributable_timeout, key)
        )

    def record_provider_throttle(self, scope: str) -> None:
        """A typed 429 / throttle from provider ``scope``. Never a host signal."""
        scope = str(scope or "unknown")
        self._evidence.throttles.append((self._clock(), scope))
        if self._on_provider_throttle is not None:
            try:
                self._on_provider_throttle(scope, 1)
            except Exception:
                logger.debug("provider throttle listener failed", exc_info=True)

    def note_gate_outcome(self, outcome: str) -> None:
        """``SpawnGate(on_settle=...)`` seam for an in-process gate."""
        if outcome in self._evidence.gate_outcomes:
            self._evidence.gate_outcomes[outcome] += 1

    def record_completion(self, *, ok: bool, attributable_timeout: bool = False) -> None:
        """A run finished. The tick also infers these from the manager's run
        table; call this only for work the manager does not track."""
        if ok:
            kind = OUTCOME_SUCCESS
            self._evidence.completions_total += 1
        elif attributable_timeout:
            kind = OUTCOME_ATTRIBUTABLE
        else:
            kind = OUTCOME_NON_CONGESTION
        self._evidence.completed_at.append((self._clock(), kind))

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self._enabled:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="adaptive-controller")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def run(self) -> None:
        while True:
            await self._sample_and_tick()

    async def _sample_and_tick(self) -> None:
        """One cycle of :meth:`run`: wait, measure how late the timer fired,
        publish that lag, tick. Separate from ``tick`` so tests can drive the
        measured path while ``tick`` keeps taking synthetic lag."""
        t0 = self._clock()
        # Read the period once: a hot-reload of controller_sample_secs that
        # lands during the sleep must not be measured as loop lag.
        secs = self._sample_secs
        await self._sleep(secs)
        lag_ms = max(0.0, (self._clock() - t0 - secs) * 1000.0)
        emit_histogram(LOOP_LAG_MS, lag_ms, {"process": _PROCESS}, unit="ms")
        try:
            await self.tick(loop_lag_ms=lag_ms)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the loop must outlive any one bad sample
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("adaptive controller tick failed", exc_info=True)

    # -- one cycle -----------------------------------------------------------

    def _emit_process_histograms(self, host: HostSample) -> None:
        """Publish this tick's resident-set and CPU-share distributions.

        Recorded from this loop rather than from the instrument module that owns
        the matching gauges, because a histogram is RECORDED and not observed:
        OTEL has no observable histogram, so the two series need a caller on a
        timer, and adding one to a module whose instruments are all callbacks
        would put a second sampler and a second cadence in the process. This loop
        already probes both readings for its own decisions.

        Each unit is read from the same mapping the dashboard resolves, so the
        value published and the unit declared for it cannot drift apart.
        """
        # ``proc_rss_bytes`` answers 0 on failure rather than raising, so the probe's
        # try/except never fires and a failed read arrives as a plain 0.0 -- which a
        # live process never truly has. Admitting it would put a fabricated zero in a
        # CUMULATIVE distribution, where it stays for the process's lifetime and no
        # later sample can correct it. Same gap-over-fake-zero rule the CPU share
        # below follows.
        if host.rss_mb > 0.0:
            emit_histogram(
                PROCESS_RSS_SAMPLED,
                host.rss_mb * 1024.0 * 1024.0,
                {"process": _PROCESS},
                unit=NON_MS_HISTOGRAM_UNITS[PROCESS_RSS_SAMPLED],
            )
        prev_seconds = self._prev_cpu_seconds
        prev_clock = self._prev_cpu_clock
        # Both endpoints come from ``probe_host``, never from this loop's clock:
        # the share divides a CPU-total difference by a time difference, so the
        # two instants have to be the ones the totals were read at.
        #
        # A failed probe reads 0.0, which must not become the next interval's
        # baseline: leaving the old pair in place differences a longer interval
        # against the reading it was actually taken with, which stays correct.
        if host.cpu_seconds > 0.0 and host.cpu_clock >= 0.0:
            self._prev_cpu_seconds = host.cpu_seconds
            self._prev_cpu_clock = host.cpu_clock
        if prev_seconds <= 0.0 or prev_clock < 0.0:
            return  # first measured tick of this process: no predecessor to difference
        if host.cpu_clock < 0.0:
            return  # a total with no instant of its own is not a measurement
        if self._cores is None:
            self._cores = read_logical_cores()
        share = cpu_utilization(
            prev_cpu_seconds=prev_seconds,
            cpu_seconds=host.cpu_seconds,
            elapsed_seconds=host.cpu_clock - prev_clock,
            cores=self._cores,
        )
        if share is None:
            return  # a gap in the series, never a fake zero
        emit_histogram(
            PROCESS_CPU_UTILIZATION,
            share,
            {"process": _PROCESS},
            unit=NON_MS_HISTOGRAM_UNITS[PROCESS_CPU_UTILIZATION],
        )

    async def tick(self, *, loop_lag_ms: float = 0.0) -> Decision:
        """Sample, decide, apply. Public so tests drive one cycle at a time."""
        self._ticks += 1
        if not self._enabled:
            return await self.step(Sample(t=self._clock()))
        if self._host_probe is None:
            host = await asyncio.to_thread(probe_host)
        else:
            host = await asyncio.to_thread(self._host_probe)
        self._emit_process_histograms(host)
        gate_snap: dict[str, Any] = {}
        budget_snap: dict[str, Any] = {}
        if self._read_gate_stats is not None:
            try:
                stats = await self._read_gate_stats()
            except Exception:
                logger.debug("adaptive: gate stats read failed", exc_info=True)
                stats = {}
            admission = stats.get("admission") if isinstance(stats, dict) else None
            if isinstance(admission, dict):
                gate_snap = admission.get("spawn_gate") or {}
                budget_snap = admission.get("host_budget") or {}
        sample = self.build_sample(
            loop_lag_ms=loop_lag_ms, host=host, gate_snap=gate_snap, budget_snap=budget_snap
        )
        return await self.step(sample)

    def build_sample(
        self,
        *,
        loop_lag_ms: float,
        host: HostSample,
        gate_snap: dict[str, Any],
        budget_snap: dict[str, Any],
    ) -> Sample:
        now = self._clock()
        progressing = self._ingest_manager_runs(now)
        ev = self._evidence
        since = now - WINDOW_SECS

        starts = [s for s in ev.starts if s.t >= since]
        durations = sorted(s.duration_ms for s in starts)
        p50 = _percentile(durations, 0.5)
        p95 = _percentile(durations, 0.95)
        attributable_starts = sum(1 for s in starts if s.attributable)
        slow_ms = self._policy.params.thresholds.start_p95_ms
        slow_keys = {
            s.key
            for s in starts
            if s.key and (s.attributable or (slow_ms > 0 and s.duration_ms >= slow_ms))
        }
        completed = [(t, kind) for t, kind in ev.completed_at if t >= since]
        done_ok = sum(1 for _t, kind in completed if kind == OUTCOME_SUCCESS)
        attributable_runs = sum(1 for _t, kind in completed if kind == OUTCOME_ATTRIBUTABLE)
        finished = len(starts) + len(completed)
        attributable = attributable_starts + attributable_runs
        timeout_rate = attributable / finished if finished else 0.0
        admitted = len(completed)
        completion_rate = (done_ok / admitted) if admitted else 1.0

        throttles: dict[str, int] = {}
        for t, scope in ev.throttles:
            if t >= since:
                throttles[scope] = throttles.get(scope, 0) + 1

        gate = SpawnGateStats.from_snapshot(gate_snap)
        if not gate_snap and any(ev.gate_outcomes.values()):
            gate = SpawnGateStats(
                successes=ev.gate_outcomes["success"],
                failures=ev.gate_outcomes["failure"],
                neutral=ev.gate_outcomes["neutral"],
            )
        running = int(getattr(self._manager, "running_count", 0) or 0)
        queued = len(getattr(self._manager, "_queue", ()) or ())
        healthy = max(0, running - self._stalled_running())

        sample = Sample(
            t=now,
            loop_lag_ms=float(loop_lag_ms),
            rss_mb=host.rss_mb,
            free_mem_mb=host.free_mem_mb,
            fd_count=host.fd_count,
            fd_limit=host.fd_limit,
            proc_count=_as_int(budget_snap.get("procs"), -1),
            proc_limit=_as_int(budget_snap.get("max_procs"), 0),
            start_latency_p50_ms=p50,
            start_latency_p95_ms=p95,
            attributable_timeout_rate=timeout_rate,
            completion_rate=completion_rate,
            admitted_in_window=admitted,
            completions=ev.completions_total,
            slow_or_failing_keys=len(slow_keys),
            per_provider_429=throttles,
            spawn_gate=gate,
            running=running,
            queued=queued,
            healthy_in_flight=healthy,
            progressing=progressing,
        )
        self._samples.append(sample)
        return sample

    async def step(self, sample: Sample) -> Decision:
        """Decide on ``sample`` and apply the decision. Pure-policy tests use
        :class:`AdaptivePolicy` directly; this is the wiring."""
        if not self._enabled:
            decision = Decision(
                effective_exec_cap=int(self._manager.user_max_concurrent),
                spawn_gate_capacity=self._gate_bounds[0],
                paused=False,
                probing=False,
                action=ACTION_HOLD,
                reason="adaptive concurrency disabled",
            )
            await self._flush_gate()
            return decision
        # The user's ceiling may have moved under us (hot reload of
        # agent.max_subagents); it is read every tick, never cached.
        ceiling = max(1, int(self._manager.user_max_concurrent))
        if ceiling != self._policy.params.exec_ceiling:
            self._policy.update_params(_with_ceiling(self._policy.params, ceiling))
        decision = self._policy.observe(sample)
        await self.apply(decision)
        return decision

    async def apply(self, decision: Decision) -> None:
        prev_exec, prev_gate = self._applied_exec, self._applied_gate
        if decision.effective_exec_cap != self._applied_exec:
            self._apply_exec(decision.effective_exec_cap)
        if decision.spawn_gate_capacity != self._applied_gate:
            self._gate_pending = decision.spawn_gate_capacity
        await self._flush_gate()
        # Movement is judged on what the actuators confirmed, after they ran:
        # a gate update the daemon did not answer stays pending and is not a
        # move. A cap's first application (``None`` before) is not one either.
        moved = (prev_exec is not None and self._applied_exec != prev_exec) or (
            prev_gate is not None and self._applied_gate != prev_gate
        )
        if decision.action in self._counts:
            self._counts[decision.action] += 1
        if decision.changed and decision.action not in (ACTION_HOLD, ACTION_FIXED):
            emit_counter(ADAPTIVE_DECISIONS, {"action": decision.action})
            # gateway.log keeps WARNING and above, and a cap reduction, and
            # the resume that closes a pause, are the events an operator later
            # needs to explain; growth stays at INFO.
            log = (
                logger.warning
                if decision.action in (ACTION_DECREASE, ACTION_PAUSE, ACTION_RESUME)
                else logger.info
            )
            log(
                "adaptive concurrency %s: exec_cap=%d gate_cap=%d paused=%s (%s)",
                decision.action,
                decision.effective_exec_cap,
                decision.spawn_gate_capacity,
                decision.paused,
                decision.reason,
            )
        if moved or (decision.changed and decision.action not in (ACTION_HOLD, ACTION_FIXED)):
            # Any confirmed move belongs in the history, whatever the decision
            # said: a hold after a live ``agent.max_subagents`` drop clamps the
            # cap, a fixed-mode hot-reload from a reduced AIMD cap restores it,
            # and a restarted daemon accepting a long-pending gate cap moves it
            # under an unchanged decision. A changed non-hold decision is
            # recorded even when its actuator has not confirmed yet. The first
            # fixed decision pins caps applied at construction: not a move.
            last = self._samples[-1] if self._samples else None
            # Wall-clock, not ``self._clock``: the entry is read from outside
            # the process, to line a cap drop up against gateway.log. The caps
            # are the CONFIRMED ones: what the actuators hold after this
            # decision, not what it asked for (a gate update the daemon did
            # not answer is still pending and shows the previous value).
            self._recent.append(
                {
                    "at": time.time(),
                    "action": decision.action,
                    "reason": decision.reason,
                    "exec_cap": self._applied_exec,
                    "gate_cap": self._applied_gate,
                    "paused": decision.paused,
                    "loop_lag_ms": round(last.loop_lag_ms, 1) if last else None,
                }
            )

    def _apply_exec(self, cap: Optional[int]) -> None:
        try:
            self._manager.set_effective_cap(cap)
        except Exception:
            logger.debug("adaptive: set_effective_cap failed", exc_info=True)
            return
        self._applied_exec = cap

    async def _flush_gate(self) -> None:
        if self._gate_pending is None or self._set_gate_capacity is None:
            return
        wanted = self._gate_pending
        try:
            applied = await self._set_gate_capacity(wanted)
        except Exception:
            logger.debug("adaptive: set_spawn_capacity failed", exc_info=True)
            return
        if applied is None:
            # Daemon not answering: keep it pending, retry next tick.
            return
        # What the daemon answered, not what was asked: an adopted daemon with
        # narrower bounds clamps the request, and the history and
        # ``applied_gate_cap`` must report the capacity actually in force.
        self._applied_gate = applied
        self._gate_pending = None

    # -- manager observation -------------------------------------------------

    def _ingest_manager_runs(self, now: float) -> int:
        agents = getattr(self._manager, "_agents", None)
        if not isinstance(agents, dict):
            return 0
        live_ids: set[str] = set()
        progressing = 0
        for agent_id, info in list(agents.items()):
            key = str(agent_id)
            live_ids.add(key)
            done = bool(getattr(info, "done", False))
            stream_started = getattr(info, "_first_stream_started", None)
            activity = float(getattr(info, "last_activity", 0.0) or 0.0)
            previous = self._seen_activity.get(key, activity)
            self._seen_activity[key] = activity
            if (
                not done
                and not getattr(info, "queued", False)
                and not getattr(info, "stalled", False)
                and not getattr(info, "_slot_released", False)
                and stream_started is not None
                and activity > max(previous, float(stream_started))
            ):
                progressing += 1
            was_done = self._seen_done.get(key)
            if done and not was_done:
                kind = classify_run_outcome(info)
                self._evidence.completed_at.append((now, kind))
                if kind == OUTCOME_SUCCESS:
                    self._evidence.completions_total += 1
            self._seen_done[key] = done
        for stale in [k for k in self._seen_done if k not in live_ids]:
            del self._seen_done[stale]
            self._seen_activity.pop(stale, None)
        return progressing

    def _stalled_running(self) -> int:
        agents = getattr(self._manager, "_agents", None)
        if not isinstance(agents, dict):
            return 0
        return sum(
            1
            for info in agents.values()
            if not getattr(info, "done", False) and getattr(info, "stalled", False)
        )

    # -- observability -------------------------------------------------------

    @property
    def policy(self) -> AdaptivePolicy:
        return self._policy

    @property
    def enabled(self) -> bool:
        return self._enabled

    def state(self) -> dict[str, Any]:
        """Structured state for ``resource_status`` and the dashboard."""
        last = self._samples[-1] if self._samples else None
        return {
            "enabled": self._enabled,
            "sample_secs": self._sample_secs,
            "ticks": self._ticks,
            "counts": dict(self._counts),
            "applied_exec_cap": self._applied_exec,
            "applied_gate_cap": self._applied_gate,
            "gate_pending": self._gate_pending,
            "last_error": self._last_error,
            "last_sample": (
                {
                    "loop_lag_ms": round(last.loop_lag_ms, 1),
                    "free_mem_mb": round(last.free_mem_mb, 1),
                    "rss_mb": round(last.rss_mb, 1),
                    "fd_count": last.fd_count,
                    "running": last.running,
                    "queued": last.queued,
                    "attributable_timeout_rate": round(last.attributable_timeout_rate, 3),
                    "completion_rate": round(last.completion_rate, 3),
                    "throttled_providers": sorted(last.per_provider_429),
                }
                if last
                else None
            ),
            "recent_decisions": list(self._recent),
            **self._policy.snapshot(),
        }


# -- process-wide registry for resource_status -------------------------------
#
# The gateway owns exactly one controller. ``resource_status`` (and the MCP
# tool behind it) reads it here so the module stays import-cheap and free of a
# gateway import. This is gateway-process state, not per-caller data.

_current: Optional[AdaptiveController] = None


def register(controller: Optional[AdaptiveController]) -> None:
    global _current
    _current = controller


def current() -> Optional[AdaptiveController]:
    return _current


def current_state() -> Optional[dict[str, Any]]:
    ctl = _current
    return ctl.state() if ctl is not None else None


# -- helpers -----------------------------------------------------------------


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list; 0.0 when empty."""
    if not sorted_values:
        return 0.0
    rank = math.ceil(q * len(sorted_values))
    return float(sorted_values[max(0, min(len(sorted_values) - 1, rank - 1))])


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


def _with_ceiling(params: PolicyParams, ceiling: int) -> PolicyParams:
    return replace(params, exec_ceiling=max(1, ceiling))


__all__ = [
    "ATTRIBUTABLE_MARKERS",
    "AdaptiveController",
    "ExecActuator",
    "HostSample",
    "classify_run_outcome",
    "current",
    "current_state",
    "probe_host",
    "register",
]

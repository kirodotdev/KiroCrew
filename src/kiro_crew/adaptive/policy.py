"""``AdaptivePolicy``: the deterministic AIMD state machine (RFC §5.2).

Pure. It reads :class:`~.signals.Sample` objects (each carrying its own
timestamp) and returns :class:`Decision` objects; it owns no clock, no task, no
socket. The controller in :mod:`.controller` is the only thing that acts on a
decision, and the tests drive the policy with hand-built samples.

Two tracks, one verdict. The gateway's execution cap (subagent spawns) and the
daemon's spawn gate (backend forks) have different bounds -- ``min(user_max, 4)``
/ 1 / ``user_max`` and 4 / 1 / 8 -- but they move on the same pressure verdict,
because both describe how many cold starts this HOST can absorb at once. Each
track earns its increases on its own evidence (completions for the exec track,
successful backend inits for the gate) and only when demand is actually at its
limit, so an idle track never drifts up.

Rules, with fixed tuning constants owned by this module:

* **Decrease** (multiplicative). Only on CORROBORATED pressure: a signal that
  is sufficient alone (loop lag >= 250 ms, memory <= critical) or at least two
  distinct signals in one sample (timeouts + slow starts, fds + gate failures,
  ...). Target ``max(ceil(cap * 0.5), healthy_in_flight)``, at most ``cap - 1``,
  never below the floor: halving is the lower bound, but a cut below the work
  that is currently succeeding frees nothing (nothing is ever killed) and would
  only be undone. That is what turns 10 concurrent starts with 4 timing out
  into 6, then 4. Cooldown 30 s between decreases; the successes counted
  before a decrease are discarded on the track that was cut, and only there
  -- a track already at its floor keeps the successes it has earned.
* **Increase**. Two regimes, one rule each, and the bound is
  ``min(exec_ceiling, sample.host_cap)`` -- the user's pin AND what this host's
  memory and CPU currently size the cap at (:attr:`~.signals.Sample.host_cap`,
  ``subagent.host_terms_subagent_cap``). A host figure of 0 means "not measured"
  and leaves the user's ceiling as the only bound; a host figure BELOW the live
  cap withholds further increases and never cuts, because nothing is killed.

  * **Slow start**, until this process meets its first corroborated pressure or
    pause: ``x2`` per clean sample window (``slow_start_clean_secs``, 5 s),
    once ``slow_start_successes`` (1) completions land and demand is at the
    cap. A fresh gateway therefore reaches the host's own figure in a handful
    of windows: a flat ``+1`` per ``increase_clean_secs`` (30 s) window from
    the fresh-start 4 to a 64 ceiling is 60 windows, i.e. 30 minutes of clean
    samples.
  * **Congestion avoidance**, afterwards: ``+1`` per ``increase_clean_secs``
    (30 s) window, once demand is at the cap and enough work has landed:
    ``min(increase_successes, cap)`` completions on the exec track -- one full
    wave of the CURRENT cap -- and ``increase_successes`` on the gate, whose
    counter is backend inits rather than finished runs. Scaling the exec bar to
    the CAP is what makes the first step off a floored cap affordable: a flat 20
    made ``1 -> 2`` cost twenty serial runs, so a cap cut to the floor stayed
    there.

  Both regimes also require the sample to be clear on the hysteresis side
  (lag < 100 ms, memory >= pressure line, no signal at all) and at least one
  window since the last pressure. An idle track never drifts up.
* **Pause and probe**. Severe pressure (memory below critical, or loop lag
  beyond 2 s) for two consecutive samples pauses dispatch: the exec cap goes
  to 0 grants and the gate to its floor. Running work is untouched. Once the
  severe condition clears, one probe is admitted (exec cap 1); when that probe
  completes without pressure the caps return to ``floor + 1`` and normal AIMD
  resumes. A probe that meets corroborated pressure re-pauses.
* **Provider throttling** never reaches the host caps. Throttled scopes are
  reported on the decision for the dependency coordinator (area L).
* **Fresh start** is ``min(user_max, 4)``; the process earns its way up.
* ``mode == "fixed"`` returns the initial caps forever (Q2 reversal).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Optional

from .signals import PressureReport, Sample, Thresholds, classify

MODE_AIMD = "aimd"
MODE_FIXED = "fixed"
MODES = (MODE_AIMD, MODE_FIXED)

ACTION_HOLD = "hold"
ACTION_DECREASE = "decrease"
ACTION_INCREASE = "increase"
ACTION_PAUSE = "pause"
ACTION_PROBE = "probe"
ACTION_RESUME = "resume"
ACTION_FIXED = "fixed"
ACTIONS = (
    ACTION_HOLD,
    ACTION_DECREASE,
    ACTION_INCREASE,
    ACTION_PAUSE,
    ACTION_PROBE,
    ACTION_RESUME,
    ACTION_FIXED,
)

DEFAULT_INITIAL = 4
DEFAULT_FLOOR = 1
DEFAULT_GATE_CEILING = 8
DEFAULT_DECREASE_FACTOR = 0.5
DEFAULT_DECREASE_COOLDOWN_SECS = 30.0
DEFAULT_INCREASE_CLEAN_SECS = 30.0
DEFAULT_INCREASE_SUCCESSES = 20
#: Slow start: on by default, ``x2`` per 5 s window on one completion, until
#: this process meets its first corroborated pressure or pause.
DEFAULT_SLOW_START = True
DEFAULT_SLOW_START_CLEAN_SECS = 5.0
DEFAULT_SLOW_START_SUCCESSES = 1
DEFAULT_SLOW_START_FACTOR = 2
DEFAULT_LAG_DECREASE_MS = 250.0
DEFAULT_LAG_INCREASE_MS = 100.0
DEFAULT_LAG_SEVERE_MS = 2000.0
DEFAULT_TIMEOUT_RATE = 0.2

_NEVER = float("-inf")


@dataclass(frozen=True)
class PolicyParams:
    """Bounds and rates. ``exec_ceiling`` is the user's cap and is never written."""

    exec_ceiling: int
    exec_initial: int = DEFAULT_INITIAL
    floor: int = DEFAULT_FLOOR
    gate_initial: int = DEFAULT_INITIAL
    gate_floor: int = DEFAULT_FLOOR
    gate_ceiling: int = DEFAULT_GATE_CEILING
    decrease_factor: float = DEFAULT_DECREASE_FACTOR
    decrease_cooldown_secs: float = DEFAULT_DECREASE_COOLDOWN_SECS
    increase_clean_secs: float = DEFAULT_INCREASE_CLEAN_SECS
    increase_successes: int = DEFAULT_INCREASE_SUCCESSES
    slow_start: bool = DEFAULT_SLOW_START
    slow_start_clean_secs: float = DEFAULT_SLOW_START_CLEAN_SECS
    slow_start_successes: int = DEFAULT_SLOW_START_SUCCESSES
    slow_start_factor: int = DEFAULT_SLOW_START_FACTOR
    mode: str = MODE_AIMD
    thresholds: Thresholds = field(default_factory=Thresholds)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.floor < 1 or self.gate_floor < 1:
            raise ValueError("floor must be >= 1")
        if self.exec_ceiling < 1:
            raise ValueError("exec_ceiling must be >= 1")
        if not 0.0 < self.decrease_factor < 1.0:
            raise ValueError("decrease_factor must be in (0, 1)")
        if self.slow_start_factor < 2:
            raise ValueError("slow_start_factor must be >= 2")

    @property
    def exec_floor(self) -> int:
        return min(self.floor, self.exec_ceiling)

    @property
    def exec_start(self) -> int:
        return _clamp(self.exec_initial, self.exec_floor, self.exec_ceiling)

    @property
    def gate_start(self) -> int:
        return _clamp(self.gate_initial, self.gate_floor, max(self.gate_floor, self.gate_ceiling))


@dataclass(frozen=True)
class Decision:
    """What the actuators should apply after one sample."""

    effective_exec_cap: int
    spawn_gate_capacity: int
    paused: bool
    probing: bool
    action: str
    reason: str
    signals: tuple[str, ...] = ()
    throttled_providers: tuple[str, ...] = ()
    #: True when either cap or the paused flag differs from the previous decision.
    changed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "effective_exec_cap": self.effective_exec_cap,
            "spawn_gate_capacity": self.spawn_gate_capacity,
            "paused": self.paused,
            "probing": self.probing,
            "action": self.action,
            "reason": self.reason,
            "signals": list(self.signals),
            "throttled_providers": list(self.throttled_providers),
        }


class AdaptivePolicy:
    """Deterministic AIMD over two caps. See the module docstring for the rules."""

    def __init__(self, params: PolicyParams) -> None:
        self._p = params
        self._exec_cap = params.exec_start
        self._gate_cap = params.gate_start
        self._paused = False
        self._probing = False
        self._severe_streak = 0
        # Slow start is active only when config enables it and this process has
        # not retired it after the first corroborated pressure or pause.
        self._slow_start_retired = False
        self._slow_start = bool(params.slow_start)
        #: Last host figure seen, for the state snapshot only.
        self._host_cap = 0
        self._last_decrease_at = _NEVER
        self._last_increase_at = _NEVER
        self._last_pressure_at = _NEVER
        # Success counters at the last cap change; increases are earned
        # relative to these. ``sample.completions`` is the controller's own
        # in-process counter, built beside this policy and only incremented, so
        # its base cannot be overtaken from below; the gate's counter belongs to
        # the DAEMON and can restart underneath a live policy, which is what
        # ``_rebase_dropped_gate_successes`` absorbs.
        self._exec_success_base = 0
        self._gate_success_base = 0
        self._probe_base: Optional[int] = None
        # (t, cumulative gate failures) per sample, kept one window deep: the
        # daemon's ``outcomes.failure`` is a lifetime counter, and the signal
        # is "failures in the window", so the policy diffs it here.
        self._gate_failure_history: deque[tuple[float, int]] = deque()
        self._last: Optional[Decision] = None
        self._decisions = 0

    # -- read-only state -----------------------------------------------------

    @property
    def params(self) -> PolicyParams:
        return self._p

    @property
    def exec_cap(self) -> int:
        return self._exec_cap

    @property
    def gate_cap(self) -> int:
        return self._gate_cap

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def slow_start(self) -> bool:
        """True while config enables slow start and this process has not retired it."""
        return self._slow_start

    @property
    def last_decision(self) -> Optional[Decision]:
        return self._last

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self._p.mode,
            "effective_exec_cap": self._exec_cap,
            "exec_ceiling": self._p.exec_ceiling,
            "exec_floor": self._p.exec_floor,
            "host_cap": self._host_cap,
            "slow_start": self._slow_start,
            "spawn_gate_capacity": self._gate_cap,
            "gate_ceiling": self._p.gate_ceiling,
            "gate_floor": self._p.gate_floor,
            "paused": self._paused,
            "probing": self._probing,
            "decisions": self._decisions,
            "last": self._last.as_dict() if self._last else None,
        }

    # -- reconfiguration -----------------------------------------------------

    def update_params(self, params: PolicyParams) -> None:
        """Adopt new bounds / rates without losing the earned position.

        A lowered ceiling clamps the live cap; a raised one leaves it where it
        is (the process still has to earn the room). Switching to ``fixed``
        snaps both caps to their initial values on the next decision.

        Slow start follows its config flag until this process observes its first
        corroborated pressure or pause. That evidence retires slow start for the
        process lifetime, so later config edits cannot revive it.
        """
        self._p = params
        self._slow_start = bool(params.slow_start) and not self._slow_start_retired
        self._exec_cap = _clamp(
            self._exec_cap, 0 if self._paused else params.exec_floor, params.exec_ceiling
        )
        self._gate_cap = _clamp(
            self._gate_cap, params.gate_floor, max(params.gate_floor, params.gate_ceiling)
        )

    # -- the decision --------------------------------------------------------

    def observe(self, sample: Sample) -> Decision:
        self._decisions += 1
        if self._p.mode == MODE_FIXED:
            self._paused = False
            self._probing = False
            self._exec_cap = self._p.exec_start
            self._gate_cap = self._p.gate_start
            return self._emit(ACTION_FIXED, "fixed mode: caps pinned at their initial values", None)

        sample = replace(sample, gate_failures_in_window=self._windowed_gate_failures(sample))
        self._host_cap = max(0, int(sample.host_cap))
        self._rebase_dropped_gate_successes(sample)
        report = classify(sample, self._p.thresholds)
        now = sample.t
        if self._last_increase_at == _NEVER:
            # A fresh process earns its first increase: the first clean window
            # is measured from the first sample, not from the dawn of time.
            self._last_increase_at = now
        if report.any:
            self._last_pressure_at = now
        self._severe_streak = self._severe_streak + 1 if report.severe else 0

        if self._paused:
            return self._while_paused(sample, report)

        if self._severe_streak >= self._p.thresholds.severe_samples:
            return self._pause(
                sample, report, "severe pressure for " f"{self._severe_streak} samples"
            )

        if report.corroborated:
            # Corroborated pressure is the evidence slow start was waiting for:
            # from here on this process grows +1 at a time, never x2. Set before
            # the cooldown check, so pressure the cooldown merely HOLDS still
            # ends slow start -- the host said no either way.
            self._slow_start_retired = True
            self._slow_start = False
            if now - self._last_decrease_at < self._p.decrease_cooldown_secs:
                return self._emit(ACTION_HOLD, "pressure inside the decrease cooldown", report)
            return self._decrease(sample, report)

        if report.any:
            return self._emit(ACTION_HOLD, "single uncorroborated signal", report)

        return self._maybe_increase(sample, report)

    def _windowed_gate_failures(self, sample: Sample) -> int:
        """Spawn-gate failures that landed inside the evidence window.

        ``sample.spawn_gate.failures`` is the daemon's LIFETIME counter. The
        window count is that value minus the value at the sample just older
        than ``gate_failure_window_secs`` (the first sample seen when the
        history is still shorter than the window: failures before the policy
        started are not evidence about the present). A counter that went DOWN
        is a daemon restart -- the history is reset to it. Without this, two
        init failures in a daemon's lifetime read as permanent pressure and no
        increase is ever earned again (the experiment's D1).
        """
        cum = int(sample.spawn_gate.failures)
        now = sample.t
        window = float(self._p.thresholds.gate_failure_window_secs)
        hist = self._gate_failure_history
        if hist and cum < hist[-1][1]:
            hist.clear()
        hist.append((now, cum))
        # Drop entries older than the window, but keep the newest of those as
        # the baseline so the delta spans exactly one window.
        while len(hist) > 1 and hist[1][0] <= now - window:
            hist.popleft()
        return max(0, cum - hist[0][1])

    def _rebase_dropped_gate_successes(self, sample: Sample) -> None:
        """Absorb a daemon restart on the gate's LIFETIME success counter.

        ``spawn_gate.successes`` is the daemon's ``outcomes.success``, and it
        starts over at zero when that process respawns under a live policy. A
        counter that went DOWN is that restart -- the base is reset to it, the
        same remedy ``_windowed_gate_failures`` applies to its history. Without
        it ``gate_successes`` is negative and the gate cap has to re-earn the
        whole stale base on top of ``increase_successes``, so a restart costs
        the cap an increase the fresh inits already paid for.

        SILENCE is not a restart. A failed ``stats()`` read reaches the policy
        as the all-zero ``SpawnGateStats`` default -- no capacity, no outcome --
        and a live daemon always reports its capacity, so that shape is "no
        snapshot" and is skipped. Rebasing onto it would let the SAME daemon's
        unchanged lifetime total buy a ``+1`` the moment it answers again. The
        skip loses nothing: a real drop is still below the base on the next
        sample that carries data, and it is absorbed there.
        """
        gate = sample.spawn_gate
        if gate.capacity <= 0 and not (gate.successes or gate.failures or gate.neutral):
            return
        successes = int(gate.successes)
        if successes < self._gate_success_base:
            self._gate_success_base = successes

    # -- transitions ---------------------------------------------------------

    def _pause(self, sample: Sample, report: PressureReport, why: str) -> Decision:
        old_exec, old_gate = self._exec_cap, self._gate_cap
        self._paused = True
        self._probing = False
        self._slow_start_retired = True
        self._slow_start = False
        self._exec_cap = 0
        self._gate_cap = self._p.gate_floor
        self._last_decrease_at = sample.t
        self._reset_bases(sample, old_exec, old_gate)
        return self._emit(ACTION_PAUSE, f"paused: {why}", report)

    def _while_paused(self, sample: Sample, report: PressureReport) -> Decision:
        if report.severe:
            if self._probing:
                # The probe met severe pressure: take the grant back.
                self._probing = False
                self._exec_cap = 0
                return self._emit(ACTION_PAUSE, "probe met severe pressure; re-paused", report)
            return self._emit(ACTION_HOLD, "paused: severe pressure persists", report)
        if not self._probing:
            self._probing = True
            self._exec_cap = min(1, self._p.exec_ceiling)
            self._gate_cap = self._p.gate_floor
            self._probe_base = sample.completions
            return self._emit(ACTION_PROBE, "severe pressure cleared; admitting one probe", report)
        # Probing: wait for the probe to complete without pressure.
        if report.corroborated:
            self._probing = False
            self._exec_cap = 0
            self._last_decrease_at = sample.t
            return self._emit(ACTION_PAUSE, "probe met corroborated pressure; re-paused", report)
        base = self._probe_base if self._probe_base is not None else sample.completions
        if sample.completions > base and not report.any:
            old_exec, old_gate = self._exec_cap, self._gate_cap
            self._paused = False
            self._probing = False
            self._probe_base = None
            self._exec_cap = _clamp(
                self._p.exec_floor + 1, self._p.exec_floor, self._p.exec_ceiling
            )
            self._gate_cap = _clamp(
                self._p.gate_floor + 1, self._p.gate_floor, self._p.gate_ceiling
            )
            self._reset_bases(sample, old_exec, old_gate)
            self._last_increase_at = sample.t
            return self._emit(ACTION_RESUME, "probe completed; resuming at floor + 1", report)
        return self._emit(ACTION_HOLD, "probe in flight", report)

    def _decrease(self, sample: Sample, report: PressureReport) -> Decision:
        new_exec = _decrease_target(
            self._exec_cap, sample.healthy_in_flight, self._p.exec_floor, self._p.decrease_factor
        )
        new_gate = _decrease_target(self._gate_cap, 0, self._p.gate_floor, self._p.decrease_factor)
        if new_exec == self._exec_cap and new_gate == self._gate_cap:
            return self._emit(ACTION_HOLD, "pressure at the floor; nothing left to cut", report)
        old_exec, old_gate = self._exec_cap, self._gate_cap
        self._exec_cap = new_exec
        self._gate_cap = new_gate
        self._last_decrease_at = sample.t
        self._reset_bases(sample, old_exec, old_gate)
        return self._emit(
            ACTION_DECREASE,
            "corroborated pressure: " + ",".join(sorted(report.signals)),
            report,
        )

    def _maybe_increase(self, sample: Sample, report: PressureReport) -> Decision:
        p = self._p
        now = sample.t
        if not report.clear_for_increase:
            return self._emit(ACTION_HOLD, "clear but inside the hysteresis band", report)
        window = p.slow_start_clean_secs if self._slow_start else p.increase_clean_secs
        if now - self._last_pressure_at < window:
            return self._emit(ACTION_HOLD, "clear; waiting out the clean window", report)
        if now - self._last_increase_at < window:
            return self._emit(ACTION_HOLD, "clear; one increase per window", report)

        changed = False
        exec_target = self._growth_ceiling(sample)
        exec_successes = sample.completions - self._exec_success_base
        completion_earned = exec_successes >= self._required_exec_successes(self._exec_cap)
        # A long useful run need not FINISH before a second slot can open.
        # Fresh stream progress buys only one exploratory slot, with measured
        # host headroom and no provider throttle, after the same clean window.
        # It never buys doubling or relaxes the independent init-gate bar.
        progress_probe = (
            sample.progressing > 0
            and sample.queued > 0
            and sample.running >= self._exec_cap
            and sample.host_cap > self._exec_cap
            and sample.free_mem_mb >= max(0.0, p.thresholds.mem_pressure_mb)
            and not report.throttled_providers
        )
        probed = False
        if (
            self._exec_cap < exec_target
            and (completion_earned or progress_probe)
            and sample.demand >= self._exec_cap
        ):
            probed = not completion_earned
            self._exec_cap = (
                min(exec_target, self._exec_cap + 1)
                if probed
                else self._step_up(self._exec_cap, exec_target)
            )
            self._exec_success_base = sample.completions
            changed = True

        gate = sample.spawn_gate
        gate_successes = gate.successes - self._gate_success_base
        gate_demand = gate.queued > 0 or gate.in_flight >= self._gate_cap
        if (
            self._gate_cap < p.gate_ceiling
            and gate_successes >= self._required_gate_successes()
            and gate_demand
        ):
            self._gate_cap = self._step_up(self._gate_cap, p.gate_ceiling)
            self._gate_success_base = gate.successes
            changed = True

        if not changed:
            return self._emit(ACTION_HOLD, "clear; increase not yet earned or no demand", report)
        self._last_increase_at = now
        reason = (
            "fresh progress with host headroom earned one exec probe"
            if probed
            else (
                f"clean window earned x{p.slow_start_factor} (slow start)"
                if self._slow_start
                else "clean window earned +1"
            )
        )
        return self._emit(ACTION_INCREASE, reason, report)

    def _growth_ceiling(self, sample: Sample) -> int:
        """How high an execution-cap increase may climb on THIS sample.

        ``min(user ceiling, host cap)``. ``host_cap`` is what this host's memory
        and CPU size the subagent cap at right now
        (``subagent.host_terms_subagent_cap``); ``0`` means it was not measured
        and leaves the user's ceiling alone. A host figure BELOW the live cap
        only withholds the next increase -- it is never a cut, because a cut
        cannot free work that is already running and the user's pin is the hard
        ceiling.
        """
        ceiling = self._p.exec_ceiling
        host = int(sample.host_cap)
        if host > 0:
            ceiling = min(ceiling, max(self._p.exec_floor, host))
        return ceiling

    def _required_exec_successes(self, cap: int) -> int:
        """Completions since the last change that an exec increase must see.

        Slow start asks for ``slow_start_successes`` -- one completion already
        shows the host absorbing the current cap. Afterwards the bar is
        ``min(increase_successes, cap)``: one full wave of the CURRENT cap. The
        flat 20 it replaces is what made a floored cap permanent -- ``1 -> 2``
        cost twenty serial runs, and every one of them ran alone.
        """
        if self._slow_start:
            return max(1, self._p.slow_start_successes)
        return max(1, min(self._p.increase_successes, cap))

    def _required_gate_successes(self) -> int:
        """The gate's bar, which is NOT scaled to its cap and NOT eased by slow start.

        Backend inits land far faster than subagent runs finish and the gate
        ceiling is 8, so ``increase_successes`` was never the barrier on this
        track -- and the restart-rebase contract (a respawned daemon owes the
        whole bar again on its fresh counter) is pinned against that number.

        Slow start deliberately does not reach here. It exists to cross the
        distance between a floored EXECUTION cap and the user's ceiling, which
        is tens of steps; the gate's whole range is ``4 -> 8``, one doubling. Had
        it applied, a single backend init inside one clean window would take the
        gate to its ceiling and hand a respawned daemon its full capacity back
        for one success -- a bar of 1, in the one place the contract above says
        the bar must be the full number.
        """
        return max(1, self._p.increase_successes)

    def _step_up(self, cap: int, ceiling: int) -> int:
        """The next cap after an earned increase, bounded by *ceiling*."""
        if self._slow_start:
            return min(ceiling, max(cap + 1, cap * self._p.slow_start_factor))
        return min(ceiling, cap + 1)

    # -- helpers -------------------------------------------------------------

    def _reset_bases(self, sample: Sample, old_exec: int, old_gate: int) -> None:
        """Discard the successes counted before a cap change -- on the track
        that MOVED, and only there.

        The two tracks share one pressure verdict but earn separately, and a
        cut on one is not evidence about the other. Resetting both on every
        transition made the exec track re-owe its full ``increase_successes``
        each time the gate alone was cut: with exec already at its floor, one
        corroborated loop-lag sample lowered the gate, wiped the exec
        completions earned since the last exec change, and the exec cap never
        climbed back. A track whose cap did not change keeps its base, so its
        earned position survives the other track's transition.
        """
        if self._exec_cap != old_exec:
            self._exec_success_base = sample.completions
        if self._gate_cap != old_gate:
            self._gate_success_base = sample.spawn_gate.successes

    def _emit(self, action: str, reason: str, report: Optional[PressureReport]) -> Decision:
        prev = self._last
        changed = (
            prev is None
            or prev.effective_exec_cap != self._exec_cap
            or prev.spawn_gate_capacity != self._gate_cap
            or prev.paused != self._paused
        )
        decision = Decision(
            effective_exec_cap=self._exec_cap,
            spawn_gate_capacity=self._gate_cap,
            paused=self._paused,
            probing=self._probing,
            action=action,
            reason=reason,
            signals=tuple(sorted(report.signals)) if report else (),
            throttled_providers=tuple(sorted(report.throttled_providers)) if report else (),
            changed=changed,
        )
        self._last = decision
        return decision


def _decrease_target(cap: int, healthy: int, floor: int, factor: float) -> int:
    """Next cap after a corroborated decrease. See the module docstring."""
    if cap <= floor:
        return floor
    target = max(math.ceil(cap * factor), int(healthy))
    target = min(target, cap - 1)
    return max(floor, target)


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(int(value), hi))


def params_from_config(
    cfg: object,
    *,
    exec_ceiling: int,
    gate_ceiling: int = DEFAULT_GATE_CEILING,
    gate_initial: int = DEFAULT_INITIAL,
    gate_floor: int = DEFAULT_FLOOR,
) -> PolicyParams:
    """Build :class:`PolicyParams` from ``cfg.agent.adaptive_*`` keys.

    Every read has a default so a partial or duck-typed config works; the
    memory thresholds come from the same ``resource_pressure_gb`` /
    ``resource_critical_gb`` pair the advisory surfaces use.
    """
    agent = getattr(cfg, "agent", None)

    def _get(name: str, default: object) -> object:
        return getattr(agent, name, default)

    mode = str(_get("adaptive_concurrency_mode", MODE_AIMD))
    if mode not in MODES:
        mode = MODE_AIMD
    thresholds = Thresholds(
        lag_decrease_ms=DEFAULT_LAG_DECREASE_MS,
        lag_increase_ms=DEFAULT_LAG_INCREASE_MS,
        lag_severe_ms=DEFAULT_LAG_SEVERE_MS,
        mem_pressure_mb=_f(_get("resource_pressure_gb", 4.0), 4.0) * 1024.0,
        mem_critical_mb=_f(_get("resource_critical_gb", 2.0), 2.0) * 1024.0,
        timeout_rate=DEFAULT_TIMEOUT_RATE,
    )
    return PolicyParams(
        exec_ceiling=max(1, int(exec_ceiling)),
        exec_initial=_i(_get("adaptive_initial", DEFAULT_INITIAL), DEFAULT_INITIAL),
        floor=max(1, _i(_get("adaptive_floor", DEFAULT_FLOOR), DEFAULT_FLOOR)),
        gate_initial=gate_initial,
        gate_floor=max(1, gate_floor),
        gate_ceiling=max(1, gate_ceiling),
        slow_start=bool(_get("adaptive_slow_start", DEFAULT_SLOW_START)),
        mode=mode,
        thresholds=thresholds,
    )


def _f(value: object, default: float) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _i(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return default


__all__ = [
    "ACTIONS",
    "ACTION_DECREASE",
    "ACTION_FIXED",
    "ACTION_HOLD",
    "ACTION_INCREASE",
    "ACTION_PAUSE",
    "ACTION_PROBE",
    "ACTION_RESUME",
    "AdaptivePolicy",
    "Decision",
    "MODES",
    "MODE_AIMD",
    "MODE_FIXED",
    "PolicyParams",
    "params_from_config",
]

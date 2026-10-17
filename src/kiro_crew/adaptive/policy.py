"""``AdaptivePolicy``: the deterministic AIMD state machine (RFC §5.2).

Pure. It reads :class:`~.signals.Sample` objects (each carrying its own
timestamp) and returns :class:`Decision` objects; it owns no clock, no task, no
socket. The controller in :mod:`.controller` is the only thing that acts on a
decision, and the tests drive the policy with hand-built samples.

Two tracks, two kinds of evidence, one decision per sample. The gateway's
execution cap (subagent and runner-lane starts) and the daemon's spawn gate
(backend forks) have different bounds -- ``user_max`` / ``floor`` /
``user_max`` and ``spawn_concurrency_initial`` / ``_min`` / the gate ceiling --
and they read different evidence (:mod:`.signals`):

* the **execution cap** reads WORK evidence only: attributable timeouts, slow
  starts, failing MCP servers, spawn-gate init failures, fd and process
  exhaustion, a low completion rate. It never reads the gateway's loop lag or
  free memory. Memory is the per-start spawn floor's (``agent.spawn_min_memory_gb``),
  which prices every start against what it will settle at; a count cap moved
  by memory or lag on top of that floor is what throttled one chat's subagents
  behind another's. Nothing is sufficient alone for it: a cut needs two
  distinct work signals in one sample. It is never paused.
* the **spawn gate** reads every signal EXCEPT the gateway's loop lag, plus the
  daemon's own slow inits: memory at the critical line, gate init failures and
  slow inits are each sufficient alone, and critical memory pauses it at its
  floor. It bounds how many backend processes fork and initialize at once
  inside the MCP daemon, a separate process, so the dashboard gateway's
  event-loop lag is no evidence about it.

Each track earns its increases on its own evidence (completions for the exec
track, successful backend inits for the gate) and only when demand is actually
at its limit, so an idle track never drifts up.

Rules, with fixed tuning constants owned by this module:

* **Decrease** (multiplicative). Only on CORROBORATED pressure for that track.
  Exec: ``max(ceil(cap * 0.5), healthy_in_flight)``, at most ``cap - 1``, never
  below the floor: halving is the lower bound, but a cut below the work that is
  currently succeeding frees nothing (nothing is ever killed) and would only be
  undone. That is what turns 10 concurrent starts with 4 timing out into 6, then
  4. Gate: ``ceil(cap * 0.5)`` at most ``cap - 1``, never below its floor.
  Cooldown 30 s between decreases, per track; the successes counted before a
  decrease are discarded on the track that was cut, and only there -- a track
  that did not move keeps the successes it has earned.
* **Increase**. Two regimes, one rule each. The exec bound is the user's
  configured ceiling (``exec_ceiling``: an explicit ``max_subagents``, or
  ``agent.subagent_auto_max`` when it is 0), which stands in for provider
  concurrency and fd/PID limits; no host prediction sits under it.

  * **Slow start**, until this process meets its first corroborated pressure or
    pause: ``x2`` per clean sample window (``slow_start_clean_secs``, 5 s),
    once ``slow_start_successes`` (1) completions land and demand is at the
    cap. Only a cap below its ceiling uses it -- the exec track STARTS at the
    ceiling -- so it is what a lowered cap or a raised ceiling climbs with.
  * **Congestion avoidance**, afterwards: ``+1`` per ``increase_clean_secs``
    (30 s) window, once demand is at the cap and enough work has landed:
    ``min(increase_successes, cap)`` completions on the exec track -- one full
    wave of the CURRENT cap -- and ``increase_successes`` on the gate, whose
    counter is backend inits rather than finished runs.

  Each track also requires the sample to be clear of ITS evidence and at least
  one window since its last pressure: the exec track no work signal, the gate
  none of its signals plus memory at or above the pressure line.
* **Idle recovery**. A cut is evidence about the work that was running when
  it fired. Once the exec track has had no demand at all (nothing running or
  queued) and no work signal for ``idle_recovery_secs`` (60 s), that evidence
  is stale, yet the earn rules above can never retire it: they need demand at
  the cap and completions, and an idle track -- or one whose load runs on the
  runner lane, which the exec track does not count -- produces neither. So a
  cap below the fresh-start value climbs ``+1`` per clean window while that
  holds. The bound is the fresh-start cap (the ceiling, unless a test pins
  ``exec_initial``). Any work signal restarts the idle clock. The spawn gate
  recovers the same way: below its fresh-start value, ``idle_recovery_secs``
  with none of its signals and no cut climbs it ``+1`` per clean window, with
  or without demand, so a cut is not permanent just because no backend start
  happens to be queued at the cap.
* **Pause and probe** (spawn gate only). Memory below critical for two
  consecutive samples holds the gate
  at its floor. Once the severe condition clears, the gate probes: when a
  completion lands without pressure it resumes at ``floor + 1``; an idle host
  resumes it at the floor. A probe that meets corroborated pressure re-pauses.
  The execution cap is untouched throughout.
* **Provider throttling** never reaches the host caps. Throttled scopes are
  reported on the decision for the dependency coordinator (area L).
* **Fresh start**: the exec track at its ceiling, the gate at its initial
  value. A raised ceiling is followed at once while the exec cap sits at the
  old one (nothing cut it); a cap below it earns the room.
* **Why the cap is low** is on the snapshot: ``last_cut`` is the decision that
  last lowered the exec cap (action, reason, signals, sample time), and a hold
  names what the next increase is waiting for (demand at the cap, completions,
  or the idle clock).
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
#: Idle recovery: no exec demand and no work signal for this long retires a
#: cut, and the cap climbs back toward the fresh-start value one clean window
#: at a time. Two congestion-avoidance windows, so a cap is never restored on
#: the sample right after the pressure that set it.
DEFAULT_IDLE_RECOVERY_SECS = 60.0

_NEVER = float("-inf")

#: How strongly each action speaks for a sample that moved both tracks: the
#: decision's ``action`` is the strongest of the two tracks' actions.
_ACTION_RANK = {
    ACTION_HOLD: 0,
    ACTION_INCREASE: 1,
    ACTION_DECREASE: 2,
    ACTION_RESUME: 3,
    ACTION_PROBE: 4,
    ACTION_PAUSE: 5,
}


@dataclass(frozen=True)
class PolicyParams:
    """Bounds and rates. ``exec_ceiling`` is the user's cap and is never written.

    ``exec_initial`` is ``None`` in production: the execution cap starts at its
    ceiling. A test that pins a congestion-avoidance rule from a lower cap
    passes a number.
    """

    exec_ceiling: int
    exec_initial: Optional[int] = None
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
    idle_recovery_secs: float = DEFAULT_IDLE_RECOVERY_SECS
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
        if self.idle_recovery_secs <= 0:
            raise ValueError("idle_recovery_secs must be > 0")

    @property
    def exec_floor(self) -> int:
        return min(self.floor, self.exec_ceiling)

    @property
    def exec_start(self) -> int:
        initial = self.exec_ceiling if self.exec_initial is None else self.exec_initial
        return _clamp(initial, self.exec_floor, self.exec_ceiling)

    @property
    def gate_start(self) -> int:
        return _clamp(self.gate_initial, self.gate_floor, max(self.gate_floor, self.gate_ceiling))


@dataclass(frozen=True)
class Decision:
    """What the actuators should apply after one sample."""

    effective_exec_cap: int
    spawn_gate_capacity: int
    #: The SPAWN GATE is paused at its floor (severe host pressure). The
    #: execution cap is never paused.
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
        # The spawn gate's pause. The execution cap has none.
        self._paused = False
        self._probing = False
        self._severe_streak = 0
        # Slow start is active only when config enables it and this process has
        # not retired it after the first corroborated pressure or pause.
        self._slow_start_retired = False
        self._slow_start = bool(params.slow_start)
        # Per-track clocks: each track decreases, waits out its clean window and
        # takes one increase per window on its own evidence.
        self._last_exec_decrease_at = _NEVER
        self._last_gate_decrease_at = _NEVER
        self._last_exec_increase_at = _NEVER
        self._last_gate_increase_at = _NEVER
        # Last sample with ANY signal (the gate's evidence) and last sample with
        # a WORK signal (the exec track's: everything but loop lag and memory).
        self._last_pressure_at = _NEVER
        # Last sample carrying one of the GATE's signals (loop lag excluded).
        self._last_gate_pressure_at = _NEVER
        self._last_exec_pressure_at = _NEVER
        # Last sample with exec demand (running + queued > 0): idle recovery
        # measures its clock from the later of this and the last pressure.
        self._last_busy_at = _NEVER
        # The decision that last LOWERED the exec cap, for ``snapshot``: the
        # answer to "why is the cap low" outlives the 32-entry decision ring.
        self._last_cut: Optional[dict[str, object]] = None
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
        """Whether the SPAWN GATE is paused at its floor."""
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
            "slow_start": self._slow_start,
            "spawn_gate_capacity": self._gate_cap,
            "gate_ceiling": self._p.gate_ceiling,
            "gate_floor": self._p.gate_floor,
            "paused": self._paused,
            "probing": self._probing,
            "last_cut": dict(self._last_cut) if self._last_cut else None,
            "decisions": self._decisions,
            "last": self._last.as_dict() if self._last else None,
        }

    # -- reconfiguration -----------------------------------------------------

    def update_params(self, params: PolicyParams) -> None:
        """Adopt new bounds / rates without losing the earned position.

        A lowered ceiling clamps the live cap. A raised one is followed at once
        when the exec cap sat AT the old ceiling -- nothing had cut it, and the
        track starts at its ceiling -- and otherwise leaves the cap where it is,
        to earn the room. Switching to ``fixed`` snaps both caps to their
        initial values on the next decision.

        Slow start follows its config flag until this process observes its first
        corroborated pressure or pause. That evidence retires slow start for the
        process lifetime, so later config edits cannot revive it.
        """
        old = self._p
        self._p = params
        self._slow_start = bool(params.slow_start) and not self._slow_start_retired
        exec_cap = self._exec_cap
        if params.exec_ceiling > old.exec_ceiling and exec_cap >= old.exec_ceiling:
            exec_cap = params.exec_ceiling
        self._exec_cap = _clamp(exec_cap, params.exec_floor, params.exec_ceiling)
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
        self._rebase_dropped_gate_successes(sample)
        report = classify(sample, self._p.thresholds)
        now = sample.t
        if self._last_exec_increase_at == _NEVER:
            # A fresh process earns its first increase: the first clean window
            # is measured from the first sample, not from the dawn of time.
            self._last_exec_increase_at = now
            self._last_gate_increase_at = now
        if report.any:
            self._last_pressure_at = now
        if report.gate_signals:
            self._last_gate_pressure_at = now
        if report.exec_signals:
            self._last_exec_pressure_at = now
        if sample.demand > 0:
            self._last_busy_at = now
        self._severe_streak = self._severe_streak + 1 if report.gate_severe else 0
        if report.corroborated:
            # Corroborated pressure is the evidence slow start was waiting for:
            # from here on this process grows +1 at a time, never x2. Retired
            # before either track's cooldown check, so pressure a cooldown
            # merely HOLDS still ends slow start -- the host said no either way.
            self._retire_slow_start()

        exec_action, exec_note = self._exec_step(sample, report)
        gate_action, gate_note = self._gate_step(sample, report)
        action = max(exec_action, gate_action, key=_ACTION_RANK.__getitem__)
        moved = [
            note
            for act, note in ((exec_action, exec_note), (gate_action, gate_note))
            if act != ACTION_HOLD
        ]
        if not moved:
            # Two holds. The exec hold names what the next exec step waits for,
            # unless the news is the gate's: it is paused, or only host-only
            # evidence (loop lag, memory) fired, which the exec track ignores.
            gate_news = self._paused or (report.any and not report.exec_signals)
            reason = gate_note if gate_news else exec_note
        elif exec_action == gate_action == ACTION_DECREASE:
            # One cut of both caps on one sample: one reason, every signal.
            reason = "corroborated pressure: " + ",".join(sorted(report.signals))
        else:
            reason = "; ".join(moved)
        return self._emit(action, reason, report)

    def _retire_slow_start(self) -> None:
        self._slow_start_retired = True
        self._slow_start = False

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

    def _idle_for(self, sample: Sample, *, since_pressure: float) -> float:
        """Seconds the exec track has been idle AND free of *since_pressure*'s
        evidence, else 0.

        Idle is no demand at all on this sample (nothing running or queued on
        the exec track) and none since the idle clock started. Both reset the
        clock, so a cap is restored only on samples that could not have been
        caused by the load that cut it.
        """
        if sample.demand > 0:
            return 0.0
        since = max(since_pressure, self._last_busy_at)
        if since == _NEVER:
            # No pressure and no demand ever seen: measured from the first
            # sample, like the first clean window.
            since = self._last_exec_increase_at
        return max(0.0, sample.t - since)

    def _exec_idle_for(self, sample: Sample) -> float:
        return self._idle_for(sample, since_pressure=self._last_exec_pressure_at)

    def _note_cut(self, sample: Sample, action: str, reason: str, signals: frozenset[str]) -> None:
        self._last_cut = {
            "t": sample.t,
            "action": action,
            "reason": reason,
            "signals": sorted(signals),
        }

    # -- the execution track -------------------------------------------------

    def _exec_step(self, sample: Sample, report: PressureReport) -> tuple[str, str]:
        """One sample on the execution cap: work evidence only, never paused."""
        p = self._p
        now = sample.t
        if report.exec_corroborated:
            if now - self._last_exec_decrease_at < p.decrease_cooldown_secs:
                return ACTION_HOLD, "pressure inside the decrease cooldown"
            new_exec = _decrease_target(
                self._exec_cap, sample.healthy_in_flight, p.exec_floor, p.decrease_factor
            )
            if new_exec == self._exec_cap:
                return ACTION_HOLD, "pressure at the floor; nothing left to cut"
            self._exec_cap = new_exec
            self._last_exec_decrease_at = now
            self._exec_success_base = sample.completions
            reason = "corroborated pressure: " + ",".join(sorted(report.exec_signals))
            self._note_cut(sample, ACTION_DECREASE, reason, report.exec_signals)
            return ACTION_DECREASE, reason
        if report.exec_signals:
            return ACTION_HOLD, "single uncorroborated signal"
        return self._exec_maybe_increase(sample, report)

    def _exec_maybe_increase(self, sample: Sample, report: PressureReport) -> tuple[str, str]:
        p = self._p
        now = sample.t
        window = p.slow_start_clean_secs if self._slow_start else p.increase_clean_secs
        exec_successes = sample.completions - self._exec_success_base
        if now - self._last_exec_pressure_at < window:
            return ACTION_HOLD, "clear; waiting out the clean window"
        if now - self._last_exec_increase_at < window:
            return ACTION_HOLD, "clear; one increase per window"
        target = self._growth_ceiling(sample)
        completion_earned = exec_successes >= self._required_exec_successes(self._exec_cap)
        # A long useful run need not FINISH before a second slot can open.
        # Fresh stream progress buys only one exploratory slot, with no
        # provider throttle, after the same clean window. It never buys
        # doubling or relaxes the independent init-gate bar. Free memory is not
        # a term: the spawn floor prices every start it admits.
        progress_probe = (
            sample.progressing > 0
            and sample.queued > 0
            and sample.at_cap_running >= self._exec_cap
            and not report.throttled_providers
        )
        if (
            self._exec_cap < target
            and (completion_earned or progress_probe)
            and sample.at_cap_demand >= self._exec_cap
        ):
            probed = not completion_earned
            self._exec_cap = (
                min(target, self._exec_cap + 1) if probed else self._step_up(self._exec_cap, target)
            )
            self._exec_success_base = sample.completions
            self._last_exec_increase_at = now
            if probed:
                return ACTION_INCREASE, "fresh progress earned one exec probe"
            if self._slow_start:
                return ACTION_INCREASE, f"clean window earned x{p.slow_start_factor} (slow start)"
            return ACTION_INCREASE, "clean window earned +1"
        # Read before ``_last_exec_increase_at`` moves: with no pressure and no
        # demand ever seen, the idle clock is measured from that instant.
        idle_secs = self._exec_idle_for(sample)
        if self._exec_cap < min(p.exec_start, target) and idle_secs >= p.idle_recovery_secs:
            self._exec_cap += 1
            self._exec_success_base = sample.completions
            self._last_exec_increase_at = now
            return ACTION_INCREASE, (
                f"idle and clear for {idle_secs:.0f}s: +1 toward the "
                f"fresh-start cap {p.exec_start}"
            )
        return ACTION_HOLD, self._hold_reason(sample, exec_successes)

    def _hold_reason(self, sample: Sample, exec_successes: int) -> str:
        """What a clear, in-window hold is waiting for on the exec track."""
        cap = self._exec_cap
        if cap >= self._growth_ceiling(sample):
            return "clear; exec cap at the ceiling"
        if sample.demand <= 0 and cap < self._p.exec_start:
            wait = max(0.0, self._p.idle_recovery_secs - self._exec_idle_for(sample))
            return f"idle; restoring toward {self._p.exec_start} in {wait:.0f}s"
        if sample.demand < cap:
            return f"clear; no demand at the cap ({sample.demand} running or queued < {cap})"
        required = self._required_exec_successes(cap)
        return (
            f"clear; increase not yet earned ({max(0, exec_successes)}/{required} "
            "completions since the last change)"
        )

    def _growth_ceiling(self, sample: Sample) -> int:
        """How high an execution-cap increase may climb on THIS sample.

        The user's ceiling, and only that. An earlier reading clamped it to a
        host figure predicted from p90 peak memory and CPU per agent; on a
        32-core host with tens of GB free that prediction held the cap at its
        fresh-start value for the life of the process, because one build-heavy
        agent's burst priced every slot. The work signals in the sample say
        whether THIS increase is safe, and the spawn floor's memory reserve is
        what queues a cold start the host cannot absorb yet.
        """
        return self._p.exec_ceiling

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

    # -- the spawn-gate track ------------------------------------------------

    def _gate_step(self, sample: Sample, report: PressureReport) -> tuple[str, str]:
        """One sample on the spawn gate: its own signals, never the gateway's loop lag."""
        p = self._p
        now = sample.t
        if self._paused:
            return self._gate_while_paused(sample, report)
        if self._severe_streak >= p.thresholds.severe_samples:
            return self._gate_pause(sample, f"severe pressure for {self._severe_streak} samples")
        if report.gate_corroborated:
            if now - self._last_gate_decrease_at < p.decrease_cooldown_secs:
                return ACTION_HOLD, "pressure inside the decrease cooldown"
            new_gate = _decrease_target(self._gate_cap, 0, p.gate_floor, p.decrease_factor)
            if new_gate == self._gate_cap:
                return ACTION_HOLD, "pressure at the floor; nothing left to cut"
            self._gate_cap = new_gate
            self._last_gate_decrease_at = now
            self._gate_success_base = sample.spawn_gate.successes
            return ACTION_DECREASE, "spawn gate: corroborated pressure: " + ",".join(
                sorted(report.gate_signals)
            )
        if report.gate_signals:
            return ACTION_HOLD, "single uncorroborated signal"
        return self._gate_maybe_increase(sample, report)

    def _gate_maybe_increase(self, sample: Sample, report: PressureReport) -> tuple[str, str]:
        p = self._p
        now = sample.t
        if not report.gate_clear_for_increase:
            return ACTION_HOLD, "clear but inside the hysteresis band"
        window = p.slow_start_clean_secs if self._slow_start else p.increase_clean_secs
        if now - self._last_gate_pressure_at < window:
            return ACTION_HOLD, "clear; waiting out the clean window"
        if now - self._last_gate_increase_at < window:
            return ACTION_HOLD, "clear; one increase per window"
        gate = sample.spawn_gate
        gate_successes = gate.successes - self._gate_success_base
        gate_demand = gate.queued > 0 or gate.in_flight >= self._gate_cap
        if (
            self._gate_cap < p.gate_ceiling
            and gate_successes >= self._required_gate_successes()
            and gate_demand
        ):
            before = self._gate_cap
            self._gate_cap = self._step_up(self._gate_cap, p.gate_ceiling)
            self._gate_success_base = gate.successes
            self._last_gate_increase_at = now
            # The gate figure is the policy's TARGET: the daemon confirms (or
            # clamps) it on apply, and ``recent_decisions`` carries what it
            # confirmed.
            return (
                ACTION_INCREASE,
                f"spawn gate target {before} -> {self._gate_cap} on backend inits",
            )
        quiet = now - max(self._last_gate_pressure_at, self._last_gate_decrease_at)
        if self._gate_cap < p.gate_start and quiet >= p.idle_recovery_secs:
            before = self._gate_cap
            self._gate_cap = before + 1
            self._gate_success_base = gate.successes
            self._last_gate_increase_at = now
            return ACTION_INCREASE, (
                f"spawn gate {before} -> {self._gate_cap}: none of its signals for "
                f"{quiet:.0f}s, toward its fresh-start {p.gate_start}"
            )
        return ACTION_HOLD, "clear; spawn gate holds"

    def _gate_pause(self, sample: Sample, why: str) -> tuple[str, str]:
        self._paused = True
        self._probing = False
        self._retire_slow_start()
        old_gate = self._gate_cap
        self._gate_cap = self._p.gate_floor
        self._last_gate_decrease_at = sample.t
        if self._gate_cap != old_gate:
            self._gate_success_base = sample.spawn_gate.successes
        return ACTION_PAUSE, f"spawn gate paused: {why}"

    def _gate_while_paused(self, sample: Sample, report: PressureReport) -> tuple[str, str]:
        if report.gate_severe:
            if self._probing:
                self._probing = False
                return ACTION_PAUSE, "spawn gate probe met severe pressure; re-paused"
            return ACTION_HOLD, "spawn gate paused: severe pressure persists"
        if not self._probing:
            self._probing = True
            self._probe_base = sample.completions
            return ACTION_PROBE, "severe pressure cleared; spawn gate probing"
        if report.gate_corroborated:
            self._probing = False
            self._last_gate_decrease_at = sample.t
            return ACTION_PAUSE, "spawn gate probe met corroborated pressure; re-paused"
        base = self._probe_base if self._probe_base is not None else sample.completions
        probe_done = sample.completions > base and not report.gate_signals
        # ``sample.completions`` carries both manager and runner-lane
        # completions, so a probe satisfied by lane work resumes here. The idle
        # path only backstops a probe where nothing ran at all; idle is no
        # evidence about backend inits, so it resumes the gate at its floor.
        idle_secs = self._idle_for(sample, since_pressure=self._last_gate_pressure_at)
        idle_done = (
            not probe_done
            and report.gate_clear_for_increase
            and idle_secs >= self._p.idle_recovery_secs
        )
        if not (probe_done or idle_done):
            return ACTION_HOLD, "spawn gate probe in flight"
        self._paused = False
        self._probing = False
        self._probe_base = None
        self._last_gate_increase_at = sample.t
        if idle_done:
            return ACTION_RESUME, (
                f"idle and clear for {idle_secs:.0f}s with no probe result; "
                "spawn gate resumes at its floor"
            )
        old_gate = self._gate_cap
        self._gate_cap = _clamp(self._p.gate_floor + 1, self._p.gate_floor, self._p.gate_ceiling)
        if self._gate_cap != old_gate:
            self._gate_success_base = sample.spawn_gate.successes
        return ACTION_RESUME, "probe completed; spawn gate resumes at floor + 1"

    def _required_gate_successes(self) -> int:
        """The gate's bar, which is NOT scaled to its cap and NOT eased by slow start.

        Backend inits land far faster than subagent runs finish, so
        ``increase_successes`` was never the barrier on this track -- and the
        restart-rebase contract (a respawned daemon owes the whole bar again on
        its fresh counter) is pinned against that number.

        Slow start deliberately does not reach here. Had it applied, a single
        backend init inside one clean window would double the gate and hand a
        respawned daemon its capacity back for one success -- a bar of 1, in
        the one place the contract above says the bar must be the full number.
        """
        return max(1, self._p.increase_successes)

    def _step_up(self, cap: int, ceiling: int) -> int:
        """The next cap after an earned increase, bounded by *ceiling*."""
        if self._slow_start:
            return min(ceiling, max(cap + 1, cap * self._p.slow_start_factor))
        return min(ceiling, cap + 1)

    # -- helpers -------------------------------------------------------------

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
    ``resource_critical_gb`` pair the advisory surfaces use (they shape the
    spawn gate only). ``agent.adaptive_initial`` is not read: the execution cap
    starts at its ceiling (the key is deprecated and inert).
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

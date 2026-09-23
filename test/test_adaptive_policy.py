"""``AdaptivePolicy`` -- the pure AIMD state machine (RFC overload-resilience §5.2).

Every test drives the policy with hand-built samples carrying their own
timestamps, so the sequence of caps below is PRODUCED by the controller's
rules, never by calling a lower-cap API by hand. Pinned here:

* the 10 -> 6 -> 4 descent under injected concurrency timeouts, and the plain
  x0.5 descent when nothing in flight is known to be healthy;
* a single provider's 429s never lower the host cap;
* the decrease cooldown and the hysteresis band (a noisy series around the
  threshold produces no oscillation);
* slow recovery: +1 per clean window, never more;
* pause-and-probe under severe pressure;
* the fresh-start cap, the ceiling, and ``mode="fixed"``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from kiro_crew.adaptive.policy import (
    ACTION_DECREASE,
    ACTION_FIXED,
    ACTION_HOLD,
    ACTION_INCREASE,
    ACTION_PAUSE,
    ACTION_PROBE,
    ACTION_RESUME,
    MODE_FIXED,
    AdaptivePolicy,
    PolicyParams,
    params_from_config,
)
from kiro_crew.adaptive.signals import (
    SIGNAL_GATE_FAILURES,
    SIGNAL_LOOP_LAG,
    SIGNAL_MEMORY,
    SIGNAL_SLOW_KEYS,
    SIGNAL_TIMEOUTS,
    Sample,
    SpawnGateStats,
    Thresholds,
    classify,
)

pytestmark = pytest.mark.timeout(30)


@pytest.mark.parametrize("slow_start,window", [(True, 5), (False, 30)])
def test_fresh_progress_probes_one_slot_without_waiting_for_task_completion(slow_start, window):
    policy = AdaptivePolicy(PolicyParams(exec_initial=1, exec_ceiling=64, slow_start=slow_start))
    sample = Sample(t=0, running=1, queued=63, free_mem_mb=32768)
    policy.observe(sample)
    result = policy.observe(replace(sample, t=window, progressing=1))
    assert result.action == ACTION_INCREASE
    assert result.effective_exec_cap == 2
    assert result.spawn_gate_capacity == 4
    assert "probe" in result.reason
    # Unchanged activity cannot keep buying slots after the clean window.
    result = policy.observe(replace(sample, t=window * 2, running=2))
    assert result.effective_exec_cap == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"free_mem_mb": -1},
        {"free_mem_mb": 1024},
        {"per_provider_429": {"provider": 1}},
        {"progressing": 0},
        {"running": 0},
        {"queued": 0},
        {"loop_lag_ms": 1000},
    ],
)
def test_progress_probe_requires_measured_clear_capacity_and_waiting_work(overrides):
    policy = AdaptivePolicy(PolicyParams(exec_initial=1, exec_ceiling=64, slow_start=True))
    sample = Sample(t=0, running=1, queued=63, free_mem_mb=32768, progressing=1)
    policy.observe(sample)
    result = policy.observe(replace(sample, t=5, **overrides))
    assert result.effective_exec_cap == 1


def test_progress_during_pressure_cannot_buy_a_later_probe():
    policy = AdaptivePolicy(PolicyParams(exec_initial=1, exec_ceiling=64))
    sample = Sample(t=0, running=1, queued=63, free_mem_mb=32768)
    policy.observe(sample)
    policy.observe(replace(sample, t=5, progressing=1, loop_lag_ms=400))
    result = policy.observe(replace(sample, t=40))
    assert result.effective_exec_cap == 1
    result = policy.observe(replace(sample, t=45, progressing=1))
    assert result.effective_exec_cap == 2


TH = Thresholds()


def _params(**over: object) -> PolicyParams:
    """Congestion-avoidance params: SLOW START OFF unless a test asks for it.

    Every test below this line pins the steady-state AIMD rules -- the shaped
    descent, the cooldown, the hysteresis band, ``+1`` per clean window -- and
    those rules are unchanged. Slow start is a separate regime that only a
    process which has never met pressure is in, so it gets its own class
    (:class:`TestSlowStart`) rather than shifting every cap in these.
    ``params_from_config`` is where the shipped default (ON) is pinned.
    """
    base: dict[str, object] = dict(exec_ceiling=10, exec_initial=10, floor=1, slow_start=False)
    base.update(over)
    return PolicyParams(**base)  # type: ignore[arg-type]


def _sample(t: float, **over: object) -> Sample:
    """A clean, idle sample at time ``t`` unless ``over`` says otherwise."""
    base: dict[str, object] = dict(
        t=t,
        loop_lag_ms=10.0,
        free_mem_mb=16_000.0,
        running=0,
        queued=0,
        completions=0,
    )
    base.update(over)
    return Sample(**base)  # type: ignore[arg-type]


def _timeouts(t: float, *, running: int, timed_out: int, completions: int = 0) -> Sample:
    """``running`` starts in flight, ``timed_out`` of them attributable timeouts
    on two distinct MCP servers -- the SPEC's "10 concurrent starts wedge" shape."""
    return _sample(
        t,
        running=running,
        queued=20,
        healthy_in_flight=running - timed_out,
        attributable_timeout_rate=timed_out / max(1, running),
        slow_or_failing_keys=2,
        completions=completions,
    )


# --- fresh start ---------------------------------------------------------------


class TestFreshStart:
    def test_fresh_process_starts_at_min_user_max_and_initial(self) -> None:
        assert AdaptivePolicy(_params(exec_ceiling=32, exec_initial=4)).exec_cap == 4
        assert AdaptivePolicy(_params(exec_ceiling=3, exec_initial=4)).exec_cap == 3

    def test_gate_starts_at_its_own_initial(self) -> None:
        pol = AdaptivePolicy(_params(gate_initial=4, gate_floor=1, gate_ceiling=8))
        assert pol.gate_cap == 4

    def test_params_from_config_defaults(self) -> None:
        p = params_from_config(object(), exec_ceiling=12)
        assert (p.exec_initial, p.floor, p.exec_ceiling) == (4, 1, 12)
        assert (p.gate_initial, p.gate_floor, p.gate_ceiling) == (4, 1, 8)
        assert p.decrease_factor == 0.5
        assert p.increase_successes == 20
        assert p.thresholds.mem_critical_mb == 2048.0
        # Slow start ships ON: a fresh gateway climbs to what the host allows
        # in seconds instead of ~30 minutes of clean windows.
        assert p.slow_start is True
        assert (p.slow_start_factor, p.slow_start_successes) == (2, 1)
        assert p.slow_start_clean_secs == 5.0

    def test_slow_start_can_be_turned_off_in_config(self) -> None:
        class _Agent:
            adaptive_slow_start = False

        class _Cfg:
            agent = _Agent()

        assert params_from_config(_Cfg(), exec_ceiling=12).slow_start is False


# --- the shaped descent ----------------------------------------------------


class TestDescent:
    def test_ten_six_four_under_injected_concurrency_timeouts(self) -> None:
        """10 in flight, 4 time out -> 6; 6 in flight, 2 time out -> 4.

        The descent is what ``observe`` returns for the injected samples; the
        test never sets a cap. Cooldown is honoured between the two cuts.
        """
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=10))
        caps = [pol.exec_cap]

        d = pol.observe(_timeouts(0.0, running=10, timed_out=4))
        assert d.action == ACTION_DECREASE
        assert {SIGNAL_TIMEOUTS, SIGNAL_SLOW_KEYS} <= set(d.signals)
        caps.append(d.effective_exec_cap)

        # Inside the 30 s cooldown: pressure persists, nothing more is cut.
        d = pol.observe(_timeouts(10.0, running=6, timed_out=2))
        assert d.action == ACTION_HOLD
        assert d.effective_exec_cap == 6

        d = pol.observe(_timeouts(31.0, running=6, timed_out=2))
        assert d.action == ACTION_DECREASE
        caps.append(d.effective_exec_cap)

        assert caps == [10, 6, 4]

    def test_plain_halving_when_no_healthy_floor_is_known(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=8, exec_initial=8, gate_initial=8))
        t = 0.0
        seen = []
        for _ in range(5):
            d = pol.observe(_sample(t, loop_lag_ms=400.0, running=8, queued=4))
            seen.append(d.effective_exec_cap)
            t += 31.0
        assert seen == [4, 2, 1, 1, 1]
        # The gate halves on the same verdict, down to its own floor.
        assert pol.gate_cap == 1

    def test_descent_always_makes_progress_and_never_undercuts_healthy_work(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=10))
        # Loop lag with every one of the 10 starts still healthy: the healthy
        # bound would keep the cap at 10, but a decrease always cuts by >= 1.
        d = pol.observe(_sample(0.0, loop_lag_ms=300.0, running=10, healthy_in_flight=10))
        assert d.action == ACTION_DECREASE and d.effective_exec_cap == 9
        # Healthy work above the halving target is the target: 9 -> 7, not 5.
        d = pol.observe(_timeouts(31.0, running=9, timed_out=2))
        assert d.effective_exec_cap == 7

    def test_at_the_floor_pressure_holds(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=4, exec_initial=1, gate_initial=1))
        d = pol.observe(_sample(0.0, loop_lag_ms=500.0))
        assert d.action == ACTION_HOLD
        assert d.effective_exec_cap == 1
        assert "floor" in d.reason


# --- corroboration -------------------------------------------------------------


class TestCorroboration:
    def test_a_single_soft_signal_never_decreases(self) -> None:
        pol = AdaptivePolicy(_params())
        d = pol.observe(_sample(0.0, attributable_timeout_rate=0.5))
        assert d.action == ACTION_HOLD
        assert d.effective_exec_cap == 10
        d = pol.observe(_sample(31.0, slow_or_failing_keys=3))
        assert d.action == ACTION_HOLD
        assert d.effective_exec_cap == 10

    def test_one_slow_server_is_not_the_host(self) -> None:
        """One PoolKey failing plus a high timeout rate is that server's fault
        until a second key corroborates it."""
        pol = AdaptivePolicy(_params())
        d = pol.observe(_sample(0.0, attributable_timeout_rate=0.9, slow_or_failing_keys=1))
        assert d.action == ACTION_HOLD
        d = pol.observe(_sample(31.0, attributable_timeout_rate=0.9, slow_or_failing_keys=2))
        assert d.action == ACTION_DECREASE

    def test_loop_lag_and_memory_are_sufficient_alone(self) -> None:
        pol = AdaptivePolicy(_params())
        assert pol.observe(_sample(0.0, loop_lag_ms=250.0)).action == ACTION_DECREASE
        pol = AdaptivePolicy(_params())
        assert pol.observe(_sample(0.0, free_mem_mb=2048.0)).action == ACTION_DECREASE

    def test_single_provider_429_does_not_lower_the_cap(self) -> None:
        pol = AdaptivePolicy(_params())
        t = 0.0
        for _ in range(6):
            d = pol.observe(_sample(t, per_provider_429={"bedrock": 40}, running=10, queued=5))
            t += 31.0
            assert d.effective_exec_cap == 10
            assert d.spawn_gate_capacity == 4
            assert d.action == ACTION_HOLD
            # ...but the scope IS reported for the dependency coordinator.
            assert d.throttled_providers == ("bedrock",)

    def test_provider_429_is_not_a_signal_in_the_classifier(self) -> None:
        report = classify(_sample(0.0, per_provider_429={"openai": 3, "bedrock": 0}), TH)
        assert not report.any
        assert report.throttled_providers == frozenset({"openai"})


# --- cooldown + hysteresis -----------------------------------------------------


class TestCooldownAndHysteresis:
    def test_cooldown_blocks_a_second_cut_and_discards_successes(self) -> None:
        pol = AdaptivePolicy(_params(increase_successes=5))
        pol.observe(_sample(0.0, loop_lag_ms=300.0, completions=0))
        assert pol.exec_cap == 5
        # 29 s later, still lagging: a hold, not a cut.
        d = pol.observe(_sample(29.0, loop_lag_ms=300.0))
        assert d.action == ACTION_HOLD and pol.exec_cap == 5
        # Successes that landed before the next decrease do not count towards
        # an increase afterwards: after the cut the base is reset.
        pol.observe(_sample(31.0, loop_lag_ms=300.0, completions=50))
        assert pol.exec_cap == 3
        d = pol.observe(_sample(62.0, completions=50, running=3, queued=2))
        assert d.action == ACTION_HOLD  # 0 successes since the cut

    def test_a_gate_only_cut_keeps_the_exec_track_earned_successes(self) -> None:
        """Exec already at its floor, the gate cut by a lag sample: the exec
        completions counted so far survive the cut, so the next clean window
        buys exec its increase with NO further completion.

        This is the shape of a host that ran one subagent at a time for two
        days: every ``>= 250 ms`` loop-lag tick lowered the gate (or held it at
        its floor) and, with both tracks reset on every transition, wiped the
        exec completions earned since the last exec change. Serial completions
        take 15-30 minutes each here, so re-owing them after each tick meant
        the exec cap never climbed back from 1.

        Completions are held CONSTANT across the cut, which is what isolates
        the base: a surviving base leaves a positive delta and increases, while
        a base reset to the count at the cut leaves zero and holds. That makes
        the assertion independent of how large the increase bar is.
        """
        pol = AdaptivePolicy(
            _params(
                exec_ceiling=4,
                exec_initial=1,
                gate_initial=4,
                gate_floor=1,
                increase_successes=20,
            )
        )
        pol.observe(_sample(0.0))  # first sample fixes the clean-window baseline
        d = pol.observe(_sample(40.0, loop_lag_ms=300.0, completions=19, running=1, queued=5))
        assert d.action == ACTION_DECREASE
        assert (pol.exec_cap, pol.gate_cap) == (1, 2)  # exec at floor, gate cut
        # 31 s of clean samples later, with NO new completion: the 19 already
        # earned still count, because the cut moved the gate and not exec.
        d = pol.observe(_sample(71.0, completions=19, running=1, queued=5))
        assert d.action == ACTION_INCREASE, d
        assert pol.exec_cap == 2

    def test_an_exec_only_cut_keeps_the_gate_track_earned_inits(self) -> None:
        """The mirror image: gate at its floor, exec cut, the gate's inits survive."""
        pol = AdaptivePolicy(
            _params(
                exec_ceiling=8,
                exec_initial=4,
                gate_initial=1,
                gate_floor=1,
                gate_ceiling=8,
                increase_successes=20,
            )
        )
        pol.observe(_sample(0.0))
        busy = SpawnGateStats(capacity=1, in_flight=1, queued=2, successes=19)
        d = pol.observe(_sample(40.0, loop_lag_ms=300.0, spawn_gate=busy, running=4, queued=4))
        assert d.action == ACTION_DECREASE
        assert (pol.exec_cap, pol.gate_cap) == (2, 1)  # exec cut, gate at floor
        busy = SpawnGateStats(capacity=1, in_flight=1, queued=2, successes=20)
        d = pol.observe(_sample(71.0, spawn_gate=busy, running=0, queued=0))
        assert d.action == ACTION_INCREASE, d
        assert (pol.exec_cap, pol.gate_cap) == (2, 2)  # exec had no demand

    def test_the_cut_track_still_discards_its_own_successes(self) -> None:
        """Per-track means the moved track IS reset: exec cut, exec re-owes 20."""
        pol = AdaptivePolicy(_params(exec_ceiling=8, exec_initial=4, increase_successes=20))
        pol.observe(_sample(0.0))
        d = pol.observe(_sample(40.0, loop_lag_ms=300.0, completions=19, running=4, queued=4))
        assert d.action == ACTION_DECREASE and pol.exec_cap == 2
        d = pol.observe(_sample(71.0, completions=20, running=2, queued=4))
        assert d.action == ACTION_HOLD and pol.exec_cap == 2  # 1 success since the cut
        d = pol.observe(_sample(102.0, completions=39, running=2, queued=4))
        assert d.action == ACTION_INCREASE and pol.exec_cap == 3

    def test_noisy_series_around_the_threshold_does_not_oscillate(self) -> None:
        """Lag bouncing between 120 ms and 260 ms: one cut, then holds.

        The increase side needs < 100 ms AND 30 s without any signal, so the
        120 ms samples (above the increase line, below the decrease line)
        neither cut nor raise -- the hysteresis band absorbs the noise.
        """
        pol = AdaptivePolicy(_params(exec_ceiling=8, exec_initial=8))
        lags = [260.0, 120.0, 260.0, 120.0, 120.0, 260.0, 120.0, 260.0, 120.0, 120.0]
        caps = []
        t = 0.0
        for lag in lags:
            d = pol.observe(_sample(t, loop_lag_ms=lag, running=8, queued=8, completions=1000))
            caps.append(d.effective_exec_cap)
            t += 5.0
        assert caps[0] == 4
        # After the first cut inside the cooldown nothing moves; past the
        # cooldown the 260 ms samples cut again (that IS pressure), but never
        # is a cut followed by a raise within the series.
        assert all(b <= a for a, b in zip(caps, caps[1:])), caps

    def test_increase_needs_the_hysteresis_side_not_merely_no_signal(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=8, exec_initial=4, increase_successes=1))
        # 150 ms lag: below the decrease line, above the increase line.
        for i in range(10):
            d = pol.observe(
                _sample(float(i * 31), loop_lag_ms=150.0, running=4, queued=4, completions=i * 5)
            )
            assert d.effective_exec_cap == 4, d
        assert d.action == ACTION_HOLD
        # Memory between critical and pressure is likewise "not clear".
        d = pol.observe(_sample(400.0, free_mem_mb=3000.0, running=4, queued=4, completions=100))
        assert d.effective_exec_cap == 4


# --- slow recovery -------------------------------------------------------------


class TestSlowRecovery:
    def test_plus_one_per_clean_window_with_demand_and_successes(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=4, increase_successes=20))
        completions = 0
        caps = []
        t = 0.0
        # Plenty of successes every 5 s, demand always at the cap.
        for _ in range(40):
            completions += 30
            d = pol.observe(_sample(t, running=pol.exec_cap, queued=10, completions=completions))
            caps.append(d.effective_exec_cap)
            t += 5.0
        # One step per 30 s window: at 30, 60, 90 ... (first at t=30: 7th sample).
        assert caps[:7] == [4, 4, 4, 4, 4, 4, 5]
        increases = [i for i, (a, b) in enumerate(zip(caps, caps[1:])) if b > a]
        gaps = [b - a for a, b in zip(increases, increases[1:])]
        assert gaps and all(g >= 6 for g in gaps), (caps, increases)
        assert all(b - a <= 1 for a, b in zip(caps, caps[1:]))

    def test_no_demand_no_increase(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=4, increase_successes=1))
        for i in range(8):
            d = pol.observe(_sample(float(i * 31), running=2, queued=0, completions=i * 10))
        assert d.effective_exec_cap == 4

    def test_gate_earns_on_inits_and_its_own_demand(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=4, exec_initial=4, increase_successes=20))
        pol.observe(_sample(0.0))  # first sample fixes the clean-window baseline
        busy = SpawnGateStats(capacity=4, in_flight=4, queued=3, successes=25)
        d = pol.observe(_sample(31.0, spawn_gate=busy, running=0, queued=0))
        assert d.spawn_gate_capacity == 5
        assert d.effective_exec_cap == 4  # exec had no demand and is at ceiling
        idle = SpawnGateStats(capacity=5, in_flight=0, queued=0, successes=60)
        d = pol.observe(_sample(62.0, spawn_gate=idle))
        assert d.spawn_gate_capacity == 5


# --- slow start and the host cap -----------------------------------------------


class TestSlowStart:
    """The climb a process gets before it has ever met pressure.

    The asymmetry these pin is the one that made a 64 ceiling unreachable:
    a decrease HALVES on one corroborated sample, while the old increase was
    ``+1`` per 30 s gated on a flat 20 completions -- so crossing 4 -> 64 cost
    ~30 minutes of perfectly clean windows and ~1200 finished runs, and a cap
    cut to the floor needed 20 SERIAL runs to buy its first step back.
    """

    def _ss(self, **over: object) -> PolicyParams:
        base: dict[str, object] = dict(exec_ceiling=64, exec_initial=4, floor=1, slow_start=True)
        base.update(over)
        return PolicyParams(**base)  # type: ignore[arg-type]

    def _busy(self, t: float, cap: int, completions: int, **over: object) -> Sample:
        """Clear sample with real demand at *cap*."""
        return _sample(t, running=cap, queued=50, completions=completions, **over)

    def test_doubles_per_window_up_to_the_user_ceiling(self) -> None:
        pol = AdaptivePolicy(self._ss(exec_ceiling=16))
        completions = 0
        caps = []
        t = 0.0
        for _ in range(8):
            completions += 5
            caps.append(pol.observe(self._busy(t, pol.exec_cap, completions)).effective_exec_cap)
            t += 5.0
        # First sample fixes the clean-window baseline, then x2 per 5 s window,
        # and the user's ceiling is where it stops.
        assert caps == [4, 8, 16, 16, 16, 16, 16, 16], caps

    def test_no_static_host_prediction_sits_under_the_user_ceiling(self) -> None:
        """The ceiling is the user's number; the host is judged live, not guessed.

        A p90-peak memory/CPU prediction clamping the climb pins a 32-core host
        with tens of GB free at its fresh-start cap. Only the live pressure
        signals in the sample decide whether an increase is safe, and a clear
        host with demand climbs all the way to the configured ceiling.
        """
        pol = AdaptivePolicy(self._ss(exec_ceiling=64))
        completions = 0
        t = 0.0
        for _ in range(8):
            completions += 5
            d = pol.observe(self._busy(t, pol.exec_cap, completions))
            t += 5.0
        assert d.effective_exec_cap == 64
        assert "host_cap" not in pol.snapshot()

    def test_memory_under_the_pressure_line_withholds_growth_and_cuts_nothing(self) -> None:
        """Free memory is the live brake on growth, and a brake is never a cut.

        Nothing is ever killed, so lowering the cap under running work frees
        nothing; memory between the pressure and critical lines holds the cap
        where it is, and only the critical line (corroborated pressure) cuts.
        """
        pol = AdaptivePolicy(self._ss(exec_ceiling=64))
        for i in range(3):
            pol.observe(self._busy(float(i * 5), pol.exec_cap, (i + 1) * 5))
        assert pol.exec_cap == 16
        d = pol.observe(self._busy(15.0, 16, 100, free_mem_mb=3072.0))
        assert d.effective_exec_cap == 16
        assert d.action == ACTION_HOLD

    def test_one_corroborated_pressure_ends_slow_start_for_the_process(self) -> None:
        pol = AdaptivePolicy(self._ss())
        pol.observe(self._busy(0.0, 4, 5))
        d = pol.observe(self._busy(5.0, 4, 10))
        assert d.effective_exec_cap == 8 and pol.slow_start is True
        # 300 ms lag is corroborated on its own: halve, and leave slow start.
        pol.observe(self._busy(10.0, 8, 10, loop_lag_ms=300.0))
        assert pol.exec_cap == 4 and pol.slow_start is False
        # From here the climb is +1 per 30 s window, never x2 again.
        caps = []
        completions = 10
        t = 41.0
        for _ in range(4):
            completions += 30
            caps.append(pol.observe(self._busy(t, pol.exec_cap, completions)).effective_exec_cap)
            t += 31.0
        assert caps == [5, 6, 7, 8], caps

    def test_config_off_then_on_restores_slow_start_before_pressure(self) -> None:
        pol = AdaptivePolicy(self._ss())
        pol.update_params(replace(pol.params, slow_start=False))
        assert pol.slow_start is False
        pol.update_params(replace(pol.params, slow_start=True))
        assert pol.slow_start is True
        pol.observe(self._busy(0.0, 4, 1))
        d = pol.observe(self._busy(5.0, 4, 2))
        assert d.action == ACTION_INCREASE
        assert d.effective_exec_cap == 8

    def test_pressure_held_by_the_cooldown_retires_slow_start(self) -> None:
        pol = AdaptivePolicy(self._ss())
        pol.observe(self._busy(0.0, 4, 5, loop_lag_ms=300.0))  # cut, cooldown starts
        assert pol.slow_start is False
        pol.update_params(replace(pol.params, slow_start=False))
        pol.update_params(replace(pol.params, slow_start=True))
        assert pol.slow_start is False, "pressure already seen does not expire on a config edit"

    def test_severe_pressure_pause_retires_slow_start(self) -> None:
        pol = AdaptivePolicy(self._ss())
        for t in (0.0, 5.0):
            pol.observe(self._busy(t, 4, 0, loop_lag_ms=9000.0))
        assert pol.paused is True and pol.slow_start is False
        pol.update_params(replace(pol.params, slow_start=False))
        pol.update_params(replace(pol.params, slow_start=True))
        assert pol.slow_start is False, "a pause retires slow start for the process lifetime"

    def test_the_increase_bar_scales_with_the_cap_not_a_flat_twenty(self) -> None:
        """``min(increase_successes, cap)`` -- one wave of the CURRENT cap.

        At the floor the old flat 20 meant twenty runs each executed ALONE
        before a second slot was allowed, which is what kept a floored cap
        floored on a busy gateway.
        """
        pol = AdaptivePolicy(_params(exec_ceiling=64, exec_initial=4, increase_successes=20))
        pol.observe(_sample(0.0))
        d = pol.observe(_sample(31.0, running=4, queued=50, completions=4))
        assert d.effective_exec_cap == 5, d
        # The bar never exceeds increase_successes: at cap 20+ it is 20 again.
        pol = AdaptivePolicy(_params(exec_ceiling=64, exec_initial=30, increase_successes=20))
        pol.observe(_sample(0.0))
        assert pol.observe(_sample(31.0, running=30, queued=50, completions=19)).action == (
            ACTION_HOLD
        )
        assert pol.observe(_sample(62.0, running=30, queued=50, completions=20)).action == (
            ACTION_INCREASE
        )

    def test_slow_start_still_needs_demand_and_a_clear_sample(self) -> None:
        pol = AdaptivePolicy(self._ss())
        # Idle: no demand, no growth, however clean the host is.
        for i in range(6):
            d = pol.observe(_sample(float(i * 5), running=0, queued=0, completions=i * 5))
        assert d.effective_exec_cap == 4
        # In the hysteresis band (150 ms): clear of a cut, not clear for growth.
        for i in range(6):
            d = pol.observe(
                _sample(200.0 + i * 5, loop_lag_ms=150.0, running=4, queued=50, completions=100 + i)
            )
        assert d.effective_exec_cap == 4

    def test_snapshot_reports_the_regime(self) -> None:
        pol = AdaptivePolicy(self._ss())
        pol.observe(self._busy(0.0, 4, 1))
        snap = pol.snapshot()
        assert snap["slow_start"] is True
        assert snap["exec_ceiling"] == 64

    def test_slow_start_does_not_lower_the_spawn_gate_bar(self) -> None:
        """Slow start eases the EXECUTION bar only; the gate still owes 20.

        The gate's whole range is ``4 -> 8`` -- one doubling -- so easing its bar
        to a single success would hand a respawned daemon its full backend
        capacity back for one init, against the restart-rebase contract that
        pins the bar to ``increase_successes``.
        """
        pol = AdaptivePolicy(self._ss(increase_successes=20))
        gate = SpawnGateStats(capacity=4, in_flight=4, queued=10, successes=1)
        # Clean, gate demand present, one init landed: exec doubles, gate holds.
        d = pol.observe(self._busy(0.0, 4, 1, spawn_gate=gate))
        assert d.spawn_gate_capacity == 4
        d = pol.observe(self._busy(5.0, pol.exec_cap, 5, spawn_gate=gate))
        assert d.effective_exec_cap == 8  # the exec track DID move
        assert d.spawn_gate_capacity == 4
        # Strictly between the gate's own cap (4) and the bar (20): this is the
        # sample that separates the flat bar from a cap-scaled one. A
        # ``min(increase_successes, gate_cap)`` bar would open the gate here.
        mid = SpawnGateStats(capacity=4, in_flight=4, queued=10, successes=10)
        d = pol.observe(self._busy(10.0, pol.exec_cap, 20, spawn_gate=mid))
        assert d.spawn_gate_capacity == 4
        # Only the full bar opens it.
        paid = SpawnGateStats(capacity=4, in_flight=4, queued=10, successes=20)
        d = pol.observe(self._busy(15.0, pol.exec_cap, 50, spawn_gate=paid))
        assert d.spawn_gate_capacity == 8


# --- pause and probe -----------------------------------------------------------


class TestPauseAndProbe:
    def test_severe_pressure_pauses_then_probes_then_resumes(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=6))
        d1 = pol.observe(_sample(0.0, free_mem_mb=1000.0, running=6))
        assert d1.action == ACTION_DECREASE  # first severe sample: cut, not yet pause
        d2 = pol.observe(_sample(5.0, free_mem_mb=1000.0, running=6))
        assert d2.action == ACTION_PAUSE
        assert d2.paused and d2.effective_exec_cap == 0
        assert d2.spawn_gate_capacity == 1
        assert SIGNAL_MEMORY in d2.signals
        # Still severe: stays paused, no grants.
        d3 = pol.observe(_sample(10.0, free_mem_mb=900.0, running=6))
        assert d3.action == ACTION_HOLD and d3.effective_exec_cap == 0
        # Cleared: one probe.
        d4 = pol.observe(_sample(15.0, free_mem_mb=6000.0, running=0, completions=3))
        assert d4.action == ACTION_PROBE
        assert d4.effective_exec_cap == 1 and d4.probing and d4.paused
        # Probe running, not yet done: hold.
        d5 = pol.observe(_sample(20.0, free_mem_mb=6000.0, running=1, completions=3))
        assert d5.action == ACTION_HOLD and d5.effective_exec_cap == 1
        # Probe completed without pressure: resume at floor + 1.
        d6 = pol.observe(_sample(25.0, free_mem_mb=6000.0, running=0, completions=4))
        assert d6.action == ACTION_RESUME
        assert not d6.paused and d6.effective_exec_cap == 2
        assert d6.spawn_gate_capacity == 2

    def test_probe_that_meets_pressure_re_pauses(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=6))
        pol.observe(_sample(0.0, loop_lag_ms=2500.0))
        pol.observe(_sample(5.0, loop_lag_ms=2500.0))
        assert pol.paused
        d = pol.observe(_sample(10.0, loop_lag_ms=50.0))
        assert d.action == ACTION_PROBE
        d = pol.observe(_sample(15.0, loop_lag_ms=300.0))
        assert d.action == ACTION_PAUSE and d.effective_exec_cap == 0

    def test_one_severe_sample_is_not_a_pause(self) -> None:
        pol = AdaptivePolicy(_params())
        d = pol.observe(_sample(0.0, loop_lag_ms=3000.0))
        assert d.action == ACTION_DECREASE and not d.paused
        d = pol.observe(_sample(5.0, loop_lag_ms=20.0))
        assert not d.paused


# --- ceiling + fixed -----------------------------------------------------------


class TestCeilingAndFixed:
    def test_ceiling_is_never_exceeded(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=5, exec_initial=4, increase_successes=1))
        for i in range(20):
            d = pol.observe(_sample(float(i * 31), running=5, queued=9, completions=i * 10))
            assert d.effective_exec_cap <= 5
        assert d.effective_exec_cap == 5
        assert d.action == ACTION_HOLD

    def test_lowered_ceiling_clamps_the_live_cap(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=8))
        from dataclasses import replace

        pol.update_params(replace(pol.params, exec_ceiling=3))
        assert pol.exec_cap == 3
        d = pol.observe(_sample(0.0))
        assert d.effective_exec_cap == 3

    def test_fixed_mode_disables_adaptation(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=4, mode=MODE_FIXED))
        for t, lag in ((0.0, 5000.0), (5.0, 5000.0), (10.0, 5000.0), (41.0, 10.0)):
            d = pol.observe(_sample(t, loop_lag_ms=lag, running=4, queued=9, completions=1000))
            assert d.action == ACTION_FIXED
            assert d.effective_exec_cap == 4
            assert d.spawn_gate_capacity == 4
            assert not d.paused

    def test_fixed_mode_from_config_string(self) -> None:
        class _Agent:
            adaptive_concurrency_mode = "fixed"

        class _Cfg:
            agent = _Agent()

        p = params_from_config(_Cfg(), exec_ceiling=6)
        assert p.mode == MODE_FIXED
        assert AdaptivePolicy(p).observe(_sample(0.0, loop_lag_ms=9000.0)).effective_exec_cap == 4

    def test_invalid_params_are_refused(self) -> None:
        with pytest.raises(ValueError):
            PolicyParams(exec_ceiling=0)
        with pytest.raises(ValueError):
            PolicyParams(exec_ceiling=4, mode="random")
        with pytest.raises(ValueError):
            PolicyParams(exec_ceiling=4, decrease_factor=1.0)


# --- decision bookkeeping ------------------------------------------------------


class TestDecisionShape:
    def test_changed_tracks_caps_and_pause(self) -> None:
        pol = AdaptivePolicy(_params())
        first = pol.observe(_sample(0.0))
        assert first.changed  # first decision always reports its caps
        second = pol.observe(_sample(5.0))
        assert not second.changed
        cut = pol.observe(_sample(10.0, loop_lag_ms=300.0))
        assert cut.changed and SIGNAL_LOOP_LAG in cut.signals

    def test_snapshot_carries_the_state_resource_status_renders(self) -> None:
        pol = AdaptivePolicy(_params(exec_ceiling=10, exec_initial=4))
        pol.observe(_sample(0.0))
        snap = pol.snapshot()
        assert snap["effective_exec_cap"] == 4
        assert snap["exec_ceiling"] == 10
        assert snap["spawn_gate_capacity"] == 4
        assert snap["paused"] is False
        assert snap["last"]["action"] == ACTION_HOLD


# ── D1 (overload experiment): gate failures are a WINDOW count ───────────────


class TestGateFailureWindow:
    def test_lifetime_failures_before_the_first_sample_are_not_pressure(self):
        pol = AdaptivePolicy(PolicyParams(exec_ceiling=10, exec_initial=4))
        gate = SpawnGateStats(capacity=4, in_flight=4, queued=1, successes=0, failures=7)
        d = pol.observe(_sample(0.0, spawn_gate=gate))
        assert SIGNAL_GATE_FAILURES not in d.signals

    def test_failures_inside_the_window_fire_and_then_age_out(self):
        th = Thresholds(gate_failures=2, gate_failure_window_secs=60.0)
        pol = AdaptivePolicy(PolicyParams(exec_ceiling=10, exec_initial=4, thresholds=th))
        pol.observe(_sample(0.0, spawn_gate=SpawnGateStats(failures=0)))
        d = pol.observe(_sample(5.0, spawn_gate=SpawnGateStats(failures=2)))
        assert SIGNAL_GATE_FAILURES in d.signals
        # The counter stays at 2 (lifetime) but nothing new failed: after one
        # window the signal is gone.
        t = 5.0
        fired = []
        while t < 130.0:
            t += 5.0
            d = pol.observe(_sample(t, spawn_gate=SpawnGateStats(failures=2)))
            fired.append(SIGNAL_GATE_FAILURES in d.signals)
        assert fired[-1] is False
        assert any(fired[:6])  # still on right after the failures landed

    def test_daemon_restart_resets_the_baseline(self):
        pol = AdaptivePolicy(PolicyParams(exec_ceiling=10, exec_initial=4))
        pol.observe(_sample(0.0, spawn_gate=SpawnGateStats(failures=9)))
        # The daemon restarted: the counter went DOWN, then two fresh failures.
        pol.observe(_sample(5.0, spawn_gate=SpawnGateStats(failures=0)))
        d = pol.observe(_sample(10.0, spawn_gate=SpawnGateStats(failures=2)))
        assert SIGNAL_GATE_FAILURES in d.signals


# ── the SAME drop on the gate's success counter ───────────────────────────────


class TestGateSuccessBaseAfterRestart:
    """``spawn_gate.successes`` is the daemon's LIFETIME counter too, so it
    restarts at zero under a live policy exactly as ``failures`` does. The drop
    must cost the gate cap nothing beyond the ``increase_successes`` the fresh
    daemon owes: the base is rebased onto the counter it can actually see."""

    STEP = 10.0
    PER_SAMPLE = 2

    def _busy(self, t: float, successes: int, cap: int) -> Sample:
        """Clean sample, gate demand at the cap, exec pinned at its ceiling."""
        return _sample(
            t, spawn_gate=SpawnGateStats(capacity=cap, in_flight=cap, queued=2, successes=successes)
        )

    def _policy(self) -> AdaptivePolicy:
        return AdaptivePolicy(
            _params(
                exec_ceiling=4,
                exec_initial=4,
                gate_initial=4,
                gate_ceiling=8,
                increase_successes=20,
            )
        )

    def _earn_one_increase(self, pol: AdaptivePolicy) -> tuple[float, int]:
        """Drive rising lifetime successes until the gate cap earns its +1."""
        t = 0.0
        successes = 0
        while pol.gate_cap == 4 and t < 4_000.0:
            successes += self.PER_SAMPLE
            pol.observe(self._busy(t, successes, pol.gate_cap))
            t += self.STEP
        assert pol.gate_cap == 5, (pol.gate_cap, successes)
        return t, successes

    def test_a_restart_costs_the_cap_only_the_fresh_successes(self) -> None:
        pol = self._policy()
        t, earned_at = self._earn_one_increase(pol)
        assert earned_at == 20  # the base the drop makes stale
        # The daemon respawned: its counter starts over at zero and climbs again.
        successes = 0
        samples = 0
        while pol.gate_cap == 5 and samples < 400:
            pol.observe(self._busy(t, successes, pol.gate_cap))
            successes += self.PER_SAMPLE
            t += self.STEP
            samples += 1
        # 20 fresh inits at 2 per sample, plus the sample the drop landed on.
        # Against a stale base of 20 it would be 21: the fresh counter would
        # have to re-pass the vanished daemon's total first.
        assert samples == 11, samples
        assert pol.gate_cap == 6

    def test_a_failed_stats_read_is_not_a_restart(self) -> None:
        pol = self._policy()
        t, base = self._earn_one_increase(pol)
        # ``GatewayManager.stats()`` timed out, so ``gate_snap = {}`` reaches the
        # policy as the all-zero default -- a shape no live daemon reports.
        pol.observe(_sample(t, spawn_gate=SpawnGateStats()))
        # The SAME daemon answers again with base + 5 lifetime inits. Rebasing
        # onto the silence would read those 5 as 25 and buy an unearned +1.
        d = pol.observe(self._busy(t + 40.0, base + 5, pol.gate_cap))
        assert d.spawn_gate_capacity == 5, "silence must not buy the gate cap a +1"

    def test_the_fresh_counter_still_owes_the_full_increase_successes(self) -> None:
        pol = self._policy()
        t, _earned_at = self._earn_one_increase(pol)
        # The respawned daemon already logged 5 inits by the time we look, so
        # the base is 5 and not 0: rebasing is onto what the counter reads.
        pol.observe(self._busy(t, 5, pol.gate_cap))
        d = pol.observe(self._busy(t + 40.0, 24, pol.gate_cap))
        assert d.spawn_gate_capacity == 5  # 19 fresh inits is not 20
        d = pol.observe(self._busy(t + 80.0, 25, pol.gate_cap))
        assert d.spawn_gate_capacity == 6

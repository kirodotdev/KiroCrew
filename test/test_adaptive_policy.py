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

import pytest

from kiro_crew.adaptive.policy import (
    ACTION_DECREASE,
    ACTION_FIXED,
    ACTION_HOLD,
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

TH = Thresholds()


def _params(**over: object) -> PolicyParams:
    base: dict[str, object] = dict(exec_ceiling=10, exec_initial=10, floor=1)
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

    def test_single_provider_429_does_not_lower_the_host_cap(self) -> None:
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

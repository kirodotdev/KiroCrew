"""Advisory health telemetry: the host/scheduling signals ``resource_status`` renders.

The classifier (:func:`classify`) owns the AIMD cap/gate decision and is left
untouched here. This lane adds :func:`health_telemetry`, a pure read of one
:class:`Sample` into a bounded :class:`HealthTelemetry` report that surfaces
active sessions, cron queue depth, per-core CPU pressure, loop lag, and sample
age as advisory signals, and names which of the audit's two hypotheses
(cron-collision, renderer) the sample corroborates.

Every assertion is driven by hand-built samples with explicit values and
timestamps; no sleeps, no wall-clock reads. The report is advisory telemetry:
it never reschedules a user job and never mutates the sample.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kiro_crew.adaptive.signals import (
    HEALTH_ACTIVE_SESSIONS,
    HEALTH_CPU,
    HEALTH_CRON_QUEUE,
    HEALTH_LOOP_LAG,
    HEALTH_SAMPLE_STALE,
    HEALTH_SIGNALS,
    HealthTelemetry,
    HealthThresholds,
    Sample,
    health_telemetry,
)


def _idle_sample(**over: object) -> Sample:
    base = dict(
        t=1_000.0,
        loop_lag_ms=5.0,
        rss_mb=800.0,
        active_sessions=1,
        cron_queue_depth=0,
        cpu_pressure=0.1,
        observed_at=1_000.0,
    )
    base.update(over)
    return Sample(**base)  # type: ignore[arg-type]


def test_quiet_host_fires_no_health_signals():
    th = HealthThresholds()
    report = health_telemetry(_idle_sample(), th)
    assert report.signals == frozenset()
    assert report.stale is False
    assert report.cron_collision_suspected is False
    assert report.renderer_suspected is False


def test_each_signal_fires_only_above_its_own_threshold():
    th = HealthThresholds(
        active_sessions=12,
        cron_queue_depth=4,
        cpu_pressure=0.85,
        loop_lag_ms=250.0,
    )
    assert health_telemetry(_idle_sample(active_sessions=12), th).signals == {
        HEALTH_ACTIVE_SESSIONS
    }
    assert health_telemetry(_idle_sample(cron_queue_depth=4), th).signals == {HEALTH_CRON_QUEUE}
    assert health_telemetry(_idle_sample(cpu_pressure=0.85), th).signals == {HEALTH_CPU}
    assert health_telemetry(_idle_sample(loop_lag_ms=250.0), th).signals == {HEALTH_LOOP_LAG}


def test_value_one_below_threshold_does_not_fire():
    th = HealthThresholds(cron_queue_depth=4, active_sessions=12)
    report = health_telemetry(_idle_sample(cron_queue_depth=3, active_sessions=11), th)
    assert HEALTH_CRON_QUEUE not in report.signals
    assert HEALTH_ACTIVE_SESSIONS not in report.signals


def test_unmeasured_fields_never_fire():
    th = HealthThresholds(active_sessions=1, cron_queue_depth=1, cpu_pressure=0.0)
    report = health_telemetry(
        Sample(
            t=1_000.0,
            observed_at=1_000.0,
            active_sessions=-1,
            cpu_pressure=-1.0,
            cron_queue_depth=-1,
        ),
        th,
    )
    assert report.signals == frozenset()


def test_per_core_pressure_fires_for_one_saturated_core_on_a_wide_host():
    th = HealthThresholds(cpu_pressure=0.9)
    one_core_of_32_as_machine_share = 1.0 / 32
    assert (
        HEALTH_CPU
        not in health_telemetry(
            _idle_sample(cpu_pressure=one_core_of_32_as_machine_share), th
        ).signals
    )
    assert HEALTH_CPU in health_telemetry(_idle_sample(cpu_pressure=1.0), th).signals


@pytest.mark.parametrize(
    "sample_secs,cutoff",
    [(1, 15.0), (5, 15.0), (15, 45.0), (60, 180.0), (300, 900.0)],
)
def test_stale_cutoff_follows_the_live_cadence_with_a_floor(sample_secs, cutoff):
    th = HealthThresholds.for_cadence(sample_secs)
    assert th.sample_max_age_secs == cutoff
    assert th.cpu_pressure == HealthThresholds().cpu_pressure


def test_a_slow_cadence_is_not_stale_between_its_own_ticks():
    th = HealthThresholds.for_cadence(60)
    between_ticks = health_telemetry(_idle_sample(t=1_100.0, observed_at=1_000.0), th)
    assert between_ticks.stale is False
    assert HEALTH_SAMPLE_STALE not in between_ticks.signals

    missed_ticks = health_telemetry(_idle_sample(t=1_200.0, observed_at=1_000.0), th)
    assert missed_ticks.stale is True
    assert HEALTH_SAMPLE_STALE in missed_ticks.signals


def test_sample_age_marks_stale_but_is_not_a_pressure_signal():
    th = HealthThresholds(sample_max_age_secs=10.0)
    fresh = health_telemetry(_idle_sample(t=1_050.0, observed_at=1_045.0), th)
    assert fresh.stale is False
    assert HEALTH_SAMPLE_STALE not in fresh.signals

    stale = health_telemetry(_idle_sample(t=1_050.0, observed_at=1_030.0), th)
    assert stale.stale is True
    assert HEALTH_SAMPLE_STALE in stale.signals
    assert stale.age_secs == pytest.approx(20.0)

    retained = health_telemetry(_idle_sample(t=1_050.0, observed_at=1_045.0), th, now=1_080.0)
    assert retained.stale is True
    assert retained.age_secs == pytest.approx(35.0)
    assert HEALTH_SAMPLE_STALE in retained.signals


def test_stale_sample_suppresses_hypothesis_evidence():
    th = HealthThresholds(
        sample_max_age_secs=10.0,
        cron_queue_depth=4,
        active_sessions=12,
        cpu_pressure=0.85,
    )
    sample = _idle_sample(
        t=1_050.0,
        observed_at=1_000.0,
        cron_queue_depth=10,
        active_sessions=20,
        cpu_pressure=0.95,
    )
    report = health_telemetry(sample, th)
    assert report.stale is True
    assert report.cron_collision_suspected is False
    assert report.renderer_suspected is False


def test_cron_collision_hypothesis_needs_queue_and_contention():
    th = HealthThresholds(
        cron_queue_depth=4, cpu_pressure=0.85, loop_lag_ms=250.0, sample_max_age_secs=60.0
    )
    queue_only = health_telemetry(_idle_sample(cron_queue_depth=10, cpu_pressure=0.1), th)
    assert queue_only.cron_collision_suspected is False

    both = health_telemetry(_idle_sample(cron_queue_depth=10, cpu_pressure=0.95), th)
    assert both.cron_collision_suspected is True

    lag_variant = health_telemetry(_idle_sample(cron_queue_depth=10, loop_lag_ms=400.0), th)
    assert lag_variant.cron_collision_suspected is True


def test_renderer_hypothesis_needs_sessions_and_loop_lag():
    th = HealthThresholds(active_sessions=12, loop_lag_ms=250.0, sample_max_age_secs=60.0)
    sessions_only = health_telemetry(_idle_sample(active_sessions=20, loop_lag_ms=5.0), th)
    assert sessions_only.renderer_suspected is False

    both = health_telemetry(_idle_sample(active_sessions=20, loop_lag_ms=400.0), th)
    assert both.renderer_suspected is True


def test_report_is_frozen_and_sample_untouched():
    th = HealthThresholds()
    sample = _idle_sample(cron_queue_depth=10, cpu_pressure=0.95)
    before = (sample.cron_queue_depth, sample.cpu_pressure, sample.active_sessions)
    report = health_telemetry(sample, th)
    assert (sample.cron_queue_depth, sample.cpu_pressure, sample.active_sessions) == before
    assert isinstance(report, HealthTelemetry)
    with pytest.raises(FrozenInstanceError):
        report.signals = frozenset()  # type: ignore[misc]


def test_signals_are_from_the_closed_health_set():
    th = HealthThresholds(
        active_sessions=1,
        cron_queue_depth=1,
        cpu_pressure=0.5,
        loop_lag_ms=1.0,
        sample_max_age_secs=10.0,
    )
    report = health_telemetry(
        _idle_sample(
            t=2_000.0,
            observed_at=1_000.0,
            active_sessions=50,
            cron_queue_depth=20,
            cpu_pressure=0.99,
            loop_lag_ms=5000.0,
        ),
        th,
    )
    assert report.signals == HEALTH_SIGNALS


class _Manager:
    user_max_concurrent = 8
    running_count = 2
    _queue: list = []
    _agents: dict = {}

    def set_effective_cap(self, cap):
        return 8 if cap is None else cap


def _controller(now: list[float], **over):
    from kiro_crew.adaptive.controller import AdaptiveController
    from kiro_crew.config.loader import KiroCrewConfig

    kwargs = dict(
        cfg=KiroCrewConfig(),
        read_active_sessions=lambda: 20,
        read_cron_queue=lambda: 9,
        clock=lambda: now[0],
    )
    kwargs.update(over)
    return AdaptiveController(_Manager(), **kwargs)


def test_controller_populates_and_surfaces_live_health_fields() -> None:
    from kiro_crew.adaptive.controller import HostSample

    now = [1_000.0]
    controller = _controller(now)
    sample = controller.build_sample(
        loop_lag_ms=400.0,
        host=HostSample(free_mem_mb=32_000.0, rss_mb=7_000.0, observed_at=995.0),
        gate_snap={"queued": 18},
        budget_snap={},
        cpu_pressure=0.95,
    )

    assert sample.active_sessions == 20
    assert sample.spawn_gate.queued == 18
    assert sample.cron_queue_depth == 9
    assert sample.cpu_pressure == 0.95
    assert sample.age_secs == 5.0

    state = controller.state()
    health = state["health"]
    assert health["stale"] is False
    assert health["max_age_secs"] == 15.0
    assert health["cron_collision_suspected"] is True
    assert health["renderer_suspected"] is True
    assert set(health["signals"]) == {
        HEALTH_ACTIVE_SESSIONS,
        HEALTH_CRON_QUEUE,
        HEALTH_CPU,
        HEALTH_LOOP_LAG,
    }
    assert "thread_count" not in state["last_sample"]
    assert "mcp_queue_depth" not in state["last_sample"]

    now[0] = 1_020.0
    retained = controller.state()
    assert retained["health"]["age_secs"] == 25.0
    assert retained["last_sample"]["sample_age_secs"] == 25.0
    assert retained["health"]["stale"] is True
    assert retained["health"]["cron_collision_suspected"] is False
    assert retained["health"]["renderer_suspected"] is False


def test_controller_state_derives_the_stale_cutoff_from_its_cadence() -> None:
    from kiro_crew.adaptive.controller import HostSample
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent.controller_sample_secs = 60
    now = [1_000.0]
    controller = _controller(now, cfg=cfg)
    controller.build_sample(
        loop_lag_ms=5.0,
        host=HostSample(free_mem_mb=32_000.0, rss_mb=700.0, observed_at=1_000.0),
        gate_snap={},
        budget_snap={},
    )

    now[0] = 1_100.0
    fresh = controller.state()["health"]
    assert fresh["max_age_secs"] == 180.0
    assert fresh["age_secs"] == 100.0
    assert fresh["stale"] is False

    now[0] = 1_200.0
    assert controller.state()["health"]["stale"] is True


@pytest.mark.asyncio
async def test_tick_reports_cpu_pressure_per_core_not_machine_share(monkeypatch) -> None:
    from kiro_crew.adaptive import controller as ctl_mod
    from kiro_crew.adaptive.controller import HostSample
    from kiro_crew.metrics.events import PROCESS_CPU_UTILIZATION

    emitted: list[tuple[str, float]] = []
    monkeypatch.setattr(
        ctl_mod,
        "emit_histogram",
        lambda name, value, attrs, unit=None: emitted.append((name, value)),
    )
    monkeypatch.setattr(ctl_mod, "read_logical_cores", lambda: 32)
    now = [1_000.0]
    reading = {"cpu": 10.0}
    controller = _controller(
        now,
        host_probe=lambda: HostSample(
            free_mem_mb=32_000.0,
            rss_mb=300.0,
            cpu_seconds=reading["cpu"],
            cpu_clock=now[0],
            observed_at=now[0],
        ),
    )

    await controller.tick()
    assert controller.state()["last_sample"]["cpu_pressure"] == -1.0

    now[0] += 20.0
    reading["cpu"] = 30.0
    await controller.tick()

    share = [value for name, value in emitted if name == PROCESS_CPU_UTILIZATION]
    assert share == [pytest.approx(20.0 / (20.0 * 32))]
    state = controller.state()
    assert state["last_sample"]["cpu_pressure"] == pytest.approx(1.0, abs=1e-3)
    assert HEALTH_CPU in state["health"]["signals"]


def _rendered_state(**over) -> dict:
    state = {
        "enabled": True,
        "mode": "aimd",
        "effective_exec_cap": 6,
        "exec_ceiling": 8,
        "spawn_gate_capacity": 4,
        "gate_ceiling": 8,
        "health": {
            "signals": [],
            "stale": False,
            "age_secs": 3.0,
            "max_age_secs": 15.0,
            "cron_collision_suspected": False,
            "renderer_suspected": False,
        },
        "last_sample": {
            "loop_lag_ms": 5.0,
            "active_sessions": 3,
            "cron_queue_depth": 0,
            "cpu_pressure": 0.12,
        },
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(state.get(key), dict):
            state[key] = {**state[key], **value}
        else:
            state[key] = value
    return state


def test_resource_status_renders_quiet_health_readings() -> None:
    from kiro_crew import resource_status as rs

    lines = rs.adaptive_summary_lines(_rendered_state())
    health = [line for line in lines if line.strip().startswith("Health")]
    assert health == [
        "  Health (advisory, sample 3s old): sessions 3   cron queue 0   cpu/core 0.12   "
        "loop lag 5ms",
        "  Health signals: none; suspected: none",
    ]


def test_resource_status_renders_signals_and_suspects() -> None:
    from kiro_crew import resource_status as rs

    lines = rs.adaptive_summary_lines(
        _rendered_state(
            health={
                "signals": [HEALTH_CPU, HEALTH_CRON_QUEUE, HEALTH_LOOP_LAG, HEALTH_ACTIVE_SESSIONS],
                "cron_collision_suspected": True,
                "renderer_suspected": True,
            },
            last_sample={
                "loop_lag_ms": 412.4,
                "active_sessions": 20,
                "cron_queue_depth": 9,
                "cpu_pressure": 1.04,
            },
        )
    )
    joined = "\n".join(lines)
    assert "sessions 20   cron queue 9   cpu/core 1.04   loop lag 412ms" in joined
    assert (
        "  Health signals: health_cpu,health_cron_queue,health_loop_lag,health_active_sessions; "
        "suspected: cron collision, renderer" in lines
    )


def test_resource_status_withholds_readings_from_a_stale_sample() -> None:
    from kiro_crew import resource_status as rs

    lines = rs.adaptive_summary_lines(
        _rendered_state(
            health={
                "signals": [HEALTH_SAMPLE_STALE],
                "stale": True,
                "age_secs": 190.0,
                "max_age_secs": 180.0,
            }
        )
    )
    health = [line for line in lines if line.strip().startswith("Health")]
    assert health == ["  Health (advisory): sample stale (190s old, limit 180s); readings withheld"]
    assert "sessions" not in "\n".join(health)


def test_resource_status_renders_unmeasured_readings_as_a_dash() -> None:
    from kiro_crew import resource_status as rs

    lines = rs.adaptive_summary_lines(
        _rendered_state(
            last_sample={"active_sessions": -1, "cron_queue_depth": -1, "cpu_pressure": -1.0}
        )
    )
    assert any("sessions -   cron queue -   cpu/core -   loop lag 5ms" in line for line in lines)


def test_resource_status_omits_health_lines_without_a_sample() -> None:
    from kiro_crew import resource_status as rs

    lines = rs.adaptive_summary_lines(_rendered_state(health=None, last_sample=None))
    assert not any(line.strip().startswith("Health") for line in lines)

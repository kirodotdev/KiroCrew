"""``AdaptiveController`` wiring: the policy's decisions reach the two actuators.

Fakes stand in for ``SubagentManager`` (``set_effective_cap`` seam) and for the
daemon (``set_spawn_capacity`` / ``stats``); an injected clock and host probe
make every cycle deterministic. Also covers the real ``SubagentManager`` seam
(``_max_concurrent`` = ``min(user cap, adaptive cap)``, ceiling never written),
the gatewayd ``set-spawn-capacity`` frame, ``GatewayManager.set_spawn_capacity``,
the ``resource_status`` rendering and the config keys.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import Clock

from kiro_crew.adaptive import controller as ctl_mod
from kiro_crew.adaptive.controller import (
    OUTCOME_ATTRIBUTABLE,
    OUTCOME_NON_CONGESTION,
    OUTCOME_SUCCESS,
    AdaptiveController,
    HostSample,
    classify_run_outcome,
)
from kiro_crew.adaptive.policy import ACTION_DECREASE, ACTION_PAUSE, MODE_FIXED
from kiro_crew.adaptive.signals import Sample
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_gateway.admission import SpawnGate

pytestmark = pytest.mark.timeout(30)


class FakeManager:
    """The ``ExecActuator`` surface plus the run table the controller diffs."""

    def __init__(self, user_max: int = 10) -> None:
        self._user = user_max
        self.running_count = 0
        self._queue: list[dict[str, Any]] = []
        self._agents: dict[str, Any] = {}
        self.calls: list[Optional[int]] = []
        self.effective: Optional[int] = None

    @property
    def user_max_concurrent(self) -> int:
        return self._user

    def set_effective_cap(self, cap: Optional[int]) -> int:
        self.calls.append(cap)
        self.effective = cap
        return self._user if cap is None else min(self._user, cap)


class FakeGate:
    def __init__(self, *, answer: bool = True) -> None:
        self.calls: list[int] = []
        self.answer = answer
        self.gate = SpawnGate(4, floor=1, ceiling=8)

    async def set_capacity(self, capacity: int) -> Optional[int]:
        self.calls.append(capacity)
        if not self.answer:
            return None
        return self.gate.set_capacity(capacity)

    async def stats(self) -> dict[str, Any]:
        return {
            "type": "stats",
            "admission": {
                "spawn_gate": self.gate.snapshot(),
                "host_budget": {"procs": 3, "max_procs": 40},
            },
        }


def _cfg(**agent_over: object) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    for k, v in agent_over.items():
        setattr(cfg.agent, k, v)
    return cfg


def _controller(
    manager: FakeManager,
    gate: Optional[FakeGate] = None,
    *,
    cfg: Optional[KiroCrewConfig] = None,
    host: Optional[HostSample] = None,
    clock: Optional[Clock] = None,
) -> tuple[AdaptiveController, Clock]:
    clock = clock or Clock()
    probe_value = host or HostSample(free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000)
    ctl = AdaptiveController(
        manager,  # type: ignore[arg-type]
        cfg=cfg or _cfg(),
        set_gate_capacity=gate.set_capacity if gate else None,
        read_gate_stats=gate.stats if gate else None,
        host_probe=lambda: probe_value,
        clock=clock,
        sleep=AsyncMock(),
    )
    return ctl, clock


def _clean(t: float, **over: object) -> Sample:
    base: dict[str, object] = dict(t=t, loop_lag_ms=5.0, free_mem_mb=16_000.0)
    base.update(over)
    return Sample(**base)  # type: ignore[arg-type]


# --- wiring to the two actuators ------------------------------------------------


class TestActuators:
    def test_fresh_start_applies_min_user_max_initial_synchronously(self) -> None:
        mgr = FakeManager(user_max=32)
        ctl, _ = _controller(mgr)
        assert mgr.calls == [4]
        mgr2 = FakeManager(user_max=3)
        _controller(mgr2)
        assert mgr2.calls == [3]

    @pytest.mark.asyncio
    async def test_decrease_reaches_apply_limits_seam_and_gate_set_capacity(self) -> None:
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate, cfg=_cfg(adaptive_initial=10))
        assert mgr.effective == 10
        await ctl.step(_clean(clock.t))  # first tick pushes the gate's initial
        assert gate.calls == [4]
        clock.advance(5)
        d = await ctl.step(_clean(clock.t, loop_lag_ms=400.0, running=10, healthy_in_flight=6))
        assert d.action == ACTION_DECREASE
        assert mgr.effective == 6
        assert gate.calls[-1] == 2 and gate.gate.capacity == 2

    @pytest.mark.asyncio
    async def test_shaped_descent_is_driven_end_to_end_by_the_controller(self) -> None:
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate, cfg=_cfg(adaptive_initial=10))
        seen = [mgr.effective]

        def wave(running: int, timed_out: int) -> Sample:
            return _clean(
                clock.t,
                running=running,
                queued=20,
                healthy_in_flight=running - timed_out,
                attributable_timeout_rate=timed_out / running,
                slow_or_failing_keys=2,
            )

        await ctl.step(wave(10, 4))
        seen.append(mgr.effective)
        clock.advance(31)
        await ctl.step(wave(6, 2))
        seen.append(mgr.effective)
        assert seen == [10, 6, 4]
        assert mgr.calls == [10, 6, 4]  # nothing set the caps but the controller

    @pytest.mark.asyncio
    async def test_gate_value_stays_pending_until_the_daemon_answers(self) -> None:
        mgr = FakeManager()
        gate = FakeGate(answer=False)
        ctl, clock = _controller(mgr, gate)
        await ctl.step(_clean(clock.t))
        assert gate.calls == [4]
        assert ctl.state()["gate_pending"] == 4
        gate.answer = True
        clock.advance(5)
        await ctl.step(_clean(clock.t))
        assert gate.calls == [4, 4]
        assert ctl.state()["gate_pending"] is None
        assert ctl.state()["applied_gate_cap"] == 4

    @pytest.mark.asyncio
    async def test_pause_sets_zero_grants_and_gate_floor(self) -> None:
        mgr = FakeManager()
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate)
        await ctl.step(_clean(clock.t, free_mem_mb=1000.0))
        clock.advance(5)
        d = await ctl.step(_clean(clock.t, free_mem_mb=1000.0))
        assert d.action == ACTION_PAUSE
        assert mgr.effective == 0
        assert gate.gate.capacity == 1

    @pytest.mark.asyncio
    async def test_ceiling_change_is_read_every_tick(self) -> None:
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, cfg=_cfg(adaptive_initial=8))
        assert mgr.effective == 8
        mgr._user = 3  # hot-reloaded agent.max_subagents
        d = await ctl.step(_clean(clock.t))
        assert d.effective_exec_cap == 3
        assert mgr.effective == 3

    @pytest.mark.asyncio
    async def test_disabled_removes_the_bound(self) -> None:
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate, cfg=_cfg(adaptive_concurrency=False))
        assert mgr.calls == [None]
        d = await ctl.step(_clean(clock.t, loop_lag_ms=5000.0))
        assert d.effective_exec_cap == 10 and not d.paused
        assert mgr.effective is None
        # Live re-enable: the earned position is applied again.
        ctl.apply_config(_cfg(adaptive_concurrency=True))
        assert mgr.effective == 4

    @pytest.mark.asyncio
    async def test_fixed_mode_pins_both_caps(self) -> None:
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate, cfg=_cfg(adaptive_concurrency_mode=MODE_FIXED))
        for _ in range(4):
            clock.advance(5)
            await ctl.step(_clean(clock.t, loop_lag_ms=5000.0, running=4, queued=9))
        assert mgr.effective == 4
        assert gate.gate.capacity == 4


# --- one full tick with the sampler --------------------------------------------


class TestTick:
    @pytest.mark.asyncio
    async def test_tick_reads_host_gate_and_manager_runs(self) -> None:
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate)
        mgr.running_count = 2
        mgr._queue = [{"task": "a"}, {"task": "b"}]
        mgr._agents = {
            "a1": SimpleNamespace(done=True, error="", stalled=False),
            "a2": SimpleNamespace(done=True, error="Timed out after 30 minutes", stalled=False),
            "a3": SimpleNamespace(done=False, error="", stalled=True),
        }
        d = await ctl.tick(loop_lag_ms=12.0)
        state = ctl.state()
        assert state["ticks"] == 1
        last = state["last_sample"]
        assert last["running"] == 2 and last["queued"] == 2
        assert last["free_mem_mb"] == 16_000.0
        assert last["loop_lag_ms"] == 12.0
        assert d.effective_exec_cap == 4
        sample = ctl._samples[-1]
        assert sample.completions == 1  # a1 succeeded
        assert sample.healthy_in_flight == 1  # 2 running, one stalled
        assert sample.proc_count == 3 and sample.proc_limit == 40
        assert sample.spawn_gate.capacity == 4
        # The same runs are not counted twice on the next tick.
        await ctl.tick()
        assert ctl._samples[-1].completions == 1

    @pytest.mark.asyncio
    async def test_hooks_feed_the_sample(self) -> None:
        mgr = FakeManager()
        ctl, clock = _controller(mgr)
        for key in ("srv-a", "srv-b"):
            ctl.record_start(45_000.0, ok=False, attributable_timeout=True, key=key)
        ctl.record_start(800.0, ok=True, key="srv-c")
        ctl.record_provider_throttle("bedrock")
        ctl.record_provider_throttle("bedrock")
        ctl.record_completion(ok=True)
        ctl.note_gate_outcome("failure")
        await ctl.tick()
        s = ctl._samples[-1]
        assert s.slow_or_failing_keys == 2
        assert s.per_provider_429 == {"bedrock": 2}
        assert s.completions == 1
        assert s.attributable_timeout_rate == pytest.approx(2 / 4)
        assert s.spawn_gate.failures == 1  # in-process seam, no daemon snapshot
        assert s.start_latency_p95_ms == 45_000.0

    @pytest.mark.asyncio
    async def test_provider_throttle_listener_is_called_for_area_l(self) -> None:
        mgr = FakeManager()
        seen: list[tuple[str, int]] = []
        ctl = AdaptiveController(
            mgr,  # type: ignore[arg-type]
            cfg=_cfg(),
            host_probe=lambda: HostSample(),
            clock=Clock(),
            sleep=AsyncMock(),
            on_provider_throttle=lambda scope, n: seen.append((scope, n)),
        )
        ctl.record_provider_throttle("openai")
        assert seen == [("openai", 1)]

    @pytest.mark.asyncio
    async def test_run_loop_measures_lag_and_survives_a_bad_tick(self) -> None:
        mgr = FakeManager()
        clock = Clock()
        sleeps: list[float] = []

        async def _sleep(secs: float) -> None:
            sleeps.append(secs)
            clock.advance(secs + 0.3)  # the timer fired 300 ms late
            if len(sleeps) >= 3:
                raise asyncio.CancelledError

        ctl = AdaptiveController(
            mgr,  # type: ignore[arg-type]
            cfg=_cfg(controller_sample_secs=5),
            host_probe=MagicMock(side_effect=[RuntimeError("boom"), HostSample()]),
            clock=clock,
            sleep=_sleep,
        )
        with pytest.raises(asyncio.CancelledError):
            await ctl.run()
        assert sleeps == [5.0, 5.0, 5.0]
        assert "RuntimeError" in ctl.state()["last_error"]
        # The second tick got through and recorded the lag.
        assert ctl._samples and ctl._samples[-1].loop_lag_ms == pytest.approx(300.0, abs=1.0)

    def test_state_lists_what_resource_status_renders(self) -> None:
        mgr = FakeManager()
        ctl, _ = _controller(mgr)
        state = ctl.state()
        for key in (
            "enabled",
            "mode",
            "effective_exec_cap",
            "exec_ceiling",
            "spawn_gate_capacity",
            "paused",
            "counts",
            "applied_exec_cap",
        ):
            assert key in state


class TestRunOutcomeClassifier:
    @pytest.mark.parametrize(
        "error,expected",
        [
            ("", OUTCOME_SUCCESS),
            ("Timed out after 30 minutes [turns=3]", OUTCOME_ATTRIBUTABLE),
            ("error: tool stall", OUTCOME_ATTRIBUTABLE),
            ("startup timeout", OUTCOME_ATTRIBUTABLE),
            ("cancelled", OUTCOME_NON_CONGESTION),
            ("turn_limit:100", OUTCOME_NON_CONGESTION),
            ("permission denied for tool x", OUTCOME_NON_CONGESTION),
            ("invalid params", OUTCOME_NON_CONGESTION),
            ("context length exceeded", OUTCOME_NON_CONGESTION),
        ],
    )
    def test_buckets(self, error: str, expected: str) -> None:
        assert classify_run_outcome(SimpleNamespace(error=error)) == expected


# --- the real SubagentManager seam ---------------------------------------------


def _real_manager(max_concurrent: int):
    from kiro_crew.subagent import SubagentManager

    mgr = SubagentManager(
        sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=max_concurrent
    )
    mgr._fire_event = AsyncMock()
    return mgr


class TestSubagentManagerSeam:
    def test_effective_cap_is_min_of_user_and_adaptive(self) -> None:
        mgr = _real_manager(10)
        assert mgr.max_concurrent == 10 and mgr.user_max_concurrent == 10
        assert mgr.set_effective_cap(4) == 4
        assert mgr.max_concurrent == 4 and mgr.user_max_concurrent == 10
        assert mgr.set_effective_cap(50) == 10  # ceiling never exceeded
        assert mgr.set_effective_cap(0) == 0  # paused: no grants
        should_queue, slot_free = mgr._admission._should_stagger_queue_impl(1e9)
        assert should_queue is True and slot_free is False
        assert mgr.set_effective_cap(None) == 10

    def test_apply_limits_moves_the_ceiling_not_the_adaptive_bound(self) -> None:
        mgr = _real_manager(10)
        mgr.set_effective_cap(4)
        fresh = KiroCrewConfig()
        fresh.agent.max_subagents = 8
        mgr.apply_limits(fresh)
        assert mgr.user_max_concurrent == 8
        assert mgr.max_concurrent == 4  # the adaptive bound still holds
        fresh.agent.max_subagents = 3
        mgr.apply_limits(fresh)
        assert mgr.max_concurrent == 3  # a ceiling below the bound clamps it

    def test_raising_the_effective_cap_pumps_the_queue(self) -> None:
        mgr = _real_manager(6)
        mgr.set_effective_cap(2)
        mgr._running_count = 2
        mgr._spawn_stagger_secs = 0.0
        mgr._last_spawn_ts = 0.0
        mgr.spawn = MagicMock(return_value=None)  # type: ignore[method-assign]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]
        mgr._queue.append({"task": "queued work", "parent_session_key": "p", "batch_id": ""})
        mgr.set_effective_cap(3)
        mgr.spawn.assert_called_once()
        assert mgr.spawn.call_args.kwargs["_from_queue"] is True

    @pytest.mark.asyncio
    async def test_reconfigure_never_shrinks_the_ceiling_to_the_adaptive_value(self) -> None:
        mgr = _real_manager(10)
        mgr.set_effective_cap(2)
        cfg = KiroCrewConfig()
        cfg.agent.max_subagents = 10
        await mgr.reconfigure(cfg)
        await mgr.reconfigure(cfg)  # sizing unchanged -> "keep the cap" path
        assert mgr.user_max_concurrent == 10
        assert mgr.max_concurrent == 2


# --- gatewayd frame + manager actuator -----------------------------------------


class TestGatewaydFrame:
    def test_set_spawn_capacity_frame_moves_the_gate(self) -> None:
        from kiro_crew.mcp_gateway import gatewayd
        from kiro_crew.mcp_gateway.admission import Admission
        from kiro_crew.mcp_gateway.host_budget import HostBudget, HostBudgetLimits

        gate = SpawnGate(4, floor=1, ceiling=8)
        adm = Admission(
            gate=gate,
            budget=HostBudget(HostBudgetLimits(max_procs=0, max_rss_mb=0, max_fds=0)),
            initialize_timeout_secs=10,
            spawn_queue_wait_secs=60,
        )
        reply = gatewayd._apply_set_spawn_capacity({"capacity": 2}, adm)
        assert reply["type"] == "spawn-capacity" and reply["capacity"] == 2
        assert gate.capacity == 2
        reply = gatewayd._apply_set_spawn_capacity({"capacity": 99}, adm)
        assert reply["capacity"] == 8  # clamped to the ceiling, reported honestly
        assert gatewayd._apply_set_spawn_capacity({"capacity": "x"}, adm)["type"] == (
            "spawn-capacity-rejected"
        )
        assert gatewayd._apply_set_spawn_capacity({"capacity": True}, adm)["type"] == (
            "spawn-capacity-rejected"
        )
        assert gatewayd._apply_set_spawn_capacity({"capacity": 2}, None)["type"] == (
            "spawn-capacity-rejected"
        )

    @pytest.mark.asyncio
    async def test_manager_set_spawn_capacity_uses_the_control_roundtrip(self) -> None:
        from kiro_crew.mcp_gateway.manager import GatewayManager

        mgr = GatewayManager.__new__(GatewayManager)
        mgr._control_roundtrip = AsyncMock(  # type: ignore[method-assign]
            return_value={"type": "spawn-capacity", "capacity": 3}
        )
        assert await mgr.set_spawn_capacity(3) == 3
        mgr._control_roundtrip.assert_awaited_once_with(
            {"type": "set-spawn-capacity", "capacity": 3}
        )
        mgr._control_roundtrip = AsyncMock(return_value=None)  # type: ignore[method-assign]
        assert await mgr.set_spawn_capacity(3) is None
        mgr._control_roundtrip = AsyncMock(  # type: ignore[method-assign]
            return_value={"type": "spawn-capacity-rejected", "reason": "x"}
        )
        assert await mgr.set_spawn_capacity(3) is None


# --- resource_status + config -------------------------------------------------


class TestVisibilityAndConfig:
    def test_resource_status_renders_the_controller_state(self, monkeypatch) -> None:
        from kiro_crew import resource_status as rs

        mgr = FakeManager(user_max=10)
        ctl, _ = _controller(mgr)
        monkeypatch.setattr(ctl_mod, "_current", ctl)
        try:
            lines = rs.adaptive_summary_lines()
            joined = "\n".join(lines)
            assert "Execution cap: 4/10" in joined
            assert "MCP spawn gate: 4/8" in joined
            assert "Dispatch: active" in joined
            assert rs.adaptive_state()["effective_exec_cap"] == 4
        finally:
            monkeypatch.setattr(ctl_mod, "_current", None)
        assert rs.adaptive_summary_lines() == []
        assert rs.adaptive_state() is None

    def test_registry_round_trip(self) -> None:
        mgr = FakeManager()
        ctl, _ = _controller(mgr)
        ctl_mod.register(ctl)
        try:
            assert ctl_mod.current() is ctl
            assert ctl_mod.current_state()["exec_ceiling"] == 10
        finally:
            ctl_mod.register(None)
        assert ctl_mod.current_state() is None

    def test_config_defaults_and_parse(self) -> None:
        cfg = KiroCrewConfig()
        a = cfg.agent
        assert a.adaptive_concurrency is True
        assert a.adaptive_concurrency_mode == "aimd"
        assert (a.adaptive_floor, a.adaptive_initial, a.controller_sample_secs) == (1, 4, 5)
        from kiro_crew.adaptive.policy import params_from_config

        policy = params_from_config(cfg, exec_ceiling=10)
        assert policy.decrease_factor == 0.5
        assert (policy.decrease_cooldown_secs, policy.increase_clean_secs) == (30.0, 30.0)
        assert policy.increase_successes == 20
        assert (policy.thresholds.lag_decrease_ms, policy.thresholds.lag_increase_ms) == (
            250.0,
            100.0,
        )
        assert policy.thresholds.lag_severe_ms == 2000.0
        assert policy.thresholds.timeout_rate == 0.2

    def test_config_parse_clamps(self, tmp_path, monkeypatch) -> None:
        import json

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "agent": {
                        "adaptive_concurrency": False,
                        "adaptive_concurrency_mode": "bogus",
                        "adaptive_floor": 0,
                        "adaptive_initial": 999,
                        "controller_sample_secs": 0,
                    }
                }
            ),
            encoding="utf-8",
        )
        cfg = KiroCrewConfig.load()
        assert cfg.agent.adaptive_concurrency is False
        assert cfg.agent.adaptive_concurrency_mode == "aimd"
        assert cfg.agent.adaptive_floor == 1
        assert cfg.agent.adaptive_initial == 64
        assert not hasattr(cfg.agent, "adaptive_decrease_factor")  # tuning belongs to policy
        assert cfg.agent.controller_sample_secs == 1  # clamped to 1..300

    def test_live_paths_are_agent_keys_the_schema_knows(self) -> None:
        from dataclasses import fields

        from kiro_crew.config.sections import AgentConfig

        known = {f.name for f in fields(AgentConfig)}
        for path in AdaptiveController.LIVE_CONFIG_PATHS:
            section, leaf = path.split(".", 1)
            assert section == "agent" and leaf in known, path


def test_public_adaptive_keys_are_operational_controls_only():
    from dataclasses import fields

    from kiro_crew.config.sections import AgentConfig

    names = {item.name for item in fields(AgentConfig)}
    assert {name for name in names if name.startswith("adaptive_")} == {
        "adaptive_concurrency",
        "adaptive_concurrency_mode",
        "adaptive_floor",
        "adaptive_initial",
    }
    assert "controller_sample_secs" in names
    assert "lane_weights" in names
    assert "system_lane_weight" not in names


@pytest.mark.asyncio
async def test_disabled_controller_never_starts_or_probes() -> None:
    probe = MagicMock(side_effect=AssertionError("disabled host probe"))
    stats = AsyncMock(side_effect=AssertionError("disabled gate probe"))
    gate = FakeGate()
    ctl = AdaptiveController(
        FakeManager(),
        cfg=_cfg(adaptive_concurrency=False),
        host_probe=probe,
        read_gate_stats=stats,
        set_gate_capacity=gate.set_capacity,
    )
    try:
        ctl.start()
        assert ctl._task is None
        decision = await ctl.tick()
        assert decision.reason == "adaptive concurrency disabled"
        probe.assert_not_called()
        stats.assert_not_awaited()
        assert not ctl.state()["last_sample"]
        ctl.apply_config(_cfg(adaptive_concurrency=True))
        ctl.apply_config(_cfg(adaptive_concurrency=False))
        await ctl.tick()
        assert gate.calls == [4]  # Disabling still restores the configured gate.
        probe.assert_not_called()
        stats.assert_not_awaited()
    finally:
        await ctl.stop()

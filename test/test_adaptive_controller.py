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
import logging
import time
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
from kiro_crew.adaptive.policy import (
    ACTION_DECREASE,
    ACTION_FIXED,
    ACTION_HOLD,
    ACTION_INCREASE,
    ACTION_PAUSE,
    ACTION_PROBE,
    ACTION_RESUME,
    MODE_FIXED,
    Decision,
)
from kiro_crew.adaptive.signals import Sample
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mcp_gateway.admission import SpawnGate
from kiro_crew.metrics.events import (
    LOOP_LAG_MS,
    PROCESS_CPU_UTILIZATION,
    PROCESS_RSS_SAMPLED,
)

pytestmark = pytest.mark.timeout(30)


@pytest.mark.asyncio
async def test_long_running_stream_can_recover_from_one_without_completion():
    manager = FakeManager(user_max=64)
    manager.running_count = 1
    manager._queue = [{} for _ in range(63)]
    info = SimpleNamespace(done=False, _first_stream_started=100.0, last_activity=101.0)
    manager._agents["long-run"] = info
    clock = Clock()
    ctl = AdaptiveController(
        manager,
        cfg=_cfg(adaptive_initial=1),
        clock=clock,
        host_probe=lambda: HostSample(free_mem_mb=32768),
    )
    await ctl.tick()
    clock.advance(5)
    await ctl.step(_clean(clock.t, running=1, queued=63, loop_lag_ms=400))
    assert manager.effective == 1
    clock.advance(31)
    info.last_activity = 102.0
    decision = await ctl.tick()
    assert decision.effective_exec_cap == 2
    assert ctl._evidence.completions_total == 0
    assert not info.done
    clock.advance(31)
    manager.running_count = 2
    assert (await ctl.tick()).effective_exec_cap == 2


@pytest.mark.parametrize("state", ["queued", "stalled", "_slot_released", "done", "unstarted"])
def test_inactive_or_unstarted_rows_cannot_supply_progress_evidence(state):
    manager = FakeManager(user_max=64)
    info = SimpleNamespace(done=False, _first_stream_started=100.0, last_activity=101.0)
    manager._agents["a"] = info
    ctl = AdaptiveController(manager, cfg=_cfg())
    assert ctl._ingest_manager_runs(0) == 0
    info.last_activity = 102.0
    if state == "unstarted":
        info._first_stream_started = None
    else:
        setattr(info, state, True)
    assert ctl._ingest_manager_runs(5) == 0


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
    async def test_the_climb_is_bounded_by_the_user_ceiling_alone(self, monkeypatch) -> None:
        """No static host prediction sits between the cap and ``max_subagents``.

        The probe reads memory, RSS and fds -- live signals the policy judges
        each tick -- and never a p90-peak "how many fit" figure: that figure
        pinned a 32-core host with tens of GB free at its fresh-start cap. A
        clear host with demand climbs to the user's ceiling, and the sizing
        helpers are not consulted on the way.
        """

        def _boom(*_a: object, **_kw: object) -> int:  # pragma: no cover - must never be called
            raise AssertionError("the sizing helper must not bound the climb")

        monkeypatch.setattr("kiro_crew.subagent.compute_max_subagents", _boom)
        host = HostSample(free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000)
        mgr = FakeManager(user_max=64)
        ctl, clock = _controller(mgr, host=host, cfg=_cfg(adaptive_initial=4))
        mgr.running_count = 4
        mgr._queue = [{"task": str(i)} for i in range(70)]
        await ctl.tick(loop_lag_ms=5.0)
        assert "host_cap" not in ctl.state()["last_sample"]
        assert ctl.policy._growth_ceiling(ctl._samples[-1]) == 64
        for i in range(6):
            clock.advance(5)
            mgr._agents[f"done-{i}"] = SimpleNamespace(done=True, error="", stalled=False)
            mgr.running_count = max(4, mgr.effective or 4)
            await ctl.tick(loop_lag_ms=5.0)
        assert ctl.state()["applied_exec_cap"] == 64, ctl.state()

    def test_probe_host_reads_only_live_signals(self, monkeypatch) -> None:
        """``probe_host`` is the ONE blocking read and it reads the host, not config.

        Config and the learned-cost store are sizing inputs, not live signals,
        so the worker thread must not touch either.
        """
        monkeypatch.setattr(
            KiroCrewConfig,
            "load",
            lambda *_a, **_kw: pytest.fail("probe_host must not load config"),
        )
        sample = ctl_mod.probe_host()
        assert set(vars(sample)) == {
            "free_mem_mb",
            "rss_mb",
            "fd_count",
            "fd_limit",
            "cpu_seconds",
            "cpu_clock",
        }

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

    @pytest.mark.asyncio
    async def test_run_emits_loop_lag_histogram(self, monkeypatch) -> None:
        mgr = FakeManager()
        clock = Clock()
        emitted: list[tuple[str, float, dict[str, Any], str]] = []

        def _capture(name: str, value: float, attrs: dict[str, Any], *, unit: str = "1") -> None:
            emitted.append((name, value, attrs, unit))

        monkeypatch.setattr(ctl_mod, "emit_histogram", _capture)

        async def _sleep(secs: float) -> None:
            clock.advance(secs + 0.3)  # the timer fired 300 ms late

        ctl = AdaptiveController(
            mgr,  # type: ignore[arg-type]
            cfg=_cfg(controller_sample_secs=5),
            host_probe=lambda: HostSample(),
            clock=clock,
            sleep=_sleep,
        )
        await ctl._sample_and_tick()
        assert len(emitted) == 1
        name, value, attrs, unit = emitted[0]
        assert name == LOOP_LAG_MS
        assert value == pytest.approx(300.0, abs=1.0)
        assert attrs == {"process": "gateway"}
        assert unit == "ms"

    @pytest.mark.asyncio
    async def test_reconfigured_sample_period_is_not_reported_as_lag(self, monkeypatch) -> None:
        mgr = FakeManager()
        clock = Clock()
        emitted: list[tuple[str, float, dict[str, Any], str]] = []

        def _capture(name: str, value: float, attrs: dict[str, Any], *, unit: str = "1") -> None:
            emitted.append((name, value, attrs, unit))

        monkeypatch.setattr(ctl_mod, "emit_histogram", _capture)
        holder: dict[str, AdaptiveController] = {}

        async def _sleep(secs: float) -> None:
            # A hot-reload shortens the period while the timer is pending;
            # the timer itself fired exactly on the period it was armed with.
            holder["ctl"]._sample_secs = 1.0
            clock.advance(5.0)

        ctl = AdaptiveController(
            mgr,  # type: ignore[arg-type]
            cfg=_cfg(controller_sample_secs=5),
            host_probe=lambda: HostSample(),
            clock=clock,
            sleep=_sleep,
        )
        holder["ctl"] = ctl
        assert ctl._sample_secs == 5.0
        await ctl._sample_and_tick()
        assert len(emitted) == 1
        assert emitted[0][0] == LOOP_LAG_MS
        assert emitted[0][1] == 0.0

    @pytest.mark.asyncio
    async def test_decrease_is_logged_at_warning_and_increase_at_info(self, caplog) -> None:
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, cfg=_cfg(adaptive_initial=10))
        await ctl.step(_clean(clock.t))
        clock.advance(5)
        with caplog.at_level(logging.INFO, logger=ctl_mod.__name__):
            d = await ctl.step(_clean(clock.t, loop_lag_ms=400.0, running=10, healthy_in_flight=6))
            assert d.action == ACTION_DECREASE
            clock.advance(31)  # past the clean window; slow start was retired by the cut
            d = await ctl.step(_clean(clock.t, running=6, queued=1, completions=6))
            assert d.action == ACTION_INCREASE
            # The remaining actions are applied directly: a pause needs a
            # memory or severe-lag sample the policy fakes above do not carry.
            for action, paused in (
                (ACTION_PAUSE, True),
                (ACTION_RESUME, False),
                (ACTION_PROBE, False),
            ):
                await ctl.apply(
                    Decision(
                        effective_exec_cap=6,
                        spawn_gate_capacity=4,
                        paused=paused,
                        probing=action == ACTION_PROBE,
                        action=action,
                        reason=f"synthetic {action}",
                        changed=True,
                    )
                )
        records = [r for r in caplog.records if r.getMessage().startswith("adaptive concurrency")]
        by_action = {r.getMessage().split()[2].rstrip(":"): r.levelno for r in records}
        assert by_action == {
            ACTION_DECREASE: logging.WARNING,
            ACTION_PAUSE: logging.WARNING,
            ACTION_RESUME: logging.WARNING,
            ACTION_INCREASE: logging.INFO,
            ACTION_PROBE: logging.INFO,
        }

    @pytest.mark.asyncio
    async def test_recent_decisions_is_bounded_and_carries_reason(self) -> None:
        mgr = FakeManager(user_max=64)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate, cfg=_cfg(adaptive_initial=1))
        await ctl.step(_clean(clock.t))
        assert ctl.state()["recent_decisions"] == []
        changed = 0
        holds = 0
        while changed < 40:
            clock.advance(5)
            # Alternate a clean sample (demand at the cap, one completion
            # landed) with a corroborated lag sample; the cooldowns between
            # cap changes are what produce the holds.
            cap = ctl.policy.exec_cap
            sample = _clean(
                clock.t,
                running=cap,
                queued=1,
                completions=changed + 1,
                loop_lag_ms=400.0 if changed % 2 else 5.0,
                healthy_in_flight=0,
            )
            # ``tick`` appends the sample it built before deciding on it; the
            # entry's ``loop_lag_ms`` is read from that ring.
            ctl._samples.append(sample)
            d = await ctl.step(sample)
            if d.action == ACTION_HOLD:
                holds += 1
            else:
                changed += 1
        recent = ctl.state()["recent_decisions"]
        assert holds > 0
        assert len(recent) == 32
        assert all(entry["action"] and entry["reason"] for entry in recent)
        assert all(entry["action"] != ACTION_HOLD for entry in recent)
        assert [entry["at"] for entry in recent] == sorted(entry["at"] for entry in recent)
        newest = recent[-1]
        assert set(newest) == {
            "at",
            "action",
            "reason",
            "exec_cap",
            "gate_cap",
            "paused",
            "loop_lag_ms",
        }
        # Wall-clock so the entry can be lined up with gateway.log; the
        # injected controller clock started at 1000 and would not be.
        assert isinstance(newest["at"], float)
        assert abs(newest["at"] - time.time()) < 60.0
        assert newest["action"] == d.action
        assert newest["reason"] == d.reason
        # The caps are the confirmed ones: the fake gate answers every update,
        # so they equal the decision's; see the unanswered-gate test for the
        # other case.
        assert newest["exec_cap"] == d.effective_exec_cap == mgr.effective
        assert newest["gate_cap"] == d.spawn_gate_capacity == gate.gate.capacity
        assert isinstance(newest["exec_cap"], int) and isinstance(newest["gate_cap"], int)
        assert newest["paused"] is d.paused
        assert isinstance(newest["paused"], bool)
        assert newest["loop_lag_ms"] == pytest.approx(400.0 if (changed - 1) % 2 else 5.0)

    @pytest.mark.asyncio
    async def test_history_records_the_confirmed_gate_cap_not_the_requested_one(self) -> None:
        """A gate update the daemon does not answer stays pending; the entry
        must show the gate cap actually in force, not the one asked for."""
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        ctl, clock = _controller(mgr, gate, cfg=_cfg(adaptive_initial=10))
        await ctl.step(_clean(clock.t))
        confirmed_gate = gate.gate.capacity
        gate.answer = False  # daemon stops answering; the update stays pending
        clock.advance(5)
        d = await ctl.step(_clean(clock.t, loop_lag_ms=400.0, running=10, healthy_in_flight=6))
        assert d.action == ACTION_DECREASE
        newest = ctl.state()["recent_decisions"][-1]
        assert newest["exec_cap"] == d.effective_exec_cap == mgr.effective
        assert newest["gate_cap"] == confirmed_gate
        assert ctl.state()["gate_pending"] == d.spawn_gate_capacity
        if d.spawn_gate_capacity != confirmed_gate:
            assert newest["gate_cap"] != d.spawn_gate_capacity

    @pytest.mark.asyncio
    async def test_a_live_ceiling_drop_that_clamps_the_cap_is_in_the_history(self) -> None:
        """Lowering ``agent.max_subagents`` live makes ``step()`` re-read the
        ceiling and the policy clamp its cap; the decision that follows is a
        hold with ``changed=True``. It moves the confirmed cap, so it is the one
        hold that belongs in the history."""
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, cfg=_cfg(adaptive_initial=8))
        await ctl.step(_clean(clock.t))
        assert mgr.effective == 8
        assert ctl.state()["recent_decisions"] == []

        mgr._user = 2  # the live ceiling drop (agent.max_subagents hot-reload)
        clock.advance(5)
        d = await ctl.step(_clean(clock.t))
        assert d.action == ACTION_HOLD and d.changed
        assert mgr.effective == 2
        newest = ctl.state()["recent_decisions"][-1]
        assert newest["action"] == ACTION_HOLD
        assert newest["exec_cap"] == 2

        # An ordinary hold (nothing moved) still appends nothing.
        clock.advance(5)
        d = await ctl.step(_clean(clock.t))
        assert d.action == ACTION_HOLD and not d.changed
        assert len(ctl.state()["recent_decisions"]) == 1

    @pytest.mark.asyncio
    async def test_a_clamped_gate_cap_is_recorded_as_the_daemon_applied_it(self) -> None:
        """An adopted daemon launched under narrower bounds clamps the request
        and answers with what took effect; the applied value and the history
        must carry that answer, not the request."""
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        gate.gate = SpawnGate(4, floor=1, ceiling=6)  # the daemon's bounds: narrower
        clock = Clock()
        ctl = AdaptiveController(
            mgr,  # type: ignore[arg-type]
            cfg=_cfg(adaptive_initial=8),
            set_gate_capacity=gate.set_capacity,
            read_gate_stats=gate.stats,
            host_probe=lambda: HostSample(
                free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000
            ),
            clock=clock,
            sleep=AsyncMock(),
            gate_initial=8,  # the controller's bounds: what an older daemon never learned
        )
        d = await ctl.step(_clean(clock.t))
        assert d.spawn_gate_capacity == 8
        assert gate.gate.capacity == 6
        assert ctl.state()["applied_gate_cap"] == 6
        clock.advance(5)
        d = await ctl.step(_clean(clock.t, loop_lag_ms=400.0, running=8, healthy_in_flight=6))
        assert d.action == ACTION_DECREASE
        newest = ctl.state()["recent_decisions"][-1]
        assert newest["gate_cap"] == gate.gate.capacity == ctl.state()["applied_gate_cap"]
        assert newest["gate_cap"] != 8

    @pytest.mark.asyncio
    async def test_a_restarted_daemon_accepting_a_pending_gate_cap_is_in_the_history(self) -> None:
        """The decision does not change when a restarted daemon with wider bounds
        finally accepts the capacity the controller kept asking for, but the
        confirmed gate cap moves, and that move is what the history is for."""
        mgr = FakeManager(user_max=10)
        gate = FakeGate()
        gate.gate = SpawnGate(4, floor=1, ceiling=6)
        clock = Clock()
        ctl = AdaptiveController(
            mgr,  # type: ignore[arg-type]
            cfg=_cfg(adaptive_initial=8),
            set_gate_capacity=gate.set_capacity,
            read_gate_stats=gate.stats,
            host_probe=lambda: HostSample(
                free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000
            ),
            clock=clock,
            sleep=AsyncMock(),
            gate_initial=8,
        )
        await ctl.step(_clean(clock.t))
        assert ctl.state()["applied_gate_cap"] == 6
        before = len(ctl.state()["recent_decisions"])

        gate.gate = SpawnGate(6, floor=1, ceiling=8)  # daemon restarted under the current bounds
        clock.advance(5)
        d = await ctl.step(_clean(clock.t))
        assert d.action == ACTION_HOLD and not d.changed
        assert ctl.state()["applied_gate_cap"] == 8
        recent = ctl.state()["recent_decisions"]
        assert len(recent) == before + 1
        assert recent[-1]["action"] == ACTION_HOLD and recent[-1]["gate_cap"] == 8

    @pytest.mark.asyncio
    async def test_starting_in_fixed_mode_records_no_cap_change(self) -> None:
        """The first decision in fixed mode pins caps that were never anything
        else; it is not a cap change and must not seed the history."""
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, cfg=_cfg(adaptive_concurrency_mode=MODE_FIXED))
        for _ in range(3):
            clock.advance(5)
            d = await ctl.step(_clean(clock.t))
            assert d.action == ACTION_FIXED
        assert ctl.state()["recent_decisions"] == []

    @pytest.mark.asyncio
    async def test_a_fixed_mode_switch_that_restores_the_cap_is_in_the_history(self) -> None:
        """A hot-reload to fixed mode pins the caps at their start values. From a
        reduced AIMD cap that is a cap change, and the history must show it even
        though the decision counter and the log skip fixed-mode decisions."""
        mgr = FakeManager(user_max=10)
        ctl, clock = _controller(mgr, cfg=_cfg(adaptive_initial=10))
        await ctl.step(_clean(clock.t))
        clock.advance(5)
        d = await ctl.step(_clean(clock.t, loop_lag_ms=400.0, running=10, healthy_in_flight=6))
        assert d.action == ACTION_DECREASE
        reduced = d.effective_exec_cap
        assert reduced < 10

        ctl.apply_config(_cfg(adaptive_initial=10, adaptive_concurrency_mode=MODE_FIXED))
        clock.advance(5)
        d = await ctl.step(_clean(clock.t, running=reduced))
        assert d.action == ACTION_FIXED and d.changed
        assert d.effective_exec_cap == 10

        newest = ctl.state()["recent_decisions"][-1]
        assert newest["action"] == ACTION_FIXED
        assert newest["exec_cap"] == 10
        assert [e["action"] for e in ctl.state()["recent_decisions"]] == [
            ACTION_DECREASE,
            ACTION_FIXED,
        ]


class TestRunOutcomeClassifier:
    @pytest.mark.parametrize(
        "error,expected",
        [
            ("", OUTCOME_SUCCESS),
            ("Timed out after 30 minutes [turns=3]", OUTCOME_ATTRIBUTABLE),
            ("error: tool stall", OUTCOME_ATTRIBUTABLE),
            ("startup timeout", OUTCOME_ATTRIBUTABLE),
            ("cancelled", OUTCOME_NON_CONGESTION),
            # The cancel prefix wins over a congestion marker in the same text:
            # an operator cancel must never pull the adaptive cap down. Without
            # the prefix test this row is OUTCOME_ATTRIBUTABLE, so it is what
            # pins that guard -- a bare "cancelled" would still bucket as
            # non-congestion by falling through to the default.
            ("Cancelled during startup", OUTCOME_NON_CONGESTION),
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

    def test_resource_status_names_the_growth_regime(self) -> None:
        """ "Execution cap: 4/64" alone reads as an unexplained throttle.

        The regime says how the cap is heading for the user's ceiling, so a cap
        below the max reads as headroom not yet earned rather than a host limit.
        """
        from kiro_crew import resource_status as rs

        joined = "\n".join(
            rs.adaptive_summary_lines(
                {
                    "enabled": True,
                    "mode": "aimd",
                    "effective_exec_cap": 8,
                    "exec_ceiling": 64,
                    "spawn_gate_capacity": 4,
                    "gate_ceiling": 8,
                    "slow_start": True,
                    "last": {"action": "increase", "reason": "clean window earned x2 (slow start)"},
                }
            )
        )
        assert "Execution cap: 8/64" in joined
        assert "Growth toward ceiling: slow start (x2/window)" in joined
        assert "Host cap" not in joined
        # A state without a regime prints no growth line rather than a guess.
        quiet = rs.adaptive_summary_lines(
            {"enabled": True, "effective_exec_cap": 4, "exec_ceiling": 64}
        )
        assert not any("Growth" in line for line in quiet)

    def test_summary_lists_recent_cap_changes(self) -> None:
        from kiro_crew import resource_status as rs

        base = {
            "enabled": True,
            "mode": "aimd",
            "effective_exec_cap": 3,
            "exec_ceiling": 64,
            "spawn_gate_capacity": 2,
            "gate_ceiling": 8,
            "last": {"action": "decrease", "reason": "loop_lag corroborated"},
        }
        recent = [
            {
                "at": 1_000_000_000.0 + i,
                "action": "decrease" if i % 2 else "increase",
                "reason": f"r{i}",
                "exec_cap": 10 - i,
                "gate_cap": 8 - i,
                "paused": False,
                "loop_lag_ms": None if i == 6 else float(i * 100),
            }
            for i in range(7)
        ]
        lines = rs.adaptive_summary_lines({**base, "recent_decisions": recent})
        header = lines.index("  Recent cap changes (newest last):")
        # ``at`` renders as the ``%H:%M:%S`` local-time stamp gateway.log
        # carries, so a line here can be matched against the log by eye.
        # Post-epoch base: a near-epoch value goes pre-epoch in any west-of-UTC
        # zone and Windows' ``localtime`` rejects it (see test_portability.py).
        stamp = [time.strftime("%H:%M:%S", time.localtime(1_000_000_000.0 + i)) for i in range(7)]
        assert lines[header + 1 : header + 6] == [
            f"    {stamp[2]} increase -> exec 8 gate 6 lag 200.0ms (r2)",
            f"    {stamp[3]} decrease -> exec 7 gate 5 lag 300.0ms (r3)",
            f"    {stamp[4]} increase -> exec 6 gate 4 lag 400.0ms (r4)",
            f"    {stamp[5]} decrease -> exec 5 gate 3 lag 500.0ms (r5)",
            f"    {stamp[6]} increase -> exec 4 gate 2 lag -ms (r6)",
        ]
        assert not any("r0" in line or "r1" in line for line in lines)
        missing_at = rs.adaptive_summary_lines(
            {**base, "recent_decisions": [{**recent[-1], "at": None}]}
        )
        assert any(line.startswith("    --:--:-- increase") for line in missing_at)
        # Without the key the block is absent and the other lines are unchanged.
        without = rs.adaptive_summary_lines(base)
        assert not any("Recent cap changes" in line for line in without)
        assert without == lines[:header]

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
        # An on/off switch for the growth REGIME, like adaptive_concurrency --
        # the factor, the window and the success bar stay in the policy.
        "adaptive_slow_start",
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


class TestSampledProcessHistograms:
    """The two resource distributions the controller records on its own tick.

    Recorded here because a histogram is RECORDED and not observed: OTEL has no
    observable histogram, so the series need a caller on a timer, and this loop
    already probes both readings for its own decisions.
    """

    @staticmethod
    def _capture(store: list) -> Any:
        def _emit(name: str, value: float, attrs: dict[str, Any], *, unit: str = "1") -> None:
            store.append((name, value, attrs, unit))

        return _emit

    @staticmethod
    def _only(store: list, name: str) -> list:
        return [row for row in store if row[0] == name]

    def _controller_for(
        self,
        probe: Any,
        clock: Clock,
        monkeypatch: Any,
        store: list,
        cores: Optional[int] = 4,
    ) -> AdaptiveController:
        monkeypatch.setattr(ctl_mod, "emit_histogram", self._capture(store))
        monkeypatch.setattr(ctl_mod, "read_logical_cores", lambda: cores)
        return AdaptiveController(
            FakeManager(),  # type: ignore[arg-type]
            cfg=_cfg(),
            host_probe=probe,
            clock=clock,
        )

    @pytest.mark.asyncio
    async def test_resident_set_is_recorded_in_bytes(self, monkeypatch) -> None:
        """The probe reads megabytes; the instrument publishes bytes, because the
        boundary array and the declared unit are both bytes."""
        store: list = []
        clock = Clock()
        ctl = self._controller_for(
            lambda: HostSample(
                free_mem_mb=16_000.0, rss_mb=300.0, cpu_seconds=10.0, cpu_clock=clock()
            ),
            clock,
            monkeypatch,
            store,
        )
        await ctl.tick()
        (row,) = self._only(store, PROCESS_RSS_SAMPLED)
        name, value, attrs, unit = row
        assert value == pytest.approx(300.0 * 1024 * 1024)
        assert attrs == {"process": "gateway"}
        assert unit == "By"

    @pytest.mark.asyncio
    async def test_the_first_tick_publishes_no_cpu_share(self, monkeypatch) -> None:
        """A share is a RATE and the probe returns a lifetime TOTAL, so one
        reading is not a sample. Publishing anything here would be invented."""
        store: list = []
        clock = Clock()
        ctl = self._controller_for(
            lambda: HostSample(rss_mb=300.0, cpu_seconds=10.0, cpu_clock=clock()),
            clock,
            monkeypatch,
            store,
        )
        await ctl.tick()
        assert self._only(store, PROCESS_CPU_UTILIZATION) == []
        assert len(self._only(store, PROCESS_RSS_SAMPLED)) == 1

    @pytest.mark.asyncio
    async def test_the_second_tick_publishes_the_measured_share(self, monkeypatch) -> None:
        """Five CPU seconds burned over twenty wall seconds on four cores."""
        store: list = []
        clock = Clock()
        reading = {"cpu": 10.0}
        ctl = self._controller_for(
            lambda: HostSample(rss_mb=300.0, cpu_seconds=reading["cpu"], cpu_clock=clock()),
            clock,
            monkeypatch,
            store,
        )
        await ctl.tick()
        clock.advance(20)
        reading["cpu"] = 15.0
        await ctl.tick()
        (row,) = self._only(store, PROCESS_CPU_UTILIZATION)
        name, value, attrs, unit = row
        assert value == pytest.approx(5.0 / (20.0 * 4))
        assert attrs == {"process": "gateway"}
        assert unit == "1"

    @pytest.mark.asyncio
    async def test_an_unmeasured_resident_set_publishes_nothing(self, monkeypatch) -> None:
        """``-1`` is the sample's "not measured"; a zero-byte process is not a
        thing, so publishing one would be a fake reading."""
        store: list = []
        ctl = self._controller_for(lambda: HostSample(), Clock(), monkeypatch, store)
        await ctl.tick()
        assert self._only(store, PROCESS_RSS_SAMPLED) == []

    @pytest.mark.asyncio
    async def test_a_zero_resident_set_publishes_nothing(self, monkeypatch) -> None:
        """The reachable failure, distinct from the ``-1`` sentinel above.

        ``proc_rss_bytes`` answers 0 on failure rather than raising, so the probe's
        own ``except`` never runs and the sample carries a plain ``0.0``. A live
        process never has a true resident set of zero, and a histogram is cumulative,
        so admitting one would leave a fabricated bucket in the series for the rest of
        the process's life.
        """
        store: list = []
        ctl = self._controller_for(
            lambda: HostSample(free_mem_mb=16_000.0, rss_mb=0.0, fd_count=50, fd_limit=1000),
            Clock(),
            monkeypatch,
            store,
        )
        await ctl.tick()
        assert self._only(store, PROCESS_RSS_SAMPLED) == []

    @pytest.mark.asyncio
    async def test_an_unknown_core_count_publishes_no_share(self, monkeypatch) -> None:
        """Without a core count there is no machine to be a share OF."""
        store: list = []
        clock = Clock()
        reading = {"cpu": 10.0}
        ctl = self._controller_for(
            lambda: HostSample(rss_mb=300.0, cpu_seconds=reading["cpu"], cpu_clock=clock()),
            clock,
            monkeypatch,
            store,
            cores=None,
        )
        await ctl.tick()
        clock.advance(20)
        reading["cpu"] = 15.0
        await ctl.tick()
        assert self._only(store, PROCESS_CPU_UTILIZATION) == []

    @pytest.mark.asyncio
    async def test_a_failed_cpu_probe_does_not_become_the_next_baseline(self, monkeypatch) -> None:
        """``proc_cpu_seconds`` reads 0.0 when the probe fails. Keeping the older
        pair differences a longer interval against the reading it was taken with,
        which stays correct arithmetic; adopting 0.0 would report a huge share.
        """
        store: list = []
        clock = Clock()
        reading = {"cpu": 10.0}
        ctl = self._controller_for(
            lambda: HostSample(rss_mb=300.0, cpu_seconds=reading["cpu"], cpu_clock=clock()),
            clock,
            monkeypatch,
            store,
        )
        await ctl.tick()
        clock.advance(10)
        reading["cpu"] = 0.0  # the probe failed
        await ctl.tick()
        assert self._only(store, PROCESS_CPU_UTILIZATION) == []
        clock.advance(10)
        reading["cpu"] = 18.0
        await ctl.tick()
        (row,) = self._only(store, PROCESS_CPU_UTILIZATION)
        # 8 seconds burned across the whole 20, not 8 across the last 10.
        assert row[1] == pytest.approx(8.0 / (20.0 * 4))

    @pytest.mark.asyncio
    async def test_a_restarted_process_reading_publishes_no_share(self, monkeypatch) -> None:
        """A lifetime total cannot decrease, so a drop means the two readings came
        from different processes and their difference describes neither."""
        store: list = []
        clock = Clock()
        reading = {"cpu": 90.0}
        ctl = self._controller_for(
            lambda: HostSample(rss_mb=300.0, cpu_seconds=reading["cpu"], cpu_clock=clock()),
            clock,
            monkeypatch,
            store,
        )
        await ctl.tick()
        clock.advance(10)
        reading["cpu"] = 4.0
        await ctl.tick()
        assert self._only(store, PROCESS_CPU_UTILIZATION) == []

    @pytest.mark.asyncio
    async def test_the_share_divides_by_the_probe_interval_not_the_resume_interval(
        self, monkeypatch
    ) -> None:
        """The CPU total is read in a worker thread; the loop resumes later. Both
        endpoints of the division come from the probe, so the resumption delay is
        outside the measured interval.

        The delay is five seconds here to keep the arithmetic unambiguous. It
        varies from tick to tick, so it does not cancel, and the mechanism is the
        same at the few hundred milliseconds a busy loop actually shows.
        """
        store: list = []
        clock = Clock()
        reading = {"cpu": 10.0, "resume_delay": 0.0}

        def probe() -> HostSample:
            instant = clock()  # the worker thread read the total at this moment
            clock.advance(reading["resume_delay"])  # the loop resumed this much later
            return HostSample(rss_mb=300.0, cpu_seconds=reading["cpu"], cpu_clock=instant)

        ctl = self._controller_for(probe, clock, monkeypatch, store)
        await ctl.tick()
        clock.advance(20)
        reading["cpu"] = 15.0
        reading["resume_delay"] = 5.0
        await ctl.tick()
        (row,) = self._only(store, PROCESS_CPU_UTILIZATION)
        # Five CPU seconds across the twenty between the two probes, on four
        # cores. Pairing the total with the loop's post-resume instant would
        # divide by twenty-five and under-report the share by a fifth.
        assert row[1] == pytest.approx(5.0 / (20.0 * 4))
        assert row[1] != pytest.approx(5.0 / (25.0 * 4))

    @pytest.mark.asyncio
    async def test_a_total_with_no_instant_publishes_no_share(self, monkeypatch) -> None:
        """``cpu_clock`` is the probe's own reading of when it took the total. A
        sample carrying one without the other is not a measurement, and no instant
        this loop could substitute belongs to that total."""
        store: list = []
        clock = Clock()
        reading = {"cpu": 10.0, "paired": True}
        ctl = self._controller_for(
            lambda: HostSample(
                rss_mb=300.0,
                cpu_seconds=reading["cpu"],
                cpu_clock=clock() if reading["paired"] else -1.0,
            ),
            clock,
            monkeypatch,
            store,
        )
        await ctl.tick()
        clock.advance(20)
        reading["cpu"] = 15.0
        reading["paired"] = False
        await ctl.tick()
        assert self._only(store, PROCESS_CPU_UTILIZATION) == []
        # The resident set is a single reading, so it needs no pair and still publishes.
        assert len(self._only(store, PROCESS_RSS_SAMPLED)) == 2

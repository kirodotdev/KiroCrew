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
        host_probe=lambda: HostSample(free_mem_mb=32768, subagent_host_cap=64),
    )
    await ctl.tick()
    clock.advance(5)
    await ctl.step(_clean(clock.t, running=1, queued=63, loop_lag_ms=400, host_cap=64))
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
    async def test_host_cap_reaches_the_sample_and_bounds_the_climb(self) -> None:
        """The probe's host figure is what an increase climbs toward.

        The user's ``max_subagents`` is a ceiling, not a promise: this is the
        number that answers "how many does the memory and CPU on THIS host
        actually allow", and the cap stops there instead of at the ceiling.
        """
        mgr = FakeManager(user_max=64)
        host = HostSample(
            free_mem_mb=16_000.0, rss_mb=300.0, fd_count=50, fd_limit=1000, subagent_host_cap=8
        )
        ctl, clock = _controller(mgr, host=host, cfg=_cfg(adaptive_initial=4))
        mgr.running_count = 4
        mgr._queue = [{"task": str(i)} for i in range(50)]
        await ctl.tick(loop_lag_ms=5.0)
        assert ctl._samples[-1].host_cap == 8
        assert ctl.state()["last_sample"]["host_cap"] == 8
        for i in range(6):
            clock.advance(5)
            mgr._agents[f"done-{i}"] = SimpleNamespace(done=True, error="", stalled=False)
            mgr.running_count = max(4, mgr.effective or 4)
            await ctl.tick(loop_lag_ms=5.0)
        assert ctl.state()["applied_exec_cap"] == 8, ctl.state()

    @pytest.mark.asyncio
    async def test_an_unreadable_host_cap_never_tightens_anything(self) -> None:
        """A failed probe reports 0, and 0 means "use the user's ceiling"."""
        mgr = FakeManager(user_max=64)
        ctl, clock = _controller(mgr, cfg=_cfg(adaptive_initial=4))  # default probe: no host cap
        await ctl.tick(loop_lag_ms=5.0)
        assert ctl._samples[-1].host_cap == 0
        assert ctl.policy._growth_ceiling(ctl._samples[-1]) == 64

    @pytest.mark.asyncio
    async def test_an_unreadable_memory_probe_still_lets_the_cap_climb(self, monkeypatch) -> None:
        """The REAL probe on a host whose memory read fails hands the climb 0.

        The sizing helper's fail-open figure is the floor, 3, which sits UNDER
        the fresh-start cap of 4: a controller handed 3 for a host it could not
        measure has ``4 < 3`` as its increase condition and stays at 4 for the
        life of the process. So an unreadable host must report "not measured"
        (0), and the cap must climb toward the user's ceiling exactly as it does
        with no host figure at all.
        """
        monkeypatch.setattr("kiro_crew.subagent._available_memory_gb", lambda: -1.0)
        ctl_mod._host_cap_cache = (0.0, 0)
        host = HostSample(
            free_mem_mb=16_000.0,
            rss_mb=300.0,
            fd_count=50,
            fd_limit=1000,
            subagent_host_cap=ctl_mod._host_cap_cached(),
        )
        mgr = FakeManager(user_max=64)
        ctl, clock = _controller(mgr, host=host, cfg=_cfg(adaptive_initial=4))
        mgr.running_count = 4
        mgr._queue = [{"task": str(i)} for i in range(70)]
        await ctl.tick(loop_lag_ms=5.0)
        for i in range(6):
            clock.advance(5)
            mgr._agents[f"done-{i}"] = SimpleNamespace(done=True, error="", stalled=False)
            mgr.running_count = max(4, mgr.effective or 4)
            await ctl.tick(loop_lag_ms=5.0)
        assert ctl.state()["applied_exec_cap"] > 4, ctl.state()
        assert ctl.state()["applied_exec_cap"] == 64, ctl.state()
        assert host.subagent_host_cap == 0
        # The 0 is cached like a measured figure: an unreadable host is not a
        # reason to re-read config and the learned-cost store every tick.
        monkeypatch.setattr(
            "kiro_crew.subagent.host_terms_subagent_cap",
            lambda _c, **_kw: pytest.fail("cached 0 must not be recomputed inside the TTL"),
        )
        assert ctl_mod._host_cap_cached() == 0

    def test_probe_host_reports_the_hosts_own_cap_figure(self) -> None:
        """``probe_host`` is the ONE blocking read, so the cap figure rides it.

        ``host_terms_subagent_cap`` loads config and the learned-cost store; it
        belongs on the worker thread with the memory read, not on the loop.
        """
        ctl_mod._host_cap_cache = (0.0, 0)
        sample = ctl_mod.probe_host()
        assert sample.subagent_host_cap >= 3  # the sizing floor

    def test_the_host_cap_is_cached_not_recomputed_every_tick(self, monkeypatch) -> None:
        """A 5 s tick must not re-read config and the learned-cost store.

        It is not free, it moves on the scale of minutes, and the clamped
        sibling ``compute_max_subagents`` logs a sizing line at INFO on every
        call -- one tick per 5 s would be ~17k gateway log lines a day.
        """
        calls: list[int] = []

        def _counted(_cfg: object, **_kw: object) -> int:
            calls.append(1)
            return 11

        monkeypatch.setattr("kiro_crew.subagent.host_terms_subagent_cap", _counted)
        ctl_mod._host_cap_cache = (0.0, 0)
        assert [ctl_mod._host_cap_cached() for _ in range(5)] == [11] * 5
        assert len(calls) == 1
        # Past the TTL it is read again -- a cache, not a one-shot.
        ctl_mod._host_cap_cache = (
            ctl_mod._host_cap_cache[0] - ctl_mod._HOST_CAP_TTL_SECS - 1.0,
            11,
        )
        assert ctl_mod._host_cap_cached() == 11
        assert len(calls) == 2

    def test_the_climb_is_not_bounded_by_the_auto_size_ceiling(self, monkeypatch) -> None:
        """``host_terms_subagent_cap`` and NOT ``compute_max_subagents``.

        The latter clamps to ``subagent_auto_max`` (default 32), documented as
        applying to the auto-sized cap only. Reading it here would stop an
        explicit ``max_subagents=64`` at 32 on a host that can carry it -- the
        precise failure this controller change exists to remove.
        """
        monkeypatch.setattr("kiro_crew.subagent.host_terms_subagent_cap", lambda _c, **_kw: 48)

        def _boom(_c: object) -> int:  # pragma: no cover - must never be called
            raise AssertionError("the clamped sizing helper must not bound the climb")

        monkeypatch.setattr("kiro_crew.subagent.compute_max_subagents", _boom)
        ctl_mod._host_cap_cache = (0.0, 0)
        assert ctl_mod.probe_host().subagent_host_cap == 48

    @pytest.mark.asyncio
    async def test_resident_headroom_is_cached_as_a_total_not_recredited(self, monkeypatch) -> None:
        cfg = _cfg(
            adaptive_initial=8,
            subagent_mem_buffer_pct=0,
            subagent_cost_gb=1.0,
            subagent_cpu_cost_cores=1.0,
        )
        cfg.session.pool_size = 0
        clock = Clock()
        monkeypatch.setattr(ctl_mod, "time", SimpleNamespace(monotonic=clock))
        monkeypatch.setattr(ctl_mod, "_host_cap_cache", (0.0, 0))
        monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
        monkeypatch.setattr("kiro_crew.subagent.read_learned_cost", lambda _key: None)
        monkeypatch.setattr("kiro_crew.subagent.os.cpu_count", lambda: 64)
        available = [6.0]
        monkeypatch.setattr("kiro_crew.subagent._available_memory_gb", lambda: available[0])
        monkeypatch.setattr("kiro_crew.resource_status._read_available_gb", lambda: available[0])
        mgr = FakeManager(user_max=64)
        mgr.running_count = 8
        mgr._queue = [{"task": str(i)} for i in range(64)]
        mgr._agents = {str(i): SimpleNamespace(_pid=100 + i) for i in range(8)}
        # A yielded parent still consumes memory; queued/terminal rows and a
        # cold start without a process do not. running_count misses the parent.
        mgr._agents["0"]._slot_released = True
        mgr._agents.update(
            queued=SimpleNamespace(queued=True, _pid=200),
            terminal=SimpleNamespace(done=True, _pid=201),
            unstarted=SimpleNamespace(_pid=None),
        )
        ctl = AdaptiveController(mgr, cfg=cfg, clock=clock)
        await ctl.tick()
        assert ctl._samples[-1].host_cap == 14
        clock.advance(5)
        ctl.record_completion(ok=True)
        await ctl.tick()
        assert mgr.effective == 14
        # Three new residents consumed three GB. A live occupancy + cached
        # headroom implementation would incorrectly raise the total to 17.
        mgr._agents.update({str(i): SimpleNamespace(_pid=100 + i) for i in range(8, 11)})
        available[0] = 3.0
        clock.advance(5)
        await ctl.tick()
        assert ctl._samples[-1].host_cap == 14
        clock.advance(ctl_mod._HOST_CAP_TTL_SECS)
        await ctl.tick()
        assert ctl._samples[-1].host_cap == 14

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

    def test_resource_status_names_the_host_cap_and_the_growth_regime(self) -> None:
        """ "Execution cap: 4/64" alone reads as an unexplained throttle.

        The host figure is the bound the climb is heading for, so a cap far
        below the user's max is only actionable next to it.
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
                    "host_cap": 14,
                    "slow_start": True,
                    "last": {"action": "increase", "reason": "clean window earned x2 (slow start)"},
                }
            )
        )
        assert "Execution cap: 8/64" in joined
        assert "Host cap (memory+CPU): 14" in joined
        assert "Growth: slow start (x2/window)" in joined
        # An unmeasured host figure prints no line rather than a bare 0.
        quiet = rs.adaptive_summary_lines(
            {"enabled": True, "effective_exec_cap": 4, "exec_ceiling": 64, "host_cap": 0}
        )
        assert not any("Host cap" in line for line in quiet)

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

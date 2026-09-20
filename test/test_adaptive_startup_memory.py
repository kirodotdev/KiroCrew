"""Bounded delayed-RSS fault injection through the real spawn/pump/controller.

Only provider execution, host observations and time are fake. No child process
or large allocation is created; the real durable queue, admission and adaptive
actuator decide which workers may start.
"""

from __future__ import annotations

import asyncio
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import Clock, mock_ctx, mock_sessions

import kiro_crew.subagent as subagent_mod
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.resource_status import POSTURE_AMPLE, AdmissionDecision
from kiro_crew.subagent import SubagentInfo, SubagentManager, _startup_memory_reserve_gb
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator


@pytest.mark.parametrize("platform", ["WINDOWS", "MACOS"])
@pytest.mark.parametrize("free,ok", [(3.0, False), (8.0, True), (-1.0, True)])
def test_startup_memory_guard_uses_native_host_reader(monkeypatch, platform, free, ok):
    for name in ("LINUX", "WINDOWS", "MACOS"):
        monkeypatch.setattr(subagent_mod.platform_compat, "IS_" + name, name == platform)
    reader = (
        "_windows_available_memory_gb" if platform == "WINDOWS" else "_macos_available_memory_gb"
    )
    monkeypatch.setattr(subagent_mod, reader, lambda: free)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (ok, free)


def test_startup_memory_guard_respects_container_headroom(monkeypatch):
    import io

    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: io.StringIO("MemAvailable: 33554432 kB"))
    monkeypatch.setattr(subagent_mod, "_cgroup_available_gb", lambda: 3.0)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (False, 3.0)


@pytest.mark.parametrize(
    ("rows", "running", "expected"),
    [
        ([], 0, 0.5),
        ([], 2, 1.5),  # Claimed starts not registered yet plus the next start.
        ([{"last_rss_gb": 0.1}], 1, 0.9),
        ([{"last_rss_gb": 0.5}], 1, 0.5),  # Observed RSS already reduced free memory.
        ([{"last_rss_gb": 0.1, "_slot_released": True}], 0, 0.9),
        ([{"_session_sharing": True, "peak_rss_gb": 4.0}], 1, 0.5),
        ([{"_session_sharing": True}, {"_session_sharing": True}], 2, 0.5),
        ([{"done": True}, {"queued": True}], 0, 0.5),
        ([{"last_rss_gb": 0.6, "peak_rss_gb": 0.8}], 1, 1.0),
    ],
)
def test_startup_reserve_tracks_unobserved_dedicated_memory(rows, running, expected) -> None:
    agents = [SubagentInfo(id=str(i), task="work", **row) for i, row in enumerate(rows)]
    assert _startup_memory_reserve_gb(agents, running_count=running, cost_gb=0.5) == pytest.approx(
        expected
    )


@pytest.mark.parametrize(
    ("cost_gb", "running", "expected"),
    [(-8.0, 0, 0.0), (-8.0, 2, 0.0), (0.0, 0, 0.0), (0.0, 2, 0.0), (0.5, 0, 0.5), (0.5, 2, 1.5)],
)
def test_startup_reserve_cannot_discount_claims(cost_gb, running, expected):
    assert _startup_memory_reserve_gb([], running_count=running, cost_gb=cost_gb) == expected


@pytest.mark.parametrize("cost_gb", [-8.0, 0.0, 0.5])
def test_measured_peak_still_binds_with_nonpositive_configured_cost(cost_gb):
    info = SubagentInfo(id="live", task="work", peak_rss_gb=0.8, last_rss_gb=0.6)
    assert _startup_memory_reserve_gb([info], running_count=1, cost_gb=cost_gb) == pytest.approx(
        1.0
    )


@pytest.mark.parametrize("cost_gb", [-8.0, 0.0, 0.5])
@pytest.mark.parametrize("floor_gb", [0.0, 4.0])
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_startup_cost_cannot_lower_enabled_floor_on_exhausted_cgroup(
    monkeypatch, cost_gb, floor_gb
):
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = cost_gb
    cfg.agent.spawn_min_memory_gb = floor_gb
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(
        subagent_mod,
        "open",
        lambda *a, **kw: StringIO("MemAvailable: 33554432 kB\n"),
        raising=False,
    )
    monkeypatch.setattr(subagent_mod, "_cgroup_available_gb", lambda: 0.0)
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=32.0),
    )
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    mgr._spawn_stagger_secs = 0.0
    worker = AsyncMock()
    monkeypatch.setattr(mgr, "_run", worker)
    try:
        info = await mgr.spawn_async("work", parent_session_key="dash:memory-floor")
        assert info is not None
        assert info.queued is (floor_gb > 0)
        if floor_gb > 0:
            assert info.id not in mgr._tasks
            worker.assert_not_called()
        else:
            await asyncio.wait_for(mgr._tasks[info.id], 5)
            worker.assert_awaited_once()
    finally:
        mgr._shutting_down = True
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()


@pytest.mark.parametrize("shock_gb", [0.0, 16.0])
@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_delayed_dedicated_rss_does_not_spend_the_startup_reserve(
    monkeypatch, shock_gb
) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.max_subagents = 64
    cfg.agent.subagent_spawn_stagger_secs = 0.25
    cfg.agent.subagent_cost_gb = 0.5
    cfg.session.pool_size = 0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    clock = Clock()
    epoch = clock()
    monkeypatch.setattr(subagent_mod, "time", SimpleNamespace(monotonic=clock, time=time.time))
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=64)
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    mgr._spawn_stagger_secs = cfg.agent.subagent_spawn_stagger_secs
    starts: dict[str, float] = {}
    finishes: dict[str, asyncio.Future] = {}
    launch_times: list[float] = []
    external_gb = 0.0
    free_samples: list[float] = []
    refused_at: list[float] = []
    decisions: list[str] = []
    timer_handles = []
    loop = asyncio.get_running_loop()
    real_call_later = loop.call_later

    def call_later(delay, callback, *args, **kwargs):
        # Drive only the pump's timers with virtual time; asyncio's own
        # wait_for deadlines retain the real clock and remain bounded.
        if callback == mgr._drain_queue:
            handle = real_call_later(3600, callback, *args, **kwargs)
            timer_handles.append(handle)
            return handle
        return real_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_later", call_later)

    def available() -> float:
        resident = sum(
            0.5 if clock() - started >= 5.0 else 0.05
            for agent_id, started in starts.items()
            if not mgr._agents[agent_id].done
        )
        return 24.0 - external_gb - resident

    def memory_check(*, min_gb, **_kw):
        free = available()
        if free < min_gb:
            refused_at.append(clock())
        return free >= min_gb, free

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    # Keep the posture cache ample to exercise the absolute spawn guard even
    # when the slower cached posture observation has not noticed the shock.
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=24.0),
    )

    async def worker(info: SubagentInfo) -> None:
        starts[info.id] = clock()
        launch_times.append(clock())
        info._pid = 1000 + len(starts)
        info._exec_started = time.time()
        info._session_sharing = False
        done = finishes[info.id] = loop.create_future()
        await done
        info.done = True
        info.result = "ok"
        mgr._claim_finalize(info)
        if mgr._release_slot(info):
            mgr._running_count -= 1
            mgr._drain_queue()

    monkeypatch.setattr(mgr, "_run", worker)
    ctl = AdaptiveController(
        mgr,
        cfg=cfg,
        clock=clock,
        host_probe=lambda: HostSample(free_mem_mb=available() * 1024, subagent_host_cap=38),
    )

    async def pump() -> None:
        mgr._drain_queue()
        task = getattr(mgr, "_drain_task", None)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), 5)
        # Registration schedules the worker; a loop barrier lets it expose
        # its start before the next virtual host observation.
        await asyncio.sleep(0)

    try:
        await ctl.tick()
        for i in range(64):
            await mgr.spawn_async(
                f"work-{i}", parent_session_key="dash:memory-wave", batch_id="wave", batch_total=64
            )
        await pump()
        for step in range(1, 101):
            clock.advance(0.25)
            for agent_id, started in starts.items():
                mgr._agents[agent_id].last_rss_gb = 0.5 if clock() - started >= 5.0 else 0.05
            if step in (20, 40):
                # One real completion earns each slow-start increase; the
                # rest of the dedicated workers remain resident.
                oldest = next(agent_id for agent_id in starts if not mgr._agents[agent_id].done)
                finishes[oldest].set_result(None)
                await asyncio.wait_for(asyncio.shield(mgr._tasks[oldest]), 5)
            if step % 20 == 0:
                decisions.append((await ctl.tick()).action)
            if step == 40:
                # Another application takes memory just AFTER the controller
                # sampled. New workers would grow five seconds after passing
                # a raw free-memory check, inside its next sampling window.
                external_gb = shock_gb
            await pump()
            free_samples.append(available())

        assert "increase" in decisions
        assert min(free_samples) >= cfg.agent.spawn_min_memory_gb, (
            min(free_samples),
            len(starts),
            decisions,
        )
        if shock_gb:
            assert refused_at, "the real admission guard must stop the drain"
        else:
            assert not refused_at
            assert len(starts) == 18, "ample hosts must fill the earned 16 slots quickly"
        assert len(starts) < 64
        assert mgr._queue or mgr._taskq.count(state="queued")
        assert all(b - a >= 0.25 for a, b in zip(launch_times, launch_times[1:]))
        assert clock() - epoch == 25.0
    finally:
        mgr._shutting_down = True
        for handle in timer_handles:
            handle.cancel()
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        drain = getattr(mgr, "_drain_task", None)
        if drain is not None and not drain.done():
            tasks.append(drain)
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()

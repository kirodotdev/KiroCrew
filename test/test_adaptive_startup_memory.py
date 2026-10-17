"""Bounded delayed-RSS fault injection through the real spawn/pump/controller.

Only provider execution, host observations and time are fake. No child process
or large allocation is created; the real durable queue, admission and adaptive
actuator decide which workers may start.
"""

from __future__ import annotations

import asyncio
import json
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import Clock, mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.subagent as subagent_mod
from kiro_crew.adaptive.controller import AdaptiveController, HostSample
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import (
    _UNLEARNED_DEDICATED_START_GB,
    SubagentInfo,
    SubagentManager,
    _startup_memory_reserve_gb,
)
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


def test_startup_memory_guard_admits_a_host_with_no_native_reader(monkeypatch):
    """Neither Linux, macOS nor Windows means there is no reader for host memory,
    so the guard must ADMIT. Blocking instead would refuse every spawn forever on
    such a host, and back-pressure that cannot measure is not back-pressure."""
    for name in ("LINUX", "WINDOWS", "MACOS"):
        monkeypatch.setattr(subagent_mod.platform_compat, "IS_" + name, False)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (True, -1.0)


def test_startup_memory_guard_respects_container_headroom(monkeypatch):
    import io

    monkeypatch.setattr(subagent_mod.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr("builtins.open", lambda *a, **kw: io.StringIO("MemAvailable: 33554432 kB"))
    monkeypatch.setattr(subagent_mod, "_cgroup_available_gb", lambda: 3.0)
    assert subagent_mod.check_memory_available(min_gb=4.5) == (False, 3.0)


_D = _UNLEARNED_DEDICATED_START_GB  # an unpriced warming row's price, nothing learned


@pytest.mark.parametrize(
    ("rows", "running", "expected"),
    [
        ([], 0, 0.5),
        ([], 2, 1.5),  # Claimed starts not registered yet plus the next start.
        # A warming row owes its price in full: the summed-RSS reading it shows
        # is not in the unit the price is, so it is not credited.
        ([{"last_rss_gb": 0.1}], 1, 0.5 + _D),
        ([{"last_rss_gb": 0.5}], 1, 0.5 + _D),
        ([{"last_rss_gb": 0.1, "_slot_released": True}], 0, 0.5 + _D),
        ([{"_session_sharing": True, "peak_rss_gb": 4.0}], 1, 0.5),
        ([{"_session_sharing": True}, {"_session_sharing": True}], 2, 0.5),
        ([{"done": True}, {"queued": True}], 0, 0.5),
        # Never its peak: a run's peak is its whole subtree (suites, builds).
        ([{"last_rss_gb": 0.6, "peak_rss_gb": 0.8}], 1, 0.5 + _D),
        ([{"last_rss_gb": 1.0, "peak_rss_gb": 7.5, "_rss_samples": 1}], 1, 0.5 + _D),
        # A row admitted at a price owes THAT price, whatever the default is.
        ([{"last_rss_gb": 0.1, "_start_price_gb": 0.65}], 1, 0.5 + 0.65),
        # A settled worker owes nothing: its memory is already in the reading.
        ([{"last_rss_gb": 0.1, "peak_rss_gb": 7.5, "_rss_samples": 2}], 1, 0.5),
    ],
)
def test_startup_reserve_tracks_unobserved_dedicated_memory(rows, running, expected) -> None:
    agents = [SubagentInfo(id=str(i), task="work", **row) for i, row in enumerate(rows)]
    assert _startup_memory_reserve_gb(agents, running_count=running, cost_gb=0.5) == pytest.approx(
        expected
    )


def test_cost_samples_are_written_under_the_bucket_the_gate_reads(monkeypatch, tmp_path) -> None:
    """An agent-less run inheriting a template builds THAT template's bucket."""
    from kiro_crew import subagent_cost as sc
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        heavy = ExecutionContext(
            member_id=None,
            store=MemoryStoreRef(store_id="default"),
            selection_kind="template",
            template_id="heavy",
        )
        inherited = SubagentInfo(
            id="a", task="w", agent="", peak_rss_gb=6.0, execution_context=heavy
        )
        named = SubagentInfo(id="b", task="w", agent="light", peak_rss_gb=1.0)
        bare = SubagentInfo(id="c", task="w", agent="", peak_rss_gb=0.4)
        shared = SubagentInfo(
            id="d", task="w", agent="light", peak_rss_gb=0.2, _session_sharing=True
        )
        for info in (inherited, named, bare, shared):
            mgr._record_cost(info)
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        assert [r["agent"] for r in rows] == ["heavy", "light", "kirocrew", "light"]
        # A shared run's sample is marked; a dedicated run's record shape is unchanged.
        assert [r.get("shared") for r in rows] == [None, None, None, True]
        assert subagent_mod._cost_bucket("", heavy) == "heavy"
        assert subagent_mod._cost_bucket("named", heavy) == "named"
        assert subagent_mod._cost_bucket("", None) == ""
    finally:
        mgr._taskq.close()


def test_compaction_keeps_dedicated_history_under_a_flood_of_shared_runs(
    monkeypatch, tmp_path
) -> None:
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for _ in range(3):
        sc.append_cost_sample("kirocrew", 6.0, 0.1)
    for _ in range(60):
        sc.append_cost_sample("kirocrew", 0.2, 0.1, shared=True)
    sc.compact_cost_log(window=50)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 53  # 3 dedicated kept, shared trimmed to its own window
    assert sum(1 for r in rows if r.get("shared") is not True) == 3


def test_the_held_map_is_bounded_against_an_agent_writable_log(monkeypatch, tmp_path) -> None:
    from kiro_crew import subagent_cost as sc

    log = tmp_path / "cost_samples.jsonl"
    monkeypatch.setattr(sc, "_cost_log_path", lambda: log)
    for i in range(sc._MAX_BUCKETS + 10):
        for _ in range(3):
            sc.append_cost_sample(f"agent-{i:03d}", 0.1 + i * 0.01, 0.1)
    for _ in range(3):
        sc.append_cost_sample("x" * (sc._BUCKET_KEY_CAP + 1), 9.0, 0.1)  # not an agent name
    costs, _ = sc.read_learned_costs_checked("mem_gb")
    # The over-long key is never a bucket; of the rest, the HEAVIEST
    # _MAX_BUCKETS are returned (the parse ceiling is far above this count).
    assert len(costs) == sc._MAX_BUCKETS
    assert all(len(k) <= sc._BUCKET_KEY_CAP for k in costs)
    assert sorted(costs) == [f"agent-{i:03d}" for i in range(10, sc._MAX_BUCKETS + 10)]
    merged = sc.cap_buckets({**costs, "extra": 50.0})
    assert len(merged) == sc._MAX_BUCKETS and "extra" in merged


def test_a_sweep_that_straddles_a_respawn_does_not_settle_the_new_process(monkeypatch) -> None:
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    try:
        info = SubagentInfo(id="r", task="w", _pid=4242)
        mgr._agents["r"] = info

        def read_then_respawn(_pid):
            # The respawn lands while the off-loop /proc read is in flight.
            info._rss_samples = 0
            info.last_rss_gb = 0.0
            info._rss_generation += 1
            return subagent_mod.platform_compat.SubtreeSample(6 * 1024 * 1024, 0, 3, 0)

        monkeypatch.setattr(subagent_mod, "_proc_subtree_sample", read_then_respawn)
        mgr._sample_live_costs()
        assert info._rss_samples == 0 and info.last_rss_gb == 0.0
        monkeypatch.setattr(
            subagent_mod,
            "_proc_subtree_sample",
            lambda _pid: subagent_mod.platform_compat.SubtreeSample(6 * 1024 * 1024, 0, 3, 0),
        )
        mgr._sample_live_costs()
        assert info._rss_samples == 1 and info.last_rss_gb == pytest.approx(6.0)
    finally:
        mgr._taskq.close()


@pytest.mark.parametrize(
    ("cost_gb", "running", "expected"),
    [(-8.0, 0, 0.0), (-8.0, 2, 0.0), (0.0, 0, 0.0), (0.0, 2, 0.0), (0.5, 0, 0.5), (0.5, 2, 1.5)],
)
def test_startup_reserve_cannot_discount_claims(cost_gb, running, expected):
    assert _startup_memory_reserve_gb([], running_count=running, cost_gb=cost_gb) == expected


@pytest.mark.parametrize(("cost_gb", "expected"), [(-8.0, _D), (0.0, _D), (0.5, 0.5 + _D)])
def test_a_measured_peak_never_prices_a_start(cost_gb, expected):
    """A run's peak is its whole subtree (suites, builds), not what a start needs:
    the 0.8 GB peak is never charged, only the projection."""
    info = SubagentInfo(id="live", task="work", peak_rss_gb=0.8, last_rss_gb=0.6)
    assert _startup_memory_reserve_gb([info], running_count=1, cost_gb=cost_gb) == pytest.approx(
        expected
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
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await wait_taskq_open(mgr)
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


@pytest.mark.parametrize("shock_gb", [0.0, 8.0, 16.0])
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
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = cfg.agent.subagent_spawn_stagger_secs
    starts: dict[str, float] = {}
    finishes: dict[str, asyncio.Future] = {}
    launch_times: list[float] = []
    external_gb = 0.0
    free_samples: list[float] = []
    shock_at = float("inf")
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
        # A real reader never reports less than nothing: a negative figure is
        # its "unmeasurable" sentinel, which fails open.
        return max(0.0, 24.0 - external_gb - resident)

    def memory_check(*, min_gb, **_kw):
        free = available()
        if free < min_gb:
            refused_at.append(clock())
        return free >= min_gb, free

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)

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
        host_probe=lambda: HostSample(free_mem_mb=available() * 1024),
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
                # One real completion lands; the rest of the dedicated workers
                # remain resident.
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
                shock_at = clock()
            await pump()
            free_samples.append(available())

        floor = cfg.agent.spawn_min_memory_gb
        # The execution cap starts at its ceiling and never moves on memory:
        # the spawn floor alone decides how many of the 64 start, and it is what
        # stops the wave -- the reserve each warming start owes, not a count.
        # (A shock to zero free memory does cut and pause the MCP spawn gate.)
        assert "increase" not in decisions, decisions
        assert mgr.max_concurrent == 64
        assert refused_at, "the real admission guard must stop the drain"
        assert min(free_samples[:39]) >= floor, (min(free_samples), len(starts))
        assert 16 <= len(starts) < 64, len(starts)
        if shock_gb:
            # Nothing starts once the shock lands: every later start would take
            # the host further below what the floor must leave free.
            assert all(at < shock_at for at in launch_times), (shock_at, launch_times)
        if shock_gb <= 8.0:
            # A shock inside the headroom the floor kept leaves it intact. A
            # larger one is another application's memory, which admission
            # does not shed (subagent.md, *Memory guard*).
            assert min(free_samples) >= floor, (min(free_samples), len(starts))
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

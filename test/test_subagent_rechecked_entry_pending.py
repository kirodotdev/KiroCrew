"""A spawn the admission pump is re-checking still reads as pending work.

A memory-deferred spawn with no durable row (temporary or incognito memory, or
the task queue off) is popped from the dispatch window for each re-check, and
the re-check awaits off-loop reads before the gate starts, re-queues or refuses
it. For that span the pump holds it in ``_undurable_in_dispatch``: it is in
neither the window nor the registry, and it has no store row. Every read of a
parent's or a wave's unstarted spawns counts it there, so a parent does not
read as idle and a wave does not read as finished or stuck while that spawn is
about to start.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.subagent as subagent_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

PARENT = "dash:held"
WAVE = "wave-held"


@contextlib.asynccontextmanager
async def held_recheck(monkeypatch, **spawn_kwargs: Any) -> AsyncIterator[Any]:
    """Yield ``(manager, info)`` while the pump holds *info* mid re-check.

    The spawn is deferred for memory with temporary memory (no store row), and
    the next re-check is held inside its off-loop agent read. On exit memory
    recovers, the held re-check goes on and the spawn starts, which proves the
    held entry was the one the pump was about to dispatch.
    """
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = 0.5
    cfg.agent.spawn_min_memory_gb = 4.0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    free = {"gb": 3.0}

    def memory_check(*, min_gb, **_kw):
        return free["gb"] >= min_gb, free["gb"]

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 0.0
    mgr._taskq_admit_wait_secs = 0.05
    started, finish = asyncio.Event(), asyncio.Event()
    entered, release = asyncio.Event(), asyncio.Event()

    async def worker(info) -> None:
        started.set()
        await finish.wait()  # runs until the teardown cancels it

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=worker))
    try:
        info = await mgr.spawn_async(
            "summarize the log",
            parent_session_key=PARENT,
            _memory_mode="temporary",
            **spawn_kwargs,
        )
        assert info is not None and info.queued is True
        assert mgr.get(info.id) is None, "fixture: a deferred spawn has no registered run"

        check_agent = mgr._check_agent_off_loop

        async def held_check(*args, **kwargs):
            entered.set()
            await release.wait()
            return await check_agent(*args, **kwargs)

        monkeypatch.setattr(mgr, "_check_agent_off_loop", held_check)

        def rechecking() -> bool:
            mgr._drain_queue()
            return entered.is_set()

        await async_wait_until(rechecking, describe=lambda: "the pump never re-checked the spawn")
        windowed = [p.get("_preassigned_id") for p in mgr._queue]
        assert (
            info.id not in windowed and mgr.get(info.id) is None
        ), "fixture: the re-check holds the spawn outside the window and the registry"
        assert info.id in mgr._undurable_in_dispatch, "fixture: the pump holds the spawn"

        yield mgr, info

        free["gb"] = 32.0
        release.set()
        await await_until(started, what="the held spawn to start")
        assert mgr.get(info.id) is not None
    finally:
        release.set()  # a failed read must not leave the pump parked on the hold
        mgr._shutting_down = True
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await await_until(
            asyncio.gather(*tasks, return_exceptions=True), what="the cancelled runs to end"
        )
        mgr._taskq.close()


async def _window_depth(mgr: SubagentManager, info: SubagentInfo) -> None:
    """The parent's queued depth and the card chip's depth count the spawn."""
    assert mgr.queued_count_for(PARENT) == 1
    assert await mgr.queued_count_for_async(PARENT) == 1
    assert await mgr._run_events._read_queue_depth(PARENT) == 1
    assert mgr.has_pending_work_for(PARENT) is True
    assert mgr.queued_count_for("dash:other") == 0


async def _in_window_count(mgr: SubagentManager, info: SubagentInfo) -> None:
    """The store-free pending check and the nullable queued count see it."""
    assert mgr.has_in_memory_pending_work_for(PARENT) is True
    assert await mgr.queued_count_or_none_async(PARENT) == 1
    assert mgr.has_in_memory_pending_work_for("dash:other") is False


async def _batch_pending(mgr: SubagentManager, info: SubagentInfo) -> None:
    """A wave whose only outstanding member is held is still pending."""
    assert mgr._batch_submitted[WAVE] == [1, 1], "fixture: every submission arrived"
    assert mgr.batch_members_pending(WAVE) is True
    assert await mgr.batch_members_pending_async(WAVE) is True
    assert mgr.batch_members_pending("wave-other") is False


async def _nested_spawns(mgr: SubagentManager, info: SubagentInfo) -> None:
    """A finished wave member whose own spawn is held has a live nested spawn."""
    member = SubagentInfo(id="0123456789abcdef", task="member", done=True, batch_id="wave-outer")
    member.conversation_key = PARENT
    mgr._agents[member.id] = member
    try:
        assert mgr.wave_has_live_nested_spawns("wave-outer") is True
    finally:
        del mgr._agents[member.id]


async def _stuck_wave(mgr: SubagentManager, info: SubagentInfo) -> None:
    """A wave missing a submission is not reconciled as stuck while its one
    accepted member is held: that member is about to start."""
    assert mgr._batch_submitted[WAVE] == [1, 2], "fixture: one submission is missing"
    lost = MagicMock()
    mgr.record_lost_submission = lost  # type: ignore[method-assign]
    now = mgr._batch_progress_ts.get(WAVE, 0.0) + subagent_mod._WAVE_STUCK_SECS + 1.0
    await mgr._sweep_stuck_waves_async(now)
    lost.assert_not_called()


READS: list[
    tuple[str, dict[str, Any], Callable[[SubagentManager, SubagentInfo], Awaitable[None]]]
] = [
    ("window_depth", {}, _window_depth),
    ("in_window_count", {}, _in_window_count),
    ("batch_pending", {"batch_id": WAVE, "batch_total": 1}, _batch_pending),
    ("nested_spawns", {}, _nested_spawns),
    ("stuck_wave", {"batch_id": WAVE, "batch_total": 2}, _stuck_wave),
]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@pytest.mark.parametrize(
    ("spawn_kwargs", "check"),
    [pytest.param(kwargs, check, id=name) for name, kwargs, check in READS],
)
async def test_a_held_recheck_still_reads_as_pending(monkeypatch, spawn_kwargs, check) -> None:
    async with held_recheck(monkeypatch, **spawn_kwargs) as (mgr, info):
        await check(mgr, info)

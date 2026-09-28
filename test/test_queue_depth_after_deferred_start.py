"""A deferred row that later starts must leave the parent's queued count at 0.

The drain counts a row as waiting when it pops it, before the claim takes it
out of the claimable states. Without a fresh count once it registers, the chip
keeps "1 waiting" and the deferral's wait reason after nothing waits at all.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions

import kiro_crew.subagent as subagent_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.resource_status import POSTURE_AMPLE, AdmissionDecision
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_deferred_row_that_starts_clears_the_queued_count(monkeypatch) -> None:
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
    monkeypatch.setattr(
        subagent_mod,
        "cached_admission_check",
        lambda: AdmissionDecision(admitted=True, posture=POSTURE_AMPLE, available_gb=32.0),
    )
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    mgr._spawn_stagger_secs = 0.0
    mgr._taskq_admit_wait_secs = 0.05
    events: list[dict[str, Any]] = []

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype == "subagent_queued":
            events.append(dict(extra))

    mgr._on_event = on_event
    started = asyncio.Event()

    async def worker(info) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=worker))
    try:
        info = await mgr.spawn_async("work", parent_session_key="dash:depth")
        assert info is not None and info.queued is True
        for _ in range(100):
            if events and events[-1].get("queued") == 1:
                break
            await asyncio.sleep(0.02)
        assert events[-1]["queued"] == 1
        free["gb"] = 32.0
        for _ in range(250):
            if started.is_set():
                break
            mgr._drain_queue()
            await asyncio.sleep(0.02)
        assert started.is_set(), "the deferred row never started"
        for _ in range(25):
            await asyncio.sleep(0.02)
        assert events[-1]["queued"] == 0, events
    finally:
        mgr._shutting_down = True
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()

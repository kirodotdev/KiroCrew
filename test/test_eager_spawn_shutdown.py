"""A gateway shutdown ends speculative session starts instead of racing them.

An eager spawn that is still reading its bindings when the gateway begins to
stop must not start an agent: nothing is registered yet, so the shutdown's
session teardown could not reach it, and the task would outlive the gateway.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, ResolvedBindings
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.testing.wait import await_until


@pytest.fixture
def eager(monkeypatch):
    slot = _ChatSlot("shutdown-race")
    state = MagicMock(spec=DashboardState)
    state.get_slot.return_value = slot
    state.sessions = MagicMock()
    cfg = KiroCrewConfig(agents={"default": KiroCrewAgentConfig()})
    bindings = ResolvedBindings(
        workspace_dir=Path("workspace"),
        effective_memory_config={},
        kiro_agent="kirocrew",
        model="",
        resolved_alias="default",
        requested_resolved=True,
        memory_store_name="default",
        selection_kind="member",
    )
    admit = AsyncMock(return_value=True)
    spawn = AsyncMock()
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_consume_pending_reset", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(chat_runner, "resolve_session_agent_bindings", lambda *args, **kw: bindings)
    monkeypatch.setattr(chat_runner, "_require_session_memory_assignment", lambda *args: None)
    monkeypatch.setattr(chat_runner, "record_agent_selection", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
    monkeypatch.setattr(chat_runner, "_pressure_hold_blocks_prewarm", lambda state: False)
    monkeypatch.setattr(chat_runner, "_admit_prefetch", admit)
    monkeypatch.setattr(chat_runner, "_spawn_admitted_prefetch", spawn)
    yield state, slot, admit, spawn
    chat_runner.shutdown_event.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("stopping", [False, True], ids=["running", "stopping"])
async def test_a_shutdown_during_the_reads_stands_down_before_admission(
    eager, monkeypatch, stopping
):
    """The stop lands inside the last read before admission; the running case
    is the same path with no stop, so the stand-down is the only difference."""
    state, slot, admit, spawn = eager
    loop = asyncio.get_running_loop()

    def read_record(*args, **kwargs):
        # A worker thread: the stop is queued on the loop ahead of this read's
        # own result, so it has landed by the time the start resumes.
        if stopping:
            loop.call_soon_threadsafe(chat_runner.shutdown_event.set)

    monkeypatch.setattr(chat_runner, "record_agent_selection", read_record)
    await chat_runner._eager_spawn(state, slot)
    if stopping:
        admit.assert_not_awaited()
        spawn.assert_not_awaited()
    else:
        admit.assert_awaited_once()
        spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_eager_spawns_cancels_only_pending_starts() -> None:
    pending = asyncio.create_task(asyncio.Event().wait())
    finished = asyncio.create_task(asyncio.sleep(0))
    await finished
    state = SimpleNamespace(
        _slots={
            "pending": SimpleNamespace(_eager_spawn_task=pending),
            "finished": SimpleNamespace(_eager_spawn_task=finished),
            "idle": SimpleNamespace(_eager_spawn_task=None),
        }
    )
    try:
        stopped = chat_runner.stop_eager_spawns(state)
        assert stopped == [pending]
        await await_until(
            asyncio.gather(pending, return_exceptions=True), what="the cancelled start"
        )
        assert pending.cancelled()
        assert not finished.cancelled()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)

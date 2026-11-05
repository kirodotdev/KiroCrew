"""The reaper trims the cost log off the event loop, and not while shutting down.

``compact_cost_log`` reads, fsyncs and replaces a file. Called on the loop, a
slow, saturated disk held the loop past the stall watchdog, which killed the
gateway. These tests run the real reaper loop with its other steps recorded and
check which thread the trim ran on.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.subagent as sa
from kiro_crew.subagent import SubagentManager
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture(autouse=True)
def _pinned_paths(tmp_path, _floor_monkeypatch):
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        path = tmp_path / sub
        path.mkdir()
        _floor_monkeypatch.setenv(var, str(path))


@pytest.fixture(autouse=True)
def _managers(close_subagent_managers):
    """Managers built here are closed at teardown (``conftest``)."""


def _manager() -> SubagentManager:
    return SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), on_done=AsyncMock())


async def _run_ticks(manager: SubagentManager, compact: MagicMock) -> None:
    """Run the real reaper loop for one whole tick, everything but the trim recorded."""
    pump = MagicMock()
    with (
        patch.object(sa, "_REAPER_INTERVAL", 0),
        patch.object(sa, "compact_cost_log", compact),
        patch.object(sa, "prune_stale_tombstones", MagicMock(return_value=0)),
        patch.object(manager, "_sample_live_costs"),
        patch.object(manager, "_refresh_learned_settled"),
        patch.object(manager, "_rebuild_conversation_registry", new=AsyncMock()),
        patch.object(manager, "_sweep_stuck_waves_async", new=AsyncMock()),
        patch.object(manager, "_sweep_digest_holds_async", new=AsyncMock()),
        patch.object(manager, "_sweep_conversations_async", new=AsyncMock()),
        patch.object(manager, "retry_owed_teardown_sweeps", new=AsyncMock()),
        patch.object(manager, "_taskq_pump", pump),
        patch.object(type(manager._admission), "taskq_schedule_owed_replay", MagicMock()),
        patch.object(manager, "_force_reap", AsyncMock()),
    ):
        task = asyncio.ensure_future(manager._reaper_loop())
        # A whole tick has run once the pump runs twice.
        await async_wait_until(
            lambda: pump.call_count >= 2 or task.done(),
            describe=lambda: f"pump ran {pump.call_count} time(s)",
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await await_until(task)
    assert pump.call_count >= 2, "the reaper loop did not complete a tick"


@pytest.mark.asyncio
async def test_the_cost_log_is_compacted_off_the_loop_thread():
    loop_thread = threading.get_ident()
    threads: list[int] = []
    compact = MagicMock(side_effect=lambda *a, **k: threads.append(threading.get_ident()))

    await _run_ticks(_manager(), compact)

    # The startup trim and at least one periodic trim.
    assert len(threads) >= 2, f"compaction ran {len(threads)} time(s)"
    assert loop_thread not in threads, "compact_cost_log ran on the event loop thread"


@pytest.mark.asyncio
async def test_a_compaction_failure_does_not_stop_the_reaper():
    compact = MagicMock(side_effect=OSError("disk is busy"))

    await _run_ticks(_manager(), compact)

    assert compact.call_count >= 2, "the reaper stopped after a failed compaction"


@pytest.mark.asyncio
async def test_the_cost_log_is_not_compacted_while_shutting_down():
    manager = _manager()
    manager._shutting_down = True
    compact = MagicMock()

    await _run_ticks(manager, compact)

    compact.assert_not_called()

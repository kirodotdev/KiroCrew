"""Background starts of a slot whose agent keeps failing back off, then stop.

An eager spawn is re-armed by focus, reconnect, slot create and reset, so a slot
whose agent cannot start would otherwise spawn and tear down a fresh process
tree on every one of those signals. These tests fail the start repeatedly and
check the next background start waits longer each time, stops at the cap with
one error row, and resumes once a start succeeds.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_runner import _eager_spawn, schedule_eager_spawn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    chat_runner._armed_prefetches.clear()
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(
        chat_runner, "_prewarm_allowance", lambda: chat_runner._RESUME_PREFETCH_MAX_LIVE
    )
    cfg = KiroCrewConfig(agents={"default": KiroCrewAgentConfig()})
    cfg.session.eager_spawn = True
    live.watch().prime(cfg)
    with patch.object(chat_runner.KiroCrewConfig, "load", MagicMock(return_value=cfg)):
        yield
    chat_runner._armed_prefetches.clear()


def _state(slot: _ChatSlot, *, fail: bool) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state.get_slot = MagicMock(return_value=slot)
    state.sessions = MagicMock()
    if fail:
        state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError("initialize timed out"))
    else:
        state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.remove = AsyncMock()
    state.sessions.remove_if_unclaimed = AsyncMock(return_value=True)
    state.sessions.resumable_hint = MagicMock(return_value=True)
    return state


def _error_rows(slot: _ChatSlot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "error"]


@pytest.mark.asyncio
async def test_repeated_start_failures_back_off_and_stop_at_the_cap():
    slot = _ChatSlot("t1")
    state = _state(slot, fail=True)
    cap = chat_runner._EAGER_SPAWN_FAILURE_CAP
    waits: list[float] = []

    for attempt in range(1, cap + 1):
        task = schedule_eager_spawn(state, slot)
        assert task is not None, f"start {attempt} was not scheduled"
        await task
        assert slot._eager_spawn_failures == attempt
        waits.append(slot._eager_spawn_retry_at - time.monotonic())
        # Inside the backoff window the next signal starts nothing.
        assert schedule_eager_spawn(state, slot) is None
        # Let the window pass without waiting it out.
        slot._eager_spawn_retry_at = 0.0

    # Each wait is longer than the one before it.
    assert waits == sorted(waits) and waits[0] < waits[-1], waits
    assert waits[0] == pytest.approx(chat_runner._EAGER_SPAWN_BACKOFF_BASE_SECS, abs=1.0)

    # At the cap no signal starts it again, however long it has waited.
    for _ in range(5):
        assert schedule_eager_spawn(state, slot) is None
    assert state.sessions.get_or_create.await_count == cap

    # The user is told once, and the row says how to recover.
    rows = _error_rows(slot)
    assert len(rows) == 1, rows
    assert "failed to start" in rows[0] and "Send a message" in rows[0]
    assert "initialize timed out" in rows[0]


@pytest.mark.asyncio
async def test_a_failure_below_the_cap_posts_no_error_row():
    slot = _ChatSlot("t1")
    state = _state(slot, fail=True)
    await _eager_spawn(state, slot)
    assert slot._eager_spawn_failures == 1
    assert _error_rows(slot) == []


@pytest.mark.asyncio
async def test_a_successful_start_clears_the_count():
    slot = _ChatSlot("t1")
    slot._eager_spawn_failures = chat_runner._EAGER_SPAWN_FAILURE_CAP - 1
    state = _state(slot, fail=False)
    await _eager_spawn(state, slot)
    assert slot._eager_spawn_failures == 0
    assert slot._eager_spawn_retry_at == 0.0
    assert schedule_eager_spawn(state, slot) is not None
    slot._eager_spawn_task.cancel()


def test_the_backoff_is_capped():
    big = chat_runner._eager_spawn_backoff_secs(50)
    assert big == chat_runner._EAGER_SPAWN_BACKOFF_MAX_SECS
    assert chat_runner._eager_spawn_backoff_secs(0) == 0.0

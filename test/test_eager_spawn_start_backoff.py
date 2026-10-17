"""Background starts of a slot whose agent keeps failing back off, then stop.

An eager spawn is re-armed by focus, reconnect, slot create and reset, so a slot
whose agent cannot start would otherwise spawn and tear down a fresh process
tree on every one of those signals. These tests fail the start repeatedly and
check the next background start waits longer each time, stops at the cap with
one error row, and resumes once a start succeeds.
"""

from __future__ import annotations

import time
from pathlib import Path
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


@pytest.mark.asyncio
async def test_a_reset_failure_does_not_count_toward_the_cap():
    """A failed pending-reset consume spawned no agent, so it moves no count."""
    slot = _ChatSlot("t1")
    state = _state(slot, fail=False)
    with patch.object(
        chat_runner, "_consume_pending_reset", AsyncMock(side_effect=RuntimeError("reset failed"))
    ):
        for _ in range(chat_runner._EAGER_SPAWN_FAILURE_CAP + 1):
            await _eager_spawn(state, slot)
    assert slot._eager_spawn_failures == 0
    assert slot._eager_spawn_retry_at == 0.0
    assert _error_rows(slot) == []
    assert state.sessions.get_or_create.await_count == 0


@pytest.mark.asyncio
async def test_a_binding_write_failure_does_not_count_toward_the_cap():
    """The agent-selection write runs before any spawn; its failure is not a start's."""
    slot = _ChatSlot("t1")
    state = _state(slot, fail=False)
    with patch.object(
        chat_runner, "record_agent_selection", MagicMock(side_effect=OSError("disk full"))
    ):
        for _ in range(chat_runner._EAGER_SPAWN_FAILURE_CAP + 1):
            await _eager_spawn(state, slot)
    assert slot._eager_spawn_failures == 0
    assert _error_rows(slot) == []
    assert state.sessions.get_or_create.await_count == 0


@pytest.mark.asyncio
async def test_a_shutdown_or_ending_refusal_is_not_a_failed_start():
    """A closing gateway or a key being ended refused the start; nothing failed to start."""
    from kiro_crew.session import SessionClosingError, SessionEndingError

    for refusal in (SessionClosingError("closing"), SessionEndingError("ending")):
        slot = _ChatSlot("t1")
        state = _state(slot, fail=False)
        state.sessions.get_or_create = AsyncMock(side_effect=refusal)
        for _ in range(chat_runner._EAGER_SPAWN_FAILURE_CAP + 1):
            await _eager_spawn(state, slot)
        assert slot._eager_spawn_failures == 0, refusal
        assert _error_rows(slot) == [], refusal


@pytest.mark.asyncio
async def test_the_stop_is_logged_and_announced_once(caplog):
    """A spawn in flight when the cap was reached fails again without a second notice."""
    slot = _ChatSlot("t1")
    state = _state(slot, fail=True)
    with caplog.at_level("ERROR", logger=chat_runner.logger.name):
        for _ in range(chat_runner._EAGER_SPAWN_FAILURE_CAP + 2):
            # Direct calls bypass the scheduler's refusal, standing in for a
            # spawn that was already running when the cap was reached.
            await _eager_spawn(state, slot)
    stops = [r for r in caplog.records if "stay off until a start succeeds" in r.getMessage()]
    assert len(stops) == 1, [r.getMessage() for r in caplog.records]
    assert "gateway restarts" in stops[0].getMessage()
    assert len(_error_rows(slot)) == 1


@pytest.mark.asyncio
async def test_a_pre_spawn_capability_refusal_is_not_a_failed_start():
    """A member's capability refusal ran no process; it must not stop background starts."""
    from kiro_crew.agent_capabilities import CapabilityError
    from kiro_crew.session_capabilities import CapabilityStartupError

    refusals = [CapabilityError("invalid_spec")] + [
        CapabilityStartupError(code) for code in sorted(chat_runner._PRE_SPAWN_CAPABILITY_CODES)
    ]
    for refusal in refusals:
        slot = _ChatSlot("t1")
        state = _state(slot, fail=False)
        state.sessions.get_or_create = AsyncMock(side_effect=refusal)
        for _ in range(chat_runner._EAGER_SPAWN_FAILURE_CAP + 1):
            await _eager_spawn(state, slot)
        assert slot._eager_spawn_failures == 0, refusal
        assert _error_rows(slot) == [], refusal


@pytest.mark.asyncio
async def test_a_capability_failure_after_the_process_ran_still_counts():
    """A code a process-ran site can raise counts: it may have spawned a tree.

    ``capability_runtime_unverified`` is raised only after the start; the cwd and
    race codes come from a check that runs both before and after it, so they are
    counted too rather than risk leaving a churning start unbounded.
    """
    from kiro_crew.session_capabilities import CapabilityStartupError

    for code in (
        "capability_runtime_unverified",
        "capability_runtime_cwd_changed",
        "capability_startup_raced",
    ):
        slot = _ChatSlot("t1")
        state = _state(slot, fail=False)
        state.sessions.get_or_create = AsyncMock(side_effect=CapabilityStartupError(code))
        await _eager_spawn(state, slot)
        assert slot._eager_spawn_failures == 1, code


@pytest.mark.asyncio
async def test_the_stop_notice_bounds_the_error_it_carries():
    """A near-frame-limit agent error must not become a multi-megabyte chat row."""
    slot = _ChatSlot("t1")
    state = _state(slot, fail=False)
    huge = "initialize failed: " + "x" * 2_000_000
    state.sessions.get_or_create = AsyncMock(side_effect=RuntimeError(huge))
    for _ in range(chat_runner._EAGER_SPAWN_FAILURE_CAP):
        await _eager_spawn(state, slot)
    rows = _error_rows(slot)
    assert len(rows) == 1, rows
    assert "initialize failed: x" in rows[0]
    # A fixed ceiling, not one read from the code under test.
    assert len(rows[0]) < 2_000, len(rows[0])


def test_the_spec_states_the_numbers_the_code_uses():
    """docs/system-specs/modules/config.md names 10s, 300s, 3 and 500; they must be true.

    Literals on both sides: a constant read from the module would agree with
    itself however far it drifted from the spec.
    """
    assert chat_runner._EAGER_SPAWN_BACKOFF_BASE_SECS == 10.0
    assert chat_runner._EAGER_SPAWN_BACKOFF_MAX_SECS == 300.0
    assert chat_runner._EAGER_SPAWN_FAILURE_CAP == 3
    assert chat_runner._EAGER_SPAWN_ERROR_DETAIL_MAX_CHARS == 500
    # The schedule itself, not only its inputs: 10s, doubling, capped at 300s.
    waits = [chat_runner._eager_spawn_backoff_secs(n) for n in range(1, 8)]
    assert waits == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]

    spec = Path(__file__).resolve().parents[1] / "docs/system-specs/modules/config.md"
    text = spec.read_text(encoding="utf-8")
    head = "**Failed background starts back off, then stop.**"
    assert head in text, "the eager-spawn backoff paragraph is gone from the spec"
    paragraph = " ".join(text[text.index(head) :].split("\n\n", 1)[0].split())
    for claim in (
        "10s after the first failure, 20s after the second",
        "doubles up to a 300s ceiling",
        "After 3 failures in a row",
        "bounded to 500 characters",
        "off until a start succeeds or the gateway restarts",
    ):
        assert claim in paragraph, claim


def _user_turn(tmp_path, monkeypatch, reproject):
    """Drive one real ``_run_chat`` turn on a slot already stopped at the cap."""
    import asyncio

    from chat_test_helpers import _make_state

    from kiro_crew.agent_sdk import spec_hooks
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    async def fresh(sessions, session_key, agent_id):
        return spec_hooks.PROJECTION_FRESH

    async def post(sessions, session_key, agent_id, claimed, claim):
        return await reproject(claimed)

    monkeypatch.setattr(chat_runner, "invalidate_stale_kas_session", fresh)
    monkeypatch.setattr(chat_runner, "reproject_claimed_session", post)
    state = _make_state(tmp_path)
    client = MagicMock()
    client.context_usage_pct = MagicMock(return_value=50.0)
    client.shutdown = AsyncMock()

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_COMPLETE)

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("eager-backoff-user-turn")
    slot._eager_spawn_failures = chat_runner._EAGER_SPAWN_FAILURE_CAP
    slot._eager_spawn_retry_at = time.monotonic() + 300.0
    assert chat_runner._eager_spawn_held_off(slot)

    async def run():
        try:
            await chat_runner._run_chat(state, slot, "hello")
        finally:
            tasks = list(state._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(run())
    return slot


def test_a_users_successful_turn_re_enables_background_starts(tmp_path, monkeypatch):
    """The stop notice promises "Send a message to try again"; a turn that gets
    its session must clear the cap so background starts resume."""

    async def keep(claimed):
        return claimed

    slot = _user_turn(tmp_path, monkeypatch, keep)
    assert slot._eager_spawn_failures == 0
    assert slot._eager_spawn_retry_at == 0.0
    assert not chat_runner._eager_spawn_held_off(slot)


def test_a_turn_whose_claim_is_refused_leaves_the_cap_in_place(tmp_path, monkeypatch):
    """A re-claim refused after the claim is not a successful start."""
    from kiro_crew.agent_sdk import spec_hooks

    async def refuse(claimed):
        raise spec_hooks.StaleProjectionError("still stale")

    slot = _user_turn(tmp_path, monkeypatch, refuse)
    assert slot._eager_spawn_failures == chat_runner._EAGER_SPAWN_FAILURE_CAP

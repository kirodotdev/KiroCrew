"""A real dashboard turn reaches the session release with the starter its entry declared.

``SessionManager.release`` stamps ``last_turn_automated`` from the running turn's declared
trigger, so the stamp is only right if ``_run_chat``'s entry (``set_current_trigger``) and its
release run in one context. These drive the real ``_run_chat`` and read
``turn_was_automated()`` at the moment the release is called.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.session_idle_clock import turn_was_automated


def _state_and_slot(tmp_path, seen: list[bool]):
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = "acp-automated-idle"
    client.shutdown = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=10.0)

    async def _stream(*_a, **_kw):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock(
        side_effect=lambda *_a, **_kw: seen.append(turn_was_automated())
    )
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.compaction_due_after_turn = MagicMock(return_value=False)
    state.sessions.effective_autocompact_pct = MagicMock(return_value=70.0)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("automated-idle-slot")
    slot.append("user", "the message", "msg msg-u")
    return state, slot


async def _released_as(tmp_path, **turn_kwargs) -> list[bool]:
    seen: list[bool] = []
    state, slot = _state_and_slot(tmp_path, seen)
    # Its own task, as a dispatched turn runs, so one turn's trigger cannot reach the next.
    await asyncio.wait_for(
        asyncio.ensure_future(_run_chat(state, slot, "the message", **turn_kwargs)), 60
    )
    return seen


@pytest.mark.asyncio
async def test_a_persons_dashboard_send_releases_as_a_person(tmp_path):
    seen = await _released_as(tmp_path, _directive_user_origin=True)

    assert seen, "the turn never released its session"
    assert seen == [False] * len(seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["cron", "subagent", "autonudge", "app"])
async def test_an_automated_dashboard_turn_releases_as_automation(tmp_path, actor):
    seen = await _released_as(tmp_path, _directive_user_origin=False, _turn_actor=actor)

    assert seen, "the turn never released its session"
    assert seen == [True] * len(seen)

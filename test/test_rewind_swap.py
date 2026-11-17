"""POST /rewind (Edit & Resend) swaps the conversation when sub-agents are attached.

With sub-agent work attached the rewind asks ``swap_conversation`` first, which
keeps that work running on the chat's live process. A session that cannot swap
falls back to the discard the rewind always made, naming why the runs stopped. A
chat with no sub-agents discards exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture(autouse=True)
def _no_real_turn(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_rewind._run_chat", AsyncMock(return_value=None)
    )


@pytest.fixture
def attached(monkeypatch):
    """Drive the shared attached-children predicate the rewind reads."""
    answer = {"value": True}

    async def _probe(state, slot, session_key, operation):
        return answer["value"]

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils_completion.subagents_attached_async", _probe
    )
    return answer


def _slot(state, *, sid: str = "native-1"):
    slot = state.get_or_create_slot("src")
    slot.append("user", "first question", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append(
        "assistant",
        "first answer",
        "msg msg-a",
        ts="2026-05-21T16:00:01Z",
        meta={"native_message_id": "m-1"},
    )
    slot.append("user", "second question", "msg msg-u", ts="2026-05-21T16:00:02Z")
    slot.drain()
    state.sessions._session_map.get = MagicMock(return_value=sid)
    state.sessions._session_map.set_native_fork = MagicMock()
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    return slot


async def _rewind(state):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/src/rewind",
            json={"ts": "2026-05-21T16:00:02Z", "content": "edited"},
        )
        return resp.status, await resp.json()


def _done(slot):
    if slot.task:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_attached_sub_agents_take_the_swap_and_no_discard(tmp_path, attached, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._delete_orphan_kiro_session", AsyncMock())
    state = _make_state(tmp_path)
    slot = _slot(state)
    state.sessions.swap_conversation = AsyncMock(return_value="swapped")

    status, body = await _rewind(state)

    assert status == 200, body
    state.sessions.swap_conversation.assert_awaited_once_with("dashboard:src")
    state.sessions.discard_conversation.assert_not_awaited()
    # The swap already started the fresh conversation: no cold start to fork.
    state.sessions._session_map.set_native_fork.assert_not_called()
    assert [m["content"] for m in slot.messages][-1] == "edited"
    _done(slot)


@pytest.mark.asyncio
async def test_a_session_that_cannot_swap_falls_back_to_the_discard(tmp_path, attached):
    state = _make_state(tmp_path)
    slot = _slot(state, sid="")
    state.sessions.swap_conversation = AsyncMock(return_value="unavailable")

    status, body = await _rewind(state)

    assert status == 200, body
    state.sessions.discard_conversation.assert_awaited_once_with(
        "dashboard:src",
        skip_if_busy=True,
        teardown_reason="the edit could not reuse the chat's process",
    )
    _done(slot)


@pytest.mark.asyncio
async def test_a_busy_session_refuses_the_edit_and_changes_nothing(tmp_path, attached):
    state = _make_state(tmp_path)
    slot = _slot(state, sid="")
    before = list(slot.messages)
    state.sessions.swap_conversation = AsyncMock(return_value="busy")

    status, body = await _rewind(state)

    assert status == 409
    assert body["code"] == "rewind_session_busy"
    state.sessions.discard_conversation.assert_not_awaited()
    assert slot.messages == before


@pytest.mark.asyncio
async def test_no_sub_agents_means_the_discard_as_before(tmp_path, attached):
    attached["value"] = False
    state = _make_state(tmp_path)
    slot = _slot(state, sid="")
    state.sessions.swap_conversation = AsyncMock(return_value="swapped")

    status, body = await _rewind(state)

    assert status == 200, body
    state.sessions.swap_conversation.assert_not_awaited()
    state.sessions.discard_conversation.assert_awaited_once_with("dashboard:src", skip_if_busy=True)
    _done(slot)


@pytest.mark.asyncio
async def test_send_during_sub_agent_probe_keeps_its_task(tmp_path, _floor_monkeypatch):
    state = _make_state(tmp_path)
    slot = _slot(state, sid="")
    before = list(slot.messages)
    state.sessions.swap_conversation = AsyncMock(return_value="busy")
    task = asyncio.create_task(asyncio.Event().wait())

    async def _probe(state, slot, session_key, operation):
        slot.task = task
        return True

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils_completion.subagents_attached_async", _probe
    )
    try:
        status, body = await _rewind(state)

        assert status == 409
        assert slot.task is task
        assert slot.running
        assert body["error"] == "slot is running"
        state.sessions.discard_conversation.assert_not_awaited()
        state.sessions.swap_conversation.assert_not_awaited()
        assert slot.messages == before
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_a_cron_append_at_the_cap_during_the_probe_still_cuts_the_edited_row(
    tmp_path, _floor_monkeypatch
):
    # A cron result appended without the slot lock, at the window cap, trims the
    # front while the probe awaits; the edit must still remove the row it named.
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state._MAX_SLOT_MESSAGES", 3)
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_rewind._delete_orphan_kiro_session", AsyncMock()
    )
    state = _make_state(tmp_path)
    slot = _slot(state)
    state.sessions.swap_conversation = AsyncMock(return_value="swapped")

    async def _probe(state, slot, session_key, operation):
        slot.append("assistant", "cron result", "msg msg-a", ts="2026-05-21T16:00:03Z")
        return True

    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils_completion.subagents_attached_async", _probe
    )

    status, body = await _rewind(state)

    assert status == 200, body
    contents = [m["content"] for m in slot.messages]
    assert "second question" not in contents
    assert contents[-1] == "edited"
    _done(slot)

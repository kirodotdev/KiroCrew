"""Closing a chat that is mid-turn records the turn as interrupted.

Closing a tab cancels its running turn, and the runner's cancel branch keeps the
streamed part of the reply as an assistant row. Without an interruption row after
it, the chat reopened from history reads that partial reply as a finished answer:
no notice and no Resume control. The close now lands the same ``error`` row a
restart lands (``turn_marker.record_close_interruption``), so the reopened chat
reads as interrupted, as it does after a restart.

The turn is the real ``_run_chat`` streaming from a fake client, the close is the
real ``DELETE /api/chat/slots/{slot}`` route, and the transcript is read back from
a fresh ``ConversationLog`` on ``tmp_path``, as reopening the chat from history
does. No cleanup, sweep, prune or reap function is called.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from test_dashboard_chat import _provider_mock

from kiro_crew.acp.types import STOP_REASON_END_TURN
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.slot_persistence.turn_marker import (
    _RESTART_INTERRUPTION_KIND,
    _reconcile_local_turn_marker,
)
from kiro_crew.dashboard.state import is_turn_interrupted
from kiro_crew.history import ConversationLog
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

_PARTIAL = "Part one of the plan is to "


async def _empty_bg_turn(text, allow_image=False):
    yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    # Auto-title runs a background one-liner; its session must be awaitable.
    bg_session = SimpleNamespace(
        served_model="fake-model",
        set_model=AsyncMock(),
        prompt=_empty_bg_turn,
        destroy=AsyncMock(),
    )
    state.sessions.get_bg_session = AsyncMock(return_value=bg_session)
    return state


async def _start_turn(state, *, finish: bool):
    slot = state.get_or_create_slot("s1")
    streamed = asyncio.Event()

    async def _stream(msg):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=_PARTIAL)
        streamed.set()
        if finish:
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
            return
        await asyncio.Event().wait()  # the rest of a long reply

    client = _provider_mock()
    client.context_usage_pct = MagicMock(return_value=10.0)
    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    from kiro_crew.dashboard.chat import _run_chat

    slot.append("user", "explain the plan", "msg msg-u")  # as api_chat does before dispatch
    turn = asyncio.ensure_future(_run_chat(state, slot, "explain the plan"))
    slot.task = turn  # as the dispatcher installs it
    await asyncio.wait_for(streamed.wait(), 10)
    # The runner has handled the chunk once its text is on the slot: a streamed
    # chunk row, or the reply row of a turn that already finished.
    await asyncio.wait_for(_until(lambda: _replied(slot)), 10)
    return slot, turn


def _replied(slot) -> bool:
    return any(m.get("role") in ("chunk", "assistant") for m in slot.messages)


async def _until(condition) -> None:
    while not condition():
        await asyncio.sleep(0.01)


def _archived(tmp_path, slot) -> list[dict]:
    return ConversationLog(base_dir=tmp_path)._read_messages(effective_session_key(slot))


def _errors(rows) -> list[dict]:
    return [r for r in rows if r.get("role") == "error"]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_turn_cut_by_closing_its_tab_reads_as_interrupted(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, turn = await _start_turn(state, finish=False)

    async with TestClient(TestServer(_make_app(state))) as http:
        resp = await http.delete("/api/chat/slots/s1")
        assert resp.status == 200
        await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 20)

    rows = _archived(tmp_path, slot)
    assert any(r.get("role") == "assistant" and _PARTIAL in r.get("content", "") for r in rows)
    assert is_turn_interrupted(rows), (
        "a turn cut off by closing its tab is archived as a finished turn: "
        f"{[(r.get('role'), str(r.get('content', ''))[:40]) for r in rows[-3:]]}"
    )
    assert [e.get("meta", {}).get("kind") for e in _errors(rows)] == ["chat_close_interruption"]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_closing_a_tab_after_a_finished_turn_archives_it_unchanged(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, turn = await _start_turn(state, finish=True)
    await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 20)

    async with TestClient(TestServer(_make_app(state))) as http:
        assert (await http.delete("/api/chat/slots/s1")).status == 200

    rows = _archived(tmp_path, slot)
    assert not is_turn_interrupted(rows)
    assert _errors(rows) == []


def test_a_restart_interrupted_turn_is_recorded_as_before():
    slot = _make_state_slot()
    slot.append("user", "explain the plan", "msg msg-u", broadcast=False)
    slot.append("assistant", _PARTIAL, "msg msg-a", broadcast=False)

    assert _reconcile_local_turn_marker(slot, 1)

    assert [e.get("meta", {}).get("kind") for e in _errors(slot.messages)] == [
        _RESTART_INTERRUPTION_KIND
    ]


def test_no_close_row_when_the_turn_already_reads_as_interrupted_or_stopped():
    from kiro_crew.dashboard.slot_persistence.turn_marker import record_close_interruption

    unanswered = _make_state_slot()
    unanswered.append("user", "explain the plan", "msg msg-u", broadcast=False)
    assert not record_close_interruption(unanswered)

    stopped = _make_state_slot()
    stopped.append("user", "explain the plan", "msg msg-u", broadcast=False)
    stopped.append("assistant", _PARTIAL, "msg msg-a", broadcast=False)
    stopped.append("system", "Stopped", "msg", broadcast=False, meta={"kind": "stop_event"})
    assert not record_close_interruption(stopped)
    assert _errors(stopped.messages) == []


def _make_state_slot():
    from kiro_crew.dashboard.state import _ChatSlot

    return _ChatSlot("s1")

"""A side conversation's close reaches every tab, and a run it closed says nothing more.

``POST .../side/close`` drops the sidecar. Every tab hears it on ``chat.side_closed``,
which names the tab that asked, so another tab does not keep rows the model has
dropped. A run still going when its sidecar is dropped, or dropped and replaced, sends no
further ``chat.side_result`` frame: every tab cleared that conversation, and a reopened
one is not the run's.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from side_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _capture_broadcasts,
    _make_side_app,
    _published_readonly_spec_fixture,
)

from kiro_crew.dashboard.handlers.side import (
    _requeue_unconsumed_side_steers,
    _run_side_turn,
)
from kiro_crew.dashboard.side_state import SideState


def _closed_frames(events: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [d for t, d in events if t == "chat.side_closed"]


def _side_frames(events: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [d for t, d in events if t == "chat.side_result"]


@pytest.mark.asyncio
async def test_a_close_tells_every_tab_and_names_the_tab_that_asked(tmp_path):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    state.sessions.destroy = AsyncMock()

    async with TestClient(TestServer(_make_side_app(state))) as client:
        asked = await client.post("/api/chat/slots/parent/side/close", json={"client": "tab-a"})
        bare = await client.post("/api/chat/slots/parent/side/close")

    assert asked.status == 200 and bare.status == 200
    # The second close finds nothing open and still tells every tab, so one that kept a
    # conversation the gateway lost in a restart clears it.
    assert _closed_frames(events) == [
        {"kind": "side", "slot": "parent", "origin_client": "tab-a"},
        {"kind": "side", "slot": "parent"},
    ]


@pytest.mark.asyncio
async def test_close_names_every_dropped_queue_entry_in_queue_order(tmp_path):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    ordinary_id = parent._side.queue_append("ordinary question")
    assert ordinary_id is not None
    parent._side.last_run_id = "run-1"
    steer_id = parent._side.steer_register("try this next")
    assert steer_id is not None
    state.sessions.destroy = AsyncMock()

    _requeue_unconsumed_side_steers(state, parent, "run-1")
    requeued_id = parent._side.queue[0]["id"]
    async with TestClient(TestServer(_make_side_app(state))) as client:
        response = await client.post("/api/chat/slots/parent/side/close")

    assert response.status == 200
    assert _closed_frames(events) == [
        {
            "kind": "side",
            "slot": "parent",
            "dropped": [
                {"queue_id": requeued_id, "steer_id": steer_id},
                {"queue_id": ordinary_id},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_close_omits_dropped_for_an_empty_queue(tmp_path):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    state.sessions.destroy = AsyncMock()

    async with TestClient(TestServer(_make_side_app(state))) as client:
        response = await client.post("/api/chat/slots/parent/side/close")

    assert response.status == 200
    assert _closed_frames(events) == [{"kind": "side", "slot": "parent"}]


@web.middleware
async def _as_the_notes_app(request: web.Request, handler):
    """Stand in for the token middleware: the app ``notes`` is asking."""
    request["app"] = "notes"
    return await handler(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["other-app", ""], ids=["another-apps-slot", "unscoped-slot"])
async def test_a_close_from_an_app_that_does_not_own_the_slot_is_refused_and_tells_no_tab(
    tmp_path, owner
):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._app = owner
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    state.sessions.destroy = AsyncMock()
    app = _make_side_app(state)
    app.middlewares.append(_as_the_notes_app)

    async with TestClient(TestServer(app)) as client:
        refused = await client.post("/api/chat/slots/parent/side/close", json={"client": "tab-a"})
        missing = await client.post("/api/chat/slots/no-such-slot/side/close")
        refusal = (refused.status, await refused.json())
        absence = (missing.status, await missing.json())

    # The refusal reads exactly like a slot that does not exist, so the app learns nothing.
    assert refusal == absence and refusal[0] == 404
    assert _closed_frames(events) == []
    assert parent._side is not None and parent._side.open
    state.sessions.destroy.assert_not_awaited()


def _running_side(state):
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q", run_id="run-1")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    return parent


@pytest.mark.asyncio
@pytest.mark.parametrize("reopened", [False, True], ids=["closed", "closed-and-reopened"])
async def test_a_run_whose_conversation_closed_sends_nothing_more(tmp_path, monkeypatch, reopened):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _running_side(state)

    async def _closed_mid_answer(provider, message, *, on_chunk=None, **kwargs):
        on_chunk("before the close ")
        parent._side = SideState(open=True) if reopened else None
        on_chunk("after the close")
        return "before the close after the close"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _closed_mid_answer)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    assert [frame["content"] for frame in _side_frames(events)] == ["before the close "]


@pytest.mark.asyncio
async def test_a_run_whose_conversation_closed_sends_no_error_notice(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _running_side(state)

    async def _failed_after_the_close(provider, message, *, on_chunk=None, **kwargs):
        parent._side = SideState(open=True)
        raise RuntimeError("the backend went away")

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect", _failed_after_the_close
    )

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    assert _side_frames(events) == []


@pytest.mark.asyncio
async def test_a_run_whose_conversation_stayed_open_still_sends_its_answer(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = _running_side(state)

    async def _answered(provider, message, *, on_chunk=None, **kwargs):
        on_chunk("the answer")
        return "the answer"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _answered)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    frames = _side_frames(events)
    # The stream redactor holds back the unfinished last word until the final frame.
    assert [(frame["content"], frame.get("final", False)) for frame in frames] == [
        ("the ", False),
        ("the answer", True),
    ]

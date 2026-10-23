"""``GET /api/chat/slots/{slot}/side``: what a reloaded Side Chat panel restores from.

The read returns the sidecar the gateway holds in memory: each row under the id
the frame that announced it carried, the queue, and whether a turn is running.
It is owner-only for every caller, like the side frames, and every text is
redacted the way those frames redact it.
"""

from __future__ import annotations

import asyncio
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

from kiro_crew.dashboard.handlers.side import _run_side_turn, api_side_queue_edit
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.llm_helpers import PromptBusyExhaustedError
from kiro_crew.testing.wait import async_wait_until, await_until

_RAW_CREDENTIAL = "AKIAIOSFODNN7EXAMPLE"


@web.middleware
async def _as_caller(request: web.Request, handler):
    """Stand in for the token middleware: ``X-Test-Caller`` names who is asking.
    ``anonymous`` sets no identity at all."""
    who = request.headers.get("X-Test-Caller", "owner")
    if who != "anonymous":
        request["user"] = "someone-else" if who == "stranger" else "local-app"
        request["app"] = "notes" if who == "app" else ""
    return await handler(request)


def _app(state) -> web.Application:
    app = _make_side_app(state)
    app.middlewares.append(_as_caller)
    return app


async def _read(state, slot: str = "parent", caller: str = "owner") -> tuple[int, Any]:
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{slot}/side", headers={"X-Test-Caller": caller})
        return resp.status, await resp.json()


def _side_frames(events: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [d for t, d in events if t == "chat.side_result"]


@pytest.mark.asyncio
async def test_the_read_returns_the_question_queue_and_running_turn(tmp_path, monkeypatch):
    """Mid-turn, the read holds the question under its frame's id, the queue, and
    ``running``, so a panel reloaded now offers steer and queue as a submit gets."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await release.wait()
        finally:
            if slot._side is not None and slot._side.last_run_id == run_id:
                slot._side.is_complete = True
                slot._side.task = None

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot = state.get_or_create_slot("parent")

    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        first = await (
            await client.post("/api/chat/slots/parent/side/turn", json={"question": "first?"})
        ).json()
        await await_until(started)
        queued = await (
            await client.post("/api/chat/slots/parent/side/turn", json={"question": "second?"})
        ).json()
        resp = await client.get("/api/chat/slots/parent/side")
        body = await resp.json()
        release.set()
        await async_wait_until(lambda: slot._side.is_complete)

    assert resp.status == 200
    assert body["open"] is True
    assert body["running"] is True
    assert body["last_run_id"] == first["run_id"]
    [row] = body["messages"]
    assert (row["role"], row["content"], row["run_id"]) == ("user", "first?", first["run_id"])
    [question_frame] = [d for d in _side_frames(events) if d["role"] == "user"]
    assert row["id"] and row["id"] == question_frame["message_id"]
    assert body["queue"] == [
        {
            "id": queued["queue_id"],
            "content": "second?",
            "ts": slot._side.queue[0]["ts"],
            "rev": 0,
        }
    ]


@pytest.mark.asyncio
async def test_the_read_and_the_edit_frame_carry_the_entry_revision(tmp_path, monkeypatch):
    """Every edit bumps a queued entry's ``rev``, and the read and the edit frame
    report the same count, so a reloaded panel holding an edit frame and a read's
    copy of the entry can tell which one is newer."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await release.wait()
        finally:
            if slot._side is not None and slot._side.last_run_id == run_id:
                slot._side.is_complete = True
                slot._side.task = None

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot = state.get_or_create_slot("parent")
    app = _app(state)
    app.router.add_patch("/api/chat/slots/{slot}/side/queue/{queue_id}", api_side_queue_edit)

    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "first?"})
        await await_until(started)
        queued = await (
            await client.post("/api/chat/slots/parent/side/turn", json={"question": "second?"})
        ).json()
        before = await (await client.get("/api/chat/slots/parent/side")).json()
        for text in ("second, edited", "second, edited again"):
            resp = await client.patch(
                f"/api/chat/slots/parent/side/queue/{queued['queue_id']}", json={"content": text}
            )
            assert resp.status == 200, await resp.json()
        after = await (await client.get("/api/chat/slots/parent/side")).json()
        release.set()
        await async_wait_until(lambda: slot._side.is_complete)

    assert [entry["rev"] for entry in before["queue"]] == [0]
    assert [(entry["content"], entry["rev"]) for entry in after["queue"]] == [
        ("second, edited again", 2)
    ]
    edits = [d for t, d in events if t == "chat.side_queue" and d.get("action") == "edit"]
    assert [(d["content"], d["rev"]) for d in edits] == [
        ("second, edited", 1),
        ("second, edited again", 2),
    ]


@pytest.mark.asyncio
async def test_a_steer_and_an_answer_are_read_back_under_their_frame_ids(tmp_path, monkeypatch):
    """The steer chip and the final answer each carry the id of the row they
    stored, and the read returns both rows under those ids with their run."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q", run_id="run-1")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False
    parent._side.steer_register("also this")

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, on_steer_consumed=None, **kwargs):
        on_chunk("the answer")
        on_steer_consumed("also this")
        return "the answer"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)
    status, body = await _read(state)

    assert status == 200
    assert body["running"] is False
    question, steer, answer = body["messages"]
    assert question["content"] == "q" and question["run_id"] == "run-1"
    [steer_frame] = [d for d in _side_frames(events) if d.get("steer")]
    assert steer["steer"] is True and steer["run_id"] == "run-1"
    assert steer["id"] and steer["id"] == steer_frame["message_id"]
    [final_frame] = [d for d in _side_frames(events) if d.get("final")]
    assert (answer["role"], answer["content"], answer["run_id"]) == (
        "assistant",
        "the answer",
        "run-1",
    )
    assert answer["id"] and answer["id"] == final_frame["message_id"]
    assert "is_error" not in answer


@pytest.mark.asyncio
async def test_a_stopped_turn_is_read_back_as_the_notice_its_frame_drew(tmp_path, monkeypatch):
    """The stop row is stored as an error notice, so a reloaded panel draws it as
    the live one did, under the id the terminal frame carried."""
    started = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if slot._side is not None and slot._side.last_run_id == run_id:
                slot._side.is_complete = True
                slot._side.task = None
            raise

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    async with TestClient(TestServer(_app(state))) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "hangs?"})
        await await_until(started)
        await client.post("/api/chat/slots/parent/side/stop", json={})
        body = await (await client.get("/api/chat/slots/parent/side")).json()

    assert body["running"] is False
    stopped = body["messages"][-1]
    assert stopped["content"] == "(side response stopped)" and stopped["is_error"] is True
    [terminal] = [d for d in _side_frames(events) if d.get("final")]
    assert stopped["id"] and stopped["id"] == terminal["message_id"]
    assert stopped["run_id"] == terminal["run_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", ["partial ans", ""], ids=["some-text", "no-text"])
async def test_an_interrupted_turn_is_read_back_as_its_frames_drew_it(
    tmp_path, monkeypatch, streamed
):
    """An interrupted turn streams some text, then a notice. The read returns the
    text (never an empty row when none streamed) followed by the notice, under the
    ids their final frames carried, so a reloaded panel draws the same rows."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
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

    async def _interrupted(provider, message, *, on_chunk=None, **kwargs):
        if streamed:
            on_chunk(streamed)
        raise PromptBusyExhaustedError("busy")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _interrupted)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)
    status, body = await _read(state)

    assert status == 200
    rows = [(m["role"], m["content"], m.get("is_error", False)) for m in body["messages"]]
    expected_partial = [("assistant", streamed, False)] if streamed else []
    notice = "(side conversation interrupted — please retry)"
    assert rows == [("user", "q", False), *expected_partial, ("assistant", notice, True)]
    final_frames = [d for d in _side_frames(events) if d.get("final")]
    if streamed:
        partial_row, notice_row = body["messages"][-2:]
        assert len(final_frames) == 2
        assert final_frames[0]["content"] == streamed
        assert final_frames[0].get("is_error", False) is False
        assert final_frames[0]["message_id"] == partial_row["id"]
        assert final_frames[1]["content"] == notice
        assert final_frames[1]["is_error"] is True
        assert final_frames[1]["message_id"] == notice_row["id"]
    else:
        [notice_frame] = final_frames
        assert notice_frame["content"] == notice
        assert notice_frame["is_error"] is True
        assert notice_frame["message_id"] == body["messages"][-1]["id"]


@pytest.mark.asyncio
async def test_an_interrupted_turn_sends_no_partial_answer_into_a_reopened_conversation(
    tmp_path, monkeypatch
):
    """A conversation closed and reopened while a turn was unwinding holds none of
    that turn's rows, so the turn's partial answer is not broadcast into it."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
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

    async def _interrupted_after_reopen(provider, message, *, on_chunk=None, **kwargs):
        on_chunk("partial ans")
        parent._side = SideState(open=True, created_at="2026-01-01T00:01:00Z")
        raise PromptBusyExhaustedError("busy")

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect", _interrupted_after_reopen
    )

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    assert parent._side.messages == []
    final_frames = [d for d in _side_frames(events) if d.get("final")]
    assert [frame["content"] for frame in final_frames] == [
        "(side conversation interrupted — please retry)"
    ]


@pytest.mark.asyncio
async def test_the_read_redacts_rows_and_queue_like_the_frames(tmp_path):
    """A question and a queued entry are stored raw; the read scrubs both, as the
    frames that announced them did."""
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True)
    parent._side.append_user(f"my key is {_RAW_CREDENTIAL}", run_id="run-1")
    parent._side.queue_append(f"and {_RAW_CREDENTIAL} again")

    status, body = await _read(state)

    assert status == 200
    texts = [body["messages"][0]["content"], body["queue"][0]["content"]]
    for text in texts:
        assert _RAW_CREDENTIAL not in text
        assert "[REDACTED" in text


@pytest.mark.asyncio
async def test_no_open_side_conversation_reads_as_closed(tmp_path):
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")

    assert await _read(state) == (200, {"open": False})

    parent._side = SideState(open=False)
    assert await _read(state) == (200, {"open": False})


@pytest.mark.asyncio
async def test_only_the_owner_reads_a_side_conversation(tmp_path):
    """The frames reach owner sockets only, so the read refuses everyone else:
    a request with no identity, another dashboard user, and an app even on a slot it
    owns. An app on a slot it does not own gets the uniform not-found."""
    state = _make_state(tmp_path)
    mine = state.get_or_create_slot("parent")
    mine._side = SideState(open=True)
    mine._side.append_user("private question", run_id="run-1")
    owned_by_app = state.get_or_create_slot("app-parent")
    owned_by_app._app = "notes"
    owned_by_app._side = SideState(open=True)
    owned_by_app._side.append_user("asked on the app's slot", run_id="run-2")

    anonymous_status, anonymous_body = await _read(state, caller="anonymous")
    stranger_status, stranger_body = await _read(state, caller="stranger")
    app_own_status, app_own_body = await _read(state, slot="app-parent", caller="app")
    app_foreign_status, app_foreign_body = await _read(state, caller="app")
    missing_status, _ = await _read(state, slot="nope")

    assert anonymous_status == 403 and anonymous_body.get("code") == "owner_only"
    assert stranger_status == 403 and stranger_body.get("code") == "owner_only"
    assert app_own_status == 403 and app_own_body.get("code") == "owner_only"
    assert app_foreign_status == 404
    assert missing_status == 404
    for body in (anonymous_body, stranger_body, app_own_body, app_foreign_body):
        assert "messages" not in body


# (last_run_id, is_complete, is_stopping): whether a submit meets a running turn.
_TURN_STATES = [
    pytest.param("", True, False, False, id="no-run-yet"),
    pytest.param("", False, False, False, id="no-run-id"),
    pytest.param("run-1", False, False, True, id="running"),
    pytest.param("run-1", True, False, False, id="finished"),
    pytest.param("run-1", True, True, True, id="stop-still-settling"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("last_run_id", "is_complete", "is_stopping", "in_flight"), _TURN_STATES)
async def test_the_read_reports_running_exactly_when_a_submit_would_queue(
    tmp_path, monkeypatch, last_run_id, is_complete, is_stopping, in_flight
):
    """``running`` on the read is the gate a submit meets (``turn_in_flight``), so
    a restored panel offers steer and queue exactly when a submit would get them."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", AsyncMock())
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(
        open=True, last_run_id=last_run_id, is_complete=is_complete, is_stopping=is_stopping
    )

    assert parent._side.turn_in_flight is in_flight
    async with TestClient(TestServer(_app(state))) as client:
        read = await (await client.get("/api/chat/slots/parent/side")).json()
        submit = await (
            await client.post("/api/chat/slots/parent/side/turn", json={"question": "next"})
        ).json()
        await async_wait_until(lambda: not state._background_tasks)

    assert read["running"] is in_flight
    assert bool(submit.get("queued")) is in_flight

"""Tests for queue edit feature.

Covers:
- _ChatSlot.queue_edit_by_id helper
- _edit_queued_by_id messages helper
- PATCH /api/chat/slots/{slot}/queue/{queue_id} endpoint
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_queue_edit
from kiro_crew.dashboard.chat_utils import _edit_queued_by_id
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ── Unit tests: _ChatSlot.queue_edit_by_id ──


class TestQueueEditHelper:
    def test_edit_by_id_found(self):
        slot = _ChatSlot("s1")
        slot.queue_append("keep")
        qid = slot.queue_append("old text")
        slot.queue_append("also keep")
        assert slot.queue_edit_by_id(qid, "new text") is True
        assert [q["content"] for q in slot._queue] == ["keep", "new text", "also keep"]

    def test_edit_by_id_preserves_order_and_id(self):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("old")
        slot.queue_edit_by_id(qid, "new")
        assert slot._queue[0]["id"] == qid
        assert slot._queue[0]["content"] == "new"

    def test_edit_by_id_not_found(self):
        slot = _ChatSlot("s1")
        slot.queue_append("msg")
        assert slot.queue_edit_by_id("nonexistent", "x") is False
        assert slot._queue[0]["content"] == "msg"

    def test_edit_by_id_empty_queue(self):
        slot = _ChatSlot("s1")
        assert slot.queue_edit_by_id("anything", "x") is False

    def test_edit_by_id_duplicate_content(self):
        """Two items with same content — only the matching ID is edited."""
        slot = _ChatSlot("s1")
        id1 = slot.queue_append("same")
        id2 = slot.queue_append("same")
        slot.queue_edit_by_id(id2, "changed")
        assert slot._queue[0] == {"id": id1, "content": "same", "kind": ""}
        assert slot._queue[1] == {"id": id2, "content": "changed", "kind": ""}


class TestEditQueuedMessagesHelper:
    def test_edit_placeholder_content(self):
        messages = [
            {"role": "user", "content": "hi", "cls": "msg msg-u"},
            {"role": "queued", "content": "old", "cls": json.dumps({"queue_id": "q1"})},
        ]
        assert _edit_queued_by_id(messages, "q1", "new") is True
        assert messages[1]["content"] == "new"

    def test_edit_placeholder_not_found(self):
        messages = [{"role": "queued", "content": "old", "cls": json.dumps({"queue_id": "q1"})}]
        assert _edit_queued_by_id(messages, "q2", "new") is False
        assert messages[0]["content"] == "old"

    def test_edit_ignores_non_queued(self):
        messages = [{"role": "user", "content": "old", "cls": json.dumps({"queue_id": "q1"})}]
        assert _edit_queued_by_id(messages, "q1", "new") is False
        assert messages[0]["content"] == "old"


# ── API tests: PATCH /api/chat/slots/{slot}/queue/{queue_id} ──


def _make_state():
    state = DashboardState.__new__(DashboardState)
    state._slots = {}
    state._ws_clients = []
    state._sse_queues = []
    state._notify_event = MagicMock()
    state._background_tasks = set()
    state._yolo = False
    state._yolo_expires_at = 0.0
    state._restricted_keys = set()
    state.sessions = None
    state.conversation_log = None
    state.channel_manager = None
    return state


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_patch(
        "/api/chat/slots/{slot}/queue/{queue_id}",
        api_chat_slot_queue_edit,
    )
    return app


class TestQueueEditEndpoint:
    @pytest.mark.asyncio
    async def test_edit_updates_queue_and_messages(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old text")
        slot.append("queued", "old text", json.dumps({"queue_id": qid}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}",
                    json={"content": "new text"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert "new text" in data["content"]

        assert slot._queue[0]["content"] == "new text"
        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert len(queued) == 1
        assert queued[0]["content"] == "new text"

    @pytest.mark.asyncio
    async def test_edit_slot_not_found(self):
        state = _make_state()
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/nope/queue/abc", json={"content": "x"}
                )
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_edit_queue_id_not_found(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        slot.queue_append("keep")
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-1/queue/wrong", json={"content": "x"}
                )
                assert resp.status == 404
        assert slot._queue[0]["content"] == "keep"

    @pytest.mark.asyncio
    async def test_edit_empty_content_rejected(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old")
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "   "}
                )
                assert resp.status == 400
        assert slot._queue[0]["content"] == "old"

    @pytest.mark.asyncio
    async def test_edit_invalid_json(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old")
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}",
                    data="not json",
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400

    @pytest.mark.asyncio
    async def test_edit_broadcasts_ws_event(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("old")
        state.broadcast_ws = MagicMock()

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}", json={"content": "new"}
                )

        state.broadcast_ws.assert_any_call(
            "queue_edit",
            {"slot": "chat-1", "queue_id": qid, "content": "new"},
        )

    @pytest.mark.asyncio
    async def test_edit_duplicate_content_targets_correct_item(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("same")
        id2 = slot.queue_append("same")
        slot.append("queued", "same", json.dumps({"queue_id": id1}))
        slot.append("queued", "same", json.dumps({"queue_id": id2}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{id2}", json={"content": "edited"}
                )
                assert resp.status == 200

        assert slot._queue[0] == {"id": id1, "content": "same", "kind": ""}
        assert slot._queue[1] == {"id": id2, "content": "edited", "kind": ""}

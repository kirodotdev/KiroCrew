"""Tests for queue cancel feature.

Covers:
- _ChatSlot queue helper methods (queue_append, queue_insert, queue_pop, queue_remove_by_id)
- DELETE /api/chat/slots/{slot}/queue/{queue_id} endpoint
- Queue ID propagation in queue_push/queue_pop WS events
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_queue_cancel
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ── Unit tests: _ChatSlot queue helpers ──


class TestQueueHelpers:
    def test_queue_append_returns_id(self):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("hello")
        assert isinstance(qid, str)
        assert len(qid) == 12
        assert len(slot._queue) == 1
        assert slot._queue[0] == {"id": qid, "content": "hello", "kind": ""}

    def test_queue_append_unique_ids(self):
        slot = _ChatSlot("s1")
        id1 = slot.queue_append("a")
        id2 = slot.queue_append("b")
        assert id1 != id2

    def test_queue_insert_at_front(self):
        slot = _ChatSlot("s1")
        slot.queue_append("second")
        qid = slot.queue_insert(0, "first")
        assert slot._queue[0]["content"] == "first"
        assert slot._queue[0]["id"] == qid
        assert slot._queue[1]["content"] == "second"

    def test_queue_pop_returns_dict(self):
        slot = _ChatSlot("s1")
        qid = slot.queue_append("msg")
        item = slot.queue_pop(0)
        assert item == {"id": qid, "content": "msg", "kind": ""}
        assert len(slot._queue) == 0

    def test_queue_pop_fifo(self):
        slot = _ChatSlot("s1")
        slot.queue_append("first")
        slot.queue_append("second")
        item = slot.queue_pop(0)
        assert item["content"] == "first"
        assert slot._queue[0]["content"] == "second"

    def test_discard_callback_runs_only_for_discard_not_consumption(self):
        slot = _ChatSlot("s1")
        discarded: list[str] = []
        consumed_id = slot.queue_insert(
            0, "consume", on_discarded=lambda: discarded.append("consumed")
        )
        discarded_id = slot.queue_insert(
            1, "discard", on_discarded=lambda: discarded.append("discarded")
        )

        assert slot.queue_pop(0)["id"] == consumed_id
        assert discarded == []
        assert slot.queue_remove_by_id(discarded_id) == "discard"
        assert discarded == ["discarded"]

    def test_bulk_discard_retires_each_callback_once(self):
        slot = _ChatSlot("s1")
        discarded: list[str] = []
        slot.queue_insert(0, "one", on_discarded=lambda: discarded.append("one"))
        slot.queue_insert(1, "two", on_discarded=lambda: discarded.append("two"))

        removed = slot.queue_discard_all()

        assert [item["content"] for item in removed] == ["one", "two"]
        assert discarded == ["one", "two"]
        assert slot._queue == []
        slot.discard_queue_entries(removed)
        assert discarded == ["one", "two"], "callbacks must be one-shot"

    def test_discard_callback_failure_does_not_restore_the_row(self):
        slot = _ChatSlot("s1")

        def _reject() -> None:
            raise RuntimeError("retirement unavailable")

        queue_id = slot.queue_insert(0, "discard", on_discarded=_reject)

        assert slot.queue_remove_by_id(queue_id) == "discard"
        assert slot._queue == []

    def test_capacity_eviction_skips_callback_owned_rows(self):
        slot = _ChatSlot("s1")
        discarded: list[str] = []
        owned_id = slot.queue_insert(
            0,
            "completion",
            on_consumed=lambda _consumed: None,
            on_discarded=lambda: discarded.append("completion"),
        )
        ordinary_id = slot.queue_append("ordinary")

        assert slot.queue_evict_oldest_if_unowned() is None
        assert [item["id"] for item in slot._queue] == [owned_id, ordinary_id]
        assert discarded == []

    def test_capacity_eviction_removes_an_unowned_head(self):
        slot = _ChatSlot("s1")
        discarded: list[str] = []
        ordinary_id = slot.queue_append("ordinary")
        owned_id = slot.queue_insert(
            1,
            "completion",
            on_discarded=lambda: discarded.append("completion"),
        )

        evicted = slot.queue_evict_oldest_if_unowned()

        assert evicted is not None
        assert evicted["id"] == ordinary_id
        assert [item["id"] for item in slot._queue] == [owned_id]
        assert discarded == []

    def test_capacity_eviction_refuses_when_every_row_is_owned(self):
        slot = _ChatSlot("s1")
        discarded: list[str] = []
        owned_id = slot.queue_insert(
            0,
            "completion",
            on_discarded=lambda: discarded.append("completion"),
        )

        assert slot.queue_evict_oldest_if_unowned() is None
        assert [item["id"] for item in slot._queue] == [owned_id]
        assert discarded == []

    def test_queue_remove_by_id_found(self):
        slot = _ChatSlot("s1")
        slot.queue_append("keep")
        qid = slot.queue_append("remove me")
        slot.queue_append("also keep")
        content = slot.queue_remove_by_id(qid)
        assert content == "remove me"
        assert len(slot._queue) == 2
        assert [q["content"] for q in slot._queue] == ["keep", "also keep"]

    def test_queue_remove_by_id_not_found(self):
        slot = _ChatSlot("s1")
        slot.queue_append("msg")
        result = slot.queue_remove_by_id("nonexistent")
        assert result is None
        assert len(slot._queue) == 1

    def test_queue_remove_by_id_empty_queue(self):
        slot = _ChatSlot("s1")
        result = slot.queue_remove_by_id("anything")
        assert result is None

    def test_queue_remove_by_id_duplicate_content(self):
        """When two items have the same content, only the one with matching ID is removed."""
        slot = _ChatSlot("s1")
        id1 = slot.queue_append("same text")
        id2 = slot.queue_append("same text")
        content = slot.queue_remove_by_id(id2)
        assert content == "same text"
        assert len(slot._queue) == 1
        assert slot._queue[0]["id"] == id1


# ── API tests: DELETE /api/chat/slots/{slot}/queue/{queue_id} ──


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
    app.router.add_delete(
        "/api/chat/slots/{slot}/queue/{queue_id}",
        api_chat_slot_queue_cancel,
    )
    return app


class TestQueueCancelEndpoint:
    @pytest.mark.asyncio
    async def test_cancel_removes_from_queue(self):
        """Cancelling a queued message removes it from the backend queue."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("cancel me")
        slot.append("queued", "cancel me", json.dumps({"queue_id": qid}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete(f"/api/chat/slots/chat-1/queue/{qid}")
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert "cancel me" in data["content"]

        assert len(slot._queue) == 0
        # Queued message should also be removed from messages
        assert not any(m["role"] == "queued" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_cancel_slot_not_found(self):
        state = _make_state()
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/chat/slots/nonexistent/queue/abc")
                assert resp.status == 404

    @pytest.mark.asyncio
    async def test_cancel_queue_id_not_found(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        slot.queue_append("keep me")

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete("/api/chat/slots/chat-1/queue/wrong-id")
                assert resp.status == 404
                data = await resp.json()
                assert "not found" in data["error"]

        # Queue should be untouched
        assert len(slot._queue) == 1

    @pytest.mark.asyncio
    async def test_cancel_middle_item(self):
        """Cancelling a middle item preserves order of remaining items."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        slot.queue_append("first")
        qid2 = slot.queue_append("second")
        slot.queue_append("third")

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete(f"/api/chat/slots/chat-1/queue/{qid2}")
                assert resp.status == 200

        assert [q["content"] for q in slot._queue] == ["first", "third"]

    @pytest.mark.asyncio
    async def test_cancel_broadcasts_ws_event(self):
        """Cancelling broadcasts a queue_cancel WS event."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append("cancel me")
        state.broadcast_ws = MagicMock()

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                await client.delete(f"/api/chat/slots/chat-1/queue/{qid}")

        state.broadcast_ws.assert_any_call(
            "queue_cancel",
            {"slot": "chat-1", "queue_id": qid, "content": "cancel me"},
        )

    @pytest.mark.asyncio
    async def test_cancel_with_duplicate_content(self):
        """When two messages have identical content, only the targeted one is removed."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("same text")
        id2 = slot.queue_append("same text")
        # Add queued placeholders with queue_id in cls metadata
        slot.append("queued", "same text", json.dumps({"queue_id": id1}))
        slot.append("queued", "same text", json.dumps({"queue_id": id2}))

        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.delete(f"/api/chat/slots/chat-1/queue/{id2}")
                assert resp.status == 200

        assert len(slot._queue) == 1
        assert slot._queue[0]["id"] == id1
        # The first placeholder (id1) should remain, second (id2) removed
        queued_msgs = [m for m in slot.messages if m.get("role") == "queued"]
        assert len(queued_msgs) == 1
        cls = json.loads(queued_msgs[0].get("cls", "{}"))
        assert cls.get("queue_id") == id1

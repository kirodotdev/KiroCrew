"""Tests for queue reorder feature.

Covers:
- PUT /api/chat/slots/{slot}/queue/order endpoint
- Reordering the _queue list and queued messages in sync
- Error cases (missing slot, invalid payload, unknown IDs)
- WS broadcast of queue_reorder event
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_queue_reorder
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

# ── Unit tests: reorder on _ChatSlot._queue ──


class TestQueueReorderUnit:
    def test_reorder_reverses_queue(self):
        slot = _ChatSlot("s1")
        id1 = slot.queue_append("first")
        id2 = slot.queue_append("second")
        id3 = slot.queue_append("third")
        # Reverse order
        by_id = {item["id"]: item for item in slot._queue}
        slot._queue[:] = [by_id[id3], by_id[id2], by_id[id1]]
        assert [q["content"] for q in slot._queue] == ["third", "second", "first"]

    def test_reorder_partial_ids_puts_remaining_at_end(self):
        slot = _ChatSlot("s1")
        id1 = slot.queue_append("a")
        id2 = slot.queue_append("b")  # noqa: F841
        id3 = slot.queue_append("c")
        # Only reorder id3, id1 — id2 should go to end
        by_id = {item["id"]: item for item in slot._queue}
        order = [id3, id1]
        reordered = [by_id[qid] for qid in order]
        remaining = [item for item in slot._queue if item["id"] not in set(order)]
        slot._queue[:] = reordered + remaining
        assert [q["content"] for q in slot._queue] == ["c", "a", "b"]


# ── API tests: PUT /api/chat/slots/{slot}/queue/order ──


def _make_state():
    state = DashboardState.__new__(DashboardState)
    state._slots = {}
    state._ws_clients = []
    state._sse_queues = []
    state._notify_event = MagicMock()
    state._background_tasks = set()
    # NOTE: the fork moved yolo state into the safety_override module; _yolo is a
    # property delegating there (no _yolo_expires_at attr) so we leave it default.
    state._restricted_keys = set()
    state.sessions = None
    state.conversation_log = None
    state.channel_manager = None
    return state


def _make_app(state):
    app = web.Application()
    app["state"] = state
    app.router.add_put(
        "/api/chat/slots/{slot}/queue/order",
        api_chat_slot_queue_reorder,
    )
    return app


class TestQueueReorderEndpoint:
    @pytest.mark.asyncio
    async def test_reorder_success(self):
        """Reordering reverses the queue and returns ok."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("first")
        id2 = slot.queue_append("second")
        id3 = slot.queue_append("third")
        # Add queued messages to match
        slot.append("queued", "first", json.dumps({"queue_id": id1}))
        slot.append("queued", "second", json.dumps({"queue_id": id2}))
        slot.append("queued", "third", json.dumps({"queue_id": id3}))

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": [id3, id1, id2]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

        assert [q["content"] for q in slot._queue] == ["third", "first", "second"]

    @pytest.mark.asyncio
    async def test_reorder_messages_match_queue(self):
        """Queued messages in slot.messages are reordered to match _queue."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        # Add a non-queued message first
        slot.append("user", "hello", "")
        id1 = slot.queue_append("a")
        id2 = slot.queue_append("b")
        slot.append("queued", "a", json.dumps({"queue_id": id1}))
        slot.append("queued", "b", json.dumps({"queue_id": id2}))

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": [id2, id1]},
            )
            assert resp.status == 200

        queued = [m for m in slot.messages if m.get("role") == "queued"]
        cls0 = json.loads(queued[0]["cls"])
        cls1 = json.loads(queued[1]["cls"])
        assert cls0["queue_id"] == id2
        assert cls1["queue_id"] == id1
        # Non-queued message is preserved
        assert slot.messages[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_reorder_slot_not_found(self):
        state = _make_state()
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/nonexistent/queue/order",
                json={"order": ["abc"]},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reorder_invalid_json(self):
        state = _make_state()
        state.get_or_create_slot("chat-1")
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_reorder_order_not_list(self):
        state = _make_state()
        state.get_or_create_slot("chat-1")
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": "not-a-list"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "list" in data["error"]

    @pytest.mark.asyncio
    async def test_reorder_unknown_id(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        slot.queue_append("msg")
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": ["nonexistent-id"]},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "unknown" in data["error"]

    @pytest.mark.asyncio
    async def test_reorder_partial_ids(self):
        """Partial order puts specified items first, rest appended at end."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("a")
        id2 = slot.queue_append("b")  # noqa: F841
        id3 = slot.queue_append("c")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": [id3, id1]},
            )
            assert resp.status == 200

        assert [q["content"] for q in slot._queue] == ["c", "a", "b"]

    @pytest.mark.asyncio
    async def test_reorder_broadcasts_ws_event(self):
        """A queue_reorder WS event is broadcast with the new order."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("x")
        id2 = slot.queue_append("y")

        ws_events: list[tuple[str, dict]] = []
        original_broadcast = state.broadcast_ws  # noqa: F841

        def capture_broadcast(event_type, payload):
            ws_events.append((event_type, payload))

        state.broadcast_ws = capture_broadcast  # type: ignore[assignment]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": [id2, id1]},
            )
            assert resp.status == 200

        reorder_events = [(t, p) for t, p in ws_events if t == "queue_reorder"]
        assert len(reorder_events) == 1
        assert reorder_events[0][1]["slot"] == "chat-1"
        assert reorder_events[0][1]["order"] == [id2, id1]

    @pytest.mark.asyncio
    async def test_reorder_empty_order_is_noop(self):
        """Empty order array is valid — remaining items stay in original order."""
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        id1 = slot.queue_append("a")
        id2 = slot.queue_append("b")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                "/api/chat/slots/chat-1/queue/order",
                json={"order": []},
            )
            assert resp.status == 200

        # Original order preserved
        assert [q["id"] for q in slot._queue] == [id1, id2]

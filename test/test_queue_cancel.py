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


# ── Attachment lists ride the slot-detail queue and the queue_push frame ──

# `[attached_file 1]` is `files[0]`: the path has a space, which is exactly the
# shape a client cannot recover from the marker text alone.
_SPACED = "/Users/me/Desktop/My Report.pdf"
_WIRE = f"summarize this\n[attached_file 1] {_SPACED}"


def _detail_app(state):
    from chat_test_helpers import _make_app

    return _make_app(state)


class TestQueueEntryAttachmentsAreEchoed:
    @pytest.mark.asyncio
    async def test_slot_detail_queue_items_carry_the_lists(self, tmp_path):
        """GET slot detail echoes each entry's lists beside its id and content."""
        from chat_test_helpers import _make_state as _make_detail_state

        state = _make_detail_state(tmp_path)
        state.push_slots_update = lambda: None
        slot = _ChatSlot(key="chat-1")
        slot.messages = [{"role": "user", "content": "hi"}]
        state._slots["chat-1"] = slot
        with_meta = slot.queue_append(_WIRE, meta={"files": [_SPACED], "sendId": "s-1"})
        bare = slot.queue_append("no attachments")

        async with TestClient(TestServer(_detail_app(state))) as client:
            resp = await client.get("/api/chat/slots/chat-1")
            assert resp.status == 200
            body = await resp.json()

        assert body["queue"] == [
            {"id": with_meta, "content": _WIRE, "meta": {"files": [_SPACED]}},
            {"id": bare, "content": "no attachments"},
        ]

    def test_queue_entry_view_redacts_a_path_like_the_content(self):
        """A credential in a path is scrubbed the same way it is in the text."""
        from kiro_crew.dashboard.chat_delivery import queue_entry_view

        secret_path = "/tmp/AKIAIOSFODNN7EXAMPLE/report.pdf"
        view = queue_entry_view(
            {
                "id": "q1",
                "content": f"see\n[attached_file 1] {secret_path}",
                "meta": {"files": [secret_path]},
            }
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in view["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in view["meta"]["files"][0]
        # Same redaction on both, so `[attached_file 1]` still equals `files[0]`.
        assert view["content"].endswith(view["meta"]["files"][0])

    def test_queue_for_next_turn_push_frame_carries_the_lists(self):
        """The busy-slot `queue_push` frame names the lists the entry got."""
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        state.broadcast_ws = MagicMock()
        with (
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"),
        ):
            qid = queue_for_next_turn(state, slot, _WIRE, attachments={"files": [_SPACED]})
        frame = next(
            payload
            for kind, payload in (c.args for c in state.broadcast_ws.call_args_list)
            if kind == "queue_push"
        )
        assert frame == {
            "slot": "chat-1",
            "content": _WIRE,
            "ts": frame["ts"],
            "queue_id": qid,
            "meta": {"files": [_SPACED]},
        }

    def test_queue_for_next_turn_push_frame_without_lists_is_unchanged(self):
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        state.broadcast_ws = MagicMock()
        with (
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"),
        ):
            qid = queue_for_next_turn(state, slot, "plain")
        frame = next(
            payload
            for kind, payload in (c.args for c in state.broadcast_ws.call_args_list)
            if kind == "queue_push"
        )
        assert set(frame) == {"slot", "content", "ts", "queue_id"}
        assert frame["queue_id"] == qid

"""The slot-detail render runs OFF the event loop, single-flight per slot.

Rendering ``GET /api/chat/slots/{slot}`` redacts the entire history with a
regex battery and serializes the result. On a multi-MB session, doing that on
the event loop blocks it past the loop-stall watchdog's exit budget, which
hard-exits the gateway. These tests pin the properties that prevent that:

- ``_prepare_messages`` executes on a worker thread, never the loop thread.
- Concurrent requests for the same slot serialize on the per-slot render lock
  (single-flight) instead of redacting the same corpus in parallel threads.
- The offloaded response keeps the shape and content type the frontend reads.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.state import _ChatSlot


@pytest.fixture()
def state(tmp_path: Any) -> Any:
    st = _make_state(tmp_path)
    st.push_slots_update = lambda: None  # type: ignore[method-assign]
    return st


def _slot_with_messages(state: Any, name: str, count: int) -> Any:
    slot = _ChatSlot(key=name)
    slot.messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(count)
    ]
    state._slots[name] = slot
    return slot


class TestRenderRunsOffTheEventLoop:
    @pytest.mark.asyncio
    async def test_prepare_messages_runs_on_a_worker_thread(
        self, state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The redaction pass must not execute on the loop thread."""
        _slot_with_messages(state, "chat-1", 6)
        loop_thread = threading.get_ident()
        seen: list[int] = []
        real = chat_handlers._prepare_messages

        def _spy(messages: list[dict], running: bool) -> list[dict]:
            seen.append(threading.get_ident())
            return real(messages, running)

        monkeypatch.setattr(chat_handlers, "_prepare_messages", _spy)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/chat-1")
            assert resp.status == 200
            body = await resp.json()

        assert body["total"] == 6
        assert seen, "render never invoked _prepare_messages"
        assert all(t != loop_thread for t in seen)

    @pytest.mark.asyncio
    async def test_concurrent_requests_for_one_slot_serialize(
        self, state: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-slot lock keeps at most ONE render in flight at a time."""
        _slot_with_messages(state, "chat-1", 4)
        real = chat_handlers._prepare_messages
        gauge_lock = threading.Lock()
        active = 0
        max_active = 0

        def _slow(messages: list[dict], running: bool) -> list[dict]:
            nonlocal active, max_active
            with gauge_lock:
                active += 1
                max_active = max(max_active, active)
            # Widens the overlap window so a removed lock is caught; the
            # asserted property (max one render in flight) is deterministic.
            time.sleep(0.05)
            with gauge_lock:
                active -= 1
            return real(messages, running)

        monkeypatch.setattr(chat_handlers, "_prepare_messages", _slow)
        async with TestClient(TestServer(_make_app(state))) as client:
            resps = await asyncio.gather(
                *[client.get("/api/chat/slots/chat-1") for _ in range(3)]
            )
            assert [r.status for r in resps] == [200, 200, 200]
            bodies = [await r.json() for r in resps]

        assert all(b["total"] == 4 for b in bodies)
        assert max_active == 1


class TestResponseParity:
    @pytest.mark.asyncio
    async def test_shape_and_content_type_are_unchanged(self, state: Any) -> None:
        """The hand-serialized response reads exactly like json_response did."""
        slot = _slot_with_messages(state, "chat-1", 2)
        slot._queue.append({"id": "q1", "content": "queued text"})
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/chat-1")
            assert resp.status == 200
            assert resp.content_type == "application/json"
            body = await resp.json()

        assert set(body) >= {
            "key",
            "title",
            "running",
            "stopping",
            "messages",
            "queue",
            "total",
            "has_more",
            "next_before",
        }
        assert body["key"] == "chat-1"
        assert body["total"] == 2
        assert body["has_more"] is False
        assert body["queue"] == [{"id": "q1", "content": "queued text"}]
        assert [m["content"] for m in body["messages"]] == ["m0", "m1"]

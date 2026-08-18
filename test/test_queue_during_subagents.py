"""Tests for the (always-on) queue-during-subagents behavior.

Covers the drain-filter primitive (_dequeue_next_system_message) that keeps a
tangential user message queued while background sub-agents run, the api_chat
ingest gate (unconditional: queues whenever sub-agents run for the slot), and
the board's subagents_running slot annotation. There is no config toggle —
steering is the effective opt-out.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    _dequeue_next_system_message,
)
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    _ChatSlot,
)

# ── Unit tests: _dequeue_next_system_message ──


class TestDequeueNextSystemMessage:
    """The helper drains system injections while keeping plain user messages queued."""

    def test_only_user_messages_holds_all(self):
        """With only user messages queued, nothing drains and the queue is intact."""
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "keep working"}, {"id": "b", "content": "and this too"}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg is None
        assert consumed == []
        assert [q["content"] for q in slot._queue] == ["keep working", "and this too"]

    def test_empty_queue(self):
        """Empty queue drains nothing."""
        slot = _ChatSlot("s1")
        slot._queue = []

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg is None
        assert consumed == []

    def test_drains_subagent_completion_holds_user(self):
        """A queued sub-agent completion drains; a leading user message stays queued."""
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed \u2705\nResult"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "tangential question"}, {"id": "b", "content": sa, "kind": SUBAGENT_COMPLETION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == sa
        assert [c["content"] for c in consumed] == [sa]
        # The user message stays queued.
        assert [q["content"] for q in slot._queue] == ["tangential question"]

    def test_drains_cron_holds_user(self):
        """A queued cron notification drains; user messages stay queued."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "hi there"}, {"id": "b", "content": cron, "kind": CRON_NOTIFICATION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == cron
        assert [c["content"] for c in consumed] == [cron]
        assert [q["content"] for q in slot._queue] == ["hi there"]

    def test_subagent_first_drains_first(self):
        """A leading sub-agent completion drains directly."""
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `x` completed \u2705\nDone"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": sa, "kind": SUBAGENT_COMPLETION_KIND}, {"id": "b", "content": "user follow-up"}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == sa
        assert [q["content"] for q in slot._queue] == ["user follow-up"]


# ── API test: api_chat ingest gate (idle + sub-agents running) ──


@pytest.mark.asyncio
class TestApiChatSubagentQueueGate:
    """The idle-path ingest gate queues a message whenever sub-agents are
    running for the slot (always on), querying the correct parent key."""

    async def test_queues_when_subagents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg):
            ran["called"] = True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "tangential q", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is True
        assert ran["called"] is False  # gate returned before starting a turn
        assert slot.queue_depth == 1
        # The gate must query the slot's parent key, not a bare/mismatched one.
        subs.running_agents_for.assert_any_call("dashboard:s1")

    async def test_not_queued_when_no_subagents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        async def fake_run_chat(st, sl, msg):
            return None

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[])  # no agents running
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "go on", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is not True  # not held → normal dispatch
        assert slot.queue_depth == 0


# ── API test: api_chat busy-slot queue branch (receipt honesty) ──


@pytest.mark.asyncio
class TestApiChatBusySlotEmptyMessage:
    """The busy-slot queue branch never answers `queued: true` for a send it
    did not queue: an empty-message send (e.g. attachments only in `meta`)
    gets an honest 400 with a stable code, and nothing is queued or
    broadcast."""

    async def _busy_client(self, tmp_path, monkeypatch):
        """Real state + slot; the slot is made busy via a live, never-done task."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        pushes: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload, **kw: pushes.append((kind, payload))
        return state, slot, pushes

    async def test_attachment_only_send_gets_honest_400(self, tmp_path, monkeypatch):
        """Busy slot + empty message + meta attachments → 4xx with stable code,
        nothing appended to the queue, no queue_push broadcast."""
        state, slot, pushes = await self._busy_client(tmp_path, monkeypatch)
        gate = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:
            slot.task = asyncio.get_running_loop().create_task(gate.wait())
            try:
                assert slot.running is True  # precondition: authentically busy
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={
                        "message": "",
                        "slot": "s1",
                        "meta": {"files": [{"name": "diagram.png"}]},
                    },
                )
                assert resp.status == 400
                data = await resp.json()
            finally:
                gate.set()
                await slot.task

        assert data.get("error") == "message is required"
        assert data.get("code") == "message_required"
        assert data.get("queued") is not True
        assert slot.queue_depth == 0
        assert [p for p in pushes if p[0] == "queue_push"] == []

    async def test_nonempty_message_still_queued(self, tmp_path, monkeypatch):
        """Busy slot + non-empty message → `queued: true`, one queue entry,
        one queue_push broadcast (the receipt implies a real enqueue)."""
        state, slot, pushes = await self._busy_client(tmp_path, monkeypatch)
        gate = asyncio.Event()

        async with TestClient(TestServer(_make_app(state))) as client:
            slot.task = asyncio.get_running_loop().create_task(gate.wait())
            try:
                resp = await client.post(
                    "/api/chat?ws=1", json={"message": "still here", "slot": "s1"}
                )
                assert resp.status == 200
                data = await resp.json()
            finally:
                gate.set()
                await slot.task

        assert data.get("queued") is True
        assert slot.queue_depth == 1
        assert len([p for p in pushes if p[0] == "queue_push"]) == 1


# ── Board annotation: DashboardState.serialize_slots subagents_running ──


@pytest.mark.asyncio
class TestSerializeSlotsSubagentsRunning:
    """serialize_slots() annotates each slot dict with subagents_running so the
    Board shows 'Working' (not 'Your turn') while background sub-agents run.

    Async because get_or_create_slot() can trigger push_slots_update() ->
    _send_ws_all() -> asyncio.ensure_future(), which needs a running loop
    (see precedent)."""

    async def test_flag_true_when_agents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
        state = _make_state(tmp_path, subagents=subs)
        state.get_or_create_slot("s1")

        slots = state.serialize_slots()

        assert slots, "expected at least one serialized slot"
        assert all(d["subagents_running"] is True for d in slots)
        subs.running_agents_for.assert_any_call("dashboard:s1")

    async def test_flag_false_when_no_agents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[])
        state = _make_state(tmp_path, subagents=subs)
        state.get_or_create_slot("s1")

        slots = state.serialize_slots()

        assert slots, "expected at least one serialized slot"
        assert all(d["subagents_running"] is False for d in slots)

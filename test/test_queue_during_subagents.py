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

from kiro_crew.dashboard.chat_runner import subagents_hold_user_messages
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


class TestDequeueCron:
    """A cron notification is a system injection, so it drains past held users."""

    def test_cron_drains(self):
        """A lone cron notification drains."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": cron, "kind": CRON_NOTIFICATION_KIND}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == cron
        assert slot._queue == []


# ── API test: api_chat ingest gate (idle + sub-agents running) ──


@pytest.mark.asyncio
class TestApiChatSubagentQueueGate:
    """The idle-path ingest gate queues a message whenever sub-agents are
    running for the slot (always on), querying the correct parent key."""

    async def test_queues_when_subagents_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
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

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
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

    async def test_stalled_child_does_not_hold_the_message(self, tmp_path, monkeypatch):
        """A child the reaper flagged stalled must not park the user's send."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran["called"] = True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "dead", "stalled": True}])
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "hello?", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is not True
        assert slot.queue_depth == 0

    async def test_send_behind_parked_message_keeps_order(self, tmp_path, monkeypatch):
        """A message parked while the child was live is answered before a later send."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        direct = []

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            direct.append(msg)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        drained = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(chat_runner, "_run_chat", drained)
        monkeypatch.setattr(chat_runner, "spawn_guarded_turn", lambda *a, **k: None)
        load = MagicMock()
        load.return_value.dashboard.merge_queued_messages = False
        monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", load)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "dead", "stalled": True}])
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")
        slot.queue_append("first, parked while the child was live")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "second", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()

        assert data.get("queued") is True
        assert direct == []  # did not jump the parked message
        assert drained.call_args.args[2] == "first, parked while the child was live"
        assert [item["content"] for item in slot._queue] == ["second"]


class TestSubagentsHoldUserMessages:
    """Only a child that is not flagged stalled holds user messages."""

    def _state(self, agents):
        state = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=agents)
        return state

    def test_fresh_child_holds(self):
        state = self._state([{"id": "a1", "stalled": False}])
        assert subagents_hold_user_messages(state, "dashboard:s1") is True
        state.subagents.running_agents_for.assert_called_once_with("dashboard:s1")

    def test_stalled_child_does_not_hold(self):
        state = self._state([{"id": "a1", "stalled": True}])
        assert subagents_hold_user_messages(state, "dashboard:s1") is False

    def test_one_fresh_sibling_still_holds(self):
        state = self._state([{"id": "a1", "stalled": True}, {"id": "a2", "stalled": False}])
        assert subagents_hold_user_messages(state, "dashboard:s1") is True

    def test_no_children_or_no_registry(self):
        assert subagents_hold_user_messages(self._state([]), "k") is False
        state = MagicMock()
        state.subagents = None
        assert subagents_hold_user_messages(state, "k") is False


@pytest.mark.asyncio
async def test_drain_releases_user_message_when_only_child_is_stalled(tmp_path, monkeypatch):
    """The drain-side hold uses the same rule: a stalled child lets users drain."""
    from kiro_crew.dashboard import chat_runner

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    subs = MagicMock()
    subs.running_agents_for = MagicMock(return_value=[{"id": "dead", "stalled": True}])
    state = _make_state(tmp_path, subagents=subs)
    slot = state.get_or_create_slot("s1")
    slot.queue_append("waiting user message")
    spawned = []
    monkeypatch.setattr(chat_runner, "spawn_guarded_turn", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(chat_runner, "_run_chat", MagicMock(return_value=MagicMock()))

    assert await chat_runner._start_next_queued_turn(state, slot) is True
    assert len(spawned) == 1
    assert slot.queue_depth == 0


@pytest.mark.asyncio
class TestDrainIdleParentQueueForStall:
    """The ``subagent_stalled`` event drains a message queued before the flag.

    A stalled child does not *hold* user messages, but only a fresh send or a
    child completion ever *starts* the drain. A message queued while the child
    was still live would otherwise sit until the next send or the 30-minute
    reap. ``drain_idle_parent_queue_for_stall`` is the trigger the stall event
    fires to release it at stall time.
    """

    def _make(self, tmp_path, monkeypatch, agents):
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=agents)
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")
        spawned = []
        monkeypatch.setattr(chat_runner, "spawn_guarded_turn", lambda *a, **k: spawned.append(a))
        monkeypatch.setattr(chat_runner, "_run_chat", MagicMock(return_value=MagicMock()))
        return chat_runner, state, slot, spawned

    async def test_stall_drains_message_queued_before_the_flag(self, tmp_path, monkeypatch):
        """An idle parent slot's parked message drains when its only child stalls."""
        chat_runner, state, slot, spawned = self._make(
            tmp_path, monkeypatch, [{"id": "dead", "stalled": True}]
        )
        slot.queue_append("message queued before the stall")

        await chat_runner.drain_idle_parent_queue_for_stall(state, "dashboard:s1")

        assert len(spawned) == 1
        assert slot.queue_depth == 0

    async def test_live_sibling_keeps_the_queue_parked(self, tmp_path, monkeypatch):
        """A still-live sibling means the hold stands; the stall drains nothing."""
        chat_runner, state, slot, spawned = self._make(
            tmp_path,
            monkeypatch,
            [{"id": "dead", "stalled": True}, {"id": "live", "stalled": False}],
        )
        slot.queue_append("waiting user message")

        await chat_runner.drain_idle_parent_queue_for_stall(state, "dashboard:s1")

        assert spawned == []
        assert slot.queue_depth == 1

    async def test_running_slot_is_left_alone(self, tmp_path, monkeypatch):
        """A busy slot drains its own queue at turn end; the stall must not double it."""
        chat_runner, state, slot, spawned = self._make(
            tmp_path, monkeypatch, [{"id": "dead", "stalled": True}]
        )
        slot.queue_append("waiting user message")
        gate = asyncio.Event()
        slot.task = asyncio.get_running_loop().create_task(gate.wait())
        try:
            assert slot.running is True
            await chat_runner.drain_idle_parent_queue_for_stall(state, "dashboard:s1")
        finally:
            gate.set()

        assert spawned == []
        assert slot.queue_depth == 1

    async def test_empty_queue_and_unknown_parent_are_noops(self, tmp_path, monkeypatch):
        """No queued work, or a parent with no slot, drains nothing and does not raise."""
        chat_runner, state, slot, spawned = self._make(
            tmp_path, monkeypatch, [{"id": "dead", "stalled": True}]
        )
        # Idle slot, empty queue.
        await chat_runner.drain_idle_parent_queue_for_stall(state, "dashboard:s1")
        # Parent with no slot at all.
        await chat_runner.drain_idle_parent_queue_for_stall(state, "dashboard:ghost")
        assert spawned == []

    async def test_refused_admission_keeps_the_message_queued(self, tmp_path, monkeypatch):
        """A stall during shutdown holds the parked prompt instead of losing it.

        When memory startup is stopping, ``_run_chat`` refuses a popped turn
        without requeueing it. The drain must leave the message queued."""
        chat_runner, state, slot, spawned = self._make(
            tmp_path, monkeypatch, [{"id": "dead", "stalled": True}]
        )
        slot.queue_append("message queued before the stall")
        monkeypatch.setattr(chat_runner, "_parked_queue_drain_admitted", lambda: False)

        await chat_runner.drain_idle_parent_queue_for_stall(state, "dashboard:s1")

        assert spawned == []
        assert slot.queue_depth == 1


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

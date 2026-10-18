"""Tests for the (always-on) queue-during-subagents behavior.

Covers the drain-filter primitive (_dequeue_next_system_message) that keeps a
peer's, channel's or app's message queued while background sub-agents run, the
api_chat ingest gate (which never holds the session's own composer send), and
the board's subagents_running slot annotation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_runner import subagents_hold_user_messages
from kiro_crew.dashboard.chat_utils import (
    COMPOSER_SEND_META,
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


class TestDequeueSteerIntent:
    """A steer that fell back to the queue drains past the hold; a queued send waits."""

    def test_composer_send_drains_past_a_held_user_message(self):
        slot = _ChatSlot("s1")
        slot._queue = [
            {"id": "a", "content": "after the wave", "meta": {}},
            {"id": "b", "content": "act on this now", "meta": {COMPOSER_SEND_META: True}},
        ]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == "act on this now"
        assert [c["id"] for c in consumed] == ["b"]
        assert [q["content"] for q in slot._queue] == ["after the wave"]

    @pytest.mark.parametrize("meta", [None, {}, {COMPOSER_SEND_META: "true"}, {COMPOSER_SEND_META: 1}])
    def test_only_a_literal_true_releases(self, meta):
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "wait", "meta": meta}]

        assert _dequeue_next_system_message(slot) == (None, [])
        assert slot.queue_depth == 1


# ── Steer fallbacks carry their intent past the hold ──


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


@pytest.mark.asyncio
class TestSteerFallbackSurvivesTheHold:
    """A composer steer the running turn could not take still acts once it ends.

    The wave-digest turn is short and usually makes no tool call, so a steer sent
    into it is often never consumed: the teardown requeues it, and the drain used
    to park it behind the remaining children like a message the sender chose to
    queue.
    """

    @pytest.fixture(autouse=True)
    def _sel(self):
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            yield

    def _state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "live"}])
        state = _make_state(tmp_path, subagents=subs)
        state.broadcast_ws = MagicMock()
        return state

    async def _drain(self, state, slot):
        from kiro_crew.dashboard import chat_runner

        slot.task = None
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()) as spawn,
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run_chat,
        ):
            started = await chat_runner._start_next_queued_turn(state, slot)
        return started, spawn, run_chat

    async def test_unconsumed_steer_drains_while_children_run(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        state = self._state(tmp_path, monkeypatch)
        slot = _busy(state.get_or_create_slot("s1"))
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        slot.queue_append("queued before, waits for the wave")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "act now", "steer": True})
            assert (await resp.json()).get("steered") is True

        # The turn ends without kiro-cli consuming the steer.
        chat_runner._requeue_unconsumed_steers(state, slot)
        assert slot._queue[0]["content"] == "act now", slot._queue
        assert slot._queue[0]["meta"][COMPOSER_SEND_META] is True

        started, _spawn, run_chat = await self._drain(state, slot)

        assert started is True
        assert run_chat.call_args.args[2] == "act now"
        assert [q["content"] for q in slot._queue] == ["queued before, waits for the wave"]
        row = [m for m in slot.messages if m.get("role") == "user"][-1]
        assert COMPOSER_SEND_META not in (row.get("meta") or {})

    async def test_unavailable_steer_is_queued_with_its_intent(self, tmp_path, monkeypatch):
        state = self._state(tmp_path, monkeypatch)
        slot = _busy(state.get_or_create_slot("s1"))
        slot._acp_client = None  # no steer-capable client: the steer falls to the queue

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "act now", "steer": True})
            assert (await resp.json()).get("queued") is True

        assert slot._queue[-1]["meta"][COMPOSER_SEND_META] is True
        started, _spawn, run_chat = await self._drain(state, slot)
        assert started is True
        assert run_chat.call_args.args[2] == "act now"

    async def test_queue_choice_runs_when_the_turn_ends(self, tmp_path, monkeypatch):
        """Queue means after this turn, not after the background children."""
        state = self._state(tmp_path, monkeypatch)
        slot = _busy(state.get_or_create_slot("s1"))
        slot._acp_client = None
        slot.queue_append("a peer's message, waits for the wave")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "after this turn"})
            assert (await resp.json()).get("queued") is True

        assert slot._queue[-1]["meta"][COMPOSER_SEND_META] is True
        started, _spawn, run_chat = await self._drain(state, slot)
        assert started is True
        assert run_chat.call_args.args[2] == "after this turn"
        assert [q["content"] for q in slot._queue] == ["a peer's message, waits for the wave"]

    async def test_turn_end_card_runs_under_the_hold(self, tmp_path, monkeypatch):
        """The card a turn-end read hands the floor runs when the hold releases it."""
        from kiro_crew.dashboard import chat_runner

        state = self._state(tmp_path, monkeypatch)
        slot = _busy(state.get_or_create_slot("s1"))
        slot._acp_client = None
        held = slot.queue_append("a peer's message, waits for the wave")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat", json={"slot": "s1", "message": "mine"})
            mine = (await resp.json())["queue_id"]

        slot.task = None
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run_chat,
        ):
            assert await chat_runner._start_next_queued_turn(state, slot, required_queue_id=held) is False
            assert await chat_runner._start_next_queued_turn(state, slot, required_queue_id=mine) is True
        assert run_chat.call_args.args[2] == "mine"
        assert [q["id"] for q in slot._queue] == [held]

    async def test_peer_steer_requeue_carries_no_intent(self, tmp_path, monkeypatch):
        """Only the session's own human gets the release; a peer's text waits."""
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_delivery import steer_into_running_turn

        state = self._state(tmp_path, monkeypatch)
        slot = _busy(state.get_or_create_slot("s1"))
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        await steer_into_running_turn(state, slot, "peer text", user_origin=False, admission={})
        chat_runner._requeue_unconsumed_steers(state, slot)

        assert slot._queue[0]["content"] == "peer text"
        assert COMPOSER_SEND_META not in (slot._queue[0].get("meta") or {})


@pytest.mark.asyncio
class TestResultsArriveWhileChatting:
    """A child's result reaches the main thread while its person is mid-chat.

    The gateway queues a completion that lands during a running turn
    (``kind=SUBAGENT_COMPLETION_KIND``), and the person may queue more messages
    behind or ahead of it. With a sibling still running, the drain must hand off
    both in arrival order, each as its own turn, and keep only third-party
    messages parked.
    """

    @pytest.fixture(autouse=True)
    def _sel(self):
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            yield

    async def _scenario(self, tmp_path, monkeypatch, *, result_first: bool):
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        load = MagicMock()
        load.return_value.dashboard.merge_queued_messages = True
        monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", load)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "sibling"}])
        state = _make_state(tmp_path, subagents=subs)
        state.broadcast_ws = MagicMock()
        slot = _busy(state.get_or_create_slot("s1"))
        slot._acp_client = None
        result = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `a1` completed \u2705\nResult"
        slot.queue_append("a peer's message, waits for the wave")

        async def _chat(text):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json={"slot": "s1", "message": text})
                assert (await resp.json()).get("queued") is True

        if result_first:
            slot.queue_append(result, kind=SUBAGENT_COMPLETION_KIND)
            await _chat("follow-up while it works")
        else:
            await _chat("follow-up while it works")
            slot.queue_append(result, kind=SUBAGENT_COMPLETION_KIND)

        ran = []
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()) as run_chat,
        ):
            for _ in range(3):
                slot.task = None
                if await chat_runner._start_next_queued_turn(state, slot):
                    ran.append(run_chat.call_args.args[2])
        return ran, result, slot

    async def test_result_then_chat_drains_in_order(self, tmp_path, monkeypatch):
        ran, result, slot = await self._scenario(tmp_path, monkeypatch, result_first=True)
        assert ran == [result, "follow-up while it works"]
        assert [q["content"] for q in slot._queue] == ["a peer's message, waits for the wave"]

    async def test_chat_then_result_drains_in_order(self, tmp_path, monkeypatch):
        ran, result, slot = await self._scenario(tmp_path, monkeypatch, result_first=False)
        assert ran == ["follow-up while it works", result]
        assert [q["content"] for q in slot._queue] == ["a peer's message, waits for the wave"]


# ── API test: api_chat ingest gate (idle + sub-agents running) ──


@pytest.mark.asyncio
class TestApiChatSubagentQueueGate:
    """The idle-path ingest gate: with sub-agents running for the slot, an app's
    send is held, while the session's own human starts a turn at once."""

    async def test_composer_send_is_not_held_by_running_subagents(self, tmp_path, monkeypatch):
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

        assert data.get("queued") is not True
        assert ran["called"] is True
        assert slot.queue_depth == 0

    @pytest.mark.parametrize("internal_auth", [True, False], ids=["internal-secret", "unmarked"])
    async def test_non_composer_send_is_held_by_running_subagents(
        self, tmp_path, monkeypatch, internal_auth
    ):
        """Only the dashboard-user mark releases a send. A loopback internal-secret
        send (an MCP tool, a cron script) and a caller carrying no mark at all wait
        for the wave, and a busy-slot copy carries no composer mark."""
        from aiohttp import web

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        ran = {"called": False}

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            ran["called"] = True

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "a1"}])
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")

        @web.middleware
        async def _internal(request, handler):
            # No app claim and no dashboard-user mark: token_auth's
            # internal-secret grant, or any caller it never marked.
            request["internal_auth"] = internal_auth
            request["is_dashboard_user"] = False
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _internal)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "from a cron", "slot": "s1"})
            assert (await resp.json()).get("queued") is True
            assert ran["called"] is False
            _busy(slot)
            slot._acp_client = None
            resp = await client.post("/api/chat?ws=1", json={"message": "again", "slot": "s1"})
            assert (await resp.json()).get("queued") is True

        assert [q["content"] for q in slot._queue] == ["from a cron", "again"]
        assert not any(COMPOSER_SEND_META in (q.get("meta") or {}) for q in slot._queue)
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

    @pytest.mark.parametrize("composer", [True, False], ids=["composer", "internal-secret"])
    async def test_send_behind_parked_message_carries_composer_mark(
        self, tmp_path, monkeypatch, composer
    ):
        """A composer send queued behind a parked message carries the composer
        mark, so a hold the drained turn reinstates still releases it at that
        turn's end; a non-composer send in the same branch carries none."""
        from aiohttp import web

        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(chat_runner, "_run_chat", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(chat_runner, "spawn_guarded_turn", lambda *a, **k: None)
        load = MagicMock()
        load.return_value.dashboard.merge_queued_messages = False
        monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", load)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[{"id": "dead", "stalled": True}])
        state = _make_state(tmp_path, subagents=subs)
        slot = state.get_or_create_slot("s1")
        slot.queue_append("first, parked while the child was live")

        @web.middleware
        async def _mark(request, handler):
            request["internal_auth"] = not composer
            request["is_dashboard_user"] = composer
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _mark)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat?ws=1", json={"message": "second", "slot": "s1"})
            assert (await resp.json()).get("queued") is True

        assert [item["content"] for item in slot._queue] == ["second"]
        meta = slot._queue[0].get("meta") or {}
        if composer:
            assert meta.get(COMPOSER_SEND_META) is True
        else:
            assert COMPOSER_SEND_META not in meta


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

"""Tests for the (always-on) queue-during-subagents behavior.

Covers the drain-filter primitive (_dequeue_next_system_message) that keeps a
tangential user message queued while background sub-agents run, the api_chat
ingest gate (unconditional: queues whenever sub-agents run for the slot), and
the board's subagents_running slot annotation. There is no config toggle —
steering is the effective opt-out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_utils import _dequeue_next_system_message
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
        slot._queue = [{"id": "a", "content": "tangential question"}, {"id": "b", "content": sa}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == sa
        assert [c["content"] for c in consumed] == [sa]
        # The user message stays queued.
        assert [q["content"] for q in slot._queue] == ["tangential question"]

    def test_drains_cron_holds_user(self):
        """A queued cron notification drains; user messages stay queued."""
        cron = f"{CRON_NOTIFY_PREFIX}daily]: run report"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": "hi there"}, {"id": "b", "content": cron}]

        next_msg, consumed = _dequeue_next_system_message(slot)

        assert next_msg == cron
        assert [c["content"] for c in consumed] == [cron]
        assert [q["content"] for q in slot._queue] == ["hi there"]

    def test_subagent_first_drains_first(self):
        """A leading sub-agent completion drains directly."""
        sa = f"{SUBAGENT_COMPLETION_PREFIX}\nAgent `x` completed \u2705\nDone"
        slot = _ChatSlot("s1")
        slot._queue = [{"id": "a", "content": sa}, {"id": "b", "content": "user follow-up"}]

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


# ── Slack surface: the _route_message subagent gate (Arbiter item 1) ──


def _make_slack_orch(running_agents):
    """Minimal mock GatewayOrchestrator for _route_message, with a subagent_mgr
    whose running_agents_for returns *running_agents*."""
    from unittest.mock import AsyncMock

    from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig

    orch = MagicMock()
    orch._cfg = KiroCrewConfig(messaging=MessagingConfig(use_transport=False))
    orch.channel_history = MagicMock()
    orch.slack = MagicMock()
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=True)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.task_runner = None
    orch.subagent_mgr = MagicMock()
    orch.subagent_mgr.running_agents_for = MagicMock(return_value=running_agents)
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


class TestSlackSubagentQueueGate:
    """A Slack DM that arrives while the thread has running sub-agents must be
    queued (not dispatched into a fresh turn that would interleave with the
    completion injections), and the busy lookup must use the CANONICAL key."""

    @pytest.mark.asyncio
    async def test_queues_when_subagents_running(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_slack_orch(running_agents=[{"id": "a1"}])
        seen = SeenCache()
        # DM with a thread_ts so the session key is a bare Slack ts.
        event = {
            "user": "U1", "channel": "D1", "text": "tangential q",
            "ts": "9.1", "thread_ts": "9.0", "team": "TTEST",
        }
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)

        # Held: enqueued, no new turn dispatched.
        orch.sessions.enqueue.assert_called_once()
        mock_hm.assert_not_called()
        # Looked up by the canonical slack:<thread> key, not the bare ts.
        orch.subagent_mgr.running_agents_for.assert_any_call("slack:9.0")

    @pytest.mark.asyncio
    async def test_dispatched_when_no_subagents_running(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_slack_orch(running_agents=[])  # none running
        orch.sessions.enqueue = MagicMock(return_value=False)
        seen = SeenCache()
        event = {
            "user": "U1", "channel": "D1", "text": "hello",
            "ts": "8.1", "thread_ts": "8.0", "team": "TTEST",
        }
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

        # Not held → normal dispatch to a new turn.
        mock_hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_drain_processes_entire_backlog_not_just_first(self):
        """The shared drain helper must re-arm and dispatch EVERY queued message,
        not only the first (regression: a plain done-callback stranded the rest)."""
        import asyncio
        from unittest.mock import patch

        from kiro_crew.slack.events import _schedule_next_queued

        orch = _make_slack_orch(running_agents=[])
        # Three messages queued during the sub-agent run; dequeue drains them
        # one at a time, then returns None.
        _backlog = [("t1", "one", {}), ("t2", "two", {}), ("t3", "three", {})]
        orch.sessions.dequeue = MagicMock(side_effect=lambda k: _backlog.pop(0) if _backlog else None)

        dispatched: list = []

        async def fake_dispatch(o, key, ts, text, kw):
            dispatched.append(text)

        with patch("kiro_crew.slack.events._dispatch_queued", side_effect=fake_dispatch):
            _schedule_next_queued(orch, "slack:7.0")
            # Let each dispatched task complete so its done-callback re-arms.
            for _ in range(6):
                await asyncio.sleep(0)
                await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

        assert dispatched == ["one", "two", "three"]
        assert orch.sessions.dequeue.call_count >= 3

    @pytest.mark.asyncio
    async def test_drain_uses_bare_key_for_canonical_parent(self):
        """The gateway drains under the BARE Slack key the enqueue gate stored
        under, not the canonical slack:<thread> parent key the sub-agent records
        — otherwise the queued message is stranded (the pending-queue dict is not
        fold-aware)."""
        import asyncio
        from unittest.mock import patch

        from kiro_crew.messaging.link import legacy_key
        from kiro_crew.slack.events import _schedule_next_queued

        # Mirror the gateway fix: parent_key is canonical, queue is keyed bare.
        parent_key = "slack:9.0"
        drain_key = legacy_key(parent_key) or parent_key
        assert drain_key == "9.0"

        orch = _make_slack_orch(running_agents=[])
        # One held message, stored under the BARE key (as the gate stored it);
        # a lookup under the canonical key would find nothing (the bug).
        _queue = {"9.0": [("t1", "held", {})]}

        def _dequeue(k):
            q = _queue.get(k)
            return q.pop(0) if q else None

        orch.sessions.dequeue = MagicMock(side_effect=_dequeue)
        dispatched: list = []

        async def fake_dispatch(o, key, ts, text, kw):
            dispatched.append((key, text))

        with patch("kiro_crew.slack.events._dispatch_queued", side_effect=fake_dispatch):
            _schedule_next_queued(orch, drain_key)
            for _ in range(4):
                await asyncio.sleep(0)
                await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

        assert dispatched == [("9.0", "held")]

    @pytest.mark.asyncio
    async def test_drain_holds_while_subagents_running(self):
        """The drain helper enforces the sub-agent gate on the drain side too: a
        turn that spawned fire-and-forget sub-agents and finished first must NOT
        drain a queued message into a fresh turn mid-fan-out (both reviewers'
        finding). Release waits for the last sub-agent's re-arm."""
        import asyncio
        from unittest.mock import patch

        from kiro_crew.slack.events import _schedule_next_queued

        orch = _make_slack_orch(running_agents=[{"id": "a1"}])  # still running
        orch.sessions.dequeue = MagicMock(return_value=("t1", "held", {}))
        dispatched: list = []

        async def fake_dispatch(o, key, ts, text, kw):
            dispatched.append(text)

        with patch("kiro_crew.slack.events._dispatch_queued", side_effect=fake_dispatch):
            _schedule_next_queued(orch, "9.0")  # bare key; canonical lookup = slack:9.0
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)

        # Held — nothing dispatched, and dequeue was NOT called (short-circuited
        # before touching the queue), so the message stays queued.
        assert dispatched == []
        orch.sessions.dequeue.assert_not_called()
        orch.subagent_mgr.running_agents_for.assert_any_call("slack:9.0")

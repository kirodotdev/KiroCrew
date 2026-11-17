"""A dashboard agent switch on a channel-born Slack tab lands on the Slack thread.

Native Slack dispatch picks a thread's agent from the handler's in-memory
``_thread_agents`` map, hydrated once per thread from the channel transcript's
metadata. A switch that only moves ``slot.agent`` and writes the ``dashboard:``
transcript leaves both of those on the old agent, so the next Slack message
cold-starts the session the switch just reset on the agent the user moved away
from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_slot_switch_atomicity_helpers import _make_app, _mock_state
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.state import _ChatSlot

SESSION = "slack:123.456"


@pytest.fixture
def slack_thread_maps():
    from kiro_crew.slack import handler

    handler._thread_agents.clear()
    handler._hydrated_sessions.clear()
    # The thread already hydrated on the old agent: hydration will not re-read it.
    handler._thread_agents[SESSION] = "old-agent"
    handler._hydrated_sessions.add(SESSION)
    yield handler
    handler._thread_agents.clear()
    handler._hydrated_sessions.clear()


def _channel_slot() -> _ChatSlot:
    slot = _ChatSlot("slack_123.456")
    slot.agent = "old-agent"
    slot.channel_origin = True
    slot.linked_session_key = SESSION
    return slot


@pytest.mark.asyncio
async def test_switch_updates_the_slack_threads_agent(monkeypatch, slack_thread_maps):
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
    slot = _channel_slot()
    state = _mock_state(slot, provider=None)
    state.sessions.reset = AsyncMock(return_value=True)
    state.conversation_log = MagicMock()
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "new-agent"})
        assert resp.status == 200
    assert slot.agent == "new-agent"
    from kiro_crew.slack.handler_runtime.inbound import _get_agent_for_session

    assert _get_agent_for_session(SESSION) == "new-agent"


@pytest.mark.asyncio
async def test_switch_persists_to_the_channel_transcript(monkeypatch, slack_thread_maps):
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
    slot = _channel_slot()
    state = _mock_state(slot, provider=None)
    state.sessions.reset = AsyncMock(return_value=True)
    state.conversation_log = MagicMock()
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/agent", json={"agent": "new-agent"})
        assert resp.status == 200
    keys = [c.args[0] for c in state.conversation_log.update_metadata.call_args_list]
    # Slack hydration reads the channel transcript, never the dashboard: one.
    assert keys and set(keys) == {SESSION}


@pytest.mark.asyncio
async def test_a_linked_dashboard_chat_leaves_the_slack_thread_alone(
    monkeypatch, slack_thread_maps
):
    # A dashboard chat linked to the thread routes its Slack replies into the
    # slot, which runs slot.agent: its own transcript and Slack's map stay put.
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", _boom)
    slot = _ChatSlot("test")
    slot.agent = "old-agent"
    slot.linked_session_key = SESSION
    state = _mock_state(slot, provider=None)
    state.sessions.reset = AsyncMock(return_value=True)
    state.conversation_log = MagicMock()
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        resp = await client.post("/api/chat/slots/test/agent", json={"agent": "new-agent"})
        assert resp.status == 200
    assert slack_thread_maps._thread_agents[SESSION] == "old-agent"
    keys = {c.args[0] for c in state.conversation_log.update_metadata.call_args_list}
    assert keys == {"dashboard:test"}


@pytest.mark.asyncio
async def test_a_crewmate_pick_hands_slack_its_execution_template(monkeypatch, slack_thread_maps):
    # A crewmate alias is not a launchable agent: the switch records the
    # crewmate's template in the execution record, and hydration runs that
    # template. The live thread entry names the same template.
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
    from kiro_crew.session_agent_selection import SelectionChange

    cfg = MagicMock()
    cfg.agents = {}
    cfg.memory_stores = {}
    cfg.default_workspace = "default"
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
        lambda *_args, **_kwargs: MagicMock(
            workspace_dir="",
            memory_store_name="",
            selection_kind="member",
            requested_resolved=True,
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: None
    )
    published = ExecutionContext(
        member_id="m-1",
        store=MemoryStoreRef("mate-store", "m-1"),
        selection_kind="member",
        template_id="mate-template",
        memory_mode="persistent",
        app="",
        selection_name="mate",
    ).to_record()
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._record_explicit_agent_selection",
        AsyncMock(return_value=SelectionChange(None, published)),
    )
    slot = _channel_slot()
    state = _mock_state(slot, provider=None)
    state.sessions.reset = AsyncMock(return_value=True)
    state.conversation_log = MagicMock()
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot.key}/agent", json={"agent": "mate", "agent_kind": "member"}
        )
        assert resp.status == 200, await resp.text()
    assert slot.agent == "mate"
    assert slack_thread_maps._thread_agents[SESSION] == "mate-template"


@pytest.mark.asyncio
async def test_a_refused_switch_has_slack_reread_the_restored_transcript(
    monkeypatch, slack_thread_maps
):
    # A Slack message hydrating the thread between the switch's metadata write and
    # its refusal caches the refused agent; the rollback restores the transcript
    # and has the thread hydrate again rather than keep that entry.
    cfg = MagicMock()
    cfg.agents = {}
    cfg.memory_stores = {}
    cfg.default_workspace = "default"
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.KiroCrewConfig.load", lambda: cfg)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.resolve_agent_bindings",
        lambda *_args, **_kwargs: MagicMock(
            workspace_dir="",
            memory_store_name="",
            selection_kind="template",
            requested_resolved=True,
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", lambda *_a, **_k: None
    )

    async def _hydrated_then_refused(*_args, **_kwargs):
        slack_thread_maps._thread_agents[SESSION] = "refused-agent"
        slack_thread_maps._hydrated_sessions.add(SESSION)
        raise RuntimeError("selection store unavailable")

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._record_explicit_agent_selection",
        _hydrated_then_refused,
    )
    slot = _channel_slot()
    state = _mock_state(slot, provider=None)
    state.sessions.reset = AsyncMock(return_value=True)
    state.conversation_log = MagicMock()
    async with TestClient(TestServer(as_owner(_make_app(state)))) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot.key}/agent",
            json={"agent": "refused-agent", "agent_kind": "template"},
        )
        assert resp.status != 200
    assert slot.agent == "old-agent"
    restored = state.conversation_log.update_metadata.call_args.args
    assert restored[0] == SESSION and restored[1]["agent"] == "old-agent"
    assert SESSION not in slack_thread_maps._hydrated_sessions
    assert SESSION not in slack_thread_maps._thread_agents

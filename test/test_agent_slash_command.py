"""``/agent <name>`` is Crew's agent switch on every surface, never kiro-cli's.

With the native skill projection on (the default), kiro-cli is refused any
``/agent <name>`` before it sees it, so Crew handles the command itself. The
main composer routes it to the agent picker's endpoint; these tests cover the
other doors: the chat runner (the split pane, and a Slack thread linked to a
dashboard chat) and a Slack thread of its own.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app_with_agent_routes, drain_background_tasks
from dashboard_owner_helpers import as_owner
from test_chat_agent_selection import TEMPLATE, _template_chat

from kiro_crew.agent_switch_command import agent_switch_target, switch_announcement
from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard.chat_handlers import SlotAgentSwitchCaller, _switch_target_busy
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.execution_context import read_session_execution

WORKER = "kirocrew-worker"


# ── The parser ──


@pytest.mark.parametrize(
    ("text", "agent"),
    [
        ("/agent fable", "fable"),
        ("  /agent kiro_planner  ", "kiro_planner"),
        ("/agent team.reviewer", "team.reviewer"),
        ("/agent\tamzn-builder", "amzn-builder"),
    ],
)
def test_a_single_agent_name_is_a_switch(text, agent):
    assert agent_switch_target(text) == agent


@pytest.mark.parametrize(
    "text",
    [
        "/agent",
        "/agent list",
        "/agent LIST",
        "/agent schema",
        "/agent create",
        "/agent create mine",
        "/agent set-default fable",
        "/agent fable please",
        "/agents fable",
        "/agentx fable",
        "switch /agent fable",
        "/agent -fable",
        "/agent fable;rm",
    ],
)
def test_anything_else_is_not_a_switch(text):
    assert agent_switch_target(text) is None


# ── The busy probe the in-turn switch relies on ──


def _state_with(*slots: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state._slots = {slot.key: slot for slot in slots}
    return state


def test_own_turn_ignores_only_its_own_reservation():
    slot = _ChatSlot("own")
    slot._turn_admission_reserved = True
    state = _state_with(slot)
    key = "dashboard:own"
    assert _switch_target_busy(state, slot, key, None) is True
    assert _switch_target_busy(state, slot, key, None, own_turn=True) is False


def test_own_turn_still_refuses_a_live_provider_turn():
    slot = _ChatSlot("own")
    provider = MagicMock(spec=chat_handlers.LLMProvider)
    provider.has_active_turn.return_value = True
    assert _switch_target_busy(_state_with(slot), slot, "dashboard:own", provider, own_turn=True)


def test_own_turn_still_refuses_another_slot_on_the_same_session():
    slot = _ChatSlot("own")
    sibling = _ChatSlot("sibling")
    sibling.linked_session_key = "dashboard:own"
    sibling._turn_admission_reserved = True
    state = _state_with(slot, sibling)
    assert _switch_target_busy(state, slot, "dashboard:own", None, own_turn=True) is True


def test_a_turns_caller_is_never_the_owner_and_keeps_its_app_scope():
    slot = _ChatSlot("own")
    slot._app = "notes"
    caller = SlotAgentSwitchCaller.for_turn(slot)
    assert caller.owner is False
    assert caller.own_turn is True
    assert caller.request is None
    assert caller.scope == {"app": "notes"}


@pytest.mark.asyncio
async def test_a_turns_caller_is_refused_an_owner_only_choice():
    denial = await SlotAgentSwitchCaller.for_turn(_ChatSlot("own")).require_owner("op")
    assert denial is not None and denial.status == 403


# ── The chat runner: split pane and linked channel threads ──


async def _chat_with_worker(tmp_path, monkeypatch):
    state, slot, _ = await asyncio.wait_for(_template_chat(tmp_path, monkeypatch), 20)
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in (TEMPLATE, WORKER) else "",
    )
    state.sessions.reset = AsyncMock(return_value=True)
    state.sessions.get_or_create.reset_mock()
    return state, slot


async def _run_command(state, slot, text):
    # Dispatch reserves the slot before the runner starts; the switch must
    # read that reservation as its own turn, not as a turn in flight.
    slot._turn_admission_reserved = True
    try:
        await asyncio.wait_for(chat_runner._run_chat(state, slot, text), 10)
    finally:
        slot._turn_admission_reserved = False
    await asyncio.wait_for(drain_background_tasks(state), 10)


def _last_assistant(slot) -> str:
    return next(row["content"] for row in reversed(slot.messages) if row["role"] == "assistant")


@pytest.mark.asyncio
async def test_agent_command_switches_through_the_pickers_transaction(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    await _run_command(state, slot, f"/agent {WORKER}")

    # Handled by Crew: the provider is never asked to run the words.
    state.sessions.get_or_create.assert_not_awaited()
    state.sessions.reset.assert_awaited()
    assert slot.agent == WORKER
    assert slot.agent_kind == "template"
    assert _last_assistant(slot) == f"🔄 Switched to agent: {WORKER}"
    execution = read_session_execution("dashboard:template-chat")
    assert execution is not None and execution.selection_kind == "template"

    # And the next turn starts on the new agent instead of being refused.
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Continue."), 10)
    await asyncio.wait_for(drain_background_tasks(state), 10)
    state.sessions.record_failure.assert_not_awaited()
    state.sessions.get_or_create.assert_awaited_once()
    assert not any(row["role"] == "error" for row in slot.messages)


@pytest.mark.asyncio
async def test_unknown_agent_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    before = (slot.agent, slot.agent_kind, slot.workspace, slot.memory_store)
    await _run_command(state, slot, "/agent no-such-agent")

    state.sessions.get_or_create.assert_not_awaited()
    state.sessions.reset.assert_not_awaited()
    assert (slot.agent, slot.agent_kind, slot.workspace, slot.memory_store) == before
    reply = _last_assistant(slot)
    assert reply.startswith("⚠️ Could not switch to agent `no-such-agent`:")
    assert "not available" in reply


@pytest.mark.asyncio
async def test_a_live_provider_turn_refuses_the_in_turn_switch(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    busy = MagicMock(spec=chat_handlers.LLMProvider)
    busy.has_active_turn.return_value = True
    state.sessions.get_provider = MagicMock(return_value=busy)
    before = slot.agent
    await _run_command(state, slot, f"/agent {WORKER}")

    state.sessions.reset.assert_not_awaited()
    assert slot.agent == before
    assert "a turn is in flight" in _last_assistant(slot)


@pytest.mark.asyncio
async def test_agent_subcommands_still_reach_the_harness(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    switch = AsyncMock()
    monkeypatch.setattr(chat_runner, "_handle_agent_command", switch)
    for text in ("/agent list", "/agent", "/agent create mine"):
        await _run_command(state, slot, text)
    switch.assert_not_awaited()


# ── The route's `announce` flag: the main composer's switch line ──


async def _post_switch(state, body):
    async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
        response = await asyncio.wait_for(
            client.post("/api/chat/slots/template-chat/agent", json=body), 10
        )
        return response.status, await response.json()


def _assistant_rows(slot) -> list[str]:
    return [row["content"] for row in slot.messages if row["role"] == "assistant"]


@pytest.mark.asyncio
async def test_announce_appends_the_same_line_the_in_turn_command_does(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    before = _assistant_rows(slot)
    status, body = await _post_switch(
        state, {"agent": WORKER, "agent_kind": "template", "announce": True}
    )
    assert status == 200 and body["ok"], body
    assert slot.agent == WORKER
    assert _assistant_rows(slot) == [*before, switch_announcement(WORKER)]
    assert switch_announcement(WORKER) == f"🔄 Switched to agent: {WORKER}"


@pytest.mark.asyncio
async def test_the_picker_without_announce_appends_nothing(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    before = list(slot.messages)
    status, body = await _post_switch(state, {"agent": WORKER, "agent_kind": "template"})
    assert status == 200 and body["ok"], body
    assert slot.agent == WORKER
    assert slot.messages == before


@pytest.mark.asyncio
async def test_a_refused_announced_switch_appends_nothing(tmp_path, monkeypatch):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    before = (list(slot.messages), slot.agent)
    status, body = await _post_switch(
        state, {"agent": "no-such-agent", "agent_kind": "template", "announce": True}
    )
    assert status == 409, body
    assert (slot.messages, slot.agent) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("announce", ["yes", 1, None])
async def test_a_non_boolean_announce_is_refused_before_the_switch(tmp_path, monkeypatch, announce):
    state, slot = await _chat_with_worker(tmp_path, monkeypatch)
    before = (list(slot.messages), slot.agent)
    status, body = await _post_switch(
        state, {"agent": WORKER, "agent_kind": "template", "announce": announce}
    )
    assert status == 400 and body["code"] == "invalid_announce", body
    state.sessions.reset.assert_not_awaited()
    assert (slot.messages, slot.agent) == before


# ── Slack ──


def _slack():
    client = MagicMock()
    client.post_message = AsyncMock()
    client.post_blocks = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_slack_thread_agent_command_switches_the_thread_agent():
    from kiro_crew.slack import handler

    thread_agent = AsyncMock(return_value="")
    with (
        patch.object(handler, "is_owner", return_value=True),
        patch.object(handler, "_bang_thread_agent", thread_agent),
    ):
        handled = await handler._route_bang_command(
            "/agent fable", _slack(), MagicMock(), "C1", "t1", "m1", "slack:t1", "U1", None
        )
    assert handled is True
    thread_agent.assert_awaited_once()
    assert thread_agent.await_args.args[0] == "!ta fable"


@pytest.mark.asyncio
async def test_slack_agent_command_is_owner_only():
    from kiro_crew.slack import handler

    slack = _slack()
    thread_agent = AsyncMock(return_value="")
    with (
        patch.object(handler, "is_owner", return_value=False),
        patch.object(handler, "_bang_thread_agent", thread_agent),
        patch.object(handler, "sel", return_value=MagicMock()),
    ):
        handled = await handler._route_bang_command(
            "/agent fable", slack, MagicMock(), "C1", "t1", "m1", "slack:t1", "U2", None
        )
    assert handled is True
    thread_agent.assert_not_awaited()
    slack.post_message.assert_awaited_once_with("C1", "⛔ Owner-only command.", "t1")


@pytest.mark.asyncio
async def test_slack_agent_subcommands_fall_through():
    from kiro_crew.slack import handler

    thread_agent = AsyncMock(return_value="")
    with patch.object(handler, "_bang_thread_agent", thread_agent):
        for text in ("/agent list", "/agent", "/agent fable please"):
            handled = await handler._route_bang_command(
                text, _slack(), MagicMock(), "C1", "t1", "m1", "slack:t1", "U1", None
            )
            assert handled is False
    thread_agent.assert_not_awaited()


def test_slack_thread_agent_accepts_kiro_cli_builtins(tmp_path):
    from kiro_crew.slack import handler

    with (
        patch.object(handler, "kiro_agents_dir", return_value=tmp_path / "agents"),
        patch.object(handler, "_resolve_cc_agent_name", return_value=None),
    ):
        assert handler._resolve_agent_name("kiro_planner") == "kiro_planner"
        assert handler._resolve_agent_name("kiro_help") is None


def _linked_ds():
    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=False)
    slot.key = "slot1"
    slot._queue = []
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._background_tasks = set()
    ds.broadcast_ws = MagicMock()
    ds.push_slots_update = MagicMock()
    return ds, slot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "delivered"),
    [
        ("<@UBOT|kirocrew> /agent fable", "/agent fable"),
        ("/agent fable", "/agent fable"),
        # Only the command loses its mention; ordinary speech is delivered as typed.
        ("<@UBOT|kirocrew> hello", "<@UBOT|kirocrew> hello"),
    ],
)
async def test_linked_thread_delivers_the_agent_command_to_the_chat_runner(text, delivered):
    from kiro_crew.slack import handler

    ds, _slot = _linked_ds()
    run_chat = AsyncMock()
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", run_chat),
    ):
        handled = await handler.maybe_route_linked_thread(
            text, "slack:t1", "U1", "C1", _slack(), "t1"
        )
        await asyncio.gather(*list(ds._background_tasks), return_exceptions=True)
    assert handled is True
    run_chat.assert_awaited_once()
    assert run_chat.await_args.args[2] == delivered

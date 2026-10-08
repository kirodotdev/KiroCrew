"""Guide and card tools answer only a turn the user sent from the dashboard.

A conversation that started in a messaging channel is mirrored into a dashboard
slot, so a live slot alone says nothing about who is asking. These tests drive
the real admission (``dashboard.handlers.guide._resolve_agent_caller``) through
the real caller resolution (``session_control.caller_slot_key``, matching the
channel session key the MCP process authenticates as), and the real turn runner
that records the turn's opener provenance (``chat_runner._run_chat``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from guide_route_helpers import CREWMATE, FakeState, agent, in_dashboard_turn, run_guide_app

from kiro_crew import mcp_guide
from kiro_crew.dashboard.handlers import change_cards as card_routes

SLACK_KEY = "slack:C1:1700000000.000100"


def _mirrored_channel_slot(state: FakeState):
    """The dashboard slot a Slack thread is surfaced as (``channel_slots``)."""
    slot = state.open_slot("chat-slack-1")
    slot.linked_session_key = SLACK_KEY
    slot.channel_origin = True
    return slot


def _start_from(session_key: str, state: FakeState) -> tuple[int, dict]:
    async def go(client):
        resp = await client.post(
            "/api/guide/agent/start",
            json={"actions": CREWMATE},
            headers=agent(session_key),
        )
        return resp.status, await resp.json()

    return run_guide_app(go, state)


def test_a_channel_turn_on_the_mirrored_slot_is_refused() -> None:
    """A Slack message the gateway ran on the mirrored slot is a channel turn."""
    state = FakeState()
    slot = _mirrored_channel_slot(state)
    slot._turn_channel_origin = True
    status, body = _start_from(SLACK_KEY, state)
    assert (status, body["code"]) == (403, "channel_caller")
    assert body["code"] in mcp_guide._OFF_DASHBOARD_CODES


def test_a_channel_turn_running_on_its_own_session_is_refused() -> None:
    """The channel's own session answered; the mirrored slot is running nothing."""
    state = FakeState()
    slot = _mirrored_channel_slot(state)
    slot.task = None
    status, body = _start_from(SLACK_KEY, state)
    assert (status, body["code"]) == (409, "no_dashboard_turn")
    assert body["code"] in mcp_guide._OFF_DASHBOARD_CODES


def test_a_dashboard_turn_narrowed_by_a_channel_steer_is_refused() -> None:
    state = FakeState()
    slot = state.open_slot("chat-1")
    slot._turn_channel_narrowed = True
    status, body = _start_from("dashboard:chat-1", state)
    assert (status, body["code"]) == (403, "channel_caller")


def test_the_user_typing_in_the_mirrored_slot_is_admitted() -> None:
    """The same mirrored slot, on a turn the user sent from the dashboard."""
    state = FakeState()
    _mirrored_channel_slot(state)
    status, body = _start_from(SLACK_KEY, state)
    assert status == 200, body


def test_a_dashboard_turn_is_admitted() -> None:
    state = FakeState()
    in_dashboard_turn(state.open_slot("chat-1"))
    status, body = _start_from("dashboard:chat-1", state)
    assert status == 200, body


def test_find_ui_reads_answer_a_channel_turn_with_not_observed() -> None:
    """The live read ``find_ui`` makes is refused like any other, never a crash."""
    state = FakeState()
    slot = _mirrored_channel_slot(state)
    slot._turn_channel_origin = True

    async def go(client):
        resp = await client.get("/api/guide/agent/language", headers=agent(SLACK_KEY))
        return resp.status, await resp.json()

    status, body = run_guide_app(go, state)
    assert (status, body["code"]) == (403, "channel_caller")


def _post_from(path: str, body: dict, state: FakeState) -> tuple[int, dict]:
    async def go(client):
        resp = await client.post(path, json=body, headers=agent("dashboard:chat-1"))
        return resp.status, await resp.json()

    propose = ("POST", "/api/cards/agent/propose", card_routes.api_cards_agent_propose)
    return run_guide_app(go, state, extra_routes=[propose])


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/guide/agent/start", {"actions": CREWMATE}),
        ("/api/cards/agent/propose", {"kind": "chat.rename", "params": {"title": "x"}}),
    ],
)
def test_a_turn_the_user_did_not_send_cannot_start_a_guide_or_a_card(path: str, body: dict) -> None:
    """A loop wake, a cron or app injection, ``session_send`` or a sub-agent
    completion runs on the dashboard slot too, but the person did not ask."""
    state = FakeState()
    state.open_slot("chat-1")._turn_user_sent = False
    status, got = _post_from(path, body, state)
    assert (status, got["code"]) == (403, "not_user_turn")
    assert got["code"] in mcp_guide._OFF_DASHBOARD_CODES


def test_a_turn_the_user_did_not_send_still_reads() -> None:
    state = FakeState()
    state.open_slot("chat-1")._turn_user_sent = False

    async def go(client):
        resp = await client.get("/api/guide/agent/language", headers=agent("dashboard:chat-1"))
        return resp.status

    assert run_guide_app(go, state) == 200


# ── the runner records the provenance the admission reads ──


@pytest.mark.asyncio
@pytest.mark.parametrize(("user", "channel"), [(True, True), (True, False), (False, False)])
async def test_the_turn_records_its_opener_provenance_for_its_duration(
    tmp_path, monkeypatch, user: bool, channel: bool
) -> None:
    from test_acp_tool_identity import _stub_state

    from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
    from kiro_crew.dashboard import chat_runner as cr

    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    seen: list[bool] = []

    async def _stream(_msg):
        seen.append((slot._turn_channel_origin, slot._turn_user_sent))
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok")
        yield AcpEvent(kind=EVENT_COMPLETE)

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))

    await cr._run_chat(
        state, slot, "hello", _directive_user_origin=user, _directive_channel_origin=channel
    )
    assert seen == [(channel, user and not channel)]
    assert slot._turn_channel_origin is False, "the provenance ends with the turn"
    assert slot._turn_user_sent is False, "the provenance ends with the turn"

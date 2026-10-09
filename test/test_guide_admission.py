"""The ``kirocrew-guide`` tools answer only a turn the user sent from the dashboard.

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
from guide_route_helpers import FakeState, agent, in_dashboard_turn, run_guide_app

from kiro_crew import mcp_guide

SLACK_KEY = "slack:C1:1700000000.000100"


def _mirrored_channel_slot(state: FakeState):
    """The dashboard slot a Slack thread is surfaced as (``channel_slots``)."""
    slot = state.open_slot("chat-slack-1")
    slot.linked_session_key = SLACK_KEY
    slot.channel_origin = True
    return slot


def _rename_from(session_key: str, state: FakeState) -> tuple[int, dict]:
    async def go(client):
        resp = await client.post(
            "/api/guide/agent/rename", json={"name": "Pebble"}, headers=agent(session_key)
        )
        return resp.status, await resp.json()

    return run_guide_app(go, state)


def _admitted(body: dict) -> bool:
    """The admission let the call through (the route may still refuse on its own)."""
    return body.get("code") not in mcp_guide._OFF_DASHBOARD_CODES


def test_a_channel_turn_on_the_mirrored_slot_is_refused() -> None:
    """A Slack message the gateway ran on the mirrored slot is a channel turn."""
    state = FakeState()
    _mirrored_channel_slot(state)._turn_channel_origin = True
    status, body = _rename_from(SLACK_KEY, state)
    assert (status, body["code"]) == (403, "channel_caller")


def test_a_channel_turn_running_on_its_own_session_is_refused() -> None:
    """The channel's own session answered; the mirrored slot is running nothing."""
    state = FakeState()
    _mirrored_channel_slot(state).task = None
    status, body = _rename_from(SLACK_KEY, state)
    assert (status, body["code"]) == (409, "no_dashboard_turn")


def test_a_dashboard_turn_narrowed_by_a_channel_steer_is_refused() -> None:
    state = FakeState()
    state.open_slot("chat-1")._turn_channel_narrowed = True
    status, body = _rename_from("dashboard:chat-1", state)
    assert (status, body["code"]) == (403, "channel_caller")


@pytest.mark.parametrize("key", [SLACK_KEY, "dashboard:chat-1"])
def test_a_dashboard_turn_the_user_sent_is_admitted(key: str) -> None:
    """The mirrored slot on a turn typed in the dashboard, or an ordinary chat."""
    state = FakeState()
    if key == SLACK_KEY:
        _mirrored_channel_slot(state)
    else:
        in_dashboard_turn(state.open_slot("chat-1"))
    _status, body = _rename_from(key, state)
    assert _admitted(body), body


def test_a_turn_the_user_did_not_send_is_refused() -> None:
    """A loop wake, a cron or app injection, ``session_send`` or a sub-agent
    completion runs on the dashboard slot too, but the person did not ask."""
    state = FakeState()
    state.open_slot("chat-1")._turn_user_sent = False
    status, body = _rename_from("dashboard:chat-1", state)
    assert (status, body["code"]) == (403, "not_user_turn")


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
    seen: list[tuple[bool, bool]] = []

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

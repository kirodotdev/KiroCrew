"""The owner's `!stop` in a live Conductor thread passes the inbound activation drops.

A thread can start in a channel whose activation is review or off, or in mention mode
where an unmentioned message is dropped. Stop only removes work, so the owner's pure
``!stop`` in such a thread must reach the thread's Stop gate. Anyone else's message,
and any other text from the owner, is dropped as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from slack_events_coverage_helpers import _fresh_trusted_bot_turn_ledger  # noqa: F401
from slack_events_coverage_helpers import _isolated_home_fixture  # noqa: F401
from slack_events_coverage_helpers import _mock_sel_fixture  # noqa: F401
from slack_events_coverage_helpers import _drain, _event, _make_orch

from kiro_crew.config.loader import (
    ACTIVATION_MENTION,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    ChannelConfig,
)
from kiro_crew.slack import events as ev
from kiro_crew.slack.code_channels import routing
from kiro_crew.slack.code_channels.store import ROOM_PRIVATE, Binding, CodeChannelStore, set_store

CHANNEL = "G0PRIVATE01"
ANCHOR = "200.000002"


@pytest.fixture
def thread_store(tmp_path):
    store = CodeChannelStore(path=tmp_path / "slack-code-channels.json")
    store.load()
    store.put_thread_binding(
        Binding(
            channel_id=CHANNEL,
            team_id="T1",
            session_key="chat-9-1",
            anchor_ts=ANCHOR,
            thread=True,
            room=ROOM_PRIVATE,
        )
    )
    set_store(store)
    yield store
    set_store(None)


async def _route(activation: str, *, user: str, text: str, switch_on: bool = True) -> AsyncMock:
    orch = _make_orch(channels={CHANNEL: ChannelConfig(activation=activation)})
    gate = AsyncMock(return_value=routing.DROP)
    event = _event(channel=CHANNEL, user=user, text=text, thread_ts=ANCHOR, ts="200.000009")
    with (
        patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
        patch("kiro_crew.slack.events.code_channel_gate", gate),
        patch("kiro_crew.slack.handler.is_owner", lambda u: u == "U_OWNER"),
        patch("kiro_crew.slack.code_channels.routing._self_user_id", lambda: "U0BOT"),
        patch(
            "kiro_crew.slack.code_channels.thread_runtime.thread_conductor_on",
            lambda _cfg: switch_on,
        ),
        patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock),
    ):
        await ev._route_message(orch, event, ev.SeenCache())
        await _drain(orch)
    return gate


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", [ACTIVATION_OFF, ACTIVATION_REVIEW, ACTIVATION_MENTION])
async def test_the_owners_stop_reaches_the_thread_gate(thread_store, activation: str) -> None:
    gate = await _route(activation, user="U_OWNER", text="!stop")
    gate.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", [ACTIVATION_OFF, ACTIVATION_REVIEW, ACTIVATION_MENTION])
async def test_anything_else_is_still_dropped(thread_store, activation: str) -> None:
    for user, text in (("U_OWNER", "hello"), ("U_OWNER", "!stop now"), ("U_OTHER", "!stop")):
        gate = await _route(activation, user=user, text=text)
        gate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", [ACTIVATION_OFF, ACTIVATION_REVIEW, ACTIVATION_MENTION])
async def test_an_idle_conductor_with_the_switch_off_leaves_the_drop_in_place(
    thread_store, activation: str
) -> None:
    """The gate would pass the message on, so the channel's own drop must still apply."""
    gate = await _route(activation, user="U_OWNER", text="!stop", switch_on=False)
    gate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", [ACTIVATION_OFF, ACTIVATION_REVIEW, ACTIVATION_MENTION])
async def test_a_stop_addressed_to_someone_else_is_not_a_stop(
    thread_store, activation: str
) -> None:
    gate = await _route(activation, user="U_OWNER", text="<@U0OTHER001> !stop")
    gate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("activation", [ACTIVATION_OFF, ACTIVATION_REVIEW, ACTIVATION_MENTION])
async def test_a_stop_that_mentions_this_bot_is_a_stop(thread_store, activation: str) -> None:
    gate = await _route(activation, user="U_OWNER", text="<@U0BOT> !stop")
    gate.assert_awaited()

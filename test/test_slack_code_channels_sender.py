"""The Slack sender a code-channel turn records on the bound session's user row.

Contract (Shared Crew reads it): every person's message a code channel hands to
its session -- the request that opened the channel and each later message in
it -- stores ``meta["slack_sender"] = {"team_id", "enterprise_id", "user_id"}``,
raw Slack ids only. A bot copy, Slack's origin-context copy, or an id that is
not a Slack id leaves the key off. Nothing decides access from it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_code_channels_helpers import OWNER, Env, make_env, mention

from kiro_crew.slack.code_channels import creation, routing
from kiro_crew.slack.code_channels.creation import ORIGIN_CONTEXT_BLOCK_ID, slack_sender
from kiro_crew.slack.code_channels.store import Binding

pytestmark = pytest.mark.asyncio

SENDER = {"team_id": "T1ABC", "enterprise_id": "", "user_id": OWNER}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


def _message(**extra: Any) -> dict[str, Any]:
    event = {
        "type": "message",
        "user": OWNER,
        "channel": "C0CODE",
        "ts": "301.000001",
        "text": "now add a regression test",
        "team": "T1ABC",
        "enterprise": "",
    }
    event.update(extra)
    return event


def _bind(env: Env) -> Binding:
    binding = Binding(
        channel_id="C0CODE",
        team_id="T1ABC",
        session_key="chat-9-1",
        anchor_ts="300.000001",
        origin_channel_id="C0ORIGIN",
        origin_ts="1700000000.000100",
    )
    env.store.put_binding(binding)
    assert env.state is not None
    env.state.linked[binding.anchor_ts] = SimpleNamespace(key="chat-9-1", running=False)
    env.info("C0CODE")
    return binding


async def _directed(env: Env, event: dict[str, Any]) -> bool:
    result = await routing.inbound_gate(
        env.orch,
        event,
        text=event["text"],
        sender_id=event["user"],
        authorized=True,
        from_trusted_bot=False,
    )
    return await routing.dispatch(
        env.orch,
        result,
        event=event,
        clean_text=event["text"],
        sender_id=event["user"],
        was_mention=False,
    )


# ── Which messages carry a sender ────────────────────────────────────────────


def test_a_persons_message_names_its_raw_slack_ids() -> None:
    assert slack_sender(_message()) == SENDER
    grid = _message(enterprise="E0ORG1", user="W0ORGUSER")
    assert slack_sender(grid) == {
        "team_id": "T1ABC",
        "enterprise_id": "E0ORG1",
        "user_id": "W0ORGUSER",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"bot_id": "B0BOT"},
        {"bot_profile": {"id": "B0BOT"}},
        {"subtype": "bot_message"},
        {"subtype": "message_changed"},
        {"blocks": [{"type": "context", "block_id": ORIGIN_CONTEXT_BLOCK_ID, "elements": []}]},
    ],
)
def test_a_bot_copy_or_the_origin_copy_has_no_sender(extra: dict[str, Any]) -> None:
    assert slack_sender(_message(**extra)) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"team": ""},
        {"team": "C0NOTATEAM"},
        {"team": "t1abc"},
        {"team": "T1ABC\n"},
        {"team": None},
        {"user": "B0NOTAUSER"},
        {"user": "U1|name"},
        {"user": "U" + "A" * 40},
        {"user": "U" + "A" * 32},  # one past the reader's 31-character body
        {"team": "T" + "1" * 32},
        {"team": "T"},  # a prefix alone has no body
        {"user": "U1\n"},
        {"enterprise": "E"},
        {"user": "u1abc"},
        {"enterprise": "E" + "0" * 32},
        {"enterprise": "T0NOTANORG"},
        {"enterprise": "E0<script>"},
        {"enterprise": 7},
    ],
)
def test_a_malformed_id_leaves_the_key_off_entirely(extra: dict[str, Any]) -> None:
    assert slack_sender(_message(**extra)) is None


def test_the_id_shapes_are_the_shared_crew_readers_verbatim() -> None:
    # Shared Crew reads slack_sender against exactly these patterns: pinned as
    # literal strings so the producer and the reader cannot drift apart.
    assert creation._SLACK_TEAM_ID.pattern == r"\AT[A-Z0-9]{1,31}\Z"
    assert creation._SLACK_ENTERPRISE_ID.pattern == r"\AE[A-Z0-9]{1,31}\Z"
    assert creation._SLACK_USER_ID.pattern == r"\A[UW][A-Z0-9]{1,31}\Z"
    for compiled in (
        creation._SLACK_TEAM_ID,
        creation._SLACK_ENTERPRISE_ID,
        creation._SLACK_USER_ID,
    ):
        assert compiled.flags & re.IGNORECASE == 0  # case-sensitive


@pytest.mark.parametrize(
    ("team", "enterprise", "user"),
    [
        ("T1", "", "U1"),  # the shortest body the reader accepts
        ("T" + "A" * 31, "E" + "B" * 31, "W" + "C" * 31),  # the longest
    ],
)
def test_ids_at_the_readers_length_bounds_are_attributed(
    team: str, enterprise: str, user: str
) -> None:
    event = _message(team=team, enterprise=enterprise, user=user)
    assert slack_sender(event) == {"team_id": team, "enterprise_id": enterprise, "user_id": user}


# ── Every route into the bound session carries it ────────────────────────────


async def test_a_message_in_a_code_channel_carries_its_sender(env: Env) -> None:
    _bind(env)
    assert await _directed(env, _message()) is True
    assert env.senders == [SENDER]


async def test_a_bot_copy_in_a_code_channel_gets_no_sender(env: Env) -> None:
    # Slack's own origin copy is dropped before it is routed; a lookalike
    # carrying the origin block (an unmatched link) is routed without a sender.
    _bind(env)
    lookalike = _message(
        blocks=[{"type": "context", "block_id": ORIGIN_CONTEXT_BLOCK_ID, "elements": []}]
    )
    assert await _directed(env, lookalike) is True
    assert env.senders == [None]


async def test_the_request_that_opens_a_channel_carries_the_origin_sender(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    monkeypatch.setattr(routing, "_queue_seed_context", lambda _binding, _framing: True)
    event = {**mention(), "team": "T1ABC", "enterprise": "E0ORG1"}
    assert await routing.maybe_open_code_channel(env.orch, event, "fix it", OWNER) is True
    assert env.senders == [{"team_id": "T1ABC", "enterprise_id": "E0ORG1", "user_id": OWNER}]


async def test_an_opener_carrying_the_thread_inline_names_no_single_sender(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The thread context could not be seated as background context, so it rides
    # inside the row: other people's words, so no one sender is named.
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0CODE"}
    monkeypatch.setattr(routing, "_queue_seed_context", lambda _binding, _framing: False)
    event = {**mention(), "team": "T1ABC", "enterprise": "E0ORG1"}
    assert await routing.maybe_open_code_channel(env.orch, event, "fix it", OWNER) is True
    assert env.senders == [None]


async def test_the_session_user_row_stores_the_sender_as_meta() -> None:
    """Through the real linked-slot delivery: the appended user row's meta."""
    from kiro_crew.slack import handler

    slot = MagicMock()
    slot.running = False
    slot.key = "chat-9-1"
    slot.window_pending = False
    slot._has_reader = False
    state = MagicMock()
    state.get_linked_slot = MagicMock(return_value=slot)
    state._background_tasks = set()
    binding = Binding(
        channel_id="C0CODE", team_id="T1ABC", session_key="chat-9-1", anchor_ts="300.000001"
    )
    orch = SimpleNamespace(slack=AsyncMock())
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock),
    ):
        assert await routing._route_to_slot(orch, binding, "hi", OWNER, sender=SENDER)
        assert await routing._route_to_slot(orch, binding, "again", OWNER)
        slot.running = True  # a busy session queues the message: the row still carries it
        assert await routing._route_to_slot(orch, binding, "queued", OWNER, sender=SENDER)
    first, second, queued = slot.append.call_args_list
    assert first.args[:2] == ("user", "hi")
    assert first.kwargs["meta"] == {"slack_sender": SENDER}
    assert second.kwargs.get("meta") is None  # no sender: no key at all
    assert queued.args[:2] == ("user", "queued")
    assert queued.kwargs["meta"] == {"slack_sender": SENDER}
    slot.queue_append.assert_called_once()


# ── A queued message: the drain writes the turn's own user row ───────────────


@pytest.fixture
def drain_fixture(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from test_responsive_chat import fixture as responsive_fixture

    return responsive_fixture.__wrapped__(tmp_path, monkeypatch)  # type: ignore[attr-defined]


async def _queue_from_slack(state: Any, slot: Any, text: str, sender: dict[str, str]) -> None:
    from kiro_crew.slack import handler

    state.get_linked_slot = lambda _ts: slot
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
    ):
        routed = await handler.maybe_route_linked_thread(
            text,
            "slack:300.000001",
            sender["user_id"],
            "C0CODE",
            AsyncMock(),
            "300.000001",
            user_meta={"slack_sender": dict(sender)},
        )
    assert routed is True


async def _drain(state: Any, slot: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import asyncio

    from kiro_crew.dashboard import chat_runner

    slot.task.cancel()
    slot.task = None
    monkeypatch.setattr(chat_runner, "_run_chat", AsyncMock())
    assert await chat_runner._start_next_queued_turn(state, slot)
    await asyncio.wait_for(slot.task, 30)
    return [m for m in slot.messages if m.get("role") == "user"][-1]


async def test_a_queued_message_turn_row_carries_its_sender(
    drain_fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_responsive_chat import _busy

    state, slot, *_ = drain_fixture
    _busy(slot)
    await _queue_from_slack(state, slot, "add a test too", SENDER)
    assert slot._queue and slot._queue[0]["meta"]["slack_sender"] == SENDER
    row = await _drain(state, slot, monkeypatch)
    assert row["content"] == "add a test too"
    assert row["meta"]["slack_sender"] == SENDER


@pytest.mark.parametrize("same_sender", [True, False])
async def test_a_merged_row_names_a_sender_only_when_all_agree(
    drain_fixture: Any, monkeypatch: pytest.MonkeyPatch, same_sender: bool
) -> None:
    from test_responsive_chat import _busy

    state, slot, _request, cfg, *_ = drain_fixture
    cfg.dashboard.merge_queued_messages = True
    _busy(slot)
    other = {**SENDER, "user_id": "UALICE"}
    await _queue_from_slack(state, slot, "first", SENDER)
    await _queue_from_slack(state, slot, "second", SENDER if same_sender else other)
    row = await _drain(state, slot, monkeypatch)
    assert "first" in row["content"] and "second" in row["content"]
    if same_sender:
        assert row["meta"]["slack_sender"] == SENDER
    else:
        assert "slack_sender" not in row["meta"]  # never one person's name on two people's words


async def test_an_edited_queued_message_drops_the_sender(
    drain_fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_responsive_chat import _busy

    state, slot, *_ = drain_fixture
    _busy(slot)
    await _queue_from_slack(state, slot, "add a test too", SENDER)
    entry_id = slot._queue[0]["id"]
    assert slot.queue_edit_by_id(entry_id, "the owner's own words")
    row = await _drain(state, slot, monkeypatch)
    assert row["content"] == "the owner's own words"
    assert "slack_sender" not in (row.get("meta") or {})  # not the Slack sender's words now


async def test_an_unchanged_edit_keeps_the_sender(
    drain_fixture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_responsive_chat import _busy

    state, slot, *_ = drain_fixture
    _busy(slot)
    await _queue_from_slack(state, slot, "add a test too", SENDER)
    assert slot.queue_edit_by_id(slot._queue[0]["id"], "add a test too")
    row = await _drain(state, slot, monkeypatch)
    assert row["meta"]["slack_sender"] == SENDER


async def test_a_routed_answer_hands_its_sender_to_the_linked_slot() -> None:
    """``handle_message(user_meta=...)`` reaches the linked-slot delivery unchanged."""
    from kiro_crew.slack import handler

    route = AsyncMock(return_value=True)
    with patch.object(handler, "maybe_route_linked_thread", route):
        await handler.handle_message(
            AsyncMock(),
            MagicMock(),
            "C0CODE",
            "B",
            None,
            "302.000001",
            OWNER,
            team_id="T1ABC",
            route_pinned=True,
            user_meta={"slack_sender": SENDER},
        )
    assert route.await_args.kwargs["user_meta"] == {"slack_sender": SENDER}


async def test_row_meta_cannot_overwrite_the_queue_containment_stamp(drain_fixture: Any) -> None:
    from test_responsive_chat import _busy

    from kiro_crew.dashboard.session_control_parts.delivery_containment import (
        QUEUED_CONTAINMENT_META_KEY,
    )
    from kiro_crew.slack import handler

    state, slot, *_ = drain_fixture
    _busy(slot)
    state.get_linked_slot = lambda _ts: slot
    forged = {"slack_sender": dict(SENDER), QUEUED_CONTAINMENT_META_KEY: {"forged": True}}
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
    ):
        await handler.maybe_route_linked_thread(
            "x", "slack:1", OWNER, "C0CODE", AsyncMock(), "300.000001", user_meta=forged
        )
    stamp = slot._queue[0]["meta"][QUEUED_CONTAINMENT_META_KEY]
    assert stamp != {"forged": True}  # the gateway's own admission stamp wins


async def test_a_dashboard_send_cannot_pass_itself_off_as_a_slack_user(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the Slack routing writes ``slack_sender``: the dashboard send ingress
    strips a caller-supplied one before it reaches the row."""
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app, _make_state

    from kiro_crew.dashboard.chat_handlers import RESERVED_ROW_META_KEYS

    assert "slack_sender" in RESERVED_ROW_META_KEYS
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", AsyncMock())
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("test")
    forged = {"slack_sender": dict(SENDER), "sendId": "s-1"}
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat?ws=1", json={"slot": "test", "message": "hello", "meta": forged}
        )
        assert resp.status == 200
    row = next(r for r in reversed(slot.messages) if r.get("role") == "user")
    assert "slack_sender" not in (row.get("meta") or {})
    assert (row.get("meta") or {}).get("sendId") == "s-1"  # the rest is untouched

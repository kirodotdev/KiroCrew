"""Slack Code channels through the real ``_route_message`` seams.

With ``slack.code_channels`` off the gateway must behave exactly as without the
feature; with it on, a code channel answers without a mention and a mention
that asks for multi-step work moves into a new channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_code_channels_helpers import OWNER, SELF_USER, Env, make_env, mention

from kiro_crew.config.loader import KiroCrewConfig, MessagingConfig
from kiro_crew.slack import features
from kiro_crew.slack.code_channels.classifier import RouteDecision
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.events import SeenCache, _route_message

pytestmark = pytest.mark.asyncio


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[Env]:
    yield from make_env(monkeypatch, tmp_path)


def _orch(env: Env) -> MagicMock:
    orch = MagicMock()
    orch._cfg = KiroCrewConfig(messaging=MessagingConfig(use_transport=False))
    orch.channel_history = MagicMock()
    orch.channel_history._user_names = {}
    orch.slack = env.slack
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    for name in (
        "ctx_builder",
        "cron_svc",
        "conv_log",
        "consolidator",
        "subagent_mgr",
        "task_runner",
    ):
        setattr(orch, name, None)
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    env.orch = orch
    return orch


async def _route(env: Env, event: dict[str, Any], *, is_mention: bool) -> MagicMock:
    orch = _orch(env)
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as handle:
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await _route_message(orch, event, SeenCache(), is_mention=is_mention)
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
    return handle


def _bind(env: Env) -> None:
    env.store.put_binding(
        Binding(channel_id="C0CODE", team_id="T1", session_key="chat-9-1", anchor_ts="300.000001")
    )
    env.info("C0CODE")


def _plain(channel: str, text: str = "now add a regression test") -> dict[str, Any]:
    return {
        "type": "message",
        "user": OWNER,
        "channel": channel,
        "ts": "301.000001",
        "text": text,
        "team": "T1",
    }


async def test_toggle_off_is_identical_to_main_in_a_bound_channel(env: Env) -> None:
    _bind(env)
    env.on = False
    handle = await _route(env, _plain("C0CODE"), is_mention=False)
    # Mention-mode channel, no mention: ignored, as on main. No Slack lookups.
    handle.assert_not_called()
    assert env.routed == [] and env.api.calls == []


async def test_toggle_off_mention_takes_the_normal_path(env: Env) -> None:
    env.on = False
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_called_once()
    assert env.classified == [] and env.api.calls == [] and env.sel.rows == []


@pytest.mark.parametrize("why", ["scope_missing", "latched"])
async def test_toggle_on_but_unavailable_is_identical_to_main(env: Env, why: str) -> None:
    if why == "scope_missing":
        features.record_granted_scopes(["chat:write"])
    else:
        features.latch_unavailable(features.CODE_CHANNELS, "feature_disabled")
    _bind(env)
    handle = await _route(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()  # mention-mode channel, no mention: ignored as on main
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_called_once()  # the normal in-place answer
    assert env.routed == [] and env.classified == [] and env.api.calls == []
    assert env.slack.posts == [] and env.slack.ephemerals == []


async def test_plain_message_in_a_code_channel_reaches_its_session(env: Env) -> None:
    _bind(env)
    handle = await _route(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == [("C0CODE", "now add a regression test", OWNER)]


async def test_message_addressed_to_someone_else_is_left_alone(env: Env) -> None:
    _bind(env)
    handle = await _route(env, _plain("C0CODE", "<@UALICE> can you review?"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == []


def _forward(note: str, body: str = "the login test fails on main") -> dict[str, Any]:
    """A message forwarded into the channel: Slack's share attachment names its author."""
    event = _plain("C0CODE", note)
    event["attachments"] = [{"is_share": True, "text": body, "author_id": "UBOB"}]
    return event


@pytest.mark.parametrize("note", ["", "please fix this", f"<@{SELF_USER}> please fix this"])
async def test_a_forward_into_a_code_channel_reaches_its_session(env: Env, note: str) -> None:
    """The assembled forward names its author (``<@UBOB>``): who the message is
    for is read from the sender's own note, never from the forwarded content."""
    _bind(env)
    handle = await _route(env, _forward(note), is_mention=bool(note))
    handle.assert_not_called()
    [(channel, text, sender)] = env.routed
    assert (channel, sender) == ("C0CODE", OWNER)
    assert "the login test fails on main" in text


async def test_a_forward_whose_note_is_for_someone_else_is_left_alone(env: Env) -> None:
    _bind(env)
    handle = await _route(env, _forward("<@UALICE> can you look?"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == []


async def test_a_forward_of_a_message_mentioning_someone_else_still_reaches_the_session(
    env: Env,
) -> None:
    _bind(env)
    await _route(env, _forward("", body="<@UALICE> the build is red"), is_mention=False)
    assert len(env.routed) == 1


async def test_multi_step_mention_moves_into_a_new_channel(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_not_called()
    assert env.store.get_binding("C0NEW") is not None
    assert env.routed and env.routed[0][0] == "C0NEW"


async def _route_shared(env: Env, event: dict[str, Any], *, is_mention: bool) -> MagicMock:
    """:func:`_route` for a shared user (``slack.shared_access``): admitted, not the owner."""
    with (
        patch("kiro_crew.slack.events.is_shared_listed", return_value=True),
        patch("kiro_crew.slack.handler.is_shared_listed", return_value=True),
    ):
        return await _route(env, event, is_mention=is_mention)


async def test_a_shared_user_is_refused_in_a_bound_code_channel(env: Env) -> None:
    _bind(env)
    handle = await _route_shared(env, _plain("C0CODE"), is_mention=False)
    handle.assert_not_called()
    assert env.routed == []
    # Quietly: a member's messages are mostly not for the bot.
    assert env.slack.ephemerals == [] and env.slack.posts == []


async def test_a_shared_user_cannot_answer_a_card_in_a_code_channel(env: Env) -> None:
    from kiro_crew.slack.code_channels.routing import options_answer_permitted

    _bind(env)
    assert options_answer_permitted("C0CODE", OWNER) is True
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert options_answer_permitted("C0CODE", OWNER) is False


async def test_a_shared_user_cannot_answer_a_card_in_a_bound_channel_with_the_feature_off(
    env: Env,
) -> None:
    from kiro_crew.slack.code_channels.routing import options_answer_permitted

    _bind(env)
    env.on = False
    assert options_answer_permitted("C0CODE", OWNER) is True
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert options_answer_permitted("C0CODE", OWNER) is False
        assert options_answer_permitted("C0PLAIN", OWNER) is True


async def test_the_code_channel_gate_itself_drops_a_shared_user(env: Env) -> None:
    from kiro_crew.slack.code_channels.routing import DROP, inbound_gate

    _bind(env)
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        result = await inbound_gate(
            _orch(env),
            _plain("C0CODE"),
            text="now add a regression test",
            sender_id=OWNER,
            authorized=False,
            from_trusted_bot=False,
        )
    assert result is DROP and env.routed == []


async def test_a_shared_users_mention_in_a_bound_channel_is_refused_with_the_feature_off(
    env: Env,
) -> None:
    # The owner turned Slack Code off and the binding (and its session) stayed.
    _bind(env)
    env.on = False
    event = {**_plain("C0CODE", f"<@{SELF_USER}> now add a regression test")}
    handle = await _route_shared(env, event, is_mention=True)
    handle.assert_not_called()
    assert env.routed == []


async def test_a_shared_users_linked_thread_reply_is_refused_with_no_slot_and_the_feature_off(
    env: Env,
) -> None:
    from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread

    _bind(env)
    env.on = False
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        outcome = await route_linked_thread(
            "now add a regression test",
            "slack:300.000001",
            OWNER,
            "C0CODE",
            env.slack,
            "300.000001",
        )
    assert outcome is LinkedRoute.REFUSED


async def test_a_revoked_channel_stays_the_owners_with_the_feature_off(env: Env) -> None:
    # Slack stopped listing the bot there; the anchor thread still leads to the session.
    from kiro_crew.slack.code_channels.routing import options_answer_permitted
    from kiro_crew.slack.handler_runtime.inbound import LinkedRoute, route_linked_thread

    _bind(env)
    env.store.drop_binding("C0CODE")
    assert env.store.was_revoked("C0CODE")
    env.on = False
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert options_answer_permitted("C0CODE", OWNER) is False
        outcome = await route_linked_thread(
            "now add a regression test",
            "slack:300.000001",
            OWNER,
            "C0CODE",
            env.slack,
            "300.000001",
        )
    assert outcome is LinkedRoute.REFUSED


async def test_a_shared_users_thread_stop_in_a_code_channel_is_refused(env: Env) -> None:
    from kiro_crew.slack.agent_sessions.session_events import _shared_user_in_code_channel

    _bind(env)
    env.on = False
    with patch("kiro_crew.slack.handler.is_shared_listed", return_value=True):
        assert await _shared_user_in_code_channel(OWNER, "C0CODE") is True
        assert await _shared_user_in_code_channel(OWNER, "D0DM00001") is False
    assert await _shared_user_in_code_channel(OWNER, "C0CODE") is False


async def test_a_shared_users_inline_stop_is_refused_in_a_revoked_channel_but_not_a_dm(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.interactions_sessions import _outside_code_channel

    _bind(env)
    env.store.drop_binding("C0CODE")
    assert await _outside_code_channel("C0CODE") is False
    monkeypatch.setattr(type(env.store), "unreadable", property(lambda self: True))
    assert await _outside_code_channel("D0DM00001") is True


def test_an_unreadable_store_refuses_a_shared_user_in_any_channel_that_could_be_one(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.slack.code_channels.routing import shared_user_refused_in

    monkeypatch.setattr(type(env.store), "unreadable", property(lambda self: True))
    assert shared_user_refused_in("C0PLAIN") is True
    assert shared_user_refused_in("D0DM00001") is False


async def test_a_shared_users_mention_never_opens_a_code_channel(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    handle = await _route_shared(env, mention(), is_mention=True)
    # Answered in place on the normal path, as without code channels.
    handle.assert_called_once()
    assert env.store.get_binding("C0NEW") is None and env.routed == []


async def test_one_line_question_is_answered_in_place(env: Env) -> None:
    env.decision = RouteDecision("stay")
    handle = await _route(
        env, {**mention(), "text": f"<@{SELF_USER}> what does CI run?"}, is_mention=True
    )
    handle.assert_called_once()
    assert env.routed == []


# ── An unmentioned follow-up in a thread the bot already answers ─────────────
#
# Build 9: "how you doing" (a mention) was answered in place, then "grab an
# issue from the backlog" in the same thread, with no mention, ran in place in
# the thread session instead of reaching the classifier.

ROOT_TS = "1700000000.000100"
FOLLOW_UP = "grab an issue from the backlog and fix it"


def _follow_up(ts: str = "1700000000.000200", text: str = FOLLOW_UP) -> dict[str, Any]:
    return {
        "type": "message",
        "user": OWNER,
        "channel": "C0ORIGIN",
        "ts": ts,
        "thread_ts": ROOT_TS,
        "text": text,
        "team": "T1",
    }


async def _route_in_bot_thread(env: Env, event: dict[str, Any]) -> MagicMock:
    """Route *event* with the bot holding a session for the ROOT_TS thread."""
    orch = _orch(env)
    orch.sessions.has_session = MagicMock(side_effect=lambda ts: ts == ROOT_TS)
    orch.sessions.get_session_for_thread = MagicMock(return_value=None)
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as handle:
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await _route_message(orch, event, SeenCache(), is_mention=False)
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
    return handle


async def test_unmentioned_follow_up_in_a_bot_thread_is_classified(env: Env) -> None:
    env.decision = RouteDecision("stay")
    handle = await _route_in_bot_thread(env, _follow_up())
    assert env.classified == [FOLLOW_UP]
    # A stay decision answers it in place in the thread session, as before.
    handle.assert_called_once()
    assert env.routed == []
    assert "agents.conversations.create" not in env.api.methods()


async def test_follow_up_that_opens_a_code_channel_is_not_answered_in_the_thread(
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    event = _follow_up()
    handle = await _route_in_bot_thread(env, event)
    handle.assert_not_called()  # the old thread session stays silent
    [(channel, seed, sender)] = env.routed
    assert channel == "C0NEW" and seed.startswith(FOLLOW_UP) and sender == OWNER
    [body] = [b for m, b in env.api.calls if m == "agents.conversations.create"]
    assert body["origin_message_ts"] == event["ts"]  # session id from the follow-up's ts
    assert ("C0ORIGIN", "Opened <#C0NEW> for this. I'll work there.", ROOT_TS) in env.slack.posts
    binding = env.store.get_binding("C0NEW")
    assert binding is not None and binding.origin_thread_ts == ROOT_TS


async def test_a_thread_that_opened_a_channel_is_not_classified_again(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    await _route_in_bot_thread(env, _follow_up())
    assert len(env.classified) == 1
    handle = await _route_in_bot_thread(env, _follow_up(ts="1700000000.000300", text="and v2"))
    assert len(env.classified) == 1  # no second model call for this thread
    handle.assert_called_once()  # answered in the thread as before
    assert len([m for m, _ in env.api.calls if m == "agents.conversations.create"]) == 1


async def test_the_opening_follow_up_redelivered_after_a_restart_is_not_answered_in_place(
    env: Env,
) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    await _route_in_bot_thread(env, _follow_up())
    # A fresh dedup cache: Slack re-delivers the same event after a restart.
    handle = await _route_in_bot_thread(env, _follow_up())
    handle.assert_not_called()
    assert len(env.classified) == 1 and len(env.routed) == 1

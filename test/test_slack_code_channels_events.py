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


async def test_multi_step_mention_moves_into_a_new_channel(env: Env) -> None:
    env.api.responses["agents.conversations.create"] = {"ok": True, "channel_id": "C0NEW"}
    handle = await _route(env, mention(), is_mention=True)
    handle.assert_not_called()
    assert env.store.get_binding("C0NEW") is not None
    assert env.routed and env.routed[0][0] == "C0NEW"


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

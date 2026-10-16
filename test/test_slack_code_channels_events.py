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
from kiro_crew.slack.code_channels import settings as cc_settings
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
        cc_settings.note_granted_scopes(["chat:write"])
    else:
        cc_settings.report_slack_refusal("feature_disabled", method="agents.conversations.create")
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

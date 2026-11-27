"""A Channel Bot or Channel Conductor beside a shared user's ``!channel agent``.

A channel is answered by an agent, a Channel Bot or a Channel Conductor, never two.
A shared user (``slack.shared_access``) may set a channel's agent only where the
owner gave the channel none of the three; the owner's ``!channel bot <name>`` and
``!channel conductor on`` replace an agent a shared user set, as the owner's
``!channel agent`` does. Config writes land in the per-test ``KIROCREW_HOME``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_slack_shared_channel_agent as _shared
from slack_transport_dispatch_helpers import RecordingSlackClient
from test_slack_channel_handler import _posts
from test_slack_shared_channel_agent import (
    _CHANNEL,
    _OWNER,
    _SHARED,
    _audits,
    _disk,
    _owner_sets,
    _restart,
    _say,
)

from kiro_crew.config.sections import ChannelConfig, drop_stale_shared_agents
from kiro_crew.slack import channel_handler as ch
from kiro_crew.slack import conductor_thread as ct
from kiro_crew.slack import handler as _handler
from kiro_crew.slack import shared_channel_agent as sca
from kiro_crew.testing.wait import until_parked

# Assignment exposes the shared fixture without redefining an imported name.
crew = _shared.crew

_TS = "1700000100.000100"
_MARKED = {"agent": "web-frontend", "shared_agent": "web-frontend"}


@pytest.fixture()
def dashboard(monkeypatch):
    """A running dashboard with no Bot chat open: what ``!channel bot <name>`` needs."""
    monkeypatch.setattr(_handler, "_dashboard_state", SimpleNamespace(_slots={}))


def _owner(coro) -> list[str]:
    slack = RecordingSlackClient()
    asyncio.run(coro(slack))
    return [text for text, _ts in _posts(slack)]


def _channel_bot(command: str) -> list[str]:
    return _owner(lambda slack: ch.set_bot(command, slack, _CHANNEL, _TS, _OWNER))


def _channel_conductor(action: str) -> list[str]:
    parts = ["!channel", "conductor", action]
    return _owner(
        lambda slack: ch.set_conductor(parts, slack, _CHANNEL, _TS, _TS, f"slack:{_TS}", _OWNER)
    )


class TestSharedUserInAHandlerChannel:
    @pytest.mark.parametrize("handler_kind", ["bot", "conductor"])
    @pytest.mark.parametrize("command", ["!channel agent research", "!channel agent off"])
    def test_a_shared_users_channel_agent_is_refused(
        self, crew, monkeypatch, handler_kind, command
    ):
        if handler_kind == "bot":
            _handler._persist_channel_config(_CHANNEL, bot="web-frontend")
        else:
            monkeypatch.setattr(ch, "channel_conductor", lambda channel: object())
        before = _disk()

        assert _say(command) == sca.CHANNEL_HANDLER_TEXT
        assert _disk() == before  # no agent beside the Bot or Conductor, no mark
        assert _audits(crew)[-1] == (_SHARED, "denied", _CHANNEL, "channel bot or conductor")

    def test_after_the_owner_takes_the_bot_off_a_shared_user_may_set_an_agent(self, crew):
        _handler._persist_channel_config(_CHANNEL, bot="web-frontend")
        assert _say("!channel agent research") == sca.CHANNEL_HANDLER_TEXT
        _handler._persist_channel_config(_CHANNEL, bot="")  # what ``!channel bot off`` saves

        assert "Agent for this channel: *research*" in _say("!channel agent research")
        assert _disk() == {"bot": "", "agent": "research", "shared_agent": "research"}

    def test_a_shared_write_waits_for_the_owners_command_and_then_sees_its_bot(self, crew):
        async def scenario() -> str:
            async with ch.lock(_CHANNEL):
                # The owner's ``!channel bot`` holds the channel through its check and write.
                waiting = asyncio.create_task(
                    sca.handle("!channel agent research", _CHANNEL, _SHARED)
                )
                await until_parked(ch.lock(_CHANNEL))
                assert not waiting.done()
                _handler._persist_channel_config(_CHANNEL, bot="web-frontend")
            return await asyncio.wait_for(waiting, timeout=30)

        assert asyncio.run(scenario()) == sca.CHANNEL_HANDLER_TEXT
        assert _disk() == {"bot": "web-frontend"}


class TestOwnerOverASharedUsersAgent:
    def test_channel_bot_replaces_the_agent_and_its_mark_in_one_write(self, crew, dashboard):
        assert "Agent for this channel: *web-frontend*" in _say("!channel agent web-frontend")
        assert _disk() == _MARKED

        posts = _channel_bot("!channel bot research")

        assert _disk() == {"agent": "", "bot": "research"}
        assert "Bot *research* now answers every thread" in posts[-1]
        assert ch.REPLACED_TEXT.format(agent="web-frontend") in posts[-1]
        cfg = _restart()
        assert (cfg.agent, cfg.bot, cfg.shared_agent) == ("", "research", "")
        # The channel is the owner's now: the shared user changes nothing in it.
        assert _say("!channel agent web-frontend") == sca.CHANNEL_HANDLER_TEXT

    def test_channel_bot_still_asks_the_owner_to_clear_their_own_agent(self, crew, dashboard):
        _owner_sets("research")

        posts = _channel_bot("!channel bot web-frontend")

        assert posts == [ch.AGENT_FIRST_TEXT.format(agent="research")]
        assert _disk() == {"agent": "research"}

    def test_a_refused_channel_bot_leaves_the_shared_users_agent(self, crew, dashboard):
        _say("!channel agent web-frontend")

        posts = _channel_bot("!channel bot nobody")

        assert posts[-1].startswith("❌ Unknown Bot `nobody`")
        assert _disk() == _MARKED

    @pytest.mark.parametrize("started", [True, False])
    def test_channel_conductor_on_replaces_it_only_once_the_conductor_started(
        self, crew, monkeypatch, started
    ):
        _say("!channel agent web-frontend")
        start = AsyncMock(return_value=(started, "started" if started else "not here"))
        monkeypatch.setattr(ct, "start", start)

        posts = _channel_conductor("on")

        start.assert_awaited_once()
        assert start.await_args.kwargs["channel_wide"] is True
        if started:
            assert _disk() == {"agent": ""}
            assert posts == [ch.REPLACED_TEXT.format(agent="web-frontend").strip()]
        else:
            assert _disk() == _MARKED  # a refused start changes nothing
            assert posts == ["❌ not here"]

    def test_channel_conductor_on_still_asks_the_owner_to_clear_their_own_agent(
        self, crew, monkeypatch
    ):
        _owner_sets("research")
        start = AsyncMock(return_value=(True, "started"))
        monkeypatch.setattr(ct, "start", start)

        assert _channel_conductor("on") == [ch.AGENT_FIRST_TEXT.format(agent="research")]
        start.assert_not_awaited()
        assert _disk() == {"agent": "research"}


class TestStaleMarkBesideAChannelBot:
    def test_a_stale_mark_is_dropped_and_the_bot_kept(self):
        entry = {"agent": "", "bot": "research", "shared_agent": "web-frontend"}
        doc = {"slack": {"channels": {_CHANNEL: dict(entry)}}}

        cfg = ChannelConfig.from_dict(entry)
        assert (cfg.agent, cfg.bot, cfg.shared_agent) == ("", "research", "")
        assert drop_stale_shared_agents(doc) is True
        assert doc["slack"]["channels"][_CHANNEL] == {"agent": "", "bot": "research"}

    def test_the_owners_bot_write_retires_a_stale_mark_on_disk(self, crew, dashboard):
        _say("!channel agent web-frontend")
        _shared._raw_edit(lambda doc: doc["slack"]["channels"][_CHANNEL].update(agent=""))

        _channel_bot("!channel bot research")

        assert _disk() == {"agent": "", "bot": "research"}

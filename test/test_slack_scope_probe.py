"""Tests for the tracked-channel history-readability probe (issue #3225).

A Slack install created before the manifest gained ``groups:history`` keeps
its old OAuth grant, so tracked private channels deliver no message events
and nothing logs. These tests lock in the probe's warning path: unreadable
channels produce a log warning + dashboard notification; readable channels
and transient failures stay silent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from slack_sdk.errors import SlackApiError

from kiro_crew.slack.client import RealSlackClient, SlackClientOps
from kiro_crew.slack.scope_probe import (
    UNREADABLE_ERRORS,
    warn_unreadable_tracked_channels,
)


class ProbeStubClient(SlackClientOps):
    """Minimal SlackClientOps stub with a scripted probe outcome per channel."""

    def __init__(self, outcomes: dict[str, str | None]):
        self._outcomes = outcomes
        self.probed: list[str] = []

    async def probe_channel_history(self, channel: str) -> str | None:
        self.probed.append(channel)
        return self._outcomes.get(channel)

    # Abstract members not exercised by the probe.
    async def post_message(self, channel, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        raise NotImplementedError

    async def post_blocks(self, channel, blocks, text, thread_ts=None, unfurl_links=None, unfurl_media=None):
        raise NotImplementedError

    async def update_message(self, channel, ts, text="", blocks=None):
        raise NotImplementedError

    async def delete_message(self, channel, ts):
        raise NotImplementedError

    async def add_reaction(self, channel, ts, emoji, raise_on_error=False):
        raise NotImplementedError

    async def remove_reaction(self, channel, ts, emoji, raise_on_error=False):
        raise NotImplementedError

    async def upload_file(self, channel, thread_ts, file, filename, title):
        raise NotImplementedError

    async def open_dm(self, user_id):
        raise NotImplementedError

    async def post_ephemeral(self, channel, user_id, text, blocks=None, thread_ts=None):
        raise NotImplementedError

    async def views_publish(self, user_id, view):
        raise NotImplementedError


class TestWarnUnreadableTrackedChannels:
    @pytest.mark.asyncio
    async def test_missing_scope_warns_and_notifies(self, caplog):
        slack = ProbeStubClient({"C_PRIV": "missing_scope"})
        notes: list[tuple] = []

        def notify(kind, title, body, **kwargs):
            notes.append((kind, title, body, kwargs))

        with caplog.at_level("WARNING", logger="kiro_crew.slack.scope_probe"):
            result = await warn_unreadable_tracked_channels(slack, {"C_PRIV"}, notify=notify)

        assert result == {"C_PRIV": "missing_scope"}
        assert any(
            "C_PRIV" in rec.getMessage() and "missing_scope" in rec.getMessage()
            for rec in caplog.records
        )
        assert len(notes) == 1
        kind, title, body, kwargs = notes[0]
        assert kind == "agent"
        assert "C_PRIV" in body
        assert kwargs["meta"] == {"channels": ["C_PRIV"]}

    @pytest.mark.asyncio
    async def test_channel_not_found_warns(self, caplog):
        slack = ProbeStubClient({"C_GONE": "channel_not_found"})
        with caplog.at_level("WARNING", logger="kiro_crew.slack.scope_probe"):
            result = await warn_unreadable_tracked_channels(slack, {"C_GONE"})
        assert result == {"C_GONE": "channel_not_found"}
        assert any("channel_not_found" in rec.getMessage() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_readable_channel_is_silent(self, caplog):
        slack = ProbeStubClient({"C_OK": None})
        notes: list[tuple] = []
        with caplog.at_level("WARNING", logger="kiro_crew.slack.scope_probe"):
            result = await warn_unreadable_tracked_channels(
                slack, {"C_OK"}, notify=lambda *a, **k: notes.append(a)
            )
        assert result == {}
        assert not notes
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    @pytest.mark.asyncio
    async def test_non_definitive_error_codes_do_not_warn(self, caplog):
        # ratelimited / not_in_channel etc. are not proof of a stale grant.
        slack = ProbeStubClient({"C_RATE": "ratelimited", "C_MEM": "not_in_channel"})
        with caplog.at_level("WARNING", logger="kiro_crew.slack.scope_probe"):
            result = await warn_unreadable_tracked_channels(slack, {"C_RATE", "C_MEM"})
        assert result == {}
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    @pytest.mark.asyncio
    async def test_mixed_set_probes_all_and_aggregates_one_note(self):
        slack = ProbeStubClient(
            {"C_A": "missing_scope", "C_B": None, "C_C": "channel_not_found"}
        )
        notes: list[tuple] = []
        result = await warn_unreadable_tracked_channels(
            slack,
            {"C_A", "C_B", "C_C", ""},  # empty id is skipped
            notify=lambda *a, **k: notes.append((a, k)),
        )
        assert sorted(slack.probed) == ["C_A", "C_B", "C_C"]
        assert result == {"C_A": "missing_scope", "C_C": "channel_not_found"}
        assert len(notes) == 1
        assert notes[0][1]["meta"] == {"channels": ["C_A", "C_C"]}

    @pytest.mark.asyncio
    async def test_notify_failure_is_swallowed(self, caplog):
        slack = ProbeStubClient({"C_PRIV": "missing_scope"})

        def bad_notify(*a, **k):
            raise RuntimeError("bus down")

        # Must not raise — the probe is fire-and-forget.
        result = await warn_unreadable_tracked_channels(slack, {"C_PRIV"}, notify=bad_notify)
        assert result == {"C_PRIV": "missing_scope"}


class TestSlackClientOpsDefault:
    @pytest.mark.asyncio
    async def test_base_default_returns_none(self):
        from conftest import MockSlackClient

        client = MockSlackClient()
        assert await client.probe_channel_history("C1") is None


class TestRealSlackClientProbe:
    def _client(self, web) -> RealSlackClient:
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = web
        return client

    @pytest.mark.asyncio
    async def test_readable_returns_none(self):
        web = AsyncMock()
        web.conversations_history = AsyncMock(return_value={"ok": True, "messages": []})
        client = self._client(web)
        assert await client.probe_channel_history("C1") is None
        web.conversations_history.assert_called_once_with(channel="C1", limit=1)

    @pytest.mark.asyncio
    async def test_missing_scope_returns_error_code(self):
        web = AsyncMock()
        web.conversations_history = AsyncMock(
            side_effect=SlackApiError("nope", {"ok": False, "error": "missing_scope"})
        )
        client = self._client(web)
        assert await client.probe_channel_history("C1") == "missing_scope"

    @pytest.mark.asyncio
    async def test_channel_not_found_returns_error_code(self):
        web = AsyncMock()
        web.conversations_history = AsyncMock(
            side_effect=SlackApiError("nope", {"ok": False, "error": "channel_not_found"})
        )
        client = self._client(web)
        assert await client.probe_channel_history("C1") == "channel_not_found"

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self):
        web = AsyncMock()
        web.conversations_history = AsyncMock(side_effect=aiohttp.ClientError("boom"))
        client = self._client(web)
        assert await client.probe_channel_history("C1") is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        web = AsyncMock()
        web.conversations_history = AsyncMock(side_effect=asyncio.TimeoutError())
        client = self._client(web)
        assert await client.probe_channel_history("C1") is None


class TestInteractionsProbeHook:
    @pytest.mark.asyncio
    async def test_probe_helper_schedules_task(self, monkeypatch):
        from kiro_crew.slack import interactions

        slack = ProbeStubClient({"C_NEW": "missing_scope"})
        tasks: set = set()
        orch = SimpleNamespace(slack=slack, dashboard_state=None, _handler_tasks=tasks)
        monkeypatch.setattr(interactions, "_orch", orch)

        interactions._probe_tracked_channel_scope({"C_NEW"})
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        assert slack.probed == ["C_NEW"]

    @pytest.mark.asyncio
    async def test_probe_helper_noop_without_slack(self, monkeypatch):
        from kiro_crew.slack import interactions

        tasks: set = set()
        orch = SimpleNamespace(slack=None, dashboard_state=None, _handler_tasks=tasks)
        monkeypatch.setattr(interactions, "_orch", orch)
        interactions._probe_tracked_channel_scope({"C_NEW"})
        assert not tasks

    @pytest.mark.asyncio
    async def test_probe_helper_noop_on_empty_set(self, monkeypatch):
        from kiro_crew.slack import interactions

        tasks: set = set()
        orch = SimpleNamespace(
            slack=ProbeStubClient({}), dashboard_state=None, _handler_tasks=tasks
        )
        monkeypatch.setattr(interactions, "_orch", orch)
        interactions._probe_tracked_channel_scope(set())
        assert not tasks


def test_unreadable_errors_are_the_definitive_codes():
    # The contract the warning path is built on: only codes that prove the
    # grant cannot read the channel, never transient or membership errors.
    assert UNREADABLE_ERRORS == frozenset({"missing_scope", "channel_not_found"})

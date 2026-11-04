"""Tests for SlackClientOps.fetch_thread_replies and RealSlackClient pagination."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from slack_wire_helpers import slack_wire


class TestSlackClientOpsBase:
    """Base class fetch_thread_replies returns empty list."""

    @pytest.mark.asyncio
    async def test_base_returns_empty(self):
        """Verify the default implementation returns [] (tested via MockSlackClient
        which inherits from SlackClientOps without overriding fetch_thread_replies)."""
        from conftest import MockSlackClient

        client = MockSlackClient()
        result = await client.fetch_thread_replies("C1", "100.0")
        assert result == []


class TestRealSlackClientFetchThreadReplies:
    """RealSlackClient.fetch_thread_replies with mocked web client."""

    @pytest.mark.asyncio
    async def test_returns_messages(self):
        from kiro_crew.slack.client import RealSlackClient

        web = AsyncMock()
        web.conversations_replies = AsyncMock(
            return_value={
                "messages": [{"user": "U1", "text": "hi"}],
                "response_metadata": {},
            }
        )
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = web

        result = await client.fetch_thread_replies("C1", "100.0")
        assert len(result) == 1
        assert result[0]["text"] == "hi"
        web.conversations_replies.assert_called_once_with(
            channel="C1", ts="100.0", limit=200,
        )

    @pytest.mark.asyncio
    async def test_logs_warning_on_pagination(self):
        from kiro_crew.slack.client import RealSlackClient

        web = AsyncMock()
        web.conversations_replies = AsyncMock(
            return_value={
                "messages": [{"user": "U1", "text": "hi"}],
                "response_metadata": {"next_cursor": "abc123"},
            }
        )
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = web

        with patch("kiro_crew.slack.client.logger") as mock_logger:
            result = await client.fetch_thread_replies("C1", "100.0")
            assert len(result) == 1
            mock_logger.warning.assert_called_once()
            assert "incomplete" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        import aiohttp

        from kiro_crew.slack.client import RealSlackClient

        web = AsyncMock()
        web.conversations_replies = AsyncMock(
            side_effect=aiohttp.ClientError("timeout")
        )
        client = RealSlackClient.__new__(RealSlackClient)
        client._web = web

        result = await client.fetch_thread_replies("C1", "100.0")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_pagination_warning_without_cursor(self, monkeypatch, caplog):
        messages = [{"user": "U1", "text": "hi"}]
        async with slack_wire(monkeypatch) as wire:
            wire.respond("conversations.replies", {"messages": messages, "response_metadata": {}})
            with caplog.at_level(logging.WARNING, logger="kiro_crew.slack.client"):
                result = await wire.client.fetch_thread_replies("C1", "100.0")

        assert result == messages
        [request] = wire.sent("conversations.replies")
        assert (request.get("channel"), request.get("ts"), request.get("limit")) == (
            "C1",
            "100.0",
            "200",
        )
        assert [r.getMessage() for r in caplog.records if r.name == "kiro_crew.slack.client"] == []

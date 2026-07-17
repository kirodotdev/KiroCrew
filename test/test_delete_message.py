"""Tests for delete_message MCP tool and API endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import api_delete_message
from kiro_crew.mcp_core import _call_tool

# ── Endpoint tests ──


def _make_delete_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/delete-message", api_delete_message)
    app["state"] = state
    return app


def _mock_state(slack_client=None):
    state = MagicMock()
    state.slack_client = slack_client
    return state


class TestDeleteMessageEndpoint:
    @pytest.mark.asyncio
    async def test_missing_channel(self):
        app = _make_delete_app(_mock_state())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/delete-message", json={"ts": "123.456"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_ts(self):
        app = _make_delete_app(_mock_state())
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/delete-message", json={"channel": "C123"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_no_slack_client(self):
        app = _make_delete_app(_mock_state(slack_client=None))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/delete-message", json={"channel": "C123", "ts": "123.456"})
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_successful_delete(self):
        slack = MagicMock()
        slack.delete_message = AsyncMock()
        app = _make_delete_app(_mock_state(slack_client=slack))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/delete-message", json={"channel": "C123", "ts": "123.456"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            slack.delete_message.assert_called_once_with("C123", "123.456")

    @pytest.mark.asyncio
    async def test_slack_error_returns_502(self):
        slack = MagicMock()
        slack.delete_message = AsyncMock(side_effect=Exception("cant_delete_message"))
        app = _make_delete_app(_mock_state(slack_client=slack))
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/delete-message", json={"channel": "C123", "ts": "123.456"})
            assert resp.status == 502
            data = await resp.json()
            assert "cant_delete_message" in data["error"]


# ── MCP tool handler tests ──


class TestDeleteMessageTool:
    def test_invalid_channel_format(self):
        result = _call_tool("delete_message", {"channel": "invalid!", "ts": "123.456"})
        assert "invalid channel" in result.lower()

    def test_invalid_ts_format(self):
        result = _call_tool("delete_message", {"channel": "C0ABC123", "ts": "not-a-timestamp"})
        assert "invalid" in result.lower() and "timestamp" in result.lower()

    def test_successful_delete(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"ok": True}
            result = _call_tool("delete_message", {"channel": "C0ABC123", "ts": "1780088134.952549"})
            assert "deleted" in result.lower()
            mock_post.assert_called_once_with(
                "/api/delete-message", {"channel": "C0ABC123", "ts": "1780088134.952549"}
            )

    def test_api_error(self):
        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"error": "message_not_found"}
            result = _call_tool("delete_message", {"channel": "C0ABC123", "ts": "1780088134.952549"})
            assert "message_not_found" in result

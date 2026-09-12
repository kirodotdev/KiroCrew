"""Tests for delete_message MCP tool and API endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import api_delete_message
from kiro_crew.mcp_core import _call_tool
from kiro_crew.mcp_tools.messaging import delete_message
from kiro_crew.validation import (
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    ValidationError,
    validate_string_field,
)

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

    # ── Missing-required-arg handling (must NOT raise; would crash the MCP server) ──
    # delete_message read args["channel"]/args["ts"] by subscript with no schema,
    # so a call omitting either raised KeyError that propagated out of the stdio
    # loop and killed the whole kirocrew-core server. It must return a clean error.

    def test_missing_ts_returns_error_not_raise(self):
        result = _call_tool("delete_message", {"channel": "C0ABC123"})
        assert isinstance(result, str)
        assert result.lower().startswith("error")

    def test_missing_channel_returns_error_not_raise(self):
        result = _call_tool("delete_message", {"ts": "1780088134.952549"})
        assert isinstance(result, str)
        assert result.lower().startswith("error")

    def test_empty_args_returns_error_not_raise(self):
        result = _call_tool("delete_message", {})
        assert isinstance(result, str)
        assert result.lower().startswith("error")

    def test_delete_message_has_validation_schema(self):
        # Guards against re-introducing the crash: the tool must be schema-gated.
        from kiro_crew.validation import MCP_CORE_SCHEMAS

        assert "delete_message" in MCP_CORE_SCHEMAS
        required = {f.name for f in MCP_CORE_SCHEMAS["delete_message"].fields if f.required}
        assert {"channel", "ts"} <= required


class TestDeleteMessageChannelIsLengthBounded:
    """The second of the two reviewer-counted uncapped sites (#7084).

    `delete_message` reads `args["channel"]` straight off the MCP payload with
    no schema in front of it, so `CHANNEL_ID_RE`'s unbounded `+` was the whole
    length contract. This is the LLM-reachable entry point of the pair.
    """

    def test_overlength_channel_is_refused_before_the_post(self):
        over_cap = "C" + "A" * CHANNEL_MAX_LEN  # 21 chars: matches the regex

        assert CHANNEL_ID_RE.match(over_cap), "fixture must be shape-valid or it proves nothing"

        with patch("kiro_crew.mcp_core._post") as mock_post:
            result = delete_message(
                "delete_message",
                {"channel": over_cap, "ts": "1780088134.952549"},
            )

        assert "invalid channel" in result.lower()
        mock_post.assert_not_called()

    def test_a_channel_at_exactly_the_cap_still_posts(self):
        """Negative control: the cap is inclusive, so a maximum-length id is
        still a delete the tool must perform."""
        at_cap = "C" + "A" * (CHANNEL_MAX_LEN - 1)
        assert len(at_cap) == CHANNEL_MAX_LEN

        with patch("kiro_crew.mcp_core._post") as mock_post:
            mock_post.return_value = {"ok": True}
            result = delete_message(
                "delete_message",
                {"channel": at_cap, "ts": "1780088134.952549"},
            )

        assert "deleted" in result.lower()
        mock_post.assert_called_once_with(
            "/api/delete-message", {"channel": at_cap, "ts": "1780088134.952549"}
        )


class TestCronChannelIsAlreadyBounded:
    """CONTROL, not a third fix.

    `dashboard/handlers/cron.py:261` reads as a third uncapped
    `CHANNEL_ID_RE.match`, but the value reaching it came through
    `validate_string_field(body, "channel", max_len=CHANNEL_MAX_LEN)` a few
    lines above, which already refuses an over-length id. Adding a cap there
    would be a second spelling of a bound that is already enforced.

    This test asserts that upstream guard directly, so the claim "cron is not a
    third site" is executable rather than a comment — and so that a later change
    which drops the `max_len=` argument is caught here instead of silently
    turning cron into the defect this PR is fixing.
    """

    def test_validate_string_field_refuses_an_overlength_channel(self):
        over_cap = "C" + "A" * CHANNEL_MAX_LEN

        assert CHANNEL_ID_RE.match(over_cap), "fixture must be shape-valid or it proves nothing"

        with pytest.raises(ValidationError):
            validate_string_field({"channel": over_cap}, "channel", max_len=CHANNEL_MAX_LEN)

    def test_validate_string_field_admits_a_channel_at_the_cap(self):
        at_cap = "C" + "A" * (CHANNEL_MAX_LEN - 1)
        assert len(at_cap) == CHANNEL_MAX_LEN
        assert validate_string_field({"channel": at_cap}, "channel", max_len=CHANNEL_MAX_LEN) == (
            at_cap
        )

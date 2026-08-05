"""Tests for the server-side Slack pins/reactions proxy routes.

These routes (POST /api/slack/pins, /api/slack/reactions) let local callers
perform pin/unpin/list and reaction add/remove WITHOUT holding the Slack bot
token — the gateway holds it in state.slack_client. The routes enforce the
same tracked-channel allowlist and SEL audit logging as the other Slack
routes (e.g. api_slack_upload_file).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.messaging import api_slack_pins, api_slack_reactions
from kiro_crew.dashboard.state import DashboardState

TRACKED = "C0TRACKED123"
UNTRACKED = "C0UNTRACKED9"
TS = "1712793600.123456"


def _make_app(slack_client):
    app = web.Application()
    state = MagicMock(spec=DashboardState)
    state.slack_client = slack_client
    app["state"] = state
    app.router.add_post("/api/slack/pins", api_slack_pins)
    app.router.add_post("/api/slack/reactions", api_slack_reactions)
    return app


def _patch_tracked(value: bool):
    return patch(
        "kiro_crew.slack.handler.is_tracked_channel",
        return_value=value,
    )


class TestSlackPins:
    @pytest.mark.asyncio
    async def test_pin_add_to_tracked_channel(self):
        slack = MagicMock()
        slack.add_pin = AsyncMock()
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/pins",
                    json={"channel": TRACKED, "ts": TS, "action": "add"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body.get("ok") is True
        slack.add_pin.assert_awaited_once_with(TRACKED, TS)

    @pytest.mark.asyncio
    async def test_pin_remove_to_tracked_channel(self):
        slack = MagicMock()
        slack.remove_pin = AsyncMock()
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/pins",
                    json={"channel": TRACKED, "ts": TS, "action": "remove"},
                )
        assert resp.status == 200
        slack.remove_pin.assert_awaited_once_with(TRACKED, TS)

    @pytest.mark.asyncio
    async def test_pin_list_returns_pins(self):
        slack = MagicMock()
        slack.list_pins = AsyncMock(return_value=[{"ts": TS, "text": "hello"}])
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/pins",
                    json={"channel": TRACKED, "action": "list"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert body["pins"] == [{"ts": TS, "text": "hello"}]
        slack.list_pins.assert_awaited_once_with(TRACKED)

    @pytest.mark.asyncio
    async def test_pin_list_redacts_credentials(self):
        """Pinned text may carry agent-posted secrets; the list action must
        scrub each text field before returning it to the caller."""
        slack = MagicMock()
        slack.list_pins = AsyncMock(
            return_value=[{"ts": TS, "text": "key AKIAIOSFODNN7EXAMPLE here"}]
        )
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/pins",
                    json={"channel": TRACKED, "action": "list"},
                )
                body = await resp.json()
        assert resp.status == 200
        assert "AKIAIOSFODNN7EXAMPLE" not in body["pins"][0]["text"]

    @pytest.mark.asyncio
    async def test_pin_untracked_channel_denied(self):
        slack = MagicMock()
        slack.add_pin = AsyncMock()
        app = _make_app(slack)
        with _patch_tracked(False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/pins",
                    json={"channel": UNTRACKED, "ts": TS, "action": "add"},
                )
                body = await resp.json()
        assert resp.status == 403
        assert "not in tracked channels" in body.get("error", "")
        slack.add_pin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pin_invalid_action(self):
        slack = MagicMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/pins",
                json={"channel": TRACKED, "ts": TS, "action": "frobnicate"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_invalid_channel(self):
        slack = MagicMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/pins",
                json={"channel": "not-a-channel", "ts": TS, "action": "add"},
            )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pin_non_string_channel(self):
        """A non-string channel (e.g. int/null) must 400, not raise 500."""
        slack = MagicMock()
        slack.add_pin = AsyncMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/pins",
                json={"channel": 123, "ts": TS, "action": "add"},
            )
        assert resp.status == 400
        slack.add_pin.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pin_invalid_ts(self):
        slack = MagicMock()
        slack.add_pin = AsyncMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/pins",
                json={"channel": TRACKED, "ts": "garbage", "action": "add"},
            )
        assert resp.status == 400
        slack.add_pin.assert_not_awaited()


class TestSlackReactions:
    @pytest.mark.asyncio
    async def test_reaction_add_to_tracked_channel(self):
        slack = MagicMock()
        slack.add_reaction = AsyncMock()
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/reactions",
                    json={
                        "channel": TRACKED,
                        "ts": TS,
                        "emoji": "white_check_mark",
                        "action": "add",
                    },
                )
                body = await resp.json()
        assert resp.status == 200
        assert body.get("ok") is True
        slack.add_reaction.assert_awaited_once_with(
            TRACKED, TS, "white_check_mark", raise_on_error=True
        )

    @pytest.mark.asyncio
    async def test_reaction_remove_to_tracked_channel(self):
        slack = MagicMock()
        slack.remove_reaction = AsyncMock()
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/reactions",
                    json={
                        "channel": TRACKED,
                        "ts": TS,
                        "emoji": "eyes",
                        "action": "remove",
                    },
                )
        assert resp.status == 200
        slack.remove_reaction.assert_awaited_once_with(
            TRACKED, TS, "eyes", raise_on_error=True
        )

    @pytest.mark.asyncio
    async def test_reaction_untracked_channel_denied(self):
        slack = MagicMock()
        slack.add_reaction = AsyncMock()
        app = _make_app(slack)
        with _patch_tracked(False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/reactions",
                    json={
                        "channel": UNTRACKED,
                        "ts": TS,
                        "emoji": "eyes",
                        "action": "add",
                    },
                )
                body = await resp.json()
        assert resp.status == 403
        assert "not in tracked channels" in body.get("error", "")
        slack.add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_invalid_emoji(self):
        slack = MagicMock()
        slack.add_reaction = AsyncMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/reactions",
                json={
                    "channel": TRACKED,
                    "ts": TS,
                    "emoji": "bad emoji!",
                    "action": "add",
                },
            )
        assert resp.status == 400
        slack.add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_slack_failure_returns_502(self):
        """When the Slack call fails, the route surfaces a 502 (the handler
        passes raise_on_error=True, so failures are not silently swallowed)."""
        slack = MagicMock()
        slack.add_reaction = AsyncMock(side_effect=RuntimeError("slack boom"))
        app = _make_app(slack)
        with _patch_tracked(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/reactions",
                    json={
                        "channel": TRACKED,
                        "ts": TS,
                        "emoji": "eyes",
                        "action": "add",
                    },
                )
        assert resp.status == 502
        slack.add_reaction.assert_awaited_once_with(
            TRACKED, TS, "eyes", raise_on_error=True
        )

    @pytest.mark.asyncio
    async def test_reaction_non_string_emoji(self):
        """A non-string emoji (e.g. null) must 400, not raise 500."""
        slack = MagicMock()
        slack.add_reaction = AsyncMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/reactions",
                json={"channel": TRACKED, "ts": TS, "emoji": None, "action": "add"},
            )
        assert resp.status == 400
        slack.add_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_invalid_action(self):
        slack = MagicMock()
        app = _make_app(slack)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/slack/reactions",
                json={"channel": TRACKED, "ts": TS, "emoji": "eyes", "action": "x"},
            )
        assert resp.status == 400

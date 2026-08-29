"""Tests for targeted send_message — channel and user routing, plus api_slack_profile."""

from __future__ import annotations

import contextlib
import itertools
import sys
import types
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Ensure kiro_crew.slack.handler is importable for patching even when
# heavy transitive deps (cron_descriptor, etc.) aren't installed.
if "kiro_crew.slack.handler" not in sys.modules:
    _stub = types.ModuleType("kiro_crew.slack.handler")
    _stub.is_allowed_user = lambda uid: False  # type: ignore[attr-defined]
    _stub.is_tracked_channel = lambda cid: False  # type: ignore[attr-defined]
    sys.modules["kiro_crew.slack.handler"] = _stub

from kiro_crew.dashboard.handlers import api_send_message, api_slack_profile  # noqa: E402
from kiro_crew.messaging.link import ChannelLink  # noqa: E402
from kiro_crew.telegram.client import TELEGRAM_MAX_TEXT  # noqa: E402


def _make_app(state) -> web.Application:
    app = web.Application()
    app.router.add_post("/api/send-message", api_send_message)
    app.router.add_post("/api/slack-profile", api_slack_profile)
    app["state"] = state
    return app


def _mock_state(slack_client=None, owner_id=""):
    state = MagicMock()
    state.slack_client = slack_client
    state.owner_id = owner_id
    return state


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


# ── send_message targeting ──


class TestTargetedChannel:
    @pytest.mark.asyncio
    async def test_channel_delivers_directly(self, mock_sel):
        """When channel param is set and tracked, deliver directly."""
        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "hello channel", "channel": "C0123ABC456"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {
                    "ok": True,
                    "slack": True,
                    "session": False,
                    "delivered_to": "slack",
                    "ts": "1712793600.000001",
                }
                slack.post_message.assert_called_once_with(
                    "C0123ABC456",
                    "hello channel",
                    thread_ts=None,
                    unfurl_links=None,
                    unfurl_media=None,
                    reply_broadcast=None,
                )
                slack.open_dm.assert_not_called()

    @pytest.mark.asyncio
    async def test_untracked_channel_returns_403(self, mock_sel):
        """Channel not in tracked set returns 403."""
        state = _mock_state(slack_client=MagicMock(), owner_id="U_OWNER")
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_tracked_channel", return_value=False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "hello", "channel": "CBADCHAN01"},
                )
                assert resp.status == 403
                data = await resp.json()
                assert "not in tracked channels" in data["error"]
                state.notify.assert_not_called()


class TestTargetedUser:
    @pytest.mark.asyncio
    async def test_user_opens_dm_and_delivers(self, mock_sel):
        """When user param is set and allowed, open DM and deliver."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_USER_DM")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "hello user", "user": "U0123ABC456"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {
                    "ok": True,
                    "slack": True,
                    "session": False,
                    "delivered_to": "slack",
                    "ts": "1712793600.000001",
                }
                slack.open_dm.assert_called_once_with("U0123ABC456")
                slack.post_message.assert_called_once_with(
                    "D_USER_DM",
                    "hello user",
                    thread_ts=None,
                    unfurl_links=None,
                    unfurl_media=None,
                    reply_broadcast=None,
                )

    @pytest.mark.asyncio
    async def test_disallowed_user_returns_403(self, mock_sel):
        """When user is not in allowlist, return 403 with no side effects."""
        slack = MagicMock()
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "hello", "user": "UBADUSER01"},
                )
                assert resp.status == 403
                data = await resp.json()
                assert "allowlist" in data["error"]
                state.notify.assert_not_called()
                assert data["code"] == "user_not_in_allowlist"
                mock_sel.log_tool_invocation.assert_called_once_with(
                    session_key="dashboard",
                    tool_name="send_message",
                    outcome="denied",
                    downstream_service="slack",
                    resources="target_user=UBADUSER01",
                )


class TestMutualExclusion:
    @pytest.mark.asyncio
    async def test_both_channel_and_user_returns_400(self, mock_sel):
        """When both channel and user are set, return 400."""
        state = _mock_state(slack_client=MagicMock(), owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hello", "channel": "CABCDEF123", "user": "UABCDEF456"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data == {"error": "specify channel or user, not both"}
            state.notify.assert_not_called()


class TestFallbackToOwnerDM:
    @pytest.mark.asyncio
    async def test_no_channel_no_user_sends_to_owner(self, mock_sel):
        """When neither channel nor user is set, fall back to owner DM."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message", json={"text": "hello owner", "session": "slack"}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "ok": True,
                "slack": True,
                "session": False,
                "delivered_to": "slack",
                "ts": "1712793600.000001",
            }
            slack.open_dm.assert_called_once_with("U_OWNER")
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "hello owner",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )


# ── api_slack_profile tests (#7) ──


class TestUnfurlControl:
    @pytest.mark.asyncio
    async def test_unfurl_links_false_passes_through(self, mock_sel):
        """When unfurl_links=false in payload, it reaches post_message."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "no previews",
                    "unfurl_links": False,
                    "unfurl_media": False,
                    "session": "slack",
                },
            )
            assert resp.status == 200
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "no previews",
                thread_ts=None,
                unfurl_links=False,
                unfurl_media=False,
                reply_broadcast=None,
            )

    @pytest.mark.asyncio
    async def test_unfurl_defaults_to_none(self, mock_sel):
        """When unfurl params are omitted, they default to None (Slack server default)."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "with previews", "session": "slack"},
            )
            assert resp.status == 200
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "with previews",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )

    @pytest.mark.asyncio
    async def test_unfurl_json_null_passes_as_none(self, mock_sel):
        """JSON null for unfurl params passes through as None (no 400)."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "null test",
                    "unfurl_links": None,
                    "unfurl_media": None,
                    "session": "slack",
                },
            )
            assert resp.status == 200
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "null test",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )

    @pytest.mark.asyncio
    async def test_unfurl_non_boolean_returns_400(self, mock_sel):
        """Non-boolean unfurl_links/unfurl_media returns 400."""
        state = _mock_state(slack_client=MagicMock(), owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "bad", "unfurl_links": "yes"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unfurl_with_blocks_passes_through(self, mock_sel):
        """unfurl params reach post_blocks when blocks are provided."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_blocks = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "fallback",
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
                    "unfurl_links": False,
                    "unfurl_media": False,
                    "session": "slack",
                },
            )
            assert resp.status == 200
            slack.post_blocks.assert_called_once_with(
                "D_OWNER",
                [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
                "fallback",
                thread_ts=None,
                unfurl_links=False,
                unfurl_media=False,
                reply_broadcast=None,
            )


class TestSlackProfile:
    @pytest.mark.asyncio
    async def test_happy_path(self, mock_sel):
        """Valid user ID returns profile with redacted fields."""
        slack = MagicMock()
        slack.get_user_profile = AsyncMock(
            return_value={
                "id": "U0123ABC456",
                "name": "testuser",
                "real_name": "Test User",
                "title": "Engineer",
                "timezone": "America/Los_Angeles",
            }
        )
        state = _mock_state(slack_client=slack)
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/slack-profile", json={"user": "U0123ABC456"})
                assert resp.status == 200
                data = await resp.json()
                assert data["profile"]["name"] == "testuser"

    @pytest.mark.asyncio
    async def test_missing_user_returns_400(self, mock_sel):
        """Missing user field returns 400."""
        state = _mock_state(slack_client=MagicMock())
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack-profile", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_user_format_returns_400(self, mock_sel):
        """Invalid user ID format returns 400."""
        state = _mock_state(slack_client=MagicMock())
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack-profile", json={"user": "not-a-slack-id"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_string_user_returns_400(self, mock_sel):
        """Non-string user returns 400."""
        state = _mock_state(slack_client=MagicMock())
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/slack-profile", json={"user": 12345})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_slack_api_failure_returns_502(self, mock_sel):
        """Slack API failure returns 502 with SEL error log."""
        slack = MagicMock()
        slack.get_user_profile = AsyncMock(side_effect=Exception("API down"))
        state = _mock_state(slack_client=slack)
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/slack-profile", json={"user": "U0123ABC456"})
                assert resp.status == 502
                mock_sel.log_tool_invocation.assert_called_with(
                    session_key="dashboard",
                    tool_name="read_slack_profile",
                    outcome="error",
                    downstream_service="slack",
                    resources="user=U0123ABC456",
                )

    @pytest.mark.asyncio
    async def test_slack_not_connected_returns_503(self, mock_sel):
        """Slack not connected returns 503."""
        state = _mock_state(slack_client=None)
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/slack-profile", json={"user": "U0123ABC456"})
                assert resp.status == 503

    @pytest.mark.asyncio
    async def test_disallowed_user_returns_403(self, mock_sel):
        """Profile lookup for user not in allowlist returns 403 with SEL denied."""
        state = _mock_state(slack_client=MagicMock())
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/slack-profile", json={"user": "U0123ABC456"})
                assert resp.status == 403
                data = await resp.json()
                assert data == {"error": "user not in allowlist"}
                mock_sel.log_tool_invocation.assert_called_once_with(
                    session_key="dashboard",
                    tool_name="read_slack_profile",
                    outcome="denied",
                    downstream_service="slack",
                    resources="user=U0123ABC456",
                )

    @pytest.mark.asyncio
    async def test_rate_limit_logs_sel_denied(self, mock_sel):
        """Rate-limit 429 emits SEL audit event with outcome=denied."""
        import time

        slack = MagicMock()
        state = _mock_state(slack_client=slack)
        # Pre-fill 5 lookups to trigger rate limit
        state._profile_lookup_times = [time.monotonic()] * 5
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/slack-profile", json={"user": "U0123ABC456"})
                assert resp.status == 429
                mock_sel.log_tool_invocation.assert_called_once_with(
                    session_key="dashboard",
                    tool_name="read_slack_profile",
                    outcome="denied",
                    downstream_service="slack",
                    resources="user=U0123ABC456 reason=rate_limit",
                )

    @pytest.mark.asyncio
    async def test_profile_redaction(self, mock_sel):
        """Status text with exfiltration URL gets redacted."""
        slack = MagicMock()
        # Use a URL with a long base64-like query that triggers exfil detection
        exfil_url = "https://evil.com/steal?d=" + "A" * 200
        slack.get_user_profile = AsyncMock(
            return_value={
                "id": "U0123ABC456",
                "name": "testuser",
                "status_text": f"check {exfil_url}",
            }
        )
        state = _mock_state(slack_client=slack)
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_allowed_user", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/slack-profile", json={"user": "U0123ABC456"})
                assert resp.status == 200
                data = await resp.json()
                status = data["profile"].get("status_text", "")
                # The exfiltration URL payload should be redacted
                assert "REDACTED" in status
                assert "A" * 200 not in status


class TestThreadTsAndBroadcast:
    @pytest.mark.asyncio
    async def test_thread_ts_passthrough(self, mock_sel):
        """thread_ts reaches post_message as a threaded reply."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "threaded", "thread_ts": "1712793600.123456", "session": "slack"},
            )
            assert resp.status == 200
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "threaded",
                thread_ts="1712793600.123456",
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )

    @pytest.mark.asyncio
    async def test_reply_broadcast_with_thread_ts(self, mock_sel):
        """reply_broadcast=true passes through when thread_ts is set."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "broadcast me",
                    "thread_ts": "1712793600.123456",
                    "reply_broadcast": True,
                    "session": "slack",
                },
            )
            assert resp.status == 200
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "broadcast me",
                thread_ts="1712793600.123456",
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=True,
            )

    @pytest.mark.asyncio
    async def test_reply_broadcast_without_thread_ts_returns_400(self, mock_sel):
        """reply_broadcast without thread_ts is rejected."""
        state = _mock_state(slack_client=MagicMock(), owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "bad", "reply_broadcast": True},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_thread_ts_format_returns_400(self, mock_sel):
        """Malformed thread_ts is rejected."""
        state = _mock_state(slack_client=MagicMock(), owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "bad", "thread_ts": "not-a-ts"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_reply_broadcast_non_boolean_returns_400(self, mock_sel):
        """Non-boolean reply_broadcast is rejected."""
        state = _mock_state(slack_client=MagicMock(), owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "bad",
                    "thread_ts": "1712793600.123456",
                    "reply_broadcast": "yes",
                },
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_thread_ts_with_target_channel(self, mock_sel):
        """thread_ts plumbs through when channel (not DM) is the target."""
        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "threaded channel",
                        "channel": "C0AP0AT1ESJ",
                        "thread_ts": "1712793600.123456",
                        "reply_broadcast": True,
                    },
                )
                assert resp.status == 200
                slack.post_message.assert_called_once_with(
                    "C0AP0AT1ESJ",
                    "threaded channel",
                    thread_ts="1712793600.123456",
                    unfurl_links=None,
                    unfurl_media=None,
                    reply_broadcast=True,
                )
                slack.open_dm.assert_not_called()


# ── cron Slack-default (B) + delivered_to reporting (A) ──


class TestCronSlackDefault:
    @pytest.mark.asyncio
    async def test_cron_bare_send_routes_to_owner_dm(self, mock_sel):
        """A cron-originated bare send (caller_session=cron:*, no channel/user/
        session) delivers to the owner Slack DM by default and reports
        delivered_to=slack."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000009")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "sweep done", "caller_session": "cron:job1"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "ok": True,
                "slack": True,
                "session": False,
                "delivered_to": "slack",
                "ts": "1712793600.000009",
            }
            slack.open_dm.assert_called_once_with("U_OWNER")
            slack.post_message.assert_called_once_with(
                "D_OWNER",
                "sweep done",
                thread_ts=None,
                unfurl_links=None,
                unfurl_media=None,
                reply_broadcast=None,
            )

    @pytest.mark.asyncio
    async def test_noncron_bare_send_is_notification_only(self, mock_sel):
        """A non-cron bare send stays dashboard-notification-only and reports
        delivered_to=notification (no Slack post)."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000009")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "fyi"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "ok": True,
                "slack": False,
                "session": False,
                "delivered_to": "notification",
            }
            slack.post_message.assert_not_called()
            state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_malformed_caller_session_not_routed_to_slack(self, mock_sel):
        """A caller_session that fails CRON_SESSION_RE does not escalate to
        Slack — it stays notification-only."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000009")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "x", "caller_session": "cron:has spaces!"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["delivered_to"] == "notification"
            assert data["slack"] is False
            slack.post_message.assert_not_called()


# ── OPTIONS button rendering ──


class TestOptionsRendering:
    @pytest.mark.asyncio
    async def test_options_tag_renders_action_block(self, mock_sel):
        """A plain-text send with an [OPTIONS: ...] tag posts the message with
        the tag stripped, then an actions block with the choices."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000010")
        slack.post_blocks = AsyncMock(return_value="1712793600.000011")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "Pick one:\n\n[OPTIONS: Alpha | Bravo | Charlie]",
                    "session": "slack",
                },
            )
            assert resp.status == 200
            # Message posted with the OPTIONS tag stripped from the text.
            posted_text = slack.post_message.call_args[0][1]
            assert "OPTIONS" not in posted_text
            assert "Alpha | Bravo | Charlie" not in posted_text
            # An actions block was posted after the message.
            slack.post_blocks.assert_called_once()
            blocks_arg = slack.post_blocks.call_args[0][1]
            assert isinstance(blocks_arg, list) and blocks_arg

    @pytest.mark.asyncio
    async def test_no_options_tag_posts_no_action_block(self, mock_sel):
        """A plain-text send with no OPTIONS tag posts only the message."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000010")
        slack.post_blocks = AsyncMock(return_value="1712793600.000011")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "no options here", "session": "slack"},
            )
            assert resp.status == 200
            slack.post_message.assert_called_once()
            slack.post_blocks.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_blocks_skip_options_parsing(self, mock_sel):
        """When the caller supplies blocks, an [OPTIONS: ...] substring in the
        fallback text is left untouched (blocks own their layout)."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_blocks = AsyncMock(return_value="1712793600.000012")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={
                    "text": "fallback [OPTIONS: X | Y]",
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
                    "session": "slack",
                },
            )
            assert resp.status == 200
            # Only the caller's blocks are posted — no second options block.
            slack.post_blocks.assert_called_once()
            fallback = slack.post_blocks.call_args[0][2]
            assert "[OPTIONS: X | Y]" in fallback

    @pytest.mark.asyncio
    async def test_options_block_failure_does_not_mask_delivery(self, mock_sel):
        """If posting the OPTIONS actions block fails, the already-sent main
        message is still reported as delivered (not a 502) — the options post
        is guarded by its own try/except."""
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000010")
        slack.post_blocks = AsyncMock(side_effect=Exception("blocks boom"))
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "Pick one:\n\n[OPTIONS: Alpha | Bravo]", "session": "slack"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["delivered_to"] == "slack"
            slack.post_message.assert_called_once()
            slack.post_blocks.assert_called_once()


class TestProactiveBodyMeetsTheDisplayFloor:
    """A proactive body is posted AS WRITTEN, so it needs the display-form floor.

    Every turn egress runs `redact_for_display`, which scans the form the client
    RENDERS as well as the literal bytes: neither `AKIA**<rest>**` nor
    `[AKIA](https://x)<rest>` matches a credential pattern as written, yet the
    reader is shown an intact key. This path passes no renderer -- the handler
    hands `text` straight to the channel -- so a literal-only scan here is the one
    hole in that floor, and it is reachable by anything that can call the endpoint.
    """

    #: Split by emphasis markers the client renders away. The literal string is
    #: not a credential; what the reader sees is.
    _COLLAPSING = "AKIA**IOSFODNN7EXAMPLE**"

    @pytest.mark.asyncio
    async def test_a_markdown_collapse_credential_is_not_posted(self, mock_sel):
        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)

        with patch("kiro_crew.slack.handler.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": f"key {self._COLLAPSING}", "channel": "C0123ABC456"},
                )
                assert resp.status == 200

        posted = slack.post_message.call_args.args[1]
        # Neither the literal form nor the form Slack renders may carry the key.
        assert "AKIAIOSFODNN7EXAMPLE" not in posted.replace("*", "")
        assert self._COLLAPSING not in posted

    @pytest.mark.asyncio
    async def test_an_ordinary_body_is_left_exactly_as_written(self, mock_sel):
        """The floor must not reformat a message that carries no credential."""
        slack = MagicMock()
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _mock_state(slack_client=slack, owner_id="U_OWNER")
        app = _make_app(state)
        body = "Deploy **finished** in `2m` — see [the run](https://example.com/r/1)."

        with patch("kiro_crew.slack.handler.is_tracked_channel", return_value=True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": body, "channel": "C0123ABC456"},
                )
                assert resp.status == 200

        assert slack.post_message.call_args.args[1] == body


# ── channel_type: proactive delivery to a non-Slack conversation ──
#
# The gap these cover: `send_message` was Slack-only end to end, so an agent
# driven from Telegram (or Discord, Teams, …) had no way to notify its own
# operator — a silent cron on such a session reported nowhere at all. Every case
# here pins one leg of the fail-closed contract: a named channel that cannot be
# reached must be a FAILURE, never a notification-only success, and never a
# fall-through to Slack.

_TG_KEY = "telegram:kirocrew:dm:99887766"
_TG_LINK = ChannelLink(channel_type="telegram", channel_id="99887766", thread_id="17")


def _permitted(value: bool):
    """A governance Decision stand-in for the `channels` scope vet."""
    decision = MagicMock()
    decision.permitted = value
    return decision


def _channel_transport(send_result: str = "tg-1"):
    transport = MagicMock()
    transport.send_message = AsyncMock(return_value=send_result)
    transport.capabilities.supports_proactive_send = True
    # A REAL number, not the MagicMock attribute: the delivery leg chunks against
    # this cap, and a Mock there fails the comparison inside `chunk_text` instead of
    # exercising the chunking. Telegram's own value, since _TG_LINK is a Telegram
    # link and a double that disagrees with the channel it stands in for is how a
    # boundary test comes to pass against the wrong limit.
    transport.capabilities.max_message_chars = TELEGRAM_MAX_TEXT
    return transport


def _channel_state(*, link=_TG_LINK, transport=None, slack_client=None, jobs=None):
    """A state whose session owns *link*, with *transport* registered for it."""
    state = _mock_state(slack_client=slack_client, owner_id="U_OWNER")
    state.sessions.get_origin_link.return_value = link
    state.sessions.get_mirror_link.return_value = None
    state.get_channel_transport.return_value = transport
    state.crons.list_jobs.return_value = jobs if jobs is not None else []
    return state


def _governance(permitted: bool = True):
    return patch(
        "kiro_crew.platform.governance_profiles.vet_and_audit",
        return_value=_permitted(permitted),
    )


class TestChannelTypeDelivery:
    @pytest.mark.asyncio
    async def test_telegram_session_delivers_to_telegram_transport(self, mock_sel):
        """A telegram: session naming channel_type=telegram posts into its own
        conversation and reports delivered_to=channel."""
        transport = _channel_transport()
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _channel_state(transport=transport, slack_client=slack)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "sweep done", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["ok"] is True
                assert data["delivered_to"] == "telegram"
                transport.send_message.assert_awaited_once_with(
                    "99887766", "sweep done", thread_id="17"
                )
                # Slack is NOT a fallback for a named channel.
                slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session", ["slack", "discord"])
    async def test_channel_type_with_a_competing_destination_is_refused(
        self, mock_sel, session: str
    ):
        """Two DESTINATIONS named, so it is refused rather than ranked.

        ``session="origin"`` is excluded on purpose and covered separately: it is a
        MODE ("inject where this came from"), not a surface, so with channel_type it
        forms the cron fallback ladder rather than a second destination.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport, slack_client=MagicMock())
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "channel_type": "telegram", "session": session},
                headers={"X-Session-Key": _TG_KEY},
            )
            assert resp.status == 400
            # Refused BEFORE any delivery, so nothing is half-sent.
            transport.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_empty_message_id_is_a_failure_not_a_delivery(self, mock_sel):
        """A transport refuses by returning an EMPTY id, not by raising.

        ``send_message`` ends in ``str(mid or "")``, so a Bot API failure comes back
        as ``""`` with no exception. Reporting that as delivered tells the caller a
        message landed that the user never saw, and on the cron path it would also
        stand the Slack fallback down and advance the dedup hash on nothing.
        """
        transport = _channel_transport()
        transport.send_message = AsyncMock(return_value="")
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _channel_state(transport=transport, slack_client=slack)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "sweep done", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["ok"] is False
                transport.send_message.assert_awaited_once()
                # Still no widening to Slack: a failed named send is reported, not
                # redirected to an audience the caller never asked for.
                slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_transport_with_no_message_id_is_not_read_as_a_failure(self, mock_sel):
        """WeCom's proactive command and Feishu's reply answer with NO id.

        For them an empty string is the SUCCESS value and failure raises, so applying
        the empty-id test turns every delivered message into a reported loss -- and on
        the cron path that means the dedup hash never advances and the identical
        result repeats on every tick. The transport declares which convention it
        follows; the caller asks rather than assuming.
        """
        transport = _channel_transport(send_result="")
        transport.capabilities.returns_message_id = False
        state = _channel_state(transport=transport, slack_client=MagicMock())
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "sweep done", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 200
                assert (await resp.json())["ok"] is True
                transport.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_long_message_is_chunked_rather_than_silently_truncated(self, mock_sel):
        """A transport caps by SLICING, and still answers with a message id.

        Telegram's ``_cap_text`` cuts at 4096, so handing it a longer message loses
        the tail and returns success — a delivery this leg would then audit as
        complete. Chunking against the transport's own cap is what makes the
        confirmation mean the whole message rather than its first part.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport, slack_client=MagicMock())
        app = _make_app(state)
        # Just over one cap, so exactly two parts, with a distinctive tail.
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-MARKER"

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": body, "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 200

        sent = [call.args[1] for call in transport.send_message.await_args_list]
        assert len(sent) > 1, "one cap-exceeding message must become several sends"
        assert all(len(part) <= TELEGRAM_MAX_TEXT for part in sent)
        # The TAIL is what a slice would have eaten, so assert it arrived rather
        # than merely counting the parts.
        assert "TAIL-MARKER" in "".join(sent)

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_an_inline_send_refuses_when_governance_tightens_during_resolve(self, mock_sel):
        """An inline send must not go out on a permit the ceiling has superseded.

        The ceiling is sampled BEFORE the target resolve, and that resolve is offloaded
        because it walks the profile directory — an unbounded await. A tightening landing
        inside that window leaves the resolved permit authorized under a ceiling that is
        not the installed one. The ceiling question is asked UNCONDITIONALLY, for every
        caller: gating it on an authored link being present would be a fail-OPEN, because
        the straddled window exists on every path and the inline leg is the LLM-facing one.

        Nothing is delivered: this refuses BEFORE part 1, so there is no partial send.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport, slack_client=MagicMock())
        app = _make_app(state)

        generation = {"v": 1}

        def _tighten_during_resolve() -> int:
            generation["v"] += 1
            return generation["v"]

        # BOTH bindings: the guard re-reads through governance_profiles' own globals, so
        # patching only the caller compares a fake sample against the real counter.
        with _governance(True):
            with (
                patch(
                    "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                    side_effect=_tighten_during_resolve,
                ),
                patch(
                    "kiro_crew.platform.governance_profiles.governance_answer_generation",
                    side_effect=_tighten_during_resolve,
                ),
            ):
                async with TestClient(TestServer(app)) as client:
                    await client.post(
                        "/api/send-message",
                        json={"text": "SHOULD-NOT-SEND", "channel_type": "telegram"},
                        headers={"X-Session-Key": _TG_KEY},
                    )

        assert transport.send_message.await_count == 0, (
            "an inline send went out after the governance ceiling moved during its own "
            "resolve, so part 1 was delivered under a superseded permit"
        )

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_an_inline_send_refuses_when_the_binding_moves_during_resolve(self, mock_sel):
        """A rebind straddling the resolve must refuse, not retarget, for every caller.

        The governance gate inside the resolve is offloaded and unbounded, so the user
        can unlink or rebind while it runs. Comparing the post-resolve walk against the
        destination this send selected needs no captured link, so gating it on one left
        the inline leg delivering part 1 into whatever binding replaced its own.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport, slack_client=MagicMock())
        app = _make_app(state)

        walks = {"n": 0}
        original = state.sessions.get_origin_link.return_value

        def _rebind_after_first_walk(*_args, **_kwargs):
            walks["n"] += 1
            return original if walks["n"] <= 1 else None

        state.sessions.get_origin_link.side_effect = _rebind_after_first_walk

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                await client.post(
                    "/api/send-message",
                    json={"text": "SHOULD-NOT-SEND", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )

        assert transport.send_message.await_count == 0, (
            "an inline send delivered after its binding changed during the resolve, so "
            "content reached a conversation the caller never selected"
        )

    @pytest.mark.asyncio
    async def test_an_inline_send_stops_remaining_parts_when_unlinked(self, mock_sel):
        """Binding revalidation is UNCONDITIONAL, inline `send_message` included.

        The comparand is `link`, this send's OWN resolved destination, so every caller
        holds it — being inline never made the check unaskable, only unasked. Being
        inline also bounds nothing useful: every transport send is an await, so a
        multi-part inline send spans the same window a dispatched one does, and parts
        2..N were reaching a conversation the user had already unlinked.

        Earlier revisions gated this to keep the LLM-facing tool's mid-send contract out
        of a note change. A security review ruled the exposure — content delivered to a
        revoked conversation — outweighs that, so it is asked before every part.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport, slack_client=MagicMock())

        def _unlink_after_first_part(*_args, **_kwargs):
            state.sessions.get_origin_link.return_value = None
            return "tg-1"

        transport.send_message = AsyncMock(side_effect=_unlink_after_first_part)
        app = _make_app(state)
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-AFTER-REVOKE"

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                await client.post(
                    "/api/send-message",
                    json={"text": body, "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )

        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert len(sent) == 1, (
            "the inline leg continued past the part in flight when the destination was "
            f"unlinked, so content reached a revoked conversation (got {len(sent)} parts)"
        )
        assert "TAIL-AFTER-REVOKE" not in "".join(
            sent
        ), "the tail reached a conversation unlinked before it was sent"

    @pytest.mark.asyncio
    async def test_a_dispatched_send_stops_remaining_parts_when_unlinked(self, mock_sel):
        """A DISPATCHED caller's later parts stop when the destination is revoked.

        The pre-send check cannot cover this. It runs once, before part 1, and a
        multi-part send then spans one await per part -- so a session unlinked after
        part 1 leaves parts 2..N addressed to a conversation the user has already
        disconnected.

        The revocation is driven from inside the first send so it lands exactly in
        that gap, and the assertion is the TAIL: counting parts would also pass if
        chunking silently collapsed to one message.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport, slack_client=MagicMock())

        def _unlink_after_first_part(*_args, **_kwargs):
            # The revocation a user performs mid-send: the session owns no
            # channel, so the next re-walk finds nothing to deliver to.
            state.sessions.get_origin_link.return_value = None
            return "tg-1"

        transport.send_message = AsyncMock(side_effect=_unlink_after_first_part)
        authored = state.sessions.get_origin_link.return_value
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-AFTER-REVOKE"

        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        with _governance(True):
            await deliver_to_channel(
                state,
                _TG_KEY,
                body,
                channel_type="telegram",
                tool_name="send_message",
                authored_link=(authored, True),
            )

        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert len(sent) == 1, (
            "the send must stop after the part that was already in flight when the "
            f"destination was unlinked; got {len(sent)} parts"
        )
        assert "TAIL-AFTER-REVOKE" not in "".join(
            sent
        ), "the tail reached a conversation that was unlinked before it was sent"

    @pytest.mark.asyncio
    async def test_a_ceiling_change_during_the_resolve_re_asks_and_delivers(self, mock_sel):
        """A token move that narrowed NOTHING must re-ask, not refuse.

        The sampled token spans ceiling AND profile and is a generation counter, so it
        moves on any unrelated profile publication. Refusing on the move alone rejected
        a send whose own permission never changed, which is what
        ``governance_ceiling_unchanged`` documents as the wrong reading: a move means the
        answer MAY have changed, so the caller re-asks.

        Driven by moving the counter across the resolve while governance stays PERMISSIVE,
        so the re-ask confirms the same permission and the part goes out. The refusal path
        is pinned by its sibling below, where the resolve itself stops permitting.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        generation = [11]
        real_resolve = chat_runner._resolve_channel_target

        def _move_token_during_resolve(*args, **kwargs):
            result = real_resolve(*args, **kwargs)
            generation[0] = 12
            return result

        with (
            _governance(True),
            patch(
                "kiro_crew.dashboard.chat_runner._resolve_channel_target",
                side_effect=_move_token_during_resolve,
            ),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                side_effect=lambda: generation[0],
            ),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=lambda: generation[0],
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                "BODY-AFTER-UNRELATED-EDIT",
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        assert delivered, (
            "an unrelated profile publication during the resolve refused a send whose "
            "permission never narrowed; the re-ask should have confirmed it"
        )
        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert any(
            "BODY-AFTER-UNRELATED-EDIT" in s for s in sent
        ), f"the body never reached the transport; got {sent}"
        refusals = [
            r for r in mock_sel.log_tool_invocation.call_args_list if "governance_changed" in str(r)
        ]
        assert not refusals, (
            "a token move that narrowed nothing filed a governance refusal; got "
            f"{[str(r) for r in refusals]}"
        )

    @pytest.mark.asyncio
    async def test_a_narrowing_during_the_resolve_still_refuses(self, mock_sel):
        """The straddled window's REAL hazard must stay closed after the early exit went.

        A tightening landing DURING the resolve is the one case the removed pre-part-1
        check uniquely caught: the resolve read the OLD ceiling and returned a target, so
        the permit is already superseded when it is handed back. The re-ask closes it
        instead -- its own resolve reads the NEW ceiling and stops permitting -- so this
        pins that the removal traded a false refusal for a re-ask, not for a fail-open.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        generation = [11]
        permits = [True]
        real_resolve = chat_runner._resolve_channel_target

        def _narrow_during_resolve(*args, **kwargs):
            result = real_resolve(*args, **kwargs)
            generation[0] = 12
            permits[0] = False
            return result

        with (
            patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=lambda *a, **k: _permitted(permits[0]),
            ),
            patch(
                "kiro_crew.dashboard.chat_runner._resolve_channel_target",
                side_effect=_narrow_during_resolve,
            ),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                side_effect=lambda: generation[0],
            ),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=lambda: generation[0],
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                "BODY-AFTER-NARROWING",
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        assert (
            not delivered
        ), "a tightening that landed while the authorizing resolve ran reported success"
        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert (
            not sent
        ), f"the send proceeded on a permit the resolve had already superseded; got {sent}"

    @pytest.mark.asyncio
    async def test_part_1_refuses_when_the_governance_ceiling_moved_after_resolve(self, mock_sel):
        """Part 1 must not send on a permit whose ceiling has since been replaced.

        Distinct window from the straddled resolve above: here the resolve completes
        under the ceiling it read, and the tightening lands between that confirmation
        and the first send. Part 1 has no re-resolve of its own, so without this check
        a single-part message had no later part to catch it.

        THE TOKEN NEVER SETTLES HERE, and that is what makes this a refusal. A moved
        token only means the answer MAY have changed, so part 1 re-asks once -- the same
        reading mid-send uses. A token still moving across that re-ask is a store nobody
        can get a stable answer out of, so the permit cannot be confirmed and the send
        refuses. Its sibling covers the case where the token settles and the permission
        is unchanged, which must DELIVER rather than 502 on an unrelated profile edit.

        Asserted on the TAIL being absent rather than on a count, so a chunker that
        collapsed to one message could not pass it vacuously.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-AFTER-TIGHTENING"

        # Called directly rather than through the endpoint, so this is the only leg in
        # flight and the reads are ordered: sample, post-resolve confirm, part 1.
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        # Moving on EVERY read, so no arm's behaviour depends on how many times the
        # generation is sampled: the token never settles and the permit cannot confirm.
        counter = itertools.count(11)

        def _next_generation() -> int:
            return next(counter)

        with (
            _governance(True),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                side_effect=_next_generation,
            ),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=_next_generation,
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                body,
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        assert not delivered, "the send reported success on a superseded permit"

        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert not sent, (
            "part 1 was sent on a permit whose governance ceiling had already been "
            f"replaced; got {len(sent)} part(s)"
        )
        assert "TAIL-AFTER-TIGHTENING" not in "".join(sent), (
            "content reached the conversation after the ceiling that authorized it "
            "was superseded"
        )
        denials = [
            r
            for r in mock_sel.log_tool_invocation.call_args_list
            if "governance_changed_before_part_1" in str(r)
        ]
        assert len(denials) == 1, (
            "the refusal needs its own audited reason so an operator can tell it from "
            f"a binding change; got {[str(r) for r in mock_sel.log_tool_invocation.call_args_list]}"
        )

    @pytest.mark.asyncio
    async def test_a_tightening_inside_the_pre_part_1_re_ask_refuses_the_send(self, mock_sel):
        """The generation returned by the pre-part-1 re-ask must be validated, not discarded.

        The ceiling-UNCHANGED arm re-asked the recipient and threw the generation
        away, while both sibling arms retain it and re-read
        governance_ceiling_unchanged. So a tightening landing strictly INSIDE that
        re-ask -- after its own profile read, before part 1 goes out -- settled an
        already-superseded permit and part 1 reached a newly denied channel. The
        window matters: a pre-resolve denial is caught by the base code already.

        A moved token alone is NOT a refusal here (see the unrelated-profile-edit
        sibling): the arm re-asks once, and the tightening surfaces as governance
        denying on that re-ask.
        """
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)

        # Single chunk on purpose, so _still_permitted(0) is the only arm that runs.
        body = "SHORT-BODY-TIGHTENED"

        # permitted for the initial resolve, denied from the pre-part-1 re-ask onward:
        # the tightening lands strictly inside that window.
        vets = [_permitted(True), _permitted(True)] + [_permitted(False)] * 20

        with (
            patch(
                "kiro_crew.platform.governance_profiles.vet_and_audit",
                side_effect=vets,
            ),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_ceiling_unchanged",
                side_effect=[True, False, True] + [True] * 20,
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                body,
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        assert not delivered, "the send reported success on a permit superseded mid-re-ask"
        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert "SHORT-BODY-TIGHTENED" not in "".join(
            sent
        ), "content reached a channel the tightened ceiling had already denied"
        denials = [
            r
            for r in mock_sel.log_tool_invocation.call_args_list
            if "governance_changed_before_part_1" in str(r)
        ]
        assert len(denials) == 1, (
            "the refusal must be audited as a governance change, not as a binding change, "
            f"so an operator can tell them apart; got "
            f"{[str(r) for r in mock_sel.log_tool_invocation.call_args_list]}"
        )

    @pytest.mark.asyncio
    async def test_an_authored_mirror_survives_revalidation_past_a_live_slack_origin(
        self, mock_sel
    ):
        """The revalidation walk must use the caller's own skips, or it refuses the note.

        Every revalidation compares the live walk against the snapshot the caller
        authored. The note mirror authors its snapshot with the Slack row skipped,
        because Slack never registers a transport here and travels on its own leg. A
        revalidation walk WITHOUT that skip lands on the live Slack origin instead, so
        the comparison reports the caller's own unchanged mirror as changed and the
        transport leg is refused whenever a Slack origin exists, which is the common
        case rather than an edge one.

        Asserted on delivery, so a refusal for any reason fails it.
        """
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        slack_origin = ChannelLink("slack", channel_id="C1", thread_id="1.1")
        telegram_mirror = ChannelLink("telegram", channel_id="T9", thread_id=None)

        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(return_value=slack_origin)
        state.sessions.get_mirror_link = MagicMock(return_value=telegram_mirror)

        with _governance(True):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                "MIRROR-BODY",
                skip_paused=True,
                skip_slack=True,
                tool_name="send_message",
                authored_link=(telegram_mirror, False),
            )

        assert delivered, (
            "the authored mirror was refused: the revalidation walk read the live Slack "
            "origin and reported the caller's own unchanged mirror as changed"
        )
        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert "MIRROR-BODY" in "".join(
            sent
        ), f"the note never reached the telegram mirror; sent {sent!r}"

    @pytest.mark.asyncio
    async def test_part_1_delivers_when_a_settled_token_move_changed_no_permission(self, mock_sel):
        """An unrelated profile edit during the resolve must not 502 a permitted send.

        The composite token spans ceiling AND profile, so ANY publication moves it --
        including an edit that changes nothing about this destination. Refusing on the
        bare move read the token as "the answer DID change", which its own contract
        denies: it means "MAY have changed". Mid-send already re-asks once on exactly
        this signal, so refusing outright before part 1 made the two paths disagree
        about one signal, and the disagreement fell on the LLM-facing tool -- a fully
        permitted single- or multi-part send failing on someone else's profile edit.

        The token moves once and then SETTLES, and governance still permits, so the
        re-ask confirms the permit and the send proceeds.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-AFTER-UNRELATED-EDIT"

        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        # Sample 11, post-resolve confirm 11, part-1 check 12 (the unrelated edit), then
        # settled at 12 for the re-ask: moved once, stable after, permission unchanged.
        generations = iter([11, 11, 12])

        def _next_generation() -> int:
            return next(generations, 12)

        with (
            _governance(True),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                side_effect=_next_generation,
            ),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=_next_generation,
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                body,
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        assert delivered, (
            "a send that governance still permits was refused because an unrelated "
            "profile publication moved the composite token while the resolve ran"
        )
        sent = "".join(c.args[1] for c in transport.send_message.await_args_list)
        assert "TAIL-AFTER-UNRELATED-EDIT" in sent, (
            "the send reported success but the tail never reached the conversation, so "
            "the message was truncated by the token move rather than delivered"
        )
        refusals = [
            r
            for r in mock_sel.log_tool_invocation.call_args_list
            if "governance_changed_before_part_1" in str(r)
        ]
        assert not refusals, (
            "a permitted send filed a governance refusal row for a token move that "
            f"narrowed nothing, which makes a real refusal unreadable; got {refusals}"
        )

    @pytest.mark.asyncio
    async def test_a_refusal_audit_names_the_selected_transport_not_the_generic_label(
        self, mock_sel
    ):
        """An egress row must name the destination it refused, not `channel`.

        `channel_type` is the caller's FILTER: a caller that does not constrain the walk
        passes nothing, so using it as the audit label makes every row from such a
        caller read `channel` regardless of where the send was actually headed. The note
        mirror is exactly that caller — it selects by `authored_link` and `skip_slack`
        rather than by type — so an operator filtering egress rows by transport could
        not see its sends at all, and two different destinations were indistinguishable
        in the audit trail. The note mirror's own leg rows already name the link's type;
        this is the shared helper's internal rows agreeing with them.
        """
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        # The authored destination and the live one differ, so the send refuses BEFORE
        # dispatch -- the earliest row that can already name a selected transport.
        authored = ChannelLink("telegram", channel_id="T-AUTHORED", thread_id=None)
        live = ChannelLink("telegram", channel_id="T-MOVED", thread_id=None)

        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(return_value=live)
        state.sessions.get_mirror_link = MagicMock(return_value=None)

        with _governance(True):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                "BODY",
                tool_name="note_mirror",
                authored_link=(authored, True),
            )

        assert not delivered, "precondition: the moved binding must refuse the send"
        rows = [
            c
            for c in mock_sel.log_tool_invocation.call_args_list
            if "link_changed_before_dispatch" in str(c)
        ]
        assert len(rows) == 1, (
            "the pre-dispatch refusal filed no audited row, so there is nothing to "
            f"carry a transport label; got {[str(c) for c in mock_sel.log_tool_invocation.call_args_list]}"
        )
        assert rows[0].kwargs.get("downstream_service") == "telegram", (
            "the egress row named the generic label instead of the transport the send "
            "had selected, so an operator filtering by transport cannot see this "
            f"caller's sends; got {rows[0].kwargs.get('downstream_service')!r}"
        )

    @pytest.mark.asyncio
    async def test_a_ceiling_change_during_the_mid_send_reresolve_refuses(self, mock_sel):
        """A later chunk must not send while the governance token is still moving.

        Parts 2..N re-resolve, and that resolve evaluates governance internally. A
        tightening completing BEFORE the read is caught by the recheck; one landing
        while the resolve runs is not, because the resolve returns a permit computed
        from the pre-tightening ceiling. So the token is sampled before each re-resolve
        and confirmed after it, synchronously, last before the send.

        A moved token alone is NOT the refusal: it spans both governance layers, so an
        unrelated profile edit moves it without changing this send's answer, and
        truncating there is the defect pinned by
        ``test_an_unrelated_profile_edit_mid_send_does_not_truncate``. What refuses here
        is a token that will not SETTLE -- it advances on every read, so the confirming
        resolve establishes no stable ceiling to have authorized under, and fail-closed
        is the only honest answer.

        Driven from inside the re-resolve, and asserted on the TAIL so a chunker
        collapsing to one message cannot pass it vacuously.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-AFTER-MIDSEND-TIGHTENING"

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        # Advance only once part 1 is authorized and delivered, so the window under test
        # is the mid-send one; then advance on every read so it never settles.
        generation = [11]
        resolves = [0]
        real_resolve = chat_runner._resolve_channel_target

        def _tighten_during_second_resolve(*args, **kwargs):
            result = real_resolve(*args, **kwargs)
            resolves[0] += 1
            return result

        def _read_generation() -> int:
            # Threshold is the MID-SEND resolve: part 1 now validates the generation its
            # own re-ask returned, so advancing at 2 refuses part 1 instead.
            if resolves[0] >= 3:
                generation[0] += 1
            return generation[0]

        with (
            _governance(True),
            patch(
                "kiro_crew.dashboard.chat_runner._resolve_channel_target",
                side_effect=_tighten_during_second_resolve,
            ),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                side_effect=_read_generation,
            ),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=_read_generation,
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                body,
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        assert not delivered, "the send reported success after a mid-send ceiling change"
        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert sent, "precondition: part 1 must have been delivered before the tightening"
        assert "TAIL-AFTER-MIDSEND-TIGHTENING" not in "".join(sent), (
            "a later chunk was sent on a permit the re-resolve computed under a ceiling "
            "that was replaced while it ran"
        )
        denials = [
            r
            for r in mock_sel.log_tool_invocation.call_args_list
            if "governance_changed_mid_send_before_part_" in str(r)
        ]
        assert len(denials) == 1, (
            "the mid-send refusal needs its own audited reason, distinct from a binding "
            f"change; got {[str(r) for r in mock_sel.log_tool_invocation.call_args_list]}"
        )

    @pytest.mark.asyncio
    async def test_an_unrelated_profile_edit_mid_send_does_not_truncate(self, mock_sel):
        """A token move that changed nothing for THIS send must not truncate it.

        The token spans both governance layers, so any profile publication moves it --
        including one that permits this destination exactly as before. Refusing on that
        move alone abandons a multi-part message after a delivered prefix, which is a
        partial delivery caused by an edit that authorized nothing differently.

        So a moved token is a reason to RE-ASK, not to stop: the confirming resolve
        re-derives the permission under the settled ceiling, and only a genuine
        narrowing, a moved binding, or a token that will not settle refuses. The tail is
        the assertion, so a chunker collapsing to one message cannot pass it vacuously.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        state.sessions.get_origin_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="T9", thread_id=None)
        )
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        body = "x" * TELEGRAM_MAX_TEXT + "TAIL-AFTER-UNRELATED-EDIT"

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        # One publication lands while the mid-send resolve runs, then the store is quiet,
        # and the resolve keeps permitting the same destination: only the counter moved.
        generation = [11]
        resolves = [0]
        real_resolve = chat_runner._resolve_channel_target

        def _publish_once_during_second_resolve(*args, **kwargs):
            result = real_resolve(*args, **kwargs)
            resolves[0] += 1
            if resolves[0] == 2:
                generation[0] += 1
            return result

        with (
            _governance(True),
            patch(
                "kiro_crew.dashboard.chat_runner._resolve_channel_target",
                side_effect=_publish_once_during_second_resolve,
            ),
            patch(
                "kiro_crew.dashboard.handlers.messaging.governance_answer_generation",
                side_effect=lambda: generation[0],
            ),
            patch(
                "kiro_crew.platform.governance_profiles.governance_answer_generation",
                side_effect=lambda: generation[0],
            ),
        ):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                body,
                tool_name="send_message",
                authored_link=(ChannelLink("telegram", channel_id="T9", thread_id=None), True),
            )

        sent = [c.args[1] for c in transport.send_message.await_args_list]
        assert "TAIL-AFTER-UNRELATED-EDIT" in "".join(sent), (
            "the send was truncated after a delivered prefix by a token move that "
            f"permitted this destination exactly as before; only {len(sent)} part(s) went"
        )
        assert delivered, "every part reached the destination, so this must report success"
        assert not [
            r
            for r in mock_sel.log_tool_invocation.call_args_list
            if "governance_changed_mid_send_before_part_" in str(r)
        ], "an unrelated publication was audited as a governance denial"

    @pytest.mark.asyncio
    async def test_a_later_chunk_failing_is_reported_not_swallowed(self, mock_sel):
        # The head landed and the tail did not, which is a partial delivery. It must
        # not read as success: on the cron path a True stands the Slack fallback down
        # and advances the dedup hash, so the missing tail would never be retried.
        transport = _channel_transport()
        transport.send_message = AsyncMock(side_effect=["tg-1", ""])
        state = _channel_state(transport=transport, slack_client=MagicMock())
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "y" * TELEGRAM_MAX_TEXT + "TAIL",
                        "channel_type": "telegram",
                    },
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502

    @pytest.mark.asyncio
    async def test_absent_channel_type_behaves_exactly_as_before(self, mock_sel):
        """The same state without channel_type stays dashboard-notification-only:
        the transport is never touched and delivered_to is unchanged."""
        transport = _channel_transport()
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _channel_state(transport=transport, slack_client=slack)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "fyi"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data == {
                    "ok": True,
                    "slack": False,
                    "session": False,
                    "delivered_to": "notification",
                }
                transport.send_message.assert_not_called()
                slack.post_message.assert_not_called()
                state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_on_a_channel_session_reaches_the_channel(self, mock_sel):
        """A cron whose job was created from a telegram session delivers to that
        conversation — the job's own session_key names it, not the request body —
        and the cron's Slack-DM default is suppressed."""
        transport = _channel_transport()
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        job = MagicMock()
        job.id = "job1"
        job.name = "nightly"
        job.session_key = _TG_KEY
        state = _channel_state(transport=transport, slack_client=slack, jobs=[job])
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "nightly report",
                        "channel_type": "telegram",
                        "caller_session": "cron:job1:run7",
                    },
                    headers={"X-Session-Key": "cron:job1:run7"},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["delivered_to"] == "telegram"
                transport.send_message.assert_awaited_once_with(
                    "99887766", "nightly report", thread_id="17"
                )
                slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_reaches_the_transport_display_safe(self, mock_sel):
        """The channel leg runs the shared outbound display sink: mentions are
        defanged and credentials redacted before the transport sees them."""
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "hey @everyone key AKIAIOSFODNN7EXAMPLE",
                        "channel_type": "telegram",
                    },
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 200
                sent = transport.send_message.await_args[0][1]
                # A literal "@everyone" would mass-notify the group.
                assert "@everyone" not in sent
                assert "@​everyone" in sent
                assert "AKIAIOSFODNN7EXAMPLE" not in sent


class TestChannelTypeMutualExclusion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra",
        [
            {"channel": "C0123ABC456"},
            {"user": "U0123ABC456"},
            {"thread_ts": "1712793600.123456"},
            {"session": "slack"},
        ],
    )
    async def test_slack_routing_fields_are_rejected(self, mock_sel, extra):
        """channel_type plus a Slack-only routing field is refused, not resolved
        by precedence — either order silently drops a named destination."""
        state = _channel_state(transport=_channel_transport(), slack_client=MagicMock())
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "channel_type": "telegram", **extra},
                headers={"X-Session-Key": _TG_KEY},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "channel_type_conflicts_slack_routing"
            state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_type_slack_is_rejected(self, mock_sel):
        """Slack has its own client and is absent from channel_transports, so
        naming it here would fail closed with no useful reason."""
        state = _channel_state()
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "channel_type": "slack"},
                headers={"X-Session-Key": _TG_KEY},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "channel_type_slack_unsupported"

    @pytest.mark.asyncio
    async def test_unknown_channel_type_is_rejected(self, mock_sel):
        state = _channel_state()
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "channel_type": "carrierpigeon"},
                headers={"X-Session-Key": _TG_KEY},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "channel_type_unknown"

    @pytest.mark.asyncio
    async def test_non_string_channel_type_is_rejected(self, mock_sel):
        state = _channel_state()
        app = _make_app(state)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/send-message",
                json={"text": "hi", "channel_type": 7},
                headers={"X-Session-Key": _TG_KEY},
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "channel_type_not_a_string"


class TestChannelTypeFailsClosed:
    @pytest.mark.asyncio
    async def test_governance_denial_refuses_the_send(self, mock_sel):
        """A `channels` denial for the transport refuses — it does not degrade to
        a Slack DM or to a notification-only success."""
        transport = _channel_transport()
        slack = MagicMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER")
        slack.post_message = AsyncMock(return_value="1712793600.000001")
        state = _channel_state(transport=transport, slack_client=slack)
        app = _make_app(state)

        with _governance(False):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "denied", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["ok"] is False
                assert data["code"] == "channel_delivery_failed"
                transport.send_message.assert_not_called()
                slack.post_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unresolvable_link_is_a_failure_not_a_notification(self, mock_sel):
        """No origin and no mirror link: the caller must be told nothing was
        posted, rather than reading ok/notification for a send that reached
        nobody on the surface it named."""
        state = _channel_state(link=None, transport=_channel_transport())
        state.sessions.get_mirror_link.return_value = None
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "orphan", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["ok"] is False
                assert data["code"] == "channel_delivery_failed"

    @pytest.mark.asyncio
    async def test_link_on_another_transport_is_refused(self, mock_sel):
        """channel_type=telegram against a discord link must not post to discord:
        that is an audience the caller never named."""
        transport = _channel_transport()
        state = _channel_state(
            link=ChannelLink(channel_type="discord", channel_id="D1", thread_id=None),
            transport=transport,
        )
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "wrong surface", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502
                transport.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_transport_is_refused(self, mock_sel):
        """A link whose transport is not registered cannot be delivered to."""
        state = _channel_state(transport=None)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "no transport", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["code"] == "channel_delivery_failed"

    @pytest.mark.asyncio
    async def test_transport_error_is_refused(self, mock_sel):
        """A transport that raises is a failure, not a silent notification."""
        transport = _channel_transport()
        transport.send_message = AsyncMock(side_effect=Exception("telegram 400"))
        state = _channel_state(transport=transport)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "boom", "channel_type": "telegram"},
                    headers={"X-Session-Key": _TG_KEY},
                )
                assert resp.status == 502
                data = await resp.json()
                assert data["code"] == "channel_delivery_failed"

    @pytest.mark.asyncio
    async def test_body_cannot_name_the_conversation(self, mock_sel):
        """A non-cron caller is identified by the kernel-attested X-Session-Key
        header, never by a body field. A body naming another session's key would
        post into a conversation the caller does not own, and no check stands
        between a body field and the link store — so it is not consulted at all.
        """
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    # A channel session key in the body, and no header at all.
                    json={
                        "text": "not my conversation",
                        "channel_type": "telegram",
                        "caller_session": _TG_KEY,
                    },
                )
                assert resp.status == 502
                transport.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unidentifiable_caller_is_refused(self, mock_sel):
        """No X-Session-Key and no cron job: there is no conversation to name, so
        the send fails closed rather than picking one."""
        transport = _channel_transport()
        state = _channel_state(transport=transport)
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={"text": "who am i", "channel_type": "telegram"},
                )
                assert resp.status == 502
                transport.send_message.assert_not_called()


class TestChannelTextIsNotTheNotificationText:
    @pytest.mark.asyncio
    async def test_session_closed_suffix_stays_on_the_bell(self, mock_sel):
        """`session=origin` on an unreachable origin appends "(session closed —
        delivered as notification)" for the BELL. The channel post is a real
        delivery, so it must not carry a sentence that says it is not one."""
        transport = _channel_transport()
        job = MagicMock()
        job.id = "job1"
        job.name = "nightly"
        job.session_key = _TG_KEY
        state = _channel_state(transport=transport, jobs=[job])
        state.get_slot.return_value = None
        app = _make_app(state)

        with (
            _governance(True),
            patch(
                "kiro_crew.dashboard.handlers.messaging.rehydrate_slot_from_history_async",
                return_value=None,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "report body",
                        "channel_type": "telegram",
                        "session": "origin",
                        "caller_session": "cron:job1",
                    },
                    headers={"X-Session-Key": "cron:job1"},
                )
                assert resp.status == 200
                assert (await resp.json())["delivered_to"] == "telegram"
                sent = transport.send_message.await_args[0][1]
                assert sent == "report body"
                # The bell keeps the suffix it always had.
                notified = state.notify.call_args[0][2]
                assert "session closed" in notified

    @pytest.mark.asyncio
    async def test_revalidation_covers_both_callers_and_authored_link_gates_comparison(
        self, mock_sel
    ):
        """Mid-send revalidation is unconditional; `authored_link` gates only the
        authored-link COMPARISON.

        An earlier revision keyed the re-walk on `authored_link` too, reasoning that
        an inline caller had already been checked against a binding that could not
        have moved. That held for the PRE-SEND check and not for the send: each part
        is an await, so an inline multi-part send has the same inter-part window, and
        its later parts reached a destination unlinked after part 1.

        So this pins two things that must not collapse into each other:
          * BOTH callers re-walk after the governance await -- more than one consult.
          * `authored_link` still decides whether the live binding is compared
            against what the WORK WAS AUTHORED FOR, which only a dispatched caller
            can ask. A stale snapshot must refuse where an inline caller delivers.

        Pinned by consult counts and by a refusal, not by source shape, so a
        behaviour-preserving refactor stays green.
        """
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        link = ChannelLink("telegram", channel_id="C1", thread_id=None)
        moved = ChannelLink("telegram", channel_id="C_MOVED", thread_id=None)

        def _fresh_state():
            tp = _channel_transport()
            st = _channel_state(transport=tp)
            st.sessions.get_origin_link = MagicMock(return_value=link)
            st.sessions.get_mirror_link = MagicMock(return_value=None)
            return st, tp

        inline_state, inline_tp = _fresh_state()
        assert await deliver_to_channel(
            inline_state, "dashboard:chat-1", "hi", tool_name="send_message"
        )
        inline_walks = inline_state.sessions.get_origin_link.call_count

        snap_state, snap_tp = _fresh_state()
        assert await deliver_to_channel(
            snap_state,
            "dashboard:chat-1",
            "hi",
            tool_name="chat.note",
            authored_link=(link, True),
        )
        snapshot_walks = snap_state.sessions.get_origin_link.call_count

        assert inline_tp.send_message.await_count == 1, "the inline send must still deliver"
        assert snap_tp.send_message.await_count == 1, "the snapshot send must still deliver"
        assert inline_walks > 1, (
            "the inline caller must re-walk after the governance await; measured "
            f"{inline_walks} consult(s). One means the revocation window between "
            "parts is open again for the endpoint every LLM send goes through."
        )
        assert (
            snapshot_walks > 1
        ), f"the dispatched caller must re-walk too; measured {snapshot_walks}"

        # The part `authored_link` DOES still gate: a snapshot that does not match
        # the live binding is a refusal, where the inline caller above delivered
        # against that same live binding.
        stale_state, stale_tp = _fresh_state()
        assert not await deliver_to_channel(
            stale_state,
            "dashboard:chat-1",
            "hi",
            tool_name="chat.note",
            authored_link=(moved, True),
        ), "a snapshot naming a different destination must refuse, never retarget"
        assert stale_tp.send_message.await_count == 0, "a refused send must not deliver"

    @pytest.mark.asyncio
    async def test_a_rebind_that_moves_only_the_thread_refuses(self, mock_sel):
        """A binding differing from the snapshot in `thread_id` ALONE must refuse.

        The sibling test above moves `channel_id`, which any comparison catches --
        including one that has silently stopped covering every field. `thread_id` is
        the field a hand-written comparison forgets, and forgetting it delivers a note
        into the wrong THREAD of the right channel, which is a real audience change.

        So this is the discriminating case for field completeness: it fails for a
        comparison that checks type and id but not thread, and passes for dataclass
        equality, which covers any field `ChannelLink` gains later.
        """
        from kiro_crew.dashboard.handlers.messaging import deliver_to_channel

        authored = ChannelLink("telegram", channel_id="C1", thread_id="T_ORIGINAL")
        live = ChannelLink("telegram", channel_id="C1", thread_id="T_REBOUND")

        tp = _channel_transport()
        state = _channel_state(transport=tp)
        state.sessions.get_origin_link = MagicMock(return_value=live)
        state.sessions.get_mirror_link = MagicMock(return_value=None)

        with _governance(True):
            delivered = await deliver_to_channel(
                state,
                "dashboard:chat-1",
                "hi",
                tool_name="chat.note",
                authored_link=(authored, True),
            )

        assert not delivered, (
            "a binding whose thread moved under the snapshot must refuse; delivering "
            "puts the note in a thread the work was never authorized for"
        )
        assert tp.send_message.await_count == 0, "a refused send must not deliver"

    @pytest.mark.asyncio
    async def test_cron_job_session_key_is_used_verbatim(self, mock_sel):
        """A dashboard-born cron's job.session_key must reach the link lookup
        whole. `_resolve_session_target` strips "dashboard:" to get a SLOT name;
        channel links are keyed by the full session key, so stripping here would
        silently lose a dashboard session's outbound mirror."""
        transport = _channel_transport()
        job = MagicMock()
        job.id = "job9"
        job.name = "mirrored"
        job.session_key = "dashboard:chat-3-1712793600"
        state = _channel_state(transport=transport, jobs=[job])
        app = _make_app(state)

        with _governance(True):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/send-message",
                    json={
                        "text": "mirrored report",
                        "channel_type": "telegram",
                        "caller_session": "cron:job9",
                    },
                    headers={"X-Session-Key": "cron:job9"},
                )
                assert resp.status == 200
                # EVERY lookup must carry the WHOLE key. `deliver_to_channel` walks
                # the ladder to choose a destination and re-walks after the
                # governance await to catch a binding revoked in between, so the
                # number of consults is an implementation detail of that discipline.
                # What this test is about is the KEY: a truncated or prefix-stripped
                # variant reaching the lookup silently loses the mirror. Asserting
                # every call's argument rather than the call COUNT keeps it pinning
                # that, and keeps it green when the revalidation shape changes.
                consults = state.sessions.get_origin_link.call_args_list
                assert consults, "the ladder must be consulted at least once"
                assert all(
                    c == call("dashboard:chat-3-1712793600") for c in consults
                ), f"every consult must use the whole key; got {consults}"


# ── the MCP tool half: which transport the governance gate names ──


@contextlib.contextmanager
def _tool_mcp_core(*, strict=_TG_KEY, lenient="", post=None):
    """Patch the mcp_core plumbing `mcp_tools.messaging.send_message` reaches.

    The handlers reach that plumbing as ATTRIBUTES of mcp_core precisely so a
    rebind here intercepts them (see the module docstring in mcp_tools/messaging).
    Yields the mocks so assertions run while the patches are still live.
    """
    from kiro_crew import mcp_core

    with contextlib.ExitStack() as stack:
        yield {
            "lenient": stack.enter_context(
                patch.object(mcp_core, "_resolve_session_key", return_value=lenient)
            ),
            "strict": stack.enter_context(
                patch.object(mcp_core, "_resolve_session_key_strict", return_value=strict)
            ),
            "chan_agent": stack.enter_context(
                patch.object(mcp_core, "_deny_channel_agent_messaging", return_value=None)
            ),
            "vet_messaging": stack.enter_context(
                patch.object(mcp_core, "_vet_messaging_governance", return_value=None)
            ),
            "vet_channel": stack.enter_context(
                patch.object(mcp_core, "_vet_channel_governance", return_value=None)
            ),
            "post": stack.enter_context(
                patch.object(
                    mcp_core,
                    "_post",
                    return_value=(
                        post if post is not None else {"ok": True, "delivered_to": "telegram"}
                    ),
                )
            ),
        }


def _call_tool(args):
    from kiro_crew.mcp_tools.messaging import send_message

    return send_message("send_message", args)


class TestSendMessageToolChannelType:
    def test_governance_vets_the_named_transport_not_slack(self):
        """The `channels` vet must name the transport the message actually leaves
        over. Vetting "slack" for a Telegram send would evaluate a Telegram
        denial against Slack's rule — and refuse a permitted Telegram send
        whenever Slack happens to be denied."""
        with _tool_mcp_core() as m:
            out = _call_tool({"text": "hi", "channel_type": "telegram"})
            assert "telegram" in out
            assert [c.args for c in m["vet_channel"].call_args_list] == [(_TG_KEY, "telegram")]

    def test_cron_without_channel_type_still_vets_slack(self):
        """Unchanged: a cron's bare send routes to the owner Slack DM, so Slack is
        the transport that gets vetted."""
        with _tool_mcp_core(
            strict="", lenient="cron:job1", post={"ok": True, "delivered_to": "slack"}
        ) as m:
            _call_tool({"text": "hi"})
            assert [c.args for c in m["vet_channel"].call_args_list] == [("cron:job1", "slack")]

    def test_bare_non_cron_send_vets_no_transport(self):
        """Unchanged: the notification-only path leaves over no transport, so the
        per-transport allowlist has nothing to rule on."""
        with _tool_mcp_core(strict="", lenient="dashboard:chat-1-1") as m:
            _call_tool({"text": "hi"})
            m["vet_channel"].assert_not_called()

    def test_channel_type_requires_a_strict_identity(self):
        """A lenient identity is an ancestor walk: a sub-agent would resolve to
        its parent and post into the parent's conversation."""
        with _tool_mcp_core(strict="", lenient=_TG_KEY) as m:
            out = _call_tool({"text": "hi", "channel_type": "telegram"})
            assert out.startswith("Error:")
            assert "verify caller identity" in out
            m["post"].assert_not_called()

    def test_verified_key_is_the_key_the_request_is_sent_under(self):
        """The identity that was checked is the identity the write carries —
        _post must not re-resolve leniently after a strict gate."""
        with _tool_mcp_core(lenient="dashboard:someone-else") as m:
            _call_tool({"text": "hi", "channel_type": "telegram"})
            assert m["post"].call_args.kwargs["session_key"] == _TG_KEY
            assert m["post"].call_args.args[1]["channel_type"] == "telegram"

    def test_channel_agent_containment_stays_ahead_of_the_new_egress(self):
        """A channel agent must not gain a destination by naming channel_type."""
        with _tool_mcp_core() as m:
            m["chan_agent"].return_value = "Error: channel agents cannot send messages."
            out = _call_tool({"text": "hi", "channel_type": "telegram"})
            assert out.startswith("Error:")
            m["post"].assert_not_called()
            m["vet_channel"].assert_not_called()

    @pytest.mark.parametrize(
        "extra",
        [
            {"channel": "C0123ABC456"},
            {"user": "U0123ABC456"},
            {"thread_ts": "1712793600.123456"},
            {"session": "slack"},
        ],
    )
    def test_slack_routing_fields_are_refused_before_any_send(self, extra):
        with _tool_mcp_core() as m:
            out = _call_tool({"text": "hi", "channel_type": "telegram", **extra})
            assert out.startswith("Error:")
            assert "cannot be combined with" in out
            m["post"].assert_not_called()

    def test_channel_delivery_failure_is_reported_as_an_error(self):
        """ "Error:" prefix, not "Failed:": call_tool_with_logging classifies only
        the former as a failure, so a "Failed:" return would be SEL-recorded as a
        completed call and hide a message that reached nobody."""
        with _tool_mcp_core(
            post={
                "ok": False,
                "code": "channel_delivery_failed",
                "error": "channel delivery to telegram failed",
            }
        ):
            out = _call_tool({"text": "hi", "channel_type": "telegram"})
            assert out.startswith("Error:")
            assert "telegram" in out

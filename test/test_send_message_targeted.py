"""Tests for targeted send_message — channel and user routing, plus api_slack_profile."""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

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
            assert data == {"ok": True, "slack": True, "session": False, "delivered_to": "slack", "ts": "1712793600.000001"}
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
                "ok": True, "slack": True, "session": False,
                "delivered_to": "slack", "ts": "1712793600.000009",
            }
            slack.open_dm.assert_called_once_with("U_OWNER")
            slack.post_message.assert_called_once_with(
                "D_OWNER", "sweep done", thread_ts=None,
                unfurl_links=None, unfurl_media=None, reply_broadcast=None,
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
                "/api/send-message", json={"text": "fyi"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data == {
                "ok": True, "slack": False, "session": False,
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


# ── OPTIONS button rendering (Mesh-2603) ──


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

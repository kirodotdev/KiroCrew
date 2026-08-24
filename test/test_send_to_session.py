"""Tests for the send_to_session MCP tool (cross-session messaging)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.mcp_tools import messaging

ORIGIN = "dashboard:chat-1-1712790000"
TARGET = "chat-2-1712793600"


@pytest.fixture(autouse=True)
def _resolvable_origin():
    """Give every test a realistic namespaced origin session key.

    The tool fails closed when the origin cannot be resolved. Tests that
    exercise origin-specific behavior re-patch the resolver inside their
    own `with` block, which takes precedence over this fixture.
    """
    with patch(
        "kiro_crew.mcp_core._resolve_session_key_strict",
        return_value=ORIGIN,
    ):
        yield


@pytest.fixture(autouse=True)
def _same_workspace_store():
    """Mock ConversationLog so the workspace-isolation gate is deterministic.

    Without this, tests would construct a real ConversationLog and read the
    ambient session store. Tests that exercise cross-workspace or
    missing-session behavior re-patch ConversationLog inside their own
    `with` block.
    """
    with patch("kiro_crew.mcp_core.ConversationLog") as mock_cl_cls:
        mock_cl_cls.return_value.has_log.return_value = True
        mock_cl_cls.return_value.get_metadata.return_value = {"workspace": "default"}
        yield


@pytest.fixture(autouse=True)
def _mock_sel():
    with patch("kiro_crew.mcp_core.sel") as m:
        instance = MagicMock()
        m.return_value = instance
        yield instance


def _cfg_with_flag(enabled: bool) -> MagicMock:
    cfg = MagicMock()
    cfg.dashboard.cross_session_send = enabled
    return cfg


def _call(args: dict) -> str:
    return messaging.send_to_session("send_to_session", args)


class TestSendToSessionFlagGate:
    def test_disabled_flag_rejects_without_posting(self):
        """When cross_session_send is off, the tool refuses and never hits the API."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(False)

            result = _call({"slot": TARGET, "message": "hello"})

            assert "disabled" in result.lower()
            mock_post.assert_not_called()

    def test_flag_gate_denial_is_audited(self, _mock_sel):
        with patch("kiro_crew.mcp_core._post"), patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(False)

            _call({"slot": TARGET, "message": "hello"})

            _mock_sel.log_tool_invocation.assert_called_once_with(
                session_key=ORIGIN,
                source="mcp",
                tool_name="send_to_session",
                outcome="denied",
            )

    def test_enabled_flag_posts_to_chat_api(self):
        """When enabled, the tool posts to /api/chat?ws=1 with the target slot."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"ok": True, "slot": TARGET}

            result = _call({"slot": TARGET, "message": "hello"})

            assert "sent" in result.lower()
            mock_post.assert_called_once()
            path, payload = mock_post.call_args[0][0], mock_post.call_args[0][1]
            assert path == "/api/chat?ws=1"
            assert payload["slot"] == TARGET

    def test_message_carries_cross_session_provenance(self):
        """The injected message is prefixed with the origin session key."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"ok": True, "slot": TARGET}

            _call({"slot": TARGET, "message": "hello"})

            payload = mock_post.call_args[0][1]
            assert payload["message"].startswith(f"[Cross-session message from {ORIGIN}]")
            assert "hello" in payload["message"]

    def test_message_credentials_are_redacted(self):
        """LLM-generated text is scanned before landing in another session."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"ok": True, "slot": TARGET}

            _call(
                {
                    "slot": TARGET,
                    "message": "key is AKIAIOSFODNN7EXAMPLE ok",
                }
            )

            payload = mock_post.call_args[0][1]
            assert "AKIAIOSFODNN7EXAMPLE" not in payload["message"]
            assert "ok" in payload["message"]


class TestSendToSessionValidation:
    def test_self_target_is_rejected(self):
        """Sending to the origin session would create a feedback loop."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)

            result = _call({"slot": "chat-1-1712790000", "message": "hi"})

            assert "own session" in result.lower()
            mock_post.assert_not_called()

    def test_unresolvable_origin_rejected(self):
        """Fail closed: an origin without a namespace cannot bypass the guards."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls, patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value="",
        ):
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)

            result = _call({"slot": TARGET, "message": "hi"})

            assert "error" in result.lower()
            mock_post.assert_not_called()

    def test_non_dashboard_origin_rejected(self):
        """cron:/hook: origins would bypass the dashboard self-target guard."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls, patch(
            "kiro_crew.mcp_core._resolve_session_key_strict",
            return_value="cron:job-123",
        ):
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)

            result = _call({"slot": TARGET, "message": "hi"})

            assert "dashboard origin" in result.lower()
            mock_post.assert_not_called()

    def test_invalid_slot_shape_rejected(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)

            result = _call({"slot": "../../etc/passwd", "message": "hi"})

            assert "error" in result.lower()
            mock_post.assert_not_called()


class TestSendToSessionWorkspaceIsolation:
    def test_nonexistent_target_rejected_before_workspace_check(self):
        """A missing session has empty metadata that buckets to 'default' and
        would pass the workspace comparison open — and /api/chat would then
        silently CREATE a session for the typo'd slot. The existence probe
        must run first."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls, patch(
            "kiro_crew.mcp_core.ConversationLog"
        ) as mock_cl_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_cl_cls.return_value.has_log.return_value = False

            result = _call({"slot": TARGET, "message": "hi"})

            assert "no session found" in result.lower()
            mock_post.assert_not_called()

    def test_cross_workspace_target_rejected(self):
        """Writes must not cross the workspace boundary the read paths guard."""
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls, patch(
            "kiro_crew.mcp_core.ConversationLog"
        ) as mock_cl_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_cl_cls.return_value.has_log.return_value = True

            def _meta(key: str) -> dict:
                if key == ORIGIN:
                    return {"workspace": "default"}
                return {"workspace": "other-workspace"}

            mock_cl_cls.return_value.get_metadata.side_effect = _meta

            result = _call({"slot": TARGET, "message": "hi"})

            assert "different workspace" in result.lower()
            mock_post.assert_not_called()


class TestSendToSessionOutcomes:
    def test_queued_response_reported(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"queued": True, "slot": TARGET}

            result = _call({"slot": TARGET, "message": "hi"})

            assert "queued" in result.lower()

    def test_api_error_reported(self):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"error": "boom"}

            result = _call({"slot": TARGET, "message": "hi"})

            assert "boom" in result or "failed" in result.lower()

    def test_success_is_audited(self, _mock_sel):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"ok": True, "slot": TARGET}

            _call({"slot": TARGET, "message": "hi"})

            _mock_sel.log_tool_invocation.assert_called_once_with(
                session_key=ORIGIN,
                source="mcp",
                tool_name="send_to_session",
                outcome="success",
            )

    def test_unexpected_response_audited_as_error(self, _mock_sel):
        with patch("kiro_crew.mcp_core._post") as mock_post, patch(
            "kiro_crew.config.loader.KiroCrewConfig"
        ) as mock_cfg_cls:
            mock_cfg_cls.load.return_value = _cfg_with_flag(True)
            mock_post.return_value = {"weird": True}

            result = _call({"slot": TARGET, "message": "hi"})

            assert "failed" in result.lower()
            _mock_sel.log_tool_invocation.assert_called_once_with(
                session_key=ORIGIN,
                source="mcp",
                tool_name="send_to_session",
                outcome="error",
            )


class TestToolRegistration:
    def test_tool_is_advertised_and_dispatchable(self):
        names = [t["name"] for t in messaging.schemas()]
        assert "send_to_session" in names
        assert messaging.HANDLERS["send_to_session"] is messaging.send_to_session


class TestConfigFlag:
    def test_dashboard_flag_defaults_false(self):
        from kiro_crew.config.loader import DashboardConfig

        assert DashboardConfig().cross_session_send is False

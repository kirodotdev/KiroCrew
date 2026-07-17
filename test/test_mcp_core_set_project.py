"""Tests for the ``set_project`` MCP tool and its strict identity resolver.

Two layers are under test:

1. ``_resolve_session_key_strict`` — refuses PID-walked identities so a
   subagent cannot silently mutate its parent slot's project.
2. ``_call_tool_inner("set_project", ...)`` — validates input, gates on
   strict identity, posts to the gateway endpoint, and maps responses.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import mcp_core

# ───────────────────────────── _resolve_session_key_strict ─────────────────


class TestResolveSessionKeyStrict:
    """Strict resolver: only the ``KIROCREW_SESSION_KEY`` env var produces a
    value. The PID-walk fallback that the lenient resolver uses is dropped."""

    def test_env_var_used(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:slot-B")
        assert mcp_core._resolve_session_key_strict() == "dashboard:slot-B"

    def test_returns_empty_when_only_pid_walk_would_match(self, monkeypatch):
        """Lenient resolver would walk /proc and find a session_pid_*.txt;
        strict returns "" so the caller can refuse."""
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        assert mcp_core._resolve_session_key_strict() == ""


# ───────────────────────────── set_project tool ─────────────────────────────


class TestSetProjectTool:
    """End-to-end behavior of the ``set_project`` dispatch branch in
    ``_call_tool_inner``. _post is mocked so the gateway HTTP layer is
    out of scope; tests focus on identity gating, error mapping, and the
    URL/body construction the wrapper produces."""

    def _invoke(self, args: dict, *, session_key: str = "dashboard:test-slot") -> str:
        """Invoke the tool with a mocked strict resolver and _post.

        Returns the tool's return string. ``_post_calls`` attribute on the
        result wrapper exposes what _post saw (so tests can assert the URL
        + body the tool constructed).
        """
        captured: dict = {"calls": []}

        def fake_post(path: str, body: dict | None = None) -> dict:
            captured["calls"].append((path, body))
            return captured.get("response", {"ok": True, "project": (body or {}).get("project", "")})

        with patch.object(mcp_core, "_resolve_session_key_strict", return_value=session_key), \
             patch.object(mcp_core, "_post", side_effect=fake_post):
            captured["response"] = getattr(self, "_post_response", None) or {
                "ok": True,
                "project": args.get("path", ""),
            }
            result = mcp_core._call_tool_inner("set_project", args)
        self._captured = captured
        return result

    def test_dashboard_session_posts_to_correct_url(self):
        result = self._invoke({"path": "/tmp/foo"})
        assert "Project set to /tmp/foo" in result
        assert len(self._captured["calls"]) == 1
        url, body = self._captured["calls"][0]
        assert url == "/api/chat/slots/test-slot/project"
        assert body == {"project": "/tmp/foo"}

    def test_clear_flag_clears_project(self):
        self._post_response = {"ok": True, "project": ""}
        result = self._invoke({"path": "", "clear": True})
        assert "Project cleared" in result
        url, body = self._captured["calls"][0]
        assert body == {"project": ""}

    def test_empty_path_without_clear_rejected(self):
        from kiro_crew.validation import ValidationError
        with patch.object(mcp_core, "_post") as mock_post, \
             patch.object(mcp_core, "_resolve_session_key_strict", return_value="dashboard:test"):
            with pytest.raises(ValidationError, match="required.*clear=true"):
                mcp_core._call_tool_inner("set_project", {"path": ""})
        mock_post.assert_not_called()

    def test_non_string_path_raises_validation_error_without_calling_post(self):
        from kiro_crew.validation import ValidationError
        with patch.object(mcp_core, "_post") as mock_post, \
             patch.object(mcp_core, "_resolve_session_key_strict", return_value="dashboard:test"):
            with pytest.raises(ValidationError):
                mcp_core._call_tool_inner("set_project", {"path": 123})
        mock_post.assert_not_called()

    def test_unresolved_session_is_rejected(self):
        with patch.object(mcp_core, "_post") as mock_post, \
             patch.object(mcp_core, "_resolve_session_key_strict", return_value=""):
            result = mcp_core._call_tool_inner("set_project", {"path": "/tmp"})
        assert "Error: set_project only works in dashboard sessions" in result
        mock_post.assert_not_called()

    def test_slack_session_is_rejected(self):
        with patch.object(mcp_core, "_post") as mock_post, patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="slack:T1:U1:C1:1.0"
        ):
            result = mcp_core._call_tool_inner("set_project", {"path": "/tmp"})
        assert "Error: set_project only works in dashboard sessions" in result
        mock_post.assert_not_called()

    def test_cron_session_is_rejected(self):
        with patch.object(mcp_core, "_post") as mock_post, patch.object(
            mcp_core, "_resolve_session_key_strict", return_value="cron:job-abc"
        ):
            result = mcp_core._call_tool_inner("set_project", {"path": "/tmp"})
        assert "Error: set_project only works in dashboard sessions" in result
        mock_post.assert_not_called()

    def test_endpoint_error_passes_through(self):
        """Endpoint may return 403 (sensitive path) / 400 (not a directory) /
        404 (slot not found). The wrapper surfaces the message verbatim."""
        self._post_response = {"error": "Access denied"}
        result = self._invoke({"path": "/some/path"})
        assert result == "Error: Access denied"

    def test_set_project_listed_in_tools(self):
        names = [t["name"] for t in mcp_core._list_tools()]
        assert "set_project" in names
        descriptor = next(t for t in mcp_core._list_tools() if t["name"] == "set_project")
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert "path" in schema["properties"]
        assert schema["properties"]["path"]["type"] == "string"
        assert schema["required"] == ["path"]

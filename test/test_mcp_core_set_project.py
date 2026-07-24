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
    """Strict resolver: the ``KIROCREW_SESSION_KEY`` env var, or the direct
    ``KIROCREW_HOST_PID`` -> ``session_pid_<pid>.txt`` lookup — the latter
    ONLY when the gateway-written HMAC sidecar verifies. The /proc ancestor
    WALK the lenient resolver uses is dropped, and an unsigned or forged
    file is refused."""

    def _signed_env(self, monkeypatch, tmp_path, pid: str, session_key: str):
        """Simulate the sandbox: env key stripped, HOST_PID set, and a
        gateway-published (signed) mapping on disk."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", pid)
        (tmp_path / "sel_hmac.key").write_bytes(b"k" * 32)
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            session_pid_sig.publish_session_pid(int(pid), session_key)

    def test_env_var_used(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:slot-B")
        assert mcp_core._resolve_session_key_strict() == "dashboard:slot-B"

    def test_returns_empty_when_only_pid_walk_would_match(self, monkeypatch):
        """Lenient resolver would walk /proc and find a session_pid_*.txt;
        strict returns "" so the caller can refuse."""
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        assert mcp_core._resolve_session_key_strict() == ""

    def test_env_var_wins_over_host_pid(self, monkeypatch, tmp_path):
        """When both identities are present the env var is authoritative."""
        from kiro_crew import session_pid_sig

        self._signed_env(monkeypatch, tmp_path, "4242", "dashboard:file-slot")
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:env-slot")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == "dashboard:env-slot"

    def test_signed_host_pid_mapping_accepted(self, monkeypatch, tmp_path):
        """Sandboxed session: env key stripped, launcher-declared HOST_PID
        maps to a gateway-published signed mapping — accepted."""
        from kiro_crew import session_pid_sig

        self._signed_env(
            monkeypatch, tmp_path, "4242", "dashboard:chat-32-1784855955"
        )
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert (
                mcp_core._resolve_session_key_strict()
                == "dashboard:chat-32-1784855955"
            )

    def test_unsigned_host_pid_file_refused(self, monkeypatch, tmp_path):
        """FORGERY: an agent writes a bare session_pid_<pid>.txt pointing at
        another slot's key. Without the HMAC sidecar (which requires the
        agent-unreadable SEL key) the strict resolver must refuse."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        (tmp_path / "sel_hmac.key").write_bytes(b"k" * 32)
        (tmp_path / "session_pid_4242.txt").write_text(
            "dashboard:victim-slot", encoding="utf-8"
        )
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_replayed_sidecar_for_other_pid_refused(self, monkeypatch, tmp_path):
        """REPLAY: a subagent copies the parent's .txt/.sig pair under its own
        pid. The pid is bound into the MAC, so verification must fail."""
        from kiro_crew import session_pid_sig

        (tmp_path / "sel_hmac.key").write_bytes(b"k" * 32)
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            # Gateway legitimately publishes the PARENT's mapping (pid 1000).
            session_pid_sig.publish_session_pid(1000, "dashboard:parent-slot")
        # Subagent (host pid 2000) replays the parent's pair under its own pid.
        for ext in ("txt", "sig"):
            (tmp_path / f"session_pid_2000.{ext}").write_text(
                (tmp_path / f"session_pid_1000.{ext}").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "2000")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_host_pid_without_file_returns_empty(self, monkeypatch, tmp_path):
        """A subagent sandbox exports its own HOST_PID, but the gateway never
        writes a session_pid file for it — strict must refuse, not walk."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "5555")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_non_numeric_host_pid_ignored(self, monkeypatch, tmp_path):
        """Malformed HOST_PID (path traversal, garbage) never reaches the
        filesystem lookup."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "../../etc/passwd")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_verifier_failure_returns_empty(self, monkeypatch):
        """Any error inside verification fails closed to ''."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        with patch.object(
            session_pid_sig, "verify_session_pid", side_effect=OSError("boom")
        ):
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

"""Tests for cron trigger feature (shared helper, CLI, MCP tool)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

import pytest

from kiro_crew.cron_trigger import trigger_cron_job
from kiro_crew.mcp_cron import _call_tool_inner

# ── Fixtures ──


class _MockDashboardHandler(BaseHTTPRequestHandler):
    """Minimal mock of the dashboard /api/crons/{id}/run endpoint."""

    secret = "test-secret-123"
    last_request_headers: dict = {}

    def do_POST(self):  # noqa: N802
        _MockDashboardHandler.last_request_headers = dict(self.headers)
        if "/api/crons/" in self.path and self.path.endswith("/run"):
            job_id = self.path.split("/api/crons/")[1].split("/run")[0]
            if self.headers.get("X-Internal-Secret") != self.secret:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"error":"forbidden"}')
                return
            if job_id == "aabbccddee":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"job not found"}')
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true,"name":"test-job-name"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


@pytest.fixture()
def mock_dashboard(tmp_path):
    """Start a mock dashboard server and write matching secret file."""
    server = HTTPServer(("127.0.0.1", 0), _MockDashboardHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    secret_path = tmp_path / ".local_secret"
    secret_path.write_text(_MockDashboardHandler.secret)
    yield port, tmp_path
    server.shutdown()


# ── Shared Helper Tests ──


class TestTriggerCronJob:
    """Tests for the shared trigger_cron_job helper."""

    def test_success(self, mock_dashboard):
        port, cfg_dir = mock_dashboard
        ok, msg = trigger_cron_job("abc12345", port, cfg_dir / ".local_secret")
        assert ok is True
        assert "test-job-name" in msg
        assert "abc12345" in msg

    def test_not_found(self, mock_dashboard):
        port, cfg_dir = mock_dashboard
        ok, msg = trigger_cron_job("aabbccddee", port, cfg_dir / ".local_secret")
        assert ok is False
        assert "Job not found" in msg

    def test_gateway_unreachable(self, tmp_path):
        secret_path = tmp_path / ".local_secret"
        secret_path.write_text("fake")
        ok, msg = trigger_cron_job("abc12345", 19999, secret_path)
        assert ok is False
        assert "cannot reach gateway" in msg

    def test_sends_secret_header(self, mock_dashboard):
        port, cfg_dir = mock_dashboard
        trigger_cron_job("abc12345", port, cfg_dir / ".local_secret")
        assert _MockDashboardHandler.last_request_headers.get("X-Internal-Secret") == "test-secret-123"

    def test_wrong_secret_returns_403(self, mock_dashboard, tmp_path):
        port, _ = mock_dashboard
        wrong_secret = tmp_path / ".local_secret"
        wrong_secret.write_text("wrong-secret")
        ok, msg = trigger_cron_job("abc12345", port, wrong_secret)
        assert ok is False
        assert "HTTP 403" in msg

    def test_missing_secret_file(self, mock_dashboard, tmp_path):
        """When .local_secret doesn't exist, request is sent without header (gets 403)."""
        port, _ = mock_dashboard
        missing_path = tmp_path / "nonexistent" / ".local_secret"
        ok, msg = trigger_cron_job("abc12345", port, missing_path)
        assert ok is False
        assert "HTTP 403" in msg

    def test_invalid_job_id_rejected(self, mock_dashboard):
        """Malformed job IDs are rejected before making HTTP request."""
        port, cfg_dir = mock_dashboard
        ok, msg = trigger_cron_job("../evil", port, cfg_dir / ".local_secret")
        assert ok is False
        assert "Invalid job ID format" in msg

    def test_invalid_job_id_slash(self, mock_dashboard):
        port, cfg_dir = mock_dashboard
        ok, msg = trigger_cron_job("abc/../../etc", port, cfg_dir / ".local_secret")
        assert ok is False
        assert "Invalid job ID format" in msg

    def test_invalid_job_id_too_short(self, mock_dashboard):
        port, cfg_dir = mock_dashboard
        ok, msg = trigger_cron_job("ab", port, cfg_dir / ".local_secret")
        assert ok is False
        assert "Invalid job ID format" in msg


# ── MCP Tool Tests ──


class TestCronTriggerMCP:
    """Tests for the cron_trigger MCP tool handler."""

    def test_trigger_success(self, mock_dashboard, monkeypatch):
        port, cfg_dir = mock_dashboard
        monkeypatch.setenv("KIROCREW_HOME", str(cfg_dir))
        from kiro_crew import mcp_cron
        with patch.object(mcp_cron, "config_dir", return_value=cfg_dir), \
             patch.object(mcp_cron, "DASHBOARD_PORT", port):
            result = _call_tool_inner("cron_trigger", {"job_id": "abc12345"})
        assert "test-job-name" in result
        assert "abc12345" in result
        assert "executing now" in result

    def test_trigger_invalid_id(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        from kiro_crew import mcp_cron
        from kiro_crew.config import loader
        orig_port = loader.DASHBOARD_PORT
        loader.DASHBOARD_PORT = 19999
        try:
            with patch.object(mcp_cron, "config_dir", return_value=tmp_path):
                result = _call_tool_inner("cron_trigger", {"job_id": "../../etc"})
        finally:
            loader.DASHBOARD_PORT = orig_port
        assert "Invalid job ID format" in result


# ── CLI Tests ──


class TestCronTriggerCLI:
    """Tests for the CLI cron trigger subcommand."""

    def test_cli_subcommand_registered(self):
        """kirocrew cron trigger is a recognized subcommand with job_id argument."""
        # Verify the handler accepts "trigger" action without crashing
        import argparse

        from kiro_crew.cli_commands import _cron
        args = argparse.Namespace(cron_action="trigger", job_id="abc12345")
        # Will fail with connection error (no gateway) but proves the action is recognized
        from pathlib import Path
        from unittest.mock import patch as _patch
        with _patch("kiro_crew.cli_commands.config_dir", return_value=Path("/tmp/nonexistent")):
            _cron(args)
        # If we get here without "Usage:" being printed, the action was recognized

    def test_cli_trigger_success(self, mock_dashboard, capsys):
        """CLI trigger prints success message."""
        import argparse

        from kiro_crew.cli_commands import _cron

        port, cfg_dir = mock_dashboard
        args = argparse.Namespace(cron_action="trigger", job_id="abc12345")

        from unittest.mock import patch as _patch

        with _patch("kiro_crew.cli_commands.config_dir", return_value=cfg_dir), \
             _patch("kiro_crew.cli_commands.DASHBOARD_PORT", port):
            _cron(args)

        captured = capsys.readouterr()
        assert "Triggered job:" in captured.out
        assert "abc12345" in captured.out

    def test_cli_trigger_invalid_id(self, capsys):
        """CLI rejects malformed job IDs."""
        import argparse

        from kiro_crew.cli_commands import _cron

        args = argparse.Namespace(cron_action="trigger", job_id="../evil")

        from pathlib import Path
        from unittest.mock import patch as _patch

        from kiro_crew.config import loader

        orig_port = loader.DASHBOARD_PORT
        loader.DASHBOARD_PORT = 19999
        try:
            with _patch("kiro_crew.cli_commands.config_dir", return_value=Path("/tmp")):
                _cron(args)
        finally:
            loader.DASHBOARD_PORT = orig_port

        captured = capsys.readouterr()
        assert "Invalid job ID format" in captured.out


# ── Tool Definition Tests ──


class TestCronTriggerToolDefinition:
    """Verify cron_trigger appears in tool list with correct schema."""

    def test_tool_listed(self):
        from kiro_crew.mcp_cron import _list_tools
        tools = _list_tools()
        names = [t["name"] for t in tools]
        assert "cron_trigger" in names

    def test_tool_schema(self):
        from kiro_crew.mcp_cron import _list_tools
        tools = _list_tools()
        trigger = next(t for t in tools if t["name"] == "cron_trigger")
        assert "job_id" in trigger["inputSchema"]["properties"]
        assert "job_id" in trigger["inputSchema"]["required"]

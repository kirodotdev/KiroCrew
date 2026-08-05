"""Tests for oversized MCP response handling.

Covers:
(a) Response under limit passes untouched
(b) Over transport limit + parseable id -> only that request fast-failed -32000
(c) Id unparseable -> all pending failed, none hanging
(d) Over spill threshold -> spill file has full content, in-band truncated
(e) Spill failure (unwritable dir) -> original forwarded unmodified
(f) Non-tool-result frames over threshold pass through untouched
(g) Config/env overrides for both limits
(h) Startup cleanup removes >24h spill files, keeps fresh
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway.spill import (
    cleanup_old_spill_files,
    extract_id_from_bytes,
    maybe_spill_response,
)

# --- (a) Response under limit passes untouched ---


class TestUnderThresholdPassthrough:
    def test_small_response_unchanged(self):
        """A response under the spill threshold is returned unchanged."""
        msg = {"jsonrpc": "2.0", "id": "req-1", "result": {"content": [{"type": "text", "text": "hello"}]}}
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        result = maybe_spill_response(line, "test-server", 1024 * 1024)
        assert result == line

    def test_empty_line_passthrough(self):
        """An empty or whitespace line passes through."""
        assert maybe_spill_response(b"\n", "test-server", 100) == b"\n"


# --- (b) Over transport limit + parseable id -> per-request fast-fail ---


class TestExtractId:
    def test_string_id(self):
        data = b'{"jsonrpc":"2.0","id":"gw-12345-7","result":{}}'
        assert extract_id_from_bytes(data) == "gw-12345-7"

    def test_integer_id(self):
        data = b'{"jsonrpc":"2.0","id":42,"result":{}}'
        assert extract_id_from_bytes(data) == 42

    def test_no_id(self):
        data = b'{"jsonrpc":"2.0","method":"notify"}'
        assert extract_id_from_bytes(data) is None

    def test_garbage(self):
        data = b'\x00\x01\x02binary garbage'
        assert extract_id_from_bytes(data) is None

    def test_id_in_first_4kb(self):
        """Id within first 4KB is found even with large payload after."""
        prefix = b'{"jsonrpc":"2.0","id":"found-it","result":{"content":[{"type":"text","text":"'
        payload = prefix + b"x" * 10000 + b'"}]}}\n'
        assert extract_id_from_bytes(payload) == "found-it"


# --- (d) Over spill threshold -> spill file with full content ---


class TestSpillToFile:
    def test_tool_result_spilled(self, tmp_path):
        """A tool/call result over threshold is spilled to file."""
        big_text = "A" * 500_000  # 500 KB
        msg = {
            "jsonrpc": "2.0",
            "id": "req-42",
            "result": {"content": [{"type": "text", "text": big_text}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        threshold = 256 * 1024

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "test-mcp", threshold)

        # Result should be valid JSON
        result_msg = json.loads(result.decode("utf-8"))
        assert result_msg["id"] == "req-42"
        assert result_msg["jsonrpc"] == "2.0"

        # Text should be truncated with marker
        text = result_msg["result"]["content"][0]["text"]
        assert "[KiroCrew: response truncated" in text
        assert "mcp_spill" in text
        assert len(text.encode("utf-8")) < len(line)

        # Spill file should exist with full content
        spill_dir = tmp_path / "mcp_spill"
        assert spill_dir.exists()
        spill_files = list(spill_dir.iterdir())
        assert len(spill_files) == 1
        spill_content = json.loads(spill_files[0].read_bytes())
        assert spill_content["result"]["content"][0]["text"] == big_text

    def test_spill_preserves_id(self, tmp_path):
        """Spilled response preserves the original JSON-RPC id."""
        big_text = "B" * 300_000
        msg = {
            "jsonrpc": "2.0",
            "id": "gw-999-15",
            "result": {"content": [{"type": "text", "text": big_text}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "builder-mcp", 100_000)

        result_msg = json.loads(result.decode("utf-8"))
        assert result_msg["id"] == "gw-999-15"

    def test_spill_dir_mode_0700(self, tmp_path):
        """Spill directory is created with mode 0700."""
        big_text = "C" * 300_000
        msg = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"content": [{"type": "text", "text": big_text}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            maybe_spill_response(line, "server", 100_000)

        spill_dir = tmp_path / "mcp_spill"
        assert spill_dir.is_dir()
        if not pc.IS_WINDOWS:
            assert spill_dir.stat().st_mode & 0o777 == 0o700

    def test_spill_dir_is_tightened_when_it_already_exists(self, tmp_path, monkeypatch):
        """A pre-existing loose directory must be tightened, not accepted.

        ``mkdir(mode=...)`` is ignored for a directory that already exists, so a
        spill directory created before the owner-only guarantee (or by a looser
        umask) would keep its permissions while holding spilled tool responses.
        """
        if pc.IS_WINDOWS:
            calls: list[str] = []
            monkeypatch.setattr(
                pc, "restrict_to_owner", lambda p: calls.append(str(p))
            )
        loose = tmp_path / "mcp_spill"
        loose.mkdir(mode=0o755)

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "y" * 200_000}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            maybe_spill_response(line, "server", 100_000)

        if pc.IS_WINDOWS:
            assert str(loose) in calls
        else:
            assert loose.stat().st_mode & 0o777 == 0o700


# --- (e) Spill failure -> original forwarded unmodified ---


class TestSpillFailure:
    def test_unwritable_dir_returns_original(self, tmp_path):
        """When spill file cannot be written, original is returned."""
        big_text = "D" * 300_000
        msg = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"content": [{"type": "text", "text": big_text}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        # Point at a non-existent path that can't be created
        fake_home = "/nonexistent/path/that/cannot/be/created"
        with patch.dict(os.environ, {"KIROCREW_HOME": fake_home}):
            result = maybe_spill_response(line, "server", 100_000)

        # Original returned unmodified
        assert result == line


# --- (f) Non-tool-result frames over threshold pass through ---


class TestNonToolResultPassthrough:
    def test_error_response_passes_through(self, tmp_path):
        """JSON-RPC error responses pass through even if large."""
        big_error = "E" * 300_000
        msg = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "error": {"code": -32000, "message": big_error},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "server", 100_000)

        assert result == line

    def test_non_text_content_passes_through(self, tmp_path):
        """Result with non-text content items passes through."""
        msg = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"content": [{"type": "image", "data": "x" * 300_000}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "server", 100_000)

        assert result == line

    def test_notification_passes_through(self, tmp_path):
        """Notifications (no id, has method) pass through."""
        msg = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"data": "x" * 300_000}}
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "server", 100_000)

        assert result == line

    def test_result_without_content_list(self, tmp_path):
        """Result without a content list passes through."""
        msg = {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"tools": [{"name": "x" * 300_000}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "server", 100_000)

        assert result == line


# --- (g) Config/env overrides for both limits ---


class TestConfigOverrides:
    def test_read_limit_env_override(self):
        """KIROCREW_MCP_READ_LIMIT env var overrides the default."""
        from kiro_crew.mcp_gateway import pool
        with patch.dict(os.environ, {"KIROCREW_MCP_READ_LIMIT": "8388608"}):
            val = pool._resolve_read_buffer_limit()
        assert val == 8388608

    def test_read_limit_env_invalid_uses_default(self):
        """Invalid KIROCREW_MCP_READ_LIMIT falls back to default."""
        from kiro_crew.mcp_gateway import pool
        with patch.dict(os.environ, {"KIROCREW_MCP_READ_LIMIT": "not_a_number"}):
            val = pool._resolve_read_buffer_limit()
        assert val == pool._DEFAULT_READ_BUFFER_LIMIT

    def test_read_limit_env_too_small_uses_default(self):
        """KIROCREW_MCP_READ_LIMIT below 1024 falls back to default."""
        from kiro_crew.mcp_gateway import pool
        with patch.dict(os.environ, {"KIROCREW_MCP_READ_LIMIT": "100"}):
            val = pool._resolve_read_buffer_limit()
        assert val == pool._DEFAULT_READ_BUFFER_LIMIT

    def test_spill_threshold_env_override(self):
        """KIROCREW_MCP_SPILL_THRESHOLD env var overrides the default."""
        from kiro_crew.mcp_gateway import pool
        with patch.dict(os.environ, {"KIROCREW_MCP_SPILL_THRESHOLD": "1048576"}):
            val = pool._resolve_spill_threshold()
        assert val == 1048576

    def test_spill_threshold_zero_disables(self):
        """KIROCREW_MCP_SPILL_THRESHOLD=0 disables spilling."""
        from kiro_crew.mcp_gateway import pool
        with patch.dict(os.environ, {"KIROCREW_MCP_SPILL_THRESHOLD": "0"}):
            val = pool._resolve_spill_threshold()
        assert val == 0

    # The read-buffer/spill config keys are parsed into McpGatewayConfig and
    # documented as settable in config.json, but the runtime globals in
    # mcp_gateway/pool.py were resolved ONLY from env vars — so the config keys
    # were dead (a user raising the 64 MiB limit per the docs got a silent
    # no-op; oversize responses kept being fast-failed). The
    # resolvers now consult the config value when the env var is unset.
    def test_read_buffer_limit_reads_config_when_env_unset(self, monkeypatch):
        import kiro_crew.mcp_gateway.pool as pool

        monkeypatch.delenv("KIROCREW_MCP_READ_LIMIT", raising=False)
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={"mcp_gateway": {"read_buffer_limit_bytes": 134217728}},
        ):
            assert pool._resolve_read_buffer_limit() == 134217728

    def test_spill_threshold_reads_config_when_env_unset(self, monkeypatch):
        import kiro_crew.mcp_gateway.pool as pool

        monkeypatch.delenv("KIROCREW_MCP_SPILL_THRESHOLD", raising=False)
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={"mcp_gateway": {"response_spill_threshold_bytes": 1048576}},
        ):
            assert pool._resolve_spill_threshold() == 1048576

    def test_env_var_still_beats_config(self, monkeypatch):
        import kiro_crew.mcp_gateway.pool as pool

        monkeypatch.setenv("KIROCREW_MCP_READ_LIMIT", "2048")
        with patch(
            "kiro_crew.config.loader._raw_config",
            return_value={"mcp_gateway": {"read_buffer_limit_bytes": 999999}},
        ):
            assert pool._resolve_read_buffer_limit() == 2048

    def test_default_when_neither_env_nor_config(self, monkeypatch):
        import kiro_crew.mcp_gateway.pool as pool

        monkeypatch.delenv("KIROCREW_MCP_READ_LIMIT", raising=False)
        with patch("kiro_crew.config.loader._raw_config", return_value={}):
            assert pool._resolve_read_buffer_limit() == pool._DEFAULT_READ_BUFFER_LIMIT


# --- (h) Startup cleanup removes >24h spill files, keeps fresh ---


class TestSpillCleanup:
    def test_cleanup_removes_old_files(self, tmp_path):
        """Files older than 24h are removed."""
        spill_dir = tmp_path / "mcp_spill"
        spill_dir.mkdir(mode=0o700)

        old_file = spill_dir / "old-response.json"
        old_file.write_text("old")
        # Set mtime to 25h ago
        old_time = time.time() - (25 * 3600)
        os.utime(old_file, (old_time, old_time))

        fresh_file = spill_dir / "fresh-response.json"
        fresh_file.write_text("fresh")

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            deleted = cleanup_old_spill_files()

        assert deleted == 1
        assert not old_file.exists()
        assert fresh_file.exists()

    def test_cleanup_no_dir_returns_zero(self, tmp_path):
        """When spill dir doesn't exist, returns 0."""
        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            assert cleanup_old_spill_files() == 0

    def test_cleanup_keeps_fresh_files(self, tmp_path):
        """Files younger than 24h are kept."""
        spill_dir = tmp_path / "mcp_spill"
        spill_dir.mkdir(mode=0o700)

        fresh_file = spill_dir / "recent.json"
        fresh_file.write_text("recent")

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            deleted = cleanup_old_spill_files()

        assert deleted == 0
        assert fresh_file.exists()

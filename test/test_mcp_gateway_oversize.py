"""Tests for oversized MCP response handling.

Covers:
(a) Response under limit passes untouched
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

import pytest

from conftest import make_dir_link
from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway.spill import (
    cleanup_old_spill_files,
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

    def test_spill_refuses_a_linked_spill_dir(self, tmp_path):
        """The WRITE path refuses a linked spill dir too.

        Sibling of the cleanup guard, same directory and same blind spot. This
        function's own comment says why it matters: "an attacker-planted link
        would redirect the write outside the data home". What gets written is a
        full tool response -- precisely the payload that may carry secrets.

        ``O_EXCL``/``O_NOFOLLOW`` on the open do not cover it: they constrain
        the FINAL path component, not the directory the path resolves through,
        and ``os.O_NOFOLLOW`` does not exist on Windows -- the only platform
        that has junctions.
        """
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()

        spill_dir = tmp_path / "mcp_spill"
        make_dir_link(spill_dir, victim_dir)

        big_text = "S" * 500_000
        msg = {
            "jsonrpc": "2.0",
            "id": "req-7",
            "result": {"content": [{"type": "text", "text": big_text}]},
        }
        line = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n"

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            result = maybe_spill_response(line, "test-mcp", 256 * 1024)

        assert list(victim_dir.iterdir()) == [], "spilled a payload outside the data home"
        assert result == line, "a refused spill must forward the original line unmodified"

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
            wrong: list[str] = []
            monkeypatch.setattr(pc, "restrict_dir_to_owner", lambda p: calls.append(str(p)))
            # The file-shaped helper must NOT be what a directory goes through:
            # its icacls grants carry no (OI)(CI), so spilled payloads written
            # into the directory afterwards would not inherit the lockdown.
            monkeypatch.setattr(pc, "restrict_to_owner", lambda p: wrong.append(str(p)))
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
            assert str(loose) not in wrong
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

        # A home that cannot be created ON ANY HOST: a path beneath a regular
        # file, so `mkdir(parents=True)` fails with NotADirectoryError /
        # FileExistsError everywhere. The previous `/nonexistent/path/...`
        # literal only looked uncreatable: on Windows a leading slash is
        # drive-relative, so it resolved to a writable `C:\nonexistent\...`,
        # the spill SUCCEEDED, and a 300 KiB sidecar was left at the drive
        # root -- which then made `install_app("/nonexistent/path")` in
        # test_app_manager see a real directory.
        blocker = tmp_path / "blocker"
        blocker.write_bytes(b"")
        fake_home = str(blocker / "home")
        with patch.dict(os.environ, {"KIROCREW_HOME": fake_home}):
            result = maybe_spill_response(line, "server", 100_000)

        # Original returned unmodified, and nothing was written anywhere.
        assert result == line
        assert blocker.is_file()
        assert not (blocker / "home").exists()


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

    def test_cleanup_refuses_a_linked_spill_dir(self, tmp_path):
        """A linked spill dir is refused, so the sweep never runs through it.

        ``is_symlink()`` answers False for a Windows junction, so the dir-level
        guard admitted one and the sweep iterated the junction's TARGET,
        calling ``unlink()`` on every 24h-old regular file there. That is
        exactly the confused deputy this function's docstring says it hardened
        against, reached by a link kind its check cannot see. The per-entry
        ``lstat``/``S_ISREG`` screen does not help: it guards links INSIDE the
        directory, while here the directory itself is the link.
        """
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        precious = victim_dir / "precious.json"
        precious.write_text("precious")
        old_time = time.time() - (25 * 3600)
        os.utime(precious, (old_time, old_time))

        spill_dir = tmp_path / "mcp_spill"
        make_dir_link(spill_dir, victim_dir)

        with patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}):
            deleted = cleanup_old_spill_files()

        assert deleted == 0
        assert precious.exists(), "cleanup deleted a file outside the spill dir"

    @pytest.mark.skipif(not pc.IS_WINDOWS, reason="junctions are Windows-only")
    def test_make_dir_link_yields_a_junction_on_windows(self, tmp_path):
        """Guard the guard: the seam above must produce a JUNCTION, not a symlink.

        What a junction *is* -- a directory that ``is_symlink()`` misses and
        ``is_link_or_junction`` catches -- is already pinned by
        ``test_junction_is_recognised_and_removable``. The property that is
        NOT pinned anywhere else, and that the two containment tests above
        depend on, is that this particular seam yields one: were
        ``make_dir_link`` ever to hand back a symlink on Windows, both would
        pass against the ORIGINAL ``is_symlink()`` guard and prove nothing.
        """
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        make_dir_link(link, target)

        assert link.is_dir()
        assert not link.is_symlink()

"""How a script cron's MCP server is stopped: the whole tree on Windows, the child on POSIX.

Windows is driven by mocks only: ``platform_compat`` is swapped through the facade,
so no real ``taskkill`` runs and no real ``.cmd`` server is started.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew.cron_script as cron_script
from kiro_crew import cron_script_mcp
from kiro_crew.cron_script_mcp import KeptMcpServers, McpToolClient
from kiro_crew.testing.ids import UNALLOCATABLE_PID


def _client(stderr_name=None):
    client = object.__new__(McpToolClient)
    client._proc = MagicMock(pid=UNALLOCATABLE_PID)
    client._sandbox_cleanup = None
    if stderr_name is not None:
        client._stderr_file = SimpleNamespace(name=str(stderr_name), close=lambda: None)
    return client


def _platform(monkeypatch, *, windows):
    fake = SimpleNamespace(IS_WINDOWS=windows, kill_popen_tree=MagicMock())
    monkeypatch.setattr(cron_script, "platform_compat", fake)
    return fake


def test_windows_close_stops_the_whole_server_tree(monkeypatch):
    fake = _platform(monkeypatch, windows=True)
    client = _client()
    client.close()
    fake.kill_popen_tree.assert_called_once_with(client._proc)
    client._proc.terminate.assert_not_called()
    client._proc.wait.assert_called_once()


def test_posix_close_still_terminates_only_the_child(monkeypatch):
    fake = _platform(monkeypatch, windows=False)
    client = _client()
    client.close()
    client._proc.terminate.assert_called_once_with()
    fake.kill_popen_tree.assert_not_called()


def test_a_capture_file_that_cannot_be_deleted_is_logged_at_warning(tmp_path, monkeypatch, caplog):
    _platform(monkeypatch, windows=False)
    held = tmp_path / "mcp-stderr-held.log"
    held.mkdir()  # unlink() of a directory raises OSError on every platform
    client = _client(stderr_name=held)
    with caplog.at_level(logging.WARNING, logger=cron_script_mcp.logger.name):
        client.close()
    assert any(
        r.levelno == logging.WARNING and "stderr capture" in r.getMessage() for r in caplog.records
    )


def test_a_failed_stop_never_replaces_the_calls_own_error(monkeypatch):
    class StuckClient:
        def __init__(self, server, session_key=""):
            pass

        def call_tool(self, tool, args):
            raise RuntimeError("MCP server 'slack' disconnected")

        def close(self):
            raise PermissionError("capture file still open")

    monkeypatch.setattr(cron_script, "McpToolClient", StuckClient)
    servers = KeptMcpServers()
    monkeypatch.setattr(servers, "_tools_switched_off", lambda server: frozenset())
    with pytest.raises(RuntimeError, match="disconnected"):
        servers.call_tool("slack", "post", {})

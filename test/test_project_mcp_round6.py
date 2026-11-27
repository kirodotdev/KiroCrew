"""Project MCP checks follow each host's own reading of its file, and cards show the command."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.change_card_project_mcp import build_preview, server_snapshot
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    settled = threading.Event()
    settled.set()
    monkeypatch.setattr(approvals, "_MIGRATION_SETTLED", settled)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _write(folder, name, servers):
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def _card(server) -> dict[str, Any]:
    params = {
        "folder": server.folder,
        "file": server.file,
        "server": server.name,
        "fingerprint": server.fingerprint,
    }
    out: dict[str, Any] = {}
    build_preview(params, server_snapshot(params), out)
    return out


def test_a_claude_entry_with_disabled_is_still_checked(project):
    _write(project, CLAUDE_PROJECT_MCP_FILE, {"weather": {"command": "w", "disabled": True}})
    [server] = approvals.pending_servers(project)
    assert (server.file, server.name) == (CLAUDE_PROJECT_MCP_FILE, "weather")


def test_a_kiro_entry_with_disabled_is_skipped(project):
    _write(project, KIRO_PROJECT_MCP_FILE, {"weather": {"command": "w", "disabled": True}})
    assert approvals.pending_servers(project) == []


def test_a_command_with_a_url_shows_the_command_line(project):
    entry = {"command": "weather-server", "args": ["--port", "1"], "url": "https://example.test/"}
    _write(project, CLAUDE_PROJECT_MCP_FILE, {"weather": entry})
    [server] = approvals.pending_servers(project)
    rows = _card(server)["changes"]
    assert {"label": "Runs", "after": "weather-server --port 1"} in rows
    assert {"label": "URL", "after": "https://example.test/"} in rows


def test_a_url_only_entry_keeps_the_url_row(project):
    _write(project, CLAUDE_PROJECT_MCP_FILE, {"weather": {"url": "https://example.test/"}})
    [server] = approvals.pending_servers(project)
    assert {"label": "URL", "after": "https://example.test/"} in _card(server)["changes"]


@pytest.mark.parametrize(
    "other",
    [
        {"command": "weather-server", "args": ["b"], "url": "https://example.test/"},
        {"command": "other", "args": ["a"], "url": "https://example.test/"},
        {"command": "weather-server", "args": ["a"], "url": "https://other.test/"},
    ],
)
def test_the_launch_digest_covers_command_args_and_url(other):
    entry = {"command": "weather-server", "args": ["a"], "url": "https://example.test/"}
    assert approvals.launch_digest(entry) != approvals.launch_digest(other)

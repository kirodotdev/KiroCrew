"""A project MCP card shows every field that decides what a server starts: URL and command."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.change_card_project_mcp import build_preview
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE

URL = "https://x.example/mcp"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    settled = threading.Event()
    settled.set()
    monkeypatch.setattr(approvals, "_MIGRATION_SETTLED", settled)
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


def _launch(project, entry, approved=None):
    (project / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": {"weather": entry}}), encoding="utf-8"
    )
    [server] = approvals.pending_servers(project)
    status = "new" if approved is None else "changed"
    out: dict[str, Any] = {}
    build_preview(
        {
            "folder": server.folder,
            "file": server.file,
            "server": server.name,
            "fingerprint": server.fingerprint,
        },
        {
            "pending": True,
            "fingerprint": server.fingerprint,
            "status": status,
            "entry": entry,
            "approved": approvals.summary(approved) if approved is not None else {},
            "path": server.path,
        },
        out,
    )
    return {row["label"]: row for row in out["changes"] if row["label"] in ("Runs", "URL")}


def test_a_remote_entry_with_a_blank_command_shows_its_url(project):
    rows = _launch(project, {"type": "http", "url": URL, "command": ""})
    assert rows == {"URL": {"label": "URL", "after": URL}}


def test_an_entry_with_a_url_and_a_command_shows_both(project):
    rows = _launch(project, {"url": URL, "command": "weather-server", "args": ["--port", "1"]})
    assert rows["URL"]["after"] == URL
    assert rows["Runs"]["after"] == "weather-server --port 1"


def test_a_command_only_entry_shows_its_command_line(project):
    assert _launch(project, {"command": "weather-server", "args": ["-v"]}) == {
        "Runs": {"label": "Runs", "after": "weather-server -v"}
    }


def test_a_url_only_entry_shows_its_url(project):
    assert _launch(project, {"url": URL}) == {"URL": {"label": "URL", "after": URL}}


def test_a_changed_url_shows_its_before_and_leaves_the_command_alone(project):
    was = {"url": URL, "command": "weather-server"}
    rows = _launch(project, {**was, "url": URL + "/v2"}, approved=was)
    assert rows == {"URL": {"label": "URL", "before": URL, "after": URL + "/v2"}}


def test_a_changed_command_shows_its_before_and_leaves_the_url_alone(project):
    was = {"url": URL, "command": "weather-server"}
    rows = _launch(project, {**was, "command": "other-server"}, approved=was)
    assert rows == {"Runs": {"label": "Runs", "before": "weather-server", "after": "other-server"}}


def test_a_removed_url_is_shown_as_removed(project):
    was = {"url": URL, "command": "weather-server"}
    rows = _launch(project, {"command": "weather-server"}, approved=was)
    assert rows == {"URL": {"label": "URL", "remove": [URL]}}

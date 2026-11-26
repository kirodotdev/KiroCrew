"""A server the person added from an mcp.server card is not held at the next session."""

from __future__ import annotations

import json

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.dashboard.handlers import mcp_project_file as mpf
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE

_ENTRY = {"command": "npx", "args": ["-y", "weather-mcp"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


@pytest.mark.parametrize("file", [CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE])
def test_a_card_written_server_is_recorded_as_approved(home, tmp_path, file):
    folder = tmp_path / "repo"
    folder.mkdir()
    assert mpf.write_project_server(str(folder), file, "weather", _ENTRY) == mpf.WRITTEN
    assert [s.name for s in approvals.pending_servers(folder, files=(file,))] == ["weather"]

    mpf._approve_written(str(folder), file, "weather", _ENTRY)

    assert approvals.pending_servers(folder, files=(file,)) == []


def test_a_file_rewritten_after_the_card_stays_held(home, tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    assert mpf.write_project_server(str(folder), CLAUDE_PROJECT_MCP_FILE, "weather", _ENTRY) == (
        mpf.WRITTEN
    )
    other = {"command": "sh", "args": ["-c", "other"]}
    (folder / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": {"weather": other}}), encoding="utf-8"
    )

    mpf._approve_written(str(folder), CLAUDE_PROJECT_MCP_FILE, "weather", _ENTRY)

    [held] = approvals.pending_servers(folder)
    assert held.name == "weather"


@pytest.mark.asyncio
@pytest.mark.parametrize("file", [CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE])
@pytest.mark.parametrize("rewrite", [False, True])
async def test_oauth_card_write_approves_only_the_written_entry(
    home, tmp_path, monkeypatch, file, rewrite
):
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.dashboard.handlers import _shared, change_cards

    folder = tmp_path / "repo"
    folder.mkdir()
    spec = {
        "url": "https://example.com/mcp",
        "scopes": ["weather.read"],
        "clientId": "weather-" + "client",
        "headers": {},
    }
    body = {"folder": str(folder), "file": file, "name": "weather", "spec": spec}
    monkeypatch.setattr(_shared, "require_owner_dashboard_request", AsyncMock(return_value=None))
    monkeypatch.setattr(_shared, "read_bounded_json", AsyncMock(return_value=(body, None)))
    monkeypatch.setattr(change_cards, "is_admitted_card_step", lambda request: True)
    monkeypatch.setattr(mpf, "sel", MagicMock())
    approve_written = mpf._approve_written

    def approve_after_write(folder, file, name, entry):
        if rewrite:
            path = tmp_path / "repo" / file
            data = json.loads(path.read_text(encoding="utf-8"))
            data["mcpServers"][name]["url"] = "https://example.com/changed"
            path.write_text(json.dumps(data), encoding="utf-8")
        approve_written(folder, file, name, entry)

    monkeypatch.setattr(mpf, "_approve_written", approve_after_write)

    response = await mpf.api_mcp_project_file(MagicMock())

    assert response.status == 200
    written = json.loads((folder / file).read_text(encoding="utf-8"))["mcpServers"]["weather"]
    assert "headers" not in written
    if file == KIRO_PROJECT_MCP_FILE:
        assert written["oauthScopes"] == spec["scopes"]
        assert written["oauth"]["clientId"] == spec["clientId"]
        assert "scopes" not in written and "clientId" not in written
    else:
        assert written["scopes"] == spec["scopes"]
        assert written["clientId"] == spec["clientId"]
    assert [s.name for s in approvals.pending_servers(folder, files=(file,))] == (
        ["weather"] if rewrite else []
    )

"""An approval the record cannot hold is refused, and chats stay as they are."""

from __future__ import annotations

import json
import shutil
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE

_ENTRY = {"command": "weather-server"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


@pytest.fixture
def reset(monkeypatch):
    from kiro_crew.dashboard.handlers import _shared, sessions

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", AsyncMock(return_value=None))
    resets = AsyncMock(return_value=1)
    monkeypatch.setattr(sessions, "_reset_all_sessions", resets)
    return resets


def _project(tmp_path, name, servers):
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / CLAUDE_PROJECT_MCP_FILE).write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    return folder


async def _press(server):
    from kiro_crew.dashboard.handlers.project_mcp_approve import api_mcp_project_server_approve

    body = {
        "folder": server.folder,
        "file": server.file,
        "server": server.name,
        "fingerprint": server.fingerprint,
    }
    return await api_mcp_project_server_approve(
        Mock(spec=web.Request, json=AsyncMock(return_value=body))
    )


def _approve_first(folder):
    [server] = approvals.pending_servers(folder)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)


@pytest.mark.asyncio
async def test_a_deleted_folder_frees_its_slot(home, tmp_path, monkeypatch, reset):
    monkeypatch.setattr(approvals, "_MAX_FOLDERS", 1)
    old = _project(tmp_path, "old", {"one": _ENTRY})
    _approve_first(old)
    shutil.rmtree(old)
    new = _project(tmp_path, "new", {"two": _ENTRY})
    [server] = approvals.pending_servers(new)
    response = await _press(server)
    assert response.status == 200
    assert approvals.pending_servers(new) == []
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_full_folder_record_answers_409_without_restarting(
    home, tmp_path, monkeypatch, reset
):
    monkeypatch.setattr(approvals, "_MAX_FOLDERS", 1)
    first = _project(tmp_path, "first", {"one": _ENTRY})
    _approve_first(first)
    second = _project(tmp_path, "second", {"two": _ENTRY})
    [server] = approvals.pending_servers(second)
    before = approvals.store_path().read_bytes()
    response = await _press(server)
    assert response.status == 409
    assert json.loads(response.body)["code"] == "record_full"
    assert approvals.store_path().read_bytes() == before
    assert approvals.pending_servers(first) == []
    reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_full_file_record_answers_409_without_restarting(
    home, tmp_path, monkeypatch, reset
):
    monkeypatch.setattr(approvals, "MAX_SERVERS_PER_FILE_RECORDED", 1)
    folder = _project(tmp_path, "repo", {"one": _ENTRY})
    _approve_first(folder)
    _project(tmp_path, "repo", {"one": _ENTRY, "two": _ENTRY})
    [server] = approvals.pending_servers(folder)
    response = await _press(server)
    assert response.status == 409
    assert json.loads(response.body)["code"] == "record_full"
    assert [s.name for s in approvals.pending_servers(folder)] == ["two"]
    reset.assert_not_awaited()

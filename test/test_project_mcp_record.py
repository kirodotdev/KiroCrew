"""Boot migration, bounded approval records and matching card/route parameters."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE

_ENTRY = {"command": "weather-server"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    crew = tmp_path / "crew"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


def _project(tmp_path, name, servers, file=CLAUDE_PROJECT_MCP_FILE):
    folder = tmp_path / name
    path = folder / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return folder


def _recent(home, *folders):
    (home / "recent_projects.json").write_text(
        json.dumps([str(folder) for folder in folders]), encoding="utf-8"
    )


def _approve(server):
    return approvals.approve(server.folder, server.file, server.name, server.fingerprint)


def _migrate():
    scan = approvals.begin_boot_migration()
    if scan is not None:
        scan()


@pytest.mark.parametrize("file", [CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE])
def test_checks_do_not_migrate_recent_folders(home, tmp_path, file):
    folder = _project(tmp_path, "recent", {"weather": _ENTRY}, file)
    _recent(home, folder)
    assert [s.name for s in approvals.pending_servers(folder)] == ["weather"]
    assert not approvals.store_path().exists()


def test_approval_records_only_the_selected_server_without_migration(home, tmp_path):
    folder = _project(tmp_path, "recent", {"weather": _ENTRY, "other": _ENTRY})
    _recent(home, folder)
    weather = next(s for s in approvals.folder_servers(folder) if s.name == "weather")
    _approve(weather)
    assert [s.name for s in approvals.pending_servers(folder)] == ["other"]


@pytest.mark.asyncio
async def test_boot_keeps_known_servers_and_new_folders_get_cards(home, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import change_cards_propose
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards
    from kiro_crew.providers.acp import AcpProvider

    known = _project(tmp_path, "known", {"weather": _ENTRY})
    _recent(home, known)
    _migrate()
    record = approvals.store_path().read_bytes()
    assert approvals.pending_servers(known) == []

    propose = AsyncMock(return_value={})
    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    slot = SimpleNamespace(key="chat-1", agent="a", project=str(known))
    client = AcpProvider(work_dir=str(known), acp_backend="claude")
    await offer_project_mcp_cards(object(), slot, client, "sk")
    propose.assert_not_awaited()

    later = _project(tmp_path, "later", {"new": _ENTRY})
    _recent(home, known, later)
    _migrate()
    assert approvals.store_path().read_bytes() == record
    assert [s.name for s in approvals.pending_servers(later)] == ["new"]
    slot.project = str(later)
    client = AcpProvider(work_dir=str(later), acp_backend="claude")
    await offer_project_mcp_cards(object(), slot, client, "sk")
    propose.assert_awaited_once()
    assert propose.call_args.kwargs["params"]["server"] == "new"


def test_failed_boot_write_leaves_known_servers_pending(home, tmp_path, monkeypatch, caplog):
    folder = _project(tmp_path, "known", {"weather": _ENTRY})
    _recent(home, folder)
    write = Mock(side_effect=OSError("read only"))
    monkeypatch.setattr(approvals, "_write_store", write)
    _migrate()
    assert not approvals.store_path().exists()
    assert [s.name for s in approvals.pending_servers(folder)] == ["weather"]
    write.assert_called_once()
    assert "could not write the first record" in caplog.text


@pytest.mark.parametrize("contents", ["{bad json", '{"version": 2}', "[]"])
def test_boot_leaves_an_existing_unreadable_record_alone(home, tmp_path, contents):
    folder = _project(tmp_path, "known", {"weather": _ENTRY})
    _recent(home, folder)
    path = approvals.store_path()
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    _migrate()
    assert path.read_text(encoding="utf-8") == contents
    assert [s.name for s in approvals.pending_servers(folder)] == ["weather"]


@pytest.mark.parametrize(
    "field,bound", [("folder", "MAX_FOLDER_CHARS"), ("name", "MAX_SERVER_NAME_CHARS")]
)
def test_record_bounds_text_fields(home, tmp_path, field, bound):
    folder = _project(tmp_path, "repo", {"weather": _ENTRY})
    [server] = approvals.folder_servers(folder)
    doc = {"version": 1, "folders": {}}
    approvals._record(doc, replace(server, **{field: "x" * (getattr(approvals, bound) + 1)}))
    assert doc["folders"] == {}
    approvals._record(doc, replace(server, **{field: "x" * getattr(approvals, bound)}))
    assert len(doc["folders"]) == 1


def test_record_bounds_servers_per_file_and_prunes_removed_names(home, tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "MAX_SERVERS_PER_FILE_RECORDED", 2)
    folder = _project(tmp_path, "repo", {"one": _ENTRY, "two": _ENTRY, "three": _ENTRY})
    _recent(home, folder)
    _migrate()
    [third] = approvals.pending_servers(folder)
    assert third.name == "three"
    with pytest.raises(approvals.RecordFull):
        _approve(third)
    assert [s.name for s in approvals.pending_servers(folder)] == ["three"]
    _project(tmp_path, "repo", {"one": _ENTRY, "three": _ENTRY})
    _approve(third)
    assert approvals.pending_servers(folder) == []
    doc = json.loads(approvals.store_path().read_text(encoding="utf-8"))
    assert set(doc["folders"][third.folder][third.file]) == {"one", "three"}


def test_record_folder_limit_preserves_existing_approvals(home, tmp_path, monkeypatch):
    monkeypatch.setattr(approvals, "_MAX_FOLDERS", 1)
    first = _project(tmp_path, "first", {"one": _ENTRY})
    second = _project(tmp_path, "second", {"two": _ENTRY})
    _recent(home, first, second)
    _migrate()
    assert approvals.pending_servers(first) == []
    [held] = approvals.pending_servers(second)
    with pytest.raises(approvals.RecordFull):
        _approve(held)
    assert approvals.pending_servers(first) == []
    assert [s.name for s in approvals.pending_servers(second)] == ["two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,bound", [("folder", "MAX_FOLDER_CHARS"), ("server", "MAX_SERVER_NAME_CHARS")]
)
async def test_card_and_route_share_text_bounds(home, tmp_path, monkeypatch, field, bound):
    from aiohttp import web

    from kiro_crew.change_card_catalog import CardCatalogError
    from kiro_crew.change_card_project_mcp import clean_params
    from kiro_crew.dashboard.handlers import _shared, project_mcp_approve, sessions

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", AsyncMock(return_value=None))
    reset = AsyncMock(return_value=0)
    monkeypatch.setattr(sessions, "_reset_all_sessions", reset)
    approve = Mock(return_value=SimpleNamespace(name="s", folder=str(tmp_path), file=".mcp.json"))
    monkeypatch.setattr(project_mcp_approve, "approve", approve)
    params = {"folder": str(tmp_path), "file": ".mcp.json", "server": "s", "fingerprint": "a" * 64}
    size = getattr(approvals, bound)
    prefix = str(tmp_path) if field == "folder" else ""
    params[field] = prefix + "x" * (size - len(prefix))
    assert clean_params(params) == params
    response = await project_mcp_approve.api_mcp_project_server_approve(
        Mock(spec=web.Request, json=AsyncMock(return_value=params))
    )
    assert response.status == 200
    params[field] += "x"
    with pytest.raises(CardCatalogError):
        clean_params(params)
    response = await project_mcp_approve.api_mcp_project_server_approve(
        Mock(spec=web.Request, json=AsyncMock(return_value=params))
    )
    assert response.status == 400
    approve.assert_called_once()
    reset.assert_awaited_once()

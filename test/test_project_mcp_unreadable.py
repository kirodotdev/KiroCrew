"""A project MCP file Crew cannot read is held whole, and the boot record is written off boot.

``project_mcp_approvals.check_folder`` reports such a file; a Claude Code session then
runs with ``strictMcpConfig`` and a kiro-cli or KAS chat shows one notice. Gateway boot
reads the folder list and leaves the scan to a background task, which a check waits
for briefly and otherwise holds.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import project_mcp_approvals as approvals
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE

_ENTRY = {"command": "weather-server"}


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    yield crew
    approvals._MIGRATION_SETTLED.set()


def _project(tmp_path: Path, name: str, file: str = CLAUDE_PROJECT_MCP_FILE) -> Path:
    folder = tmp_path / name
    path = folder / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"weather": _ENTRY}}), encoding="utf-8")
    return folder


def _recent(home: Path, *folders: Path) -> None:
    (home / "recent_projects.json").write_text(
        json.dumps([str(f) for f in folders]), encoding="utf-8"
    )


def _cannot_read(monkeypatch) -> None:
    def _refuse(path):
        raise ValueError("MCP settings could not be safely read")

    monkeypatch.setattr(holds, "read_settings_file", _refuse)
    monkeypatch.setattr(holds, "read_server_entries", lambda folder, path, refusal: None)


def _names(servers) -> list[str]:
    return sorted(s.name for s in servers)


# -- boot record -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_continues_while_the_scan_runs(home, tmp_path, monkeypatch):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    folder = _project(tmp_path, "known")
    _recent(home, folder)
    release = threading.Event()
    real_migrate = approvals._migrate

    def _slow_migrate(path, folders):
        release.wait(10)
        real_migrate(path, folders)

    class ServicesCheckpoint(Exception):
        pass

    seen: dict[str, bool] = {}

    async def checkpoint():
        seen["settled"] = approvals._MIGRATION_SETTLED.is_set()
        raise ServicesCheckpoint

    monkeypatch.setattr(approvals, "_migrate", _slow_migrate)
    gateway = SimpleNamespace(
        _slack_enabled=False, _warn_if_kiro_cli_outdated=checkpoint, _background_tasks=set()
    )
    with pytest.raises(ServicesCheckpoint):
        await GatewayOrchestrator._init_services(gateway)
    release.set()
    await asyncio.gather(*gateway._background_tasks)
    assert seen == {"settled": False}
    assert approvals.pending_servers(folder) == []


def test_a_check_waits_for_the_scan_then_known_servers_are_not_held(home, tmp_path, monkeypatch):
    folder = _project(tmp_path, "known")
    _recent(home, folder)
    scan = approvals.begin_boot_migration()
    assert scan is not None

    def _wait(timeout):
        scan()
        return True

    monkeypatch.setattr(approvals._MIGRATION_SETTLED, "wait", _wait)
    assert approvals.pending_servers(folder) == []


def test_a_check_during_the_scan_holds_and_does_not_migrate(home, tmp_path, monkeypatch):
    folder = _project(tmp_path, "known")
    _recent(home, folder)
    scan = approvals.begin_boot_migration()
    assert scan is not None
    with monkeypatch.context() as patch:
        patch.setattr(approvals._MIGRATION_SETTLED, "wait", lambda timeout: False)
        assert _names(approvals.pending_servers(folder)) == ["weather"]
    assert not approvals.store_path().exists()
    scan()
    assert approvals.pending_servers(folder) == []


def test_a_project_picked_after_boot_is_not_recorded(home, tmp_path):
    known = _project(tmp_path, "known")
    later = _project(tmp_path, "later")
    _recent(home, known)
    scan = approvals.begin_boot_migration()
    _recent(home, known, later)
    assert scan is not None
    scan()
    assert approvals.pending_servers(known) == []
    assert _names(approvals.pending_servers(later)) == ["weather"]


def test_a_failed_scan_holds_and_does_not_raise(home, tmp_path, monkeypatch):
    folder = _project(tmp_path, "known")
    _recent(home, folder)

    def _boom(*_a, **_k):
        raise OSError("read only")

    monkeypatch.setattr(approvals, "_write_store", _boom)
    scan = approvals.begin_boot_migration()
    assert scan is not None
    scan()
    assert approvals._MIGRATION_SETTLED.is_set()
    assert _names(approvals.pending_servers(folder)) == ["weather"]


def test_an_approval_during_the_scan_is_kept(home, tmp_path):
    known = _project(tmp_path, "known")
    other = _project(tmp_path, "other")
    _recent(home, known)
    scan = approvals.begin_boot_migration()
    assert scan is not None
    [server] = approvals.folder_servers(other)
    approvals.approve(server.folder, server.file, server.name, server.fingerprint)
    scan()
    assert approvals.pending_servers(known) == []
    assert approvals.pending_servers(other) == []


# -- a file Crew cannot read ---------------------------------------------------------


@pytest.mark.parametrize("file", [CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE])
def test_a_file_crew_cannot_read_is_held_whole(home, tmp_path, monkeypatch, file):
    folder = _project(tmp_path, "repo", file)
    _cannot_read(monkeypatch)
    found = approvals.check_folder(folder)
    assert found.pending == []
    assert found.unreadable == (str(folder.resolve() / file),)


def test_a_failed_check_holds_the_files_that_exist(home, tmp_path, monkeypatch):
    folder = _project(tmp_path, "repo")

    def _boom(*_a, **_k):
        raise RuntimeError("check failed")

    monkeypatch.setattr(approvals, "_scan", _boom)
    found = approvals.check_folder(folder)
    assert _names(found.pending) == ["weather"]
    assert found.unreadable == ()


def test_a_failed_hold_holds_the_hosts_file(home, tmp_path, monkeypatch):
    folder = _project(tmp_path, "repo")

    def _boom(*_a, **_k):
        raise RuntimeError("check failed")

    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    monkeypatch.setattr(holds, "check_folder", _boom)
    hold = holds.project_hold(ACP_BACKEND_CLAUDE, "a", folder)
    assert _names(hold.pending) == ["weather"]
    assert hold.unreadable == ()


@pytest.mark.asyncio
async def test_claude_runs_without_project_servers_when_the_file_cannot_be_read(
    home, tmp_path, monkeypatch
):
    from kiro_crew.acp.client import ClaudeAcpClient
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    folder = _project(tmp_path, "repo")
    client = ClaudeAcpClient(work_dir=folder, acp_backend=ACP_BACKEND_CLAUDE)
    await client._prepare_claude_held_asks()
    options = client._claude_session_meta()["claudeCode"]["options"]
    assert "strictMcpConfig" not in options

    _cannot_read(monkeypatch)
    await client._prepare_claude_held_asks()
    options = client._claude_session_meta()["claudeCode"]["options"]
    assert options["strictMcpConfig"] is True
    assert client._claude_session_needs_options()


def _spec(monkeypatch, spec: dict[str, Any]) -> None:
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda agent, work_dir=None: spec)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
async def test_kiro_hosts_show_one_notice_naming_the_file(home, tmp_path, monkeypatch, backend):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import change_cards_propose
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards

    folder = _project(tmp_path, "repo", KIRO_PROJECT_MCP_FILE)
    _spec(monkeypatch, {"name": "a", "includeMcpJson": True})
    _cannot_read(monkeypatch)
    rows: list[tuple[str, str]] = []
    monkeypatch.setattr(
        dashboard_state,
        "append_and_surface",
        lambda state, slot, role, content, cls="", **_k: rows.append((role, content)),
    )
    proposed: list[Any] = []

    async def _propose(*_a, **kw):
        proposed.append(kw)
        return {}

    monkeypatch.setattr(change_cards_propose, "propose_in_slot", _propose)
    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _client: SimpleNamespace(backend=backend)
    )
    slot = SimpleNamespace(key="chat-1", agent="a", project=str(folder))
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=str(folder)), "sk", "a")
    [(role, text)] = rows
    assert role == "notice"
    assert str(folder.resolve() / KIRO_PROJECT_MCP_FILE) in text
    assert "Fix or delete that file, then start a new chat." in text
    assert proposed == []


@pytest.mark.asyncio
async def test_claude_shows_no_notice(home, tmp_path, monkeypatch):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards

    folder = _project(tmp_path, "repo")
    _cannot_read(monkeypatch)
    rows: list[Any] = []
    monkeypatch.setattr(dashboard_state, "append_and_surface", lambda *a, **k: rows.append(a))
    from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _client: SimpleNamespace(backend=ACP_BACKEND_CLAUDE)
    )
    slot = SimpleNamespace(key="chat-1", agent="a", project=str(folder))
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=str(folder)), "sk")
    assert rows == []

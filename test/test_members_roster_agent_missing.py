# SPDX-License-Identifier: Apache-2.0
"""The roster marks a crewmate whose agent file is gone, and never drops it.

A package uninstall or a deleted file leaves the crewmate's row in the config,
bound to an agent nothing declares. ``GET /api/members`` ships
``agent_missing: true`` on that row so the page can say why it stopped
answering and offer Remove. The row stays, because it holds the crewmate's
memory, chats and schedules, and a row is marked only on evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig


@pytest.fixture
def agents_dir(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    """An agents directory of this test's own, behind a snapshot no other test fed."""
    user = tmp_path / "kiro-agents"
    user.mkdir()
    mates = tmp_path / "kiro-mates"
    mates.mkdir()
    _floor_monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user)
    _floor_monkeypatch.setattr(loader, "mate_agents_dir", lambda: mates)
    _floor_monkeypatch.setattr(loader, "_edition_agent_names", lambda: frozenset())
    for name, value in (
        ("_MATERIALIZED_AGENTS", frozenset()),
        ("_MATERIALIZED_MATE_ONLY", frozenset()),
        ("_MATERIALIZED_STEMS", {}),
        ("_MATERIALIZED_SEEN", {}),
        ("_MATERIALIZED_MEMO", None),
        ("_MATERIALIZED_COMPLETE", False),
        ("_MATERIALIZED_AGENTS_READY", False),
    ):
        _floor_monkeypatch.setattr(loader, name, value)
    return user


def members_missing_agent(cfg: KiroCrewConfig) -> frozenset[str]:
    """The check under test, imported at call time.

    A module-level import would turn a build without the check into a collection
    error for the whole file, the roster test included, where a failed assertion
    names what is missing.
    """
    from kiro_crew.dashboard.members_roster_agent import members_missing_agent as check

    return check(cfg)


def _declare(agents_dir: Path, *names: str) -> None:
    for name in names:
        (agents_dir / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")


def _config(tmp_path: Path, **bindings: str) -> KiroCrewConfig:
    """A config whose crewmates bind *bindings*, in a workspace that declares no agent."""
    cfg = KiroCrewConfig()
    cfg.workspaces = {"default": WorkspaceConfig(dir=str(tmp_path / "workspace"))}
    cfg.agents = {name: KiroCrewAgentConfig(kiro_agent=agent) for name, agent in bindings.items()}
    return cfg


def test_only_the_crewmate_whose_agent_file_is_gone_is_named(tmp_path, agents_dir):
    _declare(agents_dir, "kirocrew", "captain", "pilot")
    cfg = _config(
        tmp_path,
        default="kirocrew",
        kept="captain",
        gone="pilot",
        unbound="",
        builtin="kiro_default",
    )
    assert members_missing_agent(cfg) == frozenset()

    (agents_dir / "pilot.json").unlink()

    # No refresh here: the check rescans by itself, so a file removed outside
    # Kiro Crew is seen on the next roster read.
    assert members_missing_agent(cfg) == frozenset({"gone"})


def test_an_agent_added_since_the_last_scan_is_not_named(tmp_path, agents_dir):
    _declare(agents_dir, "kirocrew")
    cfg = _config(tmp_path, late="pilot")
    assert members_missing_agent(cfg) == frozenset({"late"})

    _declare(agents_dir, "pilot")

    assert members_missing_agent(cfg) == frozenset()


def test_an_empty_or_partial_snapshot_names_nobody(tmp_path, agents_dir):
    cfg = _config(tmp_path, gone="pilot")
    # Empty: a directory that declares nothing proves nothing.
    assert members_missing_agent(cfg) == frozenset()

    _declare(agents_dir, "kirocrew")
    (agents_dir / "broken.json").write_text("{not json", encoding="utf-8")

    # Partial: the scan skipped a file it could not read, which may be the agent.
    assert members_missing_agent(cfg) == frozenset()
    assert loader.materialized_snapshot_complete() is False


@pytest.mark.parametrize("failed_listing", [False, True], ids=["unparseable", "listing-failed"])
def test_an_existing_crewmate_file_is_not_named(
    tmp_path, agents_dir, _floor_monkeypatch, failed_listing
):
    from kiro_crew.config import loader_snapshots

    _declare(agents_dir, "kirocrew")
    mates = loader.mate_agents_dir()
    own_file = mates / "pilot.json"
    own_file.write_text("{not json", encoding="utf-8")
    cfg = _config(tmp_path, pilot="pilot")
    if failed_listing:
        scan = loader_snapshots.iter_agent_spec_files_strict

        def _list(directory):
            if directory == mates:
                raise OSError("crewmate directory unreadable")
            return scan(directory)

        _floor_monkeypatch.setattr(loader_snapshots, "iter_agent_spec_files_strict", _list)

    # A cold snapshot cannot retain the name of this skipped crewmate file.
    assert members_missing_agent(cfg) == frozenset()
    assert loader.materialized_snapshot_complete() is True

    own_file.unlink()

    assert members_missing_agent(cfg) == frozenset({"pilot"})


def test_a_failed_crewmate_stat_names_nobody(tmp_path, agents_dir, _floor_monkeypatch):
    _declare(agents_dir, "kirocrew")
    cfg = _config(tmp_path, gone="navigator", uncertain="pilot")
    own_file = loader.mate_agents_dir() / "pilot.json"
    stat = Path.stat

    def _stat(path, *args, **kwargs):
        if path == own_file:
            raise PermissionError("crewmate file inaccessible")
        return stat(path, *args, **kwargs)

    _floor_monkeypatch.setattr(Path, "stat", _stat)

    assert members_missing_agent(cfg) == frozenset()


def test_an_agent_the_workspace_or_the_edition_supplies_is_not_named(
    tmp_path, agents_dir, _floor_monkeypatch
):
    _declare(agents_dir, "kirocrew")
    cfg = _config(tmp_path, local="pilot", shipped="navigator")
    assert members_missing_agent(cfg) == frozenset({"local", "shipped"})

    project_agents = tmp_path / "workspace" / ".kiro" / "agents"
    project_agents.mkdir(parents=True)
    _declare(project_agents, "pilot")
    _floor_monkeypatch.setattr(loader, "_edition_agent_names", lambda: frozenset({"navigator"}))

    assert members_missing_agent(cfg) == frozenset()


def test_a_failed_check_names_nobody(tmp_path, agents_dir, _floor_monkeypatch):
    _declare(agents_dir, "kirocrew")
    assert members_missing_agent(_config(tmp_path, gone="pilot")) == frozenset({"gone"})

    def _boom() -> bool:
        raise OSError("agents directory unreadable")

    _floor_monkeypatch.setattr(loader, "refresh_materialized_agents", _boom)

    assert members_missing_agent(_config(tmp_path, gone="pilot")) == frozenset()


@pytest.fixture
def _owner_caller(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.mark.asyncio
async def test_the_roster_marks_the_row_and_keeps_it(tmp_path, agents_dir, _owner_caller):
    from kiro_crew.dashboard.handlers.members import api_members

    state = _make_state(tmp_path)
    _declare(agents_dir, "kirocrew", "captain", "pilot")
    cfg = KiroCrewConfig.load()
    cfg.agents["kept"] = KiroCrewAgentConfig(kiro_agent="captain", source="package")
    cfg.agents["gone"] = KiroCrewAgentConfig(kiro_agent="pilot", source="package")
    cfg.save()
    (agents_dir / "pilot.json").unlink()

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "dashboard")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/members")).json()
    rows = {row["name"]: row for row in body["members"]}

    assert rows["gone"].get("agent_missing") is True
    # Omitted, not false, on every row whose agent is still there.
    assert [name for name, row in rows.items() if "agent_missing" in row] == ["gone"]
    # Reading the roster removed nothing.
    assert KiroCrewConfig.load().agents["gone"].kiro_agent == "pilot"

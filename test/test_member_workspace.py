"""A new Crew Member gets a workspace of its own, published with its record."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers import agents as handlers
from kiro_crew.member_workspace import (
    AUTO_WORKSPACE,
    MemberWorkspaceUnavailable,
    member_workspace_name,
    provision_member_workspace,
    publish_member_workspace,
    retire_unpublished_workspace,
    workspace_dir_name,
)
from kiro_crew.memory_stores import (
    UnknownMemoryStore,
    persist_member_config,
    provision_member_memory,
)


def _config(**agents: KiroCrewAgentConfig) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents.update(agents)
    return cfg


class TestNaming:
    def test_named_after_the_member(self):
        assert member_workspace_name("Review Agent", taken=set()) == "member-review-agent"

    def test_a_taken_name_takes_a_numeric_suffix(self):
        taken = {"member-atlas", "member-atlas-2"}
        assert member_workspace_name("Atlas", taken=taken) == "member-atlas-3"

    def test_the_directory_follows_the_created_workspace_convention(self):
        assert workspace_dir_name("member-atlas") == "workspace-member-atlas"


class TestProvision:
    def test_auto_binds_a_fresh_workspace_in_the_snapshot(self):
        cfg = _config(atlas=KiroCrewAgentConfig(workspace=AUTO_WORKSPACE))
        assert provision_member_workspace(cfg, "atlas") == "member-atlas"
        assert cfg.agents["atlas"].workspace == "member-atlas"
        assert cfg.workspaces["member-atlas"] == WorkspaceConfig(dir="workspace-member-atlas")
        # Nothing on disk yet: the directory is made inside the locked publish.
        assert not (config_dir() / "workspace-member-atlas").exists()

    def test_a_declared_workspace_is_an_explicit_reuse(self):
        cfg = _config(atlas=KiroCrewAgentConfig(workspace="shared"))
        cfg.workspaces["shared"] = WorkspaceConfig(dir="workspace-shared")
        assert provision_member_workspace(cfg, "atlas") == ""
        assert cfg.agents["atlas"].workspace == "shared"

    def test_default_is_always_a_legal_reuse(self):
        cfg = _config(atlas=KiroCrewAgentConfig(workspace="default"))
        assert provision_member_workspace(cfg, "atlas") == ""
        assert cfg.agents["atlas"].workspace == "default"

    def test_an_undeclared_name_is_stored_as_given(self):
        # The binding every create path always stored; it resolves to the base
        # directory at use (workspace_dir_for), as it always did.
        cfg = _config(atlas=KiroCrewAgentConfig(workspace="nowhere"))
        assert provision_member_workspace(cfg, "atlas") == ""
        assert cfg.agents["atlas"].workspace == "nowhere"
        assert "nowhere" not in cfg.workspaces

    def test_a_directory_another_entry_uses_is_skipped(self):
        cfg = _config(atlas=KiroCrewAgentConfig(workspace=AUTO_WORKSPACE))
        cfg.workspaces["old"] = WorkspaceConfig(dir="workspace-member-atlas")
        assert provision_member_workspace(cfg, "atlas") == "member-atlas-2"
        assert cfg.workspaces["member-atlas-2"].dir == "workspace-member-atlas-2"

    def test_a_stale_entry_that_cannot_resolve_does_not_block_a_create(self, monkeypatch):
        # `~gone/project` after that account is removed: expanduser raises
        # RuntimeError. The entry cannot name our fresh directory, so it is skipped.
        def expand(path: str) -> str:
            if path.startswith("~gone"):
                raise RuntimeError("Could not determine home directory.")
            return path

        monkeypatch.setattr("os.path.expanduser", expand)
        cfg = _config(atlas=KiroCrewAgentConfig(workspace=AUTO_WORKSPACE))
        cfg.workspaces["old"] = WorkspaceConfig(dir="~gone/project")
        assert provision_member_workspace(cfg, "atlas") == "member-atlas"
        doc = {"workspaces": {"old": {"dir": "~gone/project"}}}
        publish_member_workspace(doc, "member-atlas", cfg.workspaces["member-atlas"])
        assert "member-atlas" in doc["workspaces"]

    def test_retire_restores_the_snapshot(self):
        cfg = _config(atlas=KiroCrewAgentConfig(workspace=AUTO_WORKSPACE))
        name = provision_member_workspace(cfg, "atlas")
        retire_unpublished_workspace(cfg, "atlas", name, previous_workspace=AUTO_WORKSPACE)
        assert cfg.agents["atlas"].workspace == AUTO_WORKSPACE
        assert name not in cfg.workspaces

    def test_retire_of_nothing_is_a_no_op(self):
        cfg = _config(atlas=KiroCrewAgentConfig(workspace="default"))
        retire_unpublished_workspace(cfg, "atlas", "", previous_workspace="default")
        assert cfg.agents["atlas"].workspace == "default"


class TestPublish:
    def test_writes_the_entry_and_makes_the_directory_owner_only(self):
        doc: dict = {"agents": {}}
        entry = WorkspaceConfig(dir="workspace-member-atlas")
        publish_member_workspace(doc, "member-atlas", entry)
        assert doc["workspaces"] == {"member-atlas": {"dir": "workspace-member-atlas"}}
        made = config_dir() / "workspace-member-atlas"
        assert made.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(made.stat().st_mode) == 0o700

    def test_a_name_taken_under_the_lock_is_refused(self):
        doc = {"workspaces": {"member-atlas": {"dir": "elsewhere"}}}
        with pytest.raises(MemberWorkspaceUnavailable) as exc_info:
            publish_member_workspace(doc, "member-atlas", WorkspaceConfig(dir="workspace-x"))
        assert exc_info.value.code == "workspace_exists"
        assert not (config_dir() / "workspace-x").exists()

    def test_a_directory_taken_under_the_lock_is_refused(self):
        doc = {"workspaces": {"other": {"dir": "workspace-member-atlas"}}}
        with pytest.raises(MemberWorkspaceUnavailable) as exc_info:
            publish_member_workspace(
                doc, "member-atlas", WorkspaceConfig(dir="workspace-member-atlas")
            )
        assert exc_info.value.code == "workspace_dir_in_use"

    def test_a_degraded_section_is_replaced(self):
        doc: dict = {"workspaces": []}
        publish_member_workspace(doc, "member-atlas", WorkspaceConfig(dir="workspace-member-atlas"))
        assert doc["workspaces"] == {"member-atlas": {"dir": "workspace-member-atlas"}}


class TestPersistTogether:
    def test_the_record_and_its_workspace_land_in_one_write(self):
        cfg = KiroCrewConfig.load()
        cfg.save()
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace=AUTO_WORKSPACE)
        provision_member_memory(cfg, "atlas")
        name = provision_member_workspace(cfg, "atlas")
        persist_member_config(cfg, "atlas", create=True, new_workspace=name)
        loaded = KiroCrewConfig.load()
        assert loaded.agents["atlas"].workspace == "member-atlas"
        assert loaded.workspaces["member-atlas"].dir == "workspace-member-atlas"
        assert (config_dir() / "workspace-member-atlas").is_dir()

    def test_a_workspace_that_is_not_the_binding_is_refused(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, "atlas")
        with pytest.raises(UnknownMemoryStore):
            persist_member_config(cfg, "atlas", create=True, new_workspace="member-atlas")

    def test_an_update_never_allocates(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["atlas"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace=AUTO_WORKSPACE)
        provision_member_memory(cfg, "atlas")
        name = provision_member_workspace(cfg, "atlas")
        persist_member_config(cfg, "atlas", create=True, new_workspace=name)
        with pytest.raises(UnknownMemoryStore):
            persist_member_config(
                cfg, "atlas", expected_store=cfg.agents["atlas"].memory_store, new_workspace=name
            )


@pytest.fixture
def owner_gateway(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request", lambda _: True
    )
    monkeypatch.setattr(handlers, "list_agents", lambda: [])
    cfg = KiroCrewConfig.load()
    cfg.save()
    return cfg


async def _create(monkeypatch, body: dict) -> web.Response:
    request = make_mocked_request("POST", "/api/agents", app=web.Application())
    monkeypatch.setattr(request, "json", AsyncMock(return_value=body))
    return await handlers.api_kirocrew_agents_create(request)


@pytest.mark.asyncio
async def test_dashboard_create_gives_the_member_its_own_workspace(owner_gateway, monkeypatch):
    response = await _create(monkeypatch, {"name": "atlas", "kiro_agent": "kirocrew"})
    assert response.status == 200, response.text
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.agents["atlas"].workspace == "member-atlas"
    assert loaded.workspaces["member-atlas"].dir == "workspace-member-atlas"
    assert (config_dir() / "workspace-member-atlas").is_dir()


@pytest.mark.asyncio
async def test_two_members_get_two_workspaces(owner_gateway, monkeypatch):
    first = await _create(monkeypatch, {"name": "atlas", "kiro_agent": "kirocrew"})
    second = await _create(monkeypatch, {"name": "beacon", "kiro_agent": "kirocrew"})
    assert first.status == 200 and second.status == 200
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.agents["atlas"].workspace != loaded.agents["beacon"].workspace


@pytest.mark.asyncio
async def test_dashboard_create_can_share_a_declared_workspace(owner_gateway, monkeypatch):
    owner_gateway.workspaces["shared"] = WorkspaceConfig(dir="workspace-shared")
    owner_gateway.save()
    response = await _create(
        monkeypatch, {"name": "atlas", "kiro_agent": "kirocrew", "workspace": "shared"}
    )
    assert response.status == 200, response.text
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert loaded.agents["atlas"].workspace == "shared"
    assert set(loaded.workspaces) == {"shared"}
    assert not (config_dir() / "workspace-member-atlas").exists()


@pytest.mark.asyncio
async def test_a_failed_create_leaves_no_workspace_entry(owner_gateway, monkeypatch):
    # Publication refused after both allocations: the retire steps leave the
    # roster, the store table and the workspace table as they were.
    def refuse(cfg, name, **kwargs):
        raise OSError("publication refused")

    monkeypatch.setattr(handlers, "persist_member_config", refuse)
    response = await _create(monkeypatch, {"name": "atlas", "kiro_agent": "kirocrew"})
    assert response.status == 409
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    assert "atlas" not in loaded.agents
    assert "member-atlas" not in loaded.workspaces
    assert all(not store.owner_member for store in loaded.memory_stores.values())
    # Nothing reached the locked publish, so no directory was made.
    assert not (config_dir() / "workspace-member-atlas").exists()


@pytest.mark.asyncio
async def test_dashboard_create_refuses_a_non_string_workspace(owner_gateway, monkeypatch):
    response = await _create(
        monkeypatch, {"name": "atlas", "kiro_agent": "kirocrew", "workspace": 7}
    )
    assert response.status == 400
    assert json.loads(response.text)["code"] == "invalid_workspace"

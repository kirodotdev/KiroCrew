"""Execution discovery never enrolls, prunes or allocates a member."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.agent_discovery import AgentInfo
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.dashboard.handlers import agent_catalog


def _template(name: str, **kwargs) -> AgentInfo:
    return AgentInfo(
        name=name, filename=f"{name}.json", description="Test helper", model="", **kwargs
    )


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    config = KiroCrewConfig()
    config.agents = {
        "reviewer": KiroCrewAgentConfig(kiro_agent="reviewer", memory_store="retained-store"),
        "retained-member": KiroCrewAgentConfig(kiro_agent="missing-template", source="package"),
    }
    config.default_agent = "reviewer"
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: config)
    discovery = Mock(return_value=[_template("reviewer"), _template("test-writer")])
    monkeypatch.setattr(agent_catalog, "list_agents", discovery)
    monkeypatch.setattr(agent_catalog.agent_state, "all_fork_info", lambda: {})
    save = Mock(side_effect=AssertionError("Catalog must not persist config"))
    allocate = Mock(side_effect=AssertionError("Catalog must not allocate member memory"))
    monkeypatch.setattr(KiroCrewConfig, "save", save)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.agents.provision_member_memory", allocate)
    state = SimpleNamespace(
        owner_id="catalog-owner",
        _slots={
            "chat-project": SimpleNamespace(project=str(tmp_path), _app=""),
            "chat-empty": SimpleNamespace(project="", _app=""),
        },
    )
    caller = SimpleNamespace(user="catalog-owner", app="")

    @web.middleware
    async def identity(request, handler):
        request["user"] = caller.user
        request["app"] = caller.app
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = state
    app.router.add_get("/api/agents/catalog", agent_catalog.api_agent_catalog)
    return SimpleNamespace(
        app=app,
        config=config,
        discovery=discovery,
        save=save,
        allocate=allocate,
        state=state,
        caller=caller,
    )


@pytest.mark.asyncio
async def test_catalog_keeps_namespaces_and_never_changes_registry(catalog):
    before = dataclasses.asdict(catalog.config)
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get("/api/agents/catalog")
        assert response.status == 200
        result = await response.json()
    identities = [(row["selection_kind"], row["name"]) for row in result["agents"]]
    assert identities == [
        ("member", "reviewer"),
        ("member", "retained-member"),
        ("template", "reviewer"),
        ("template", "test-writer"),
    ]
    assert result["default_agent"] == "reviewer"
    assert result["agents"][0]["memory_store"] == "retained-store"
    assert "memory_store" not in result["agents"][-1]
    assert dataclasses.asdict(catalog.config) == before
    catalog.save.assert_not_called()
    catalog.allocate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", [None, "dashboard:ui", "chat-empty"])
async def test_catalog_never_borrows_another_slots_project(catalog, session_key):
    headers = {"X-Session-Key": session_key} if session_key else {}
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get("/api/agents/catalog", headers=headers)
        assert response.status == 200
    catalog.discovery.assert_called_once_with(project_dir=None)


@pytest.mark.asyncio
async def test_catalog_uses_requesting_project_and_preserves_scope(catalog, tmp_path):
    catalog.discovery.return_value = [_template("project-helper", scope="project")]
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get(
            "/api/agents/catalog", headers={"X-Session-Key": " dashboard:chat-project "}
        )
        assert response.status == 200
        rows = (await response.json())["agents"]
    catalog.discovery.assert_called_once_with(project_dir=tmp_path)
    assert rows[-1]["scope"] == "project"
    assert rows[-1]["selection_kind"] == "template"


@pytest.mark.asyncio
async def test_unknown_slot_is_not_a_global_fallback(catalog):
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get("/api/agents/catalog", headers={"X-Session-Key": "missing"})
        assert response.status == 404
        assert (await response.json())["code"] == "slot_not_found"
    catalog.discovery.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("slot_app", ["", "another-app"])
async def test_app_cannot_discover_a_foreign_slots_project(catalog, slot_app):
    catalog.caller.app = "caller-app"
    catalog.state._slots["chat-project"]._app = slot_app
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get(
            "/api/agents/catalog", headers={"X-Session-Key": "chat-project"}
        )
        assert response.status == 404
        assert (await response.json())["code"] == "slot_not_found"
    catalog.discovery.assert_not_called()


@pytest.mark.asyncio
async def test_app_can_discover_its_own_slots_project(catalog, tmp_path):
    catalog.caller.app = "caller-app"
    catalog.state._slots["chat-project"]._app = "caller-app"
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get(
            "/api/agents/catalog", headers={"X-Session-Key": "chat-project"}
        )
        assert response.status == 200
    catalog.discovery.assert_called_once_with(project_dir=tmp_path)


@pytest.mark.asyncio
async def test_private_and_owned_templates_are_not_standalone_choices(catalog, monkeypatch):
    catalog.discovery.return_value = [
        _template("shared"),
        _template("private", private_to="reviewer"),
        _template("hidden-by-lineage"),
        _template("kirocrew", source="kirocrew", kirocrew_owned=True),
        _template("kirocrew-conductor", kirocrew_owned=True),
    ]
    monkeypatch.setattr(
        agent_catalog.agent_state,
        "all_fork_info",
        lambda: {"hidden-by-lineage": {"private_to": "reviewer", "forked_from": "shared"}},
    )
    async with TestClient(TestServer(catalog.app)) as client:
        rows = (await (await client.get("/api/agents/catalog")).json())["agents"]
    # The runtime's own `kirocrew` file is withheld, as the sync route withholds
    # it; a shipped-but-ordinary owned spec (the conductor) stays selectable.
    assert [row["name"] for row in rows if row["selection_kind"] == "template"] == [
        "shared",
        "kirocrew-conductor",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["discovery", "lineage", "config"])
async def test_catalog_failure_is_explicit_without_partial_success(catalog, monkeypatch, failure):
    broken = Mock(side_effect=OSError("sensitive filesystem detail"))
    if failure == "discovery":
        monkeypatch.setattr(agent_catalog, "list_agents", broken)
    elif failure == "lineage":
        monkeypatch.setattr(agent_catalog.agent_state, "all_fork_info", broken)
    else:
        monkeypatch.setattr(KiroCrewConfig, "load", broken)
    async with TestClient(TestServer(catalog.app)) as client:
        response = await client.get("/api/agents/catalog")
        assert response.status == 503
        body = await response.json()
    assert body["code"] == "agent_catalog_unavailable"
    assert "sensitive filesystem detail" not in json.dumps(body)
    assert "agents" not in body


def test_template_projection_omits_runtime_and_file_details():
    row = agent_catalog._template_row(_template("reviewer"))
    assert set(row) == {"name", "selection_kind", "scope", "kiro_agent", "description", "source"}
    assert "filename" not in row


def test_lineage_stem_cannot_be_published_as_an_alias(monkeypatch):
    agent = AgentInfo(name="declared-alias", filename="private-stem.json", description="", model="")
    monkeypatch.setattr(agent_catalog, "list_agents", lambda **kwargs: [agent])
    monkeypatch.setattr(agent_catalog.agent_state, "all_fork_info", lambda: {"private-stem": {}})
    assert agent_catalog._templates(Path("project")) == []

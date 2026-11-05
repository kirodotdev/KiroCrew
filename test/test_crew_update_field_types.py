"""``PUT /api/agents/{name}`` input checks for ``workspace``, ``description``,
``triggers`` and ``source``.

The loader reads all four as strings and turns any other type into the default,
but only on the next load, so the route refuses a wrong type itself, the same
way it refuses a non-string ``display_name``. A workspace name that is not
configured is still saved, as before, but the answer carries a warning: such a
name resolves to the base workspace directory and silently shares it. An empty
or whitespace workspace keeps the current binding.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.sections import WorkspaceConfig


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


def _seed(workspace: str = "default") -> str:
    cfg = KiroCrewConfig.load()
    cfg.workspaces["team"] = WorkspaceConfig(dir="workspace-team")
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew",
        workspace=workspace,
        memory_store="default",
        description="before",
        triggers="before",
        source="kirocrew",
    )
    cfg.save()
    return "existing"


async def _put(name: str, body: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_crud_app())) as client:
        resp = await client.put(f"/api/agents/{name}", json=body)
        return resp.status, await resp.json()


def _stored(name: str) -> KiroCrewAgentConfig:
    return KiroCrewConfig.load().agents[name]


class TestWrongTypeIsRefused:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["workspace", "description", "triggers", "source"])
    @pytest.mark.parametrize("junk", [1, None, True, ["x"], {"k": "v"}])
    async def test_non_string_is_a_400_and_writes_nothing(self, field, junk):
        name = _seed()
        # Paired with a valid edit: the whole request must be a no-op.
        status, data = await _put(name, {field: junk, "display_name": "Changed"})
        assert status == 400
        assert data["code"] == f"invalid_{field}"
        stored = _stored(name)
        assert stored.display_name == ""
        assert (stored.workspace, stored.description, stored.triggers, stored.source) == (
            "default",
            "before",
            "before",
            "kirocrew",
        )

    @pytest.mark.asyncio
    async def test_strings_still_round_trip(self):
        name = _seed()
        status, _ = await _put(
            name, {"description": "Writes notes", "triggers": "notes, docs", "source": ""}
        )
        assert status == 200
        stored = _stored(name)
        assert (stored.description, stored.triggers, stored.source) == (
            "Writes notes",
            "notes, docs",
            "",
        )


class TestCreateRefusesWrongTypes:
    """``POST /api/agents`` stores the same three text fields."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["description", "triggers", "source"])
    @pytest.mark.parametrize("junk", [1, None, ["x"]])
    async def test_non_string_is_a_400_and_creates_nothing(self, field, junk):
        from kiro_crew.dashboard.handlers import api_kirocrew_agents_create

        app = web.Application()
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/agents", json={"name": "fresh-crew", "kiro_agent": "kirocrew", field: junk}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == f"invalid_{field}"
        assert "fresh-crew" not in KiroCrewConfig.load().agents


class TestWorkspaceBinding:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("unknown", ["nowhere", " team ", "TEAM"])
    async def test_unknown_name_is_saved_with_a_warning(self, unknown):
        name = _seed()
        status, data = await _put(name, {"workspace": unknown, "description": "after"})
        assert status == 200
        assert len(data["warnings"]) == 1
        assert "not configured" in data["warnings"][0]
        stored = _stored(name)
        assert stored.workspace == unknown
        assert stored.description == "after"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank", ["", "   "])
    async def test_blank_keeps_the_current_binding(self, blank):
        name = _seed(workspace="team")
        status, data = await _put(name, {"workspace": blank, "description": "after"})
        assert status == 200
        assert "warnings" not in data
        stored = _stored(name)
        assert stored.workspace == "team"
        assert stored.description == "after"

    @pytest.mark.asyncio
    async def test_a_configured_workspace_saves_with_no_warning(self):
        name = _seed()
        status, data = await _put(name, {"workspace": "team"})
        assert status == 200
        assert "warnings" not in data
        assert _stored(name).workspace == "team"

    @pytest.mark.asyncio
    async def test_default_saves_with_no_warning_even_when_not_declared(self):
        name = _seed(workspace="team")
        cfg = KiroCrewConfig.load()
        cfg.workspaces.pop("default", None)
        cfg.save()
        assert "default" not in KiroCrewConfig.load().workspaces
        status, data = await _put(name, {"workspace": "default"})
        assert status == 200
        assert "warnings" not in data
        assert _stored(name).workspace == "default"

    @pytest.mark.asyncio
    async def test_the_stored_binding_sent_back_raises_no_warning(self):
        """The editor echoes every field on every save: a binding whose
        workspace was since removed must not warn on an unrelated edit."""
        name = _seed(workspace="gone")
        status, data = await _put(name, {"workspace": "gone", "description": "after"})
        assert status == 200
        assert "warnings" not in data
        stored = _stored(name)
        assert stored.workspace == "gone"
        assert stored.description == "after"

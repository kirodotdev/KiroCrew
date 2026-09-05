"""The per-crew ``starred`` favourite mark.

A roster preference, nothing more: it is written by the Crew Members page's
star button (PUT /api/agents/{name}), read back by GET /api/members so the
page's star filter can hide the package-installed majority, and ignored by
everything else (routing, spawning, the orchestrator). These tests pin the
three things that make it usable as a filter key:

* it round-trips through the real config load/save,
* junk in the hand-editable config reads as un-starred, never truthy,
* the API refuses a non-bool instead of coercing (a string ``"false"`` must
  not star the crew), and the whole PUT is a no-op on refusal.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class TestConfigField:
    def test_defaults_to_unstarred(self):
        assert KiroCrewAgentConfig(kiro_agent="x").starred is False

    def test_round_trips_through_save_and_load(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["fav"] = KiroCrewAgentConfig(kiro_agent="kirocrew", starred=True)
        cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        again = KiroCrewConfig.load()
        assert again.agents["fav"].starred is True
        assert again.agents["plain"].starred is False

    @pytest.mark.parametrize("junk", ["true", "yes", 1, [True], {"on": 1}, None])
    def test_non_bool_in_config_reads_as_unstarred(self, junk):
        """config.json is hand-editable: only a real bool may star a crew."""
        cfg = KiroCrewConfig.load()
        cfg.agents["weird"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        # Write the junk value straight into the file the loader reads, the
        # way a hand edit would — the dataclass itself cannot hold it.
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["weird"]["starred"] = junk
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["weird"].starred is False


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_agent():
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "existing"


class TestUpdateEndpoint:
    @pytest.mark.asyncio
    async def test_star_then_unstar_persists(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            assert (
                await client.put(f"/api/agents/{seeded_agent}", json={"starred": True})
            ).status == 200
            assert KiroCrewConfig.load().agents[seeded_agent].starred is True
            assert (
                await client.put(f"/api/agents/{seeded_agent}", json={"starred": False})
            ).status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].starred is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", ["true", "false", 1, 0, None])
    async def test_non_bool_is_refused_and_writes_nothing(self, seeded_agent, junk):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"workspace": "other", "starred": junk}
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_starred"
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.starred is False
        # The refused request is a no-op as a whole, not just for the bad field.
        assert stored.workspace == "default"


def _members_app(state) -> web.Application:
    from kiro_crew.dashboard.handlers.members import api_members

    @web.middleware
    async def _auth(request: web.Request, handler):
        request.setdefault("app", "")
        request.setdefault("user", "local-app")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_get("/api/members", api_members)
    return app


class TestRosterExposesFilterKeys:
    @pytest.mark.asyncio
    async def test_roster_rows_carry_source_and_starred(self, tmp_path):
        fake = SimpleNamespace(
            agents={
                "conductor": KiroCrewAgentConfig(kiro_agent="conductor", starred=True),
                "pkg": KiroCrewAgentConfig(kiro_agent="pkg", source="package"),
            },
            default_agent="conductor",
        )
        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
            async with TestClient(TestServer(_members_app(state))) as client:
                resp = await client.get("/api/members")
                assert resp.status == 200
                rows = {r["name"]: r for r in (await resp.json())["members"]}
        assert rows["conductor"]["starred"] is True
        assert rows["conductor"]["source"] == "kirocrew"
        assert rows["pkg"]["starred"] is False
        assert rows["pkg"]["source"] == "package"

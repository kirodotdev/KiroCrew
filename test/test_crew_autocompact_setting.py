"""The per-Bot "Auto-compact at" on the Profile tab (``agents.<name>.autocompact_pct``).

The field already bounds every session the crew runs (``crew_autocompact_pct``);
these tests pin the two halves the Profile row adds: ``PUT /api/agents/{name}``
stores it, refusing a value outside the global knob's range instead of letting the
loader clamp it later, and ``GET /api/members`` carries it to the card.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, crew_autocompact_pct


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
    async def test_a_value_is_stored_and_is_what_the_crews_sessions_compact_at(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"autocompact_pct": 30})
            assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.agents[seeded_agent].autocompact_pct == 30.0
        assert crew_autocompact_pct(cfg, seeded_agent) == 30.0

    @pytest.mark.asyncio
    async def test_zero_goes_back_to_the_global(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            await client.put(f"/api/agents/{seeded_agent}", json={"autocompact_pct": 40})
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"autocompact_pct": 0})
            assert resp.status == 200
        cfg = KiroCrewConfig.load()
        assert cfg.agents[seeded_agent].autocompact_pct == 0.0
        assert crew_autocompact_pct(cfg, seeded_agent) is None

    @pytest.mark.asyncio
    async def test_other_updates_leave_it_alone(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            await client.put(f"/api/agents/{seeded_agent}", json={"autocompact_pct": 50})
            await client.put(f"/api/agents/{seeded_agent}", json={"starred": True})
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.autocompact_pct == 50.0
        assert stored.starred is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", [True, "30", None, [30], 4, 91, -1])
    async def test_a_value_outside_the_range_is_refused_and_writes_nothing(
        self, seeded_agent, junk
    ):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "autocompact_pct": junk},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_autocompact_pct"
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.autocompact_pct == 0.0
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


@pytest.mark.asyncio
async def test_roster_rows_carry_the_value(tmp_path):
    fake = SimpleNamespace(
        agents={
            "lean": KiroCrewAgentConfig(kiro_agent="lean", autocompact_pct=30.0),
            "plain": KiroCrewAgentConfig(kiro_agent="plain"),
        },
        default_agent="plain",
        memory_stores={},
        degraded_sections=frozenset(),
    )
    state = _make_state(tmp_path)
    with patch("kiro_crew.dashboard.handlers.members.KiroCrewConfig.load", return_value=fake):
        async with TestClient(TestServer(_members_app(state))) as client:
            resp = await client.get("/api/members")
            assert resp.status == 200
            rows = {r["name"]: r for r in (await resp.json())["members"]}
    assert rows["lean"].get("autocompact_pct") == 30.0
    assert rows["plain"].get("autocompact_pct") == 0.0

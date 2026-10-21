"""The per-crew ``dashboard_card_send`` switch on the Profile tab.

On by default. A button on the crewmate's Dashboard tab may then send its reply into
the crewmate's thread in one click; off, it only fills the chat box. The gateway
reads it on every card send (``test_dashboard_card_send``), so these tests pin the
three things the switch rests on, in the same shape as ``test_crew_starred``:

* it defaults to ON and round-trips through the real config load/save,
* junk in the hand-editable config reads as ON (the default), never as a surprise
  third state,
* the API refuses a non-bool instead of coercing (a string ``"false"`` must not
  switch it), the PUT is a no-op as a whole on refusal, and the roster row carries it.
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
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class TestConfigField:
    def test_defaults_to_on(self):
        assert KiroCrewAgentConfig(kiro_agent="x").dashboard_card_send is True

    def test_round_trips_through_save_and_load(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["quiet"] = KiroCrewAgentConfig(kiro_agent="kirocrew", dashboard_card_send=False)
        cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        again = KiroCrewConfig.load()
        assert again.agents["quiet"].dashboard_card_send is False
        assert again.agents["plain"].dashboard_card_send is True

    @pytest.mark.parametrize("junk", ["false", "no", 0, [False], {"on": 0}, None])
    def test_non_bool_in_config_reads_as_the_default(self, junk):
        """config.json is hand-editable: only a real bool may change the switch."""
        cfg = KiroCrewConfig.load()
        cfg.agents["weird"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["weird"]["dashboard_card_send"] = junk
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["weird"].dashboard_card_send is True

    def test_a_record_without_the_key_is_on(self):
        """Every crewmate that exists today has no key; none of them changes."""
        cfg = KiroCrewConfig.load()
        cfg.agents["old"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["old"].pop("dashboard_card_send", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["old"].dashboard_card_send is True


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
    async def test_switch_off_then_on_persists(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            off = await client.put(
                f"/api/agents/{seeded_agent}", json={"dashboard_card_send": False}
            )
            assert off.status == 200
            assert KiroCrewConfig.load().agents[seeded_agent].dashboard_card_send is False
            on = await client.put(f"/api/agents/{seeded_agent}", json={"dashboard_card_send": True})
            assert on.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].dashboard_card_send is True

    @pytest.mark.asyncio
    async def test_other_updates_leave_the_switch_alone(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            await client.put(f"/api/agents/{seeded_agent}", json={"dashboard_card_send": False})
            await client.put(f"/api/agents/{seeded_agent}", json={"starred": True})
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.dashboard_card_send is False
        assert stored.starred is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", ["true", "false", 1, 0, None])
    async def test_non_bool_is_refused_and_writes_nothing(self, seeded_agent, junk):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "dashboard_card_send": junk},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_dashboard_card_send"
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.dashboard_card_send is True
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


class TestRosterCarriesTheSwitch:
    @pytest.mark.asyncio
    async def test_roster_rows_carry_the_switch(self, tmp_path):
        fake = SimpleNamespace(
            agents={
                "quiet": KiroCrewAgentConfig(kiro_agent="quiet", dashboard_card_send=False),
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
        assert rows["quiet"]["dashboard_card_send"] is False
        assert rows["plain"]["dashboard_card_send"] is True

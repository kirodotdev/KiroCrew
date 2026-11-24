"""The per-Bot ``worker_conducts`` switch (rfc-bot-workers-conduct.md, section 5.1).

Owner-set and OFF by default. On, each worker the Bot's own thread opens may run as a
conductor for its item (``members.conducting_worker_session`` reads it at the worker's
session start); off, a worker does its task itself. These tests pin the three things
the switch rests on, in the same shape as ``test_crew_card_send_setting``:

* it defaults to OFF and round-trips through the real config load/save,
* junk in the hand-editable config reads as OFF, never as a surprise third state,
* the API refuses a non-bool instead of coercing (a string ``"true"`` must not switch
  it), the PUT is a no-op as a whole on refusal, and the roster row carries it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class TestConfigField:
    def test_defaults_to_off(self):
        assert KiroCrewAgentConfig(kiro_agent="x").worker_conducts is False

    def test_round_trips_through_save_and_load(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["lead"] = KiroCrewAgentConfig(kiro_agent="kirocrew", worker_conducts=True)
        cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        again = KiroCrewConfig.load()
        assert again.agents["lead"].worker_conducts is True
        assert again.agents["plain"].worker_conducts is False

    @pytest.mark.parametrize("junk", ["true", "yes", 1, [True], {"on": 1}, None])
    def test_non_bool_in_config_reads_as_the_default(self, junk):
        """config.json is hand-editable: only a real bool may change the switch."""
        cfg = KiroCrewConfig.load()
        cfg.agents["weird"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["weird"]["worker_conducts"] = junk
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["weird"].worker_conducts is False

    def test_a_record_without_the_key_is_off(self):
        """Every Bot that exists today has no key; none of them opts in."""
        cfg = KiroCrewConfig.load()
        cfg.agents["old"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["old"].pop("worker_conducts", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["old"].worker_conducts is False


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
            off = await client.put(f"/api/agents/{seeded_agent}", json={"worker_conducts": False})
            assert off.status == 200
            assert KiroCrewConfig.load().agents[seeded_agent].worker_conducts is False
            on = await client.put(f"/api/agents/{seeded_agent}", json={"worker_conducts": True})
            assert on.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].worker_conducts is True

    @pytest.mark.asyncio
    async def test_other_updates_leave_the_switch_alone(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            await client.put(f"/api/agents/{seeded_agent}", json={"worker_conducts": True})
            await client.put(f"/api/agents/{seeded_agent}", json={"starred": True})
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.worker_conducts is True
        assert stored.starred is True

    @pytest.mark.asyncio
    async def test_only_a_change_to_the_switch_reloads_the_bots_workers(
        self, seeded_agent, monkeypatch, tmp_path
    ):
        """A running worker keeps what its start gave it until its next start; a
        change to the switch schedules the Bot's workers' reload so that start comes
        soon. A save that leaves the switch as it was, or touches another field,
        reloads nothing."""
        from kiro_crew.dashboard import crewmate_worker_reload

        reloads: list[tuple[object, str]] = []
        monkeypatch.setattr(
            crewmate_worker_reload,
            "schedule_reload_workers",
            lambda state, name, **_kw: reloads.append((state, name)),
        )
        app = _crud_app()
        app["state"] = state = _make_state(tmp_path)
        saves = [
            ({"worker_conducts": True}, 1),
            ({"worker_conducts": True}, 1),
            ({"starred": True}, 1),
            ({"worker_conducts": False}, 2),
        ]
        async with TestClient(TestServer(app)) as client:
            for body, expected in saves:
                resp = await client.put(f"/api/agents/{seeded_agent}", json=body)
                assert resp.status == 200
                assert len(reloads) == expected, body
        assert reloads == [(state, seeded_agent), (state, seeded_agent)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("turn_on", [True, False])
    async def test_a_switch_change_tells_each_reloaded_worker_its_setting_changed(
        self, seeded_agent, monkeypatch, tmp_path, turn_on
    ):
        """Each worker the change reloads is told that its Bot's worker setting changed,
        in either direction. The reload's default notice says capabilities were added,
        which turning the switch off is not."""
        from kiro_crew.dashboard import crewmate_worker_reload as reload_mod

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].worker_conducts = not turn_on
        cfg.save()
        notices: list[str] = []

        async def reload_if_idle(state, slot, *, notice=reload_mod._NOTICE, on_reloaded=None):
            notices.append(notice)
            return reload_mod.RELOADED

        monkeypatch.setattr(
            reload_mod, "_workers_of", lambda st, member: [SimpleNamespace(key="w1")]
        )
        monkeypatch.setattr(reload_mod, "reload_if_idle", reload_if_idle)
        app = _crud_app()
        app["state"] = state = _make_state(tmp_path)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"worker_conducts": turn_on}
            )
            assert resp.status == 200
            # The reload runs as a task the route leaves behind: await its signal.
            pending = tuple(getattr(state, "_crewmate_reload_tasks", ()))
            await await_until(asyncio.gather(*pending), what="the worker reload tasks")
        assert notices == [
            "Session reloaded so it picks up this Bot's changed worker setting. "
            "The conversation is kept."
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", ["true", "false", 1, 0, None])
    async def test_non_bool_is_refused_and_writes_nothing(self, seeded_agent, junk):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "worker_conducts": junk},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_worker_conducts"
        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.worker_conducts is False
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
                "lead": KiroCrewAgentConfig(kiro_agent="lead", worker_conducts=True),
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
        assert rows["lead"]["worker_conducts"] is True
        assert rows["plain"]["worker_conducts"] is False

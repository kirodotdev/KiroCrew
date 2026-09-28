"""The per-crew ``display_name`` presentation label.

Presentation, nothing more: the dashboard shows it in place of the crew's
``name`` when non-empty, and everything that ADDRESSES a crew — dispatch,
``/api/agents/{name}``, crons, spawn — keeps using ``name``, which stays
immutable. These tests pin the properties that make the label safe to edit
freely:

* it round-trips through the real config load/save and defaults to "" (show
  the name),
* junk in the hand-editable config collapses to "" instead of reaching the
  wire (every roster surface renders the value verbatim),
* the API trims, accepts "" as a real value (clear back to the name), and
  refuses a non-string with no write,
* both rosters (GET /api/agents, GET /api/members) carry it, and a
  credential-shaped label leaves as the mask like every other free-text field.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir
from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class TestConfigField:
    def test_defaults_to_empty(self):
        assert KiroCrewAgentConfig(kiro_agent="x").display_name == ""

    def test_round_trips_through_save_and_load(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["labelled"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        again = KiroCrewConfig.load()
        assert again.agents["labelled"].display_name == "Release Writer"
        assert again.agents["plain"].display_name == ""

    @pytest.mark.parametrize("junk", [1, ["Nice Name"], {"label": "x"}, None, True])
    def test_non_string_in_config_reads_as_empty(self, junk):
        """config.json is hand-editable: the label is rendered verbatim by
        every roster surface, so a non-string must collapse to "" (show the
        name), never crash the load or reach the wire."""
        cfg = KiroCrewConfig.load()
        cfg.agents["weird"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        path = config_dir() / "config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["agents"]["weird"]["display_name"] = junk
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().agents["weird"].display_name == ""


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
    async def test_set_trim_and_clear_persist(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            assert (
                await client.put(
                    f"/api/agents/{seeded_agent}", json={"display_name": "  Ops Crew  "}
                )
            ).status == 200
            assert KiroCrewConfig.load().agents[seeded_agent].display_name == "Ops Crew"
            # "" is a real value: it clears the label back to the name.
            assert (
                await client.put(f"/api/agents/{seeded_agent}", json={"display_name": ""})
            ).status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].display_name == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("junk", [1, 0, None, True, ["x"], {"label": "x"}])
    async def test_non_string_is_refused_and_writes_nothing(self, seeded_agent, junk):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"display_name": junk})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_display_name"
        assert KiroCrewConfig.load().agents[seeded_agent].display_name == ""


class TestCreateEndpoint:
    """Create-path coverage on the same patched harness as
    ``test_api_agents_create_template.py``: ``update_config_locked`` records the
    mutated document, so the assertions observe exactly what would persist."""

    @staticmethod
    def _fake_config():
        from types import SimpleNamespace

        from kiro_crew.config.sections import MemoryConfig

        saved: list[bool] = []
        return SimpleNamespace(
            agent=SimpleNamespace(provider="acp"),
            memory=MemoryConfig(),
            degraded_sections=frozenset(),
            agents={},
            memory_stores={},
            default_agent="kirocrew",
            save=lambda: saved.append(True),
            saved=saved,
            written={},
        )

    async def _post(self, body, cfg):
        from types import SimpleNamespace

        from kiro_crew.dashboard.handlers.agents import api_kirocrew_agents_create

        def _fake_update_config_locked(*args, **kwargs):
            doc: dict = {"agents": {}}
            result = kwargs["mutate"](doc)
            cfg.written["doc"] = result
            cfg.saved.append(True)
            return result

        app = web.Application()
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
                return_value=cfg,
            ),
            patch(
                "kiro_crew.config.loader.update_config_locked",
                new=_fake_update_config_locked,
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents.list_agents",
                new=lambda *a, **k: [SimpleNamespace(name="kirocrew")],
            ),
            patch(
                "kiro_crew.dashboard.handlers.agents._sel",
                return_value=SimpleNamespace(log_api_access=lambda **kwargs: None),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post("/api/agents", json=body)
                return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_create_stores_trimmed_label(self):
        cfg = self._fake_config()
        status, _ = await self._post(
            {
                "name": "labelled-crew",
                "kiro_agent": "kirocrew",
                "display_name": "  Labelled Crew  ",
            },
            cfg,
        )
        assert status == 200
        assert cfg.written["doc"]["agents"]["labelled-crew"]["display_name"] == "Labelled Crew"

    @pytest.mark.asyncio
    async def test_create_refuses_non_string(self):
        cfg = self._fake_config()
        status, data = await self._post(
            {"name": "bad-label", "kiro_agent": "kirocrew", "display_name": 7}, cfg
        )
        assert status == 400
        assert data["code"] == "invalid_display_name"
        # Refused before any state was touched: nothing persisted.
        assert cfg.saved == []


class TestRosterSurfaces:
    @pytest.mark.asyncio
    async def test_agents_roster_carries_the_label(self, tmp_path):
        from kiro_crew.dashboard.handlers import api_kirocrew_agents

        cfg = KiroCrewConfig.load()
        cfg.agents["labelled"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", display_name="Release Writer"
        )
        cfg.save()
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/agents", api_kirocrew_agents)
        async with TestClient(TestServer(app)) as client:
            body = await (await client.get("/api/agents")).json()
        row = next(a for a in body["agents"] if a["name"] == "labelled")
        assert row["display_name"] == "Release Writer"

    @pytest.mark.asyncio
    async def test_credential_shaped_label_leaves_as_the_mask(self, tmp_path):
        """Same posture as description/triggers: the label is agent-writable
        free text, so a credential-shaped value must not reach the browser."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agents

        # Assembled at runtime: the value must look real enough to trip the
        # redactor, but the joined literal must never appear in the file or
        # GitHub push protection (rightly) refuses the blob.
        cred = "xoxb-" + "1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx"
        cfg = KiroCrewConfig.load()
        cfg.agents["leaky"] = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name=cred)
        cfg.save()
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/agents", api_kirocrew_agents)
        async with TestClient(TestServer(app)) as client:
            body = await (await client.get("/api/agents")).json()
        row = next(a for a in body["agents"] if a["name"] == "leaky")
        assert row["display_name"] == _SENSITIVE_MASK
        assert cred not in json.dumps(body)


class TestProjectionLockstep:
    """The label travels the live member projection, not just the HTTP roster.

    A `display_name`-only save must land a member/config event and fold into
    the roster projection, or a subscribed Members view keeps the old label
    until a full refetch. The vocabulary lives in FOUR places kept in
    lockstep: the two `_CONFIG_FIELDS` tuples (eventlog_hooks,
    members_projections) and the `_ev_before`/`_ev_after` snapshot dicts in
    handlers/agents.py.
    """

    def test_config_fields_tuples_match_and_carry_display_name(self):
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog import members_projections

        assert eventlog_hooks._CONFIG_FIELDS == members_projections._CONFIG_FIELDS, (
            "the event snapshot vocabulary and the roster fold vocabulary drifted: "
            "a field in one but not the other is written and never folded (or "
            "folded and never written)"
        )
        assert "display_name" in eventlog_hooks._CONFIG_FIELDS

    def test_agents_handler_snapshots_carry_every_config_field(self):
        """Source-scan: `_ev_before`/`_ev_after` are inline dict literals, so a
        field added to `_CONFIG_FIELDS` but not to them silently emits no
        member/config for that field's changes -- exactly the bug this guards."""
        import inspect

        from kiro_crew import eventlog_hooks
        from kiro_crew.dashboard.handlers import agents as agents_mod

        source = inspect.getsource(agents_mod)
        before_at = source.index("_ev_before = {")
        after_at = source.index("_ev_after = {")
        before_block = source[before_at : source.index("}", before_at)]
        after_block = source[after_at : source.index("}", after_at)]
        for field in eventlog_hooks._CONFIG_FIELDS:
            assert f'"{field}"' in before_block, f"_ev_before omits {field!r}"
            assert f'"{field}"' in after_block, f"_ev_after omits {field!r}"

    def test_snapshot_for_agent_carries_display_name(self):
        from kiro_crew.eventlog_hooks import _config_snapshot_for_agent

        agent = KiroCrewAgentConfig(kiro_agent="kirocrew", display_name="Release Writer")
        assert _config_snapshot_for_agent(agent)["display_name"] == "Release Writer"

    def test_roster_projection_folds_display_name(self):
        from kiro_crew.eventlog import types
        from kiro_crew.eventlog.members_projections import RosterProjection

        proj = RosterProjection()
        state = proj.init()
        state = proj.apply(
            state,
            {"type": types.MEMBER_CONFIG, "data": {"display_name": "Release Writer"}},
        )
        assert proj.view(state)["display_name"] == "Release Writer"

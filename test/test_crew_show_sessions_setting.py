"""The per-Bot ``show_sessions_in_list`` switch on the Profile tab.

Off by default: the sessions a Bot's thread opens leave the person's own session list
for the Bot's Sessions tab (``crew_mode.list_view``, ``test_chat_slots_crew_view``).
These tests pin what the switch rests on, in the shape of
``test_crew_card_send_setting``: the default and the config round trip, junk reading as
off, the API refusing a non-bool, the stamp on the mate thread's row, and the slot
list re-sent when the switch flips.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import crew_mode
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def test_defaults_to_off_and_round_trips():
    assert KiroCrewAgentConfig(kiro_agent="x").show_sessions_in_list is False
    cfg = KiroCrewConfig.load()
    cfg.agents["shown"] = KiroCrewAgentConfig(kiro_agent="kirocrew", show_sessions_in_list=True)
    cfg.agents["plain"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    again = KiroCrewConfig.load()
    assert again.agents["shown"].show_sessions_in_list is True
    assert again.agents["plain"].show_sessions_in_list is False


@pytest.mark.parametrize("junk", ["true", 1, None])
def test_non_bool_in_config_reads_as_off(junk):
    cfg = KiroCrewConfig.load()
    cfg.agents["weird"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"]["weird"]["show_sessions_in_list"] = junk
    path.write_text(json.dumps(data), encoding="utf-8")
    assert KiroCrewConfig.load().agents["weird"].show_sessions_in_list is False


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


@pytest.mark.asyncio
async def test_the_switch_persists_on_and_off(seeded_agent):
    async with TestClient(TestServer(_crud_app())) as client:
        on = await client.put(f"/api/agents/{seeded_agent}", json={"show_sessions_in_list": True})
        assert on.status == 200
        assert KiroCrewConfig.load().agents[seeded_agent].show_sessions_in_list is True
        off = await client.put(f"/api/agents/{seeded_agent}", json={"show_sessions_in_list": False})
        assert off.status == 200
    assert KiroCrewConfig.load().agents[seeded_agent].show_sessions_in_list is False


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", ["true", 1, None])
async def test_non_bool_is_refused_and_writes_nothing(seeded_agent, junk):
    async with TestClient(TestServer(_crud_app())) as client:
        resp = await client.put(
            f"/api/agents/{seeded_agent}",
            json={"workspace": "other", "show_sessions_in_list": junk},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_show_sessions_in_list"
    stored = KiroCrewConfig.load().agents[seeded_agent]
    assert stored.show_sessions_in_list is False
    assert stored.workspace == "default"


def test_the_stamp_is_on_the_mate_threads_row_only(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(agents={"ada": SimpleNamespace(show_sessions_in_list=True)})
    monkeypatch.setattr(crew_mode, "_known_mate_roots", set())
    monkeypatch.setattr(crew_mode, "_known_roots", set())
    rows = [
        {"key": "dm-ada", "mode": "member", "agent": "ada"},
        {"key": "dm-bo", "mode": "member", "agent": "bo"},
        {"key": "chat-1", "created_by": "dm-ada", crew_mode.MATE_LISTED_FIELD: True},
    ]
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    crew_mode.stamp_rows(rows, parents={}, tree_slots=set())
    assert [r.get(crew_mode.MATE_LISTED_FIELD) for r in rows] == [True, False, None]


@pytest.mark.asyncio
async def test_a_flip_resends_the_slot_list() -> None:
    from kiro_crew.dashboard.server import _register_config_watch

    app = web.Application()
    state = SimpleNamespace(
        workflow_service=None,
        channel_manager=None,
        set_dynamic_cards_enabled=MagicMock(),
        push_slots_update=MagicMock(),
        push_refresh=MagicMock(),
    )
    _register_config_watch(app, state, None)
    subs = {s.name: s for s in app["config_watch_subscriptions"]}
    apply = subs["agents.show_sessions_in_list"].callback()
    cfg = KiroCrewConfig()
    apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agents.ada.display_name"})))
    state.push_slots_update.assert_not_called()
    state.push_refresh.assert_not_called()
    apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agents.ada.show_sessions_in_list"})))
    state.push_slots_update.assert_called_once_with()
    state.push_refresh.assert_called_once_with("agents")

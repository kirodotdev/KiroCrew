"""A Bot's sessions leave the person's own session list unless a switch keeps them.

The sessions a live Bot thread opens are listed on its Sessions tab and its side
panel's Sessions tab (``crew_mode.list_view``, ``test_chat_slots_crew_view``). While
``dashboard.show_bot_sessions_in_list`` is on, every Bot's are listed in the
person's list too and the per-Bot switches do not apply; while it is off, each
Bot's own ``show_sessions_in_list`` decides for that Bot.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import crew_mode
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _owner_caller(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


def test_the_per_bot_switch_defaults_on_when_the_bot_config_omits_it():
    assert KiroCrewAgentConfig(kiro_agent="kirocrew").show_sessions_in_list is True
    cfg = KiroCrewConfig.load()
    cfg.agents["ada"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"]["ada"].pop("show_sessions_in_list", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert KiroCrewConfig.load().agents["ada"].show_sessions_in_list is True


@pytest.mark.parametrize(
    ("stored", "want"), [(True, True), (False, False), ("yes", True), (0, True)]
)
def test_a_stored_per_bot_switch_loads_only_a_real_bool(stored, want):
    cfg = KiroCrewConfig.load()
    cfg.agents["ada"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"]["ada"]["show_sessions_in_list"] = stored
    path.write_text(json.dumps(data), encoding="utf-8")
    assert KiroCrewConfig.load().agents["ada"].show_sessions_in_list is want


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.mark.asyncio
async def test_the_update_api_saves_the_per_bot_switch_and_refuses_a_non_bool():
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    async with TestClient(TestServer(_crud_app())) as client:
        bad = await client.put("/api/agents/existing", json={"show_sessions_in_list": "yes"})
        assert bad.status == 400
        assert (await bad.json())["code"] == "invalid_show_sessions_in_list"
        resp = await client.put("/api/agents/existing", json={"show_sessions_in_list": True})
        assert resp.status == 200
    assert KiroCrewConfig.load().agents["existing"].show_sessions_in_list is True


def _rows(listed: object) -> list[dict]:
    mate = {"key": "dm-ada", "mode": "member", "agent": "ada"}
    if listed is not None:
        mate[crew_mode.MATE_LISTED_FIELD] = listed
    return [
        mate,
        {"key": "chat-1", crew_mode.MATE_ROOT_FIELD: "dm-ada"},
        {"key": "chat-2", crew_mode.MATE_ROOT_FIELD: "dm-gone"},
        {"key": "chat-3"},
    ]


@pytest.mark.parametrize("listed", [None, False, "yes"])
def test_a_live_mate_threads_sessions_leave_a_persons_list_unless_listed(listed) -> None:
    assert [r["key"] for r in crew_mode.list_view(_rows(listed), "", agent=False)] == [
        "dm-ada",
        "chat-2",
        "chat-3",
    ]


def test_show_bot_sessions_in_list_keeps_them_in_a_persons_list() -> None:
    assert [r["key"] for r in crew_mode.list_view(_rows(True), "", agent=False)] == [
        "dm-ada",
        "chat-1",
        "chat-2",
        "chat-3",
    ]


def test_a_stamp_under_another_name_changes_nothing() -> None:
    rows = _rows(None)
    rows[0]["show_sessions_in_list"] = True
    assert "chat-1" not in [r["key"] for r in crew_mode.list_view(rows, "", agent=False)]


@pytest.mark.parametrize(
    ("everyone", "own", "stamped"),
    [
        (True, False, True),
        (True, True, True),
        (False, True, True),
        (False, False, False),
        ("yes", "yes", False),
    ],
)
def test_stamp_rows_lists_a_bots_sessions_when_global_is_on_else_by_its_own_switch(
    _floor_monkeypatch, everyone, own, stamped
) -> None:
    cfg = KiroCrewConfig()
    cfg.dashboard.show_bot_sessions_in_list = everyone
    cfg.agents["ada"] = KiroCrewAgentConfig(kiro_agent="kirocrew", show_sessions_in_list=own)
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: cfg)
    rows = [
        {"key": "dm-ada", "mode": "member", "agent": "ada"},
        {"key": "chat-1", "created_by": "dm-ada"},
        {"key": "chat-3"},
    ]
    crew_mode.stamp_rows(rows, parents={}, saved_creators={}, tree_slots=())
    assert rows[0][crew_mode.MATE_LISTED_FIELD] is stamped
    assert rows[1][crew_mode.MATE_ROOT_FIELD] == "dm-ada"
    assert crew_mode.MATE_LISTED_FIELD not in rows[1]
    assert crew_mode.MATE_LISTED_FIELD not in rows[2]


def test_the_switch_defaults_off_and_loads_only_a_real_bool() -> None:
    assert KiroCrewConfig().dashboard.show_bot_sessions_in_list is False
    path = config_dir() / "config.json"
    for stored, want in [(True, True), ("yes", False), (1, False)]:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data.setdefault("dashboard", {})["show_bot_sessions_in_list"] = stored
        path.write_text(json.dumps(data), encoding="utf-8")
        assert KiroCrewConfig.load().dashboard.show_bot_sessions_in_list is want


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("dashboard.show_bot_sessions_in_list", "dashboard.show_bot_sessions_in_list"),
        ("agents.show_sessions_in_list", "agents.ada.show_sessions_in_list"),
    ],
)
def test_a_flip_of_either_switch_re_sends_the_slot_list(name, path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from kiro_crew.config.live import ConfigChange
    from kiro_crew.dashboard.server import _register_config_watch

    state = SimpleNamespace(
        workflow_service=None,
        channel_manager=None,
        push_slots_update=MagicMock(),
        push_refresh=MagicMock(),
        set_dynamic_cards_enabled=MagicMock(),
    )
    app = web.Application()
    _register_config_watch(app, state, None)
    (sub,) = [s for s in app["config_watch_subscriptions"] if s.name == name]
    cfg = KiroCrewConfig()
    apply = sub.callback()
    apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({"agents.ada.workspace"})))
    state.push_slots_update.assert_not_called()
    apply(ConfigChange(old=cfg, new=cfg, changed=frozenset({path})))
    state.push_slots_update.assert_called_once_with()
    state.push_refresh.assert_called_once_with("agents")

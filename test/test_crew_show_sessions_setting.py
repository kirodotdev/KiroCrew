"""A Bot's sessions always leave the person's own session list.

There is no per-Bot switch: the sessions a live Bot thread opens are listed on its
Sessions tab and its side panel's Sessions tab only (``crew_mode.list_view``,
``test_chat_slots_crew_view``). A config written while a ``show_sessions_in_list``
switch existed still loads, the old key changes nothing, and the update API takes
no such field.
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


@pytest.mark.parametrize("stored", [True, False, "yes"])
def test_a_stored_switch_loads_and_is_ignored(stored):
    cfg = KiroCrewConfig.load()
    cfg.agents["ada"] = KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="default")
    cfg.save()
    path = config_dir() / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["agents"]["ada"]["show_sessions_in_list"] = stored
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = KiroCrewConfig.load().agents["ada"]
    assert loaded.workspace == "default"
    assert not hasattr(loaded, "show_sessions_in_list")


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

    app = web.Application()
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.mark.asyncio
async def test_the_update_api_takes_no_switch():
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    async with TestClient(TestServer(_crud_app())) as client:
        resp = await client.put(
            "/api/agents/existing", json={"workspace": "other", "show_sessions_in_list": True}
        )
        assert resp.status == 200
    stored = KiroCrewConfig.load().agents["existing"]
    assert stored.workspace == "other"
    assert not hasattr(stored, "show_sessions_in_list")


def test_a_live_mate_threads_sessions_leave_a_persons_list_whatever_a_row_says() -> None:
    rows = [
        {"key": "dm-ada", "mode": "member", "agent": "ada", "show_sessions_in_list": True},
        {"key": "chat-1", crew_mode.MATE_ROOT_FIELD: "dm-ada"},
        {"key": "chat-2", crew_mode.MATE_ROOT_FIELD: "dm-gone"},
        {"key": "chat-3"},
    ]
    assert [r["key"] for r in crew_mode.list_view(rows, "", agent=False)] == [
        "dm-ada",
        "chat-2",
        "chat-3",
    ]

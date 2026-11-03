"""Deleting a crewmate forgets its asked-once page offer, and nothing else's.

The offer record sits in ``crew-panels``, outside the member's own tree, so no other
step of the delete reaches it. Left behind, a later crewmate that derives the same
slug would never be offered its own page. A surviving namesake keeps its record.
A ``<slug>.dashboard-lock`` file left by the removed structure lock is not read by
anything and is left as it is.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew import agent_panel
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import reset_degraded_observations
from kiro_crew.dashboard.handlers import agents as agents_mod
from kiro_crew.eventlog import service as service_mod

GONE = "retired"
LIVE = "keeper"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Every test writes into its own data home, never the live one."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    reset_degraded_observations()
    service_mod.set_service(None)
    yield
    service_mod.set_service(None)
    reset_degraded_observations()


def _write_roster(home: Path, **members: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"agents": members}), encoding="utf-8")


def _namespace_lock_is_held() -> bool:
    from kiro_crew import memory_stores

    roots = getattr(memory_stores._NAMESPACE_LOCK_STATE, "roots", None) or set()
    return memory_stores.memory_stores_root().resolve() in roots


def _delete_request(name: str):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    request.app = {"state": None}
    return request


@pytest.mark.asyncio
async def test_the_delete_route_forgets_the_offer_under_the_namespace_lock(tmp_path, monkeypatch):
    from kiro_crew.dashboard import page_offer

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"agents": {LIVE: {}, GONE: {}}, "default_agent": LIVE}), encoding="utf-8"
    )
    page_offer.record_asked(GONE, "Goal board", "goal-board")
    page_offer.record_asked(LIVE, "Goal board", "goal-board")
    # A lock file the removed structure lock wrote: nothing reads it, so it stays.
    legacy = agent_panel.panel_dir() / f"{GONE}.dashboard-lock"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("{}", encoding="utf-8")

    held: list[bool] = []
    real = agents_mod._reclaim_deleted_member_page_offer

    def _spy(name: str, cfg: KiroCrewConfig) -> None:
        held.append(_namespace_lock_is_held())
        real(name, cfg)

    monkeypatch.setattr(agents_mod, "_reclaim_deleted_member_page_offer", _spy)

    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    resp = await api_kirocrew_agent_delete(_delete_request(GONE))

    assert resp.status == 200
    assert GONE not in KiroCrewConfig.load().agents
    assert held == [True]
    assert page_offer.was_asked(GONE) is False
    assert page_offer.was_asked(LIVE) is True
    assert legacy.read_text(encoding="utf-8") == "{}"


def test_delete_removes_the_departed_crewmates_page_offer_record(tmp_path):
    """Kept, a later crewmate on the same slug would never be offered its own page."""
    from kiro_crew.dashboard import page_offer

    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    page_offer.record_asked(GONE, "Goal board", "goal-board")
    captured = KiroCrewConfig.load()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_page_offer(GONE, captured)

    assert page_offer.was_asked(GONE) is False


def test_a_recreated_namesake_keeps_its_page_offer_record(tmp_path):
    from kiro_crew.dashboard import page_offer

    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    page_offer.record_asked(GONE, "Goal board", "goal-board")
    captured = KiroCrewConfig.load()
    _write_roster(home, **{GONE: {}})

    agents_mod._reclaim_deleted_member_page_offer(GONE, captured)

    assert page_offer.was_asked(GONE) is True


def test_an_unreadable_roster_forgets_nothing(tmp_path):
    from kiro_crew.dashboard import page_offer

    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    page_offer.record_asked(GONE, "Goal board", "goal-board")
    captured = KiroCrewConfig.load()
    (home / "config.json").write_text("{not json", encoding="utf-8")

    agents_mod._reclaim_deleted_member_page_offer(GONE, captured)  # must not raise

    assert page_offer.was_asked(GONE) is True

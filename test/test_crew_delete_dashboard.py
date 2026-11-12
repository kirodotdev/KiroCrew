"""Deleting a crewmate removes its dashboard instance, and nothing else's.

The instance sits under ``members/<slug>/dashboard``, which no other step of the delete
reaches. Left behind, a later crewmate that derives the same slug opens on the page the
departed one had, and an import card's Undo leaves the page its dashboard step adopted.
A surviving namesake keeps its page.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import reset_degraded_observations
from kiro_crew.dashboard.agent_admin import crew_removal
from kiro_crew.dashboard_templates import instance
from kiro_crew.eventlog import service as service_mod

GONE = "retired"
LIVE = "keeper"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    reset_degraded_observations()
    service_mod.set_service(None)
    yield
    service_mod.set_service(None)
    reset_degraded_observations()


def _write_roster(home: Path, **members: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(json.dumps({"agents": members}), encoding="utf-8")


def _delete_request(name: str):
    request = MagicMock(spec=web.Request)
    request.method = "DELETE"
    request.match_info = {"name": name}
    request.app = {"state": None}
    return request


@pytest.mark.asyncio
async def test_a_crewmate_on_the_slug_of_a_deleted_one_does_not_inherit_its_dashboard(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"agents": {LIVE: {}, GONE: {}}, "default_agent": LIVE}), encoding="utf-8"
    )
    instance.adopt(GONE, "standup")
    instance.adopt(LIVE, "standup")

    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    resp = await api_kirocrew_agent_delete(_delete_request(GONE))

    assert resp.status == 200
    assert GONE not in KiroCrewConfig.load().agents
    assert instance.read(GONE).template_id == ""
    assert instance.read(LIVE).template_id == "standup"


def test_a_recreated_namesake_keeps_its_dashboard(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    instance.adopt(GONE, "standup")
    captured = KiroCrewConfig.load()
    _write_roster(home, **{GONE: {}})

    crew_removal._reclaim_deleted_member_dashboard(GONE, captured)

    assert instance.read(GONE).template_id == "standup"


def test_an_unreadable_roster_removes_no_dashboard(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    instance.adopt(GONE, "standup")
    captured = KiroCrewConfig.load()
    (home / "config.json").write_text("{not json", encoding="utf-8")

    crew_removal._reclaim_deleted_member_dashboard(GONE, captured)  # must not raise

    assert instance.read(GONE).template_id == "standup"

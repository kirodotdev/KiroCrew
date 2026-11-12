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


def test_a_linked_member_directory_never_leads_the_delete_into_a_live_dashboard(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    instance.adopt(LIVE, "standup")
    captured = KiroCrewConfig.load()
    _write_roster(home, **{LIVE: {}})
    members = instance.instance_dir(LIVE).parent.parent
    (members / GONE).symlink_to(members / LIVE, target_is_directory=True)

    crew_removal._reclaim_deleted_member_dashboard(GONE, captured)  # must not raise

    assert instance.read(LIVE).template_id == "standup"


def _refused(slug: str) -> None:
    with pytest.raises(instance.InstanceError):
        instance.forget(slug)


def test_a_linked_member_directory_is_refused_and_both_pages_stay(tmp_path):
    instance.adopt(LIVE, "standup")
    members = instance.instance_dir(LIVE).parent.parent
    (members / GONE).symlink_to(members / LIVE, target_is_directory=True)
    _refused(GONE)
    assert instance.read(LIVE).template_id == "standup"
    assert (members / GONE).is_symlink()


def test_a_linked_dashboard_directory_is_refused_and_its_target_stays(tmp_path):
    instance.adopt(LIVE, "standup")
    target = instance.instance_dir(LIVE)
    gone_dir = instance.instance_dir(GONE)
    gone_dir.parent.mkdir(parents=True)
    gone_dir.symlink_to(target, target_is_directory=True)
    _refused(GONE)
    assert gone_dir.is_symlink()
    assert instance.read(LIVE).template_id == "standup"


def test_a_linked_members_directory_is_refused(tmp_path):
    instance.adopt(GONE, "standup")
    members = instance.instance_dir(GONE).parent.parent
    moved = tmp_path / "moved-members"
    members.rename(moved)
    members.symlink_to(moved, target_is_directory=True)
    _refused(GONE)
    assert (moved / GONE / "dashboard").is_dir()


def test_a_linked_data_home_is_the_resolved_home_and_the_delete_stays_inside_it(
    tmp_path, monkeypatch
):
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("KIROCREW_HOME", str(link))
    instance.adopt(GONE, "standup")
    instance.adopt(LIVE, "standup")
    instance.forget(GONE)
    assert instance.read(GONE).template_id == ""
    assert instance.read(LIVE).template_id == "standup"
    assert not (real / "members" / GONE / "dashboard").exists()


@pytest.mark.parametrize("slug", ["../escape", "a/b", ""])
def test_a_slug_that_leaves_its_own_directory_is_refused(slug):
    instance.adopt(LIVE, "standup")
    _refused(slug)
    assert instance.read(LIVE).template_id == "standup"

"""Deleting a crewmate removes its dashboard structure lock, and nothing else's.

The lock is ``crew-panels/<slug>.dashboard-lock``, outside the member's own tree, so
no other step of the delete reaches it. Left behind, it would greet a later crewmate
that derives the same slug with a page locked to the deleted one's snapshot.

Clearing the lock is the weightier direction: a lock is the PERSON's decision about
what a crewmate's agent may change, so a removal that reached a live crewmate's lock
would hand that agent the page. The refusals are pinned beside the removal for that
reason -- a surviving namesake, a sibling, an unreadable roster, a predicate that
answers "claimed", and an aliased store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew import agent_panel
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.resolution import reset_degraded_observations
from kiro_crew.dashboard.handlers import agents as agents_mod
from kiro_crew.dashboard_templates import instance, structure_lock_removal
from kiro_crew.eventlog import service as service_mod
from kiro_crew.platform_compat import open_create_or_existing, try_acquire_lock

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


def _lock(slug: str) -> Path:
    instance.set_structure_lock(slug, True)
    path = instance.lock_path(slug)
    assert path.is_file()
    return path


def _guard(slug: str) -> Path:
    return agent_panel.panel_dir() / f"{slug}.dashboard-lock.guard"


# --- the departed crewmate's lock goes ---------------------------------------


def test_delete_removes_the_departed_crewmates_lock(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    path = _lock(GONE)

    captured = KiroCrewConfig.load()
    _write_roster(home)  # the delete committed: the record is gone
    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert not path.exists()
    assert instance.is_locked(GONE) is False


def test_the_lock_is_found_by_the_persisted_member_id(tmp_path):
    """A crewmate with an explicit id keys its lock by that id, not its folded name."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {"member_id": "mem-7"}})
    by_id = _lock("mem-7")
    by_name = _lock(GONE)  # what the NAME alone folds to: never this crewmate's lock

    captured = KiroCrewConfig.load()
    _write_roster(home)
    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert not by_id.exists()
    assert by_name.is_file()


def test_other_crewmates_locks_are_untouched(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}, LIVE: {}})
    gone = _lock(GONE)
    live = _lock(LIVE)
    live_bytes = live.read_bytes()

    captured = KiroCrewConfig.load()
    _write_roster(home, **{LIVE: {}})
    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert not gone.exists()
    assert live.read_bytes() == live_bytes
    assert instance.is_locked(LIVE) is True


def test_a_crewmate_that_never_locked_creates_nothing(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    captured = KiroCrewConfig.load()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)  # must not raise

    assert not instance.lock_path(GONE).exists()
    assert not _guard(GONE).exists()


def test_a_repeated_cleanup_is_a_no_op(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    path = _lock(GONE)
    captured = KiroCrewConfig.load()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)
    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert not path.exists()
    assert structure_lock_removal.remove_structure_lock(GONE, still_unclaimed=lambda: True) is False


def test_an_unreadable_lock_file_is_still_removed(tmp_path):
    """Unreadable reads LOCKED (fail-closed), so it is exactly the file a delete must take."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    path = _lock(GONE)
    path.write_text("not json", encoding="utf-8")
    assert instance.is_locked(GONE) is True
    captured = KiroCrewConfig.load()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert not path.exists()


# --- a lock the roster still claims stays ------------------------------------


def test_a_recreated_namesake_keeps_its_lock(tmp_path):
    """The roster re-read under the guard decides, not the captured snapshot."""
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    path = _lock(GONE)
    captured = KiroCrewConfig.load()
    _write_roster(home, **{GONE: {}})  # a same-name crewmate is back on disk

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert path.is_file()
    assert instance.is_locked(GONE) is True


def test_an_unreadable_roster_removes_nothing(tmp_path):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    path = _lock(GONE)
    captured = KiroCrewConfig.load()
    (home / "config.json").write_text("{not json", encoding="utf-8")

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert path.is_file()


def test_the_predicate_is_asked_under_the_guard_and_claimed_keeps_the_lock():
    path = _lock(GONE)
    asked: list[bool] = []

    def _claimed() -> bool:
        # The guard is held: a second, non-blocking take of it must fail.
        fd = open_create_or_existing(_guard(GONE), os.O_RDWR, 0o600)
        try:
            asked.append(not try_acquire_lock(fd, exclusive=True))
        finally:
            os.close(fd)
        return False

    assert structure_lock_removal.remove_structure_lock(GONE, still_unclaimed=_claimed) is False
    assert asked == [True]
    assert path.is_file()


# --- unsafe names stay refused -----------------------------------------------


def test_a_session_key_has_no_lock_to_remove():
    with pytest.raises(instance.InstanceError):
        structure_lock_removal.remove_structure_lock("session:abc", still_unclaimed=lambda: True)


def test_an_aliased_store_is_refused_and_the_lock_kept(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    path = _lock(GONE)
    captured = KiroCrewConfig.load()
    _write_roster(home)
    with monkeypatch.context() as mp:
        mp.setattr(agent_panel.platform_compat, "is_link_or_junction", lambda p: True)
        with pytest.raises(agent_panel.PanelError):
            structure_lock_removal.remove_structure_lock(GONE, still_unclaimed=lambda: True)
        agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)  # swallowed, logged

    assert path.is_file()


# --- the real delete route ---------------------------------------------------


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
async def test_the_delete_route_removes_the_lock_under_the_namespace_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"agents": {LIVE: {}, GONE: {}}, "default_agent": LIVE}), encoding="utf-8"
    )
    gone = _lock(GONE)
    live = _lock(LIVE)

    held: list[bool] = []
    real = agents_mod._reclaim_deleted_member_dashboard_lock

    def _spy(name: str, cfg: KiroCrewConfig) -> None:
        held.append(_namespace_lock_is_held())
        real(name, cfg)

    monkeypatch.setattr(agents_mod, "_reclaim_deleted_member_dashboard_lock", _spy)

    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    resp = await api_kirocrew_agent_delete(_delete_request(GONE))

    assert resp.status == 200
    assert GONE not in KiroCrewConfig.load().agents
    assert held == [True]
    assert not gone.exists()
    assert live.is_file()


# --- the asked-once page offer beside the lock ---------------------------------


def test_delete_removes_the_departed_crewmates_page_offer_record(tmp_path):
    """Kept, a later crewmate on the same slug would never be offered its own page."""
    from kiro_crew.dashboard import page_offer

    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    page_offer.record_asked(GONE, "Goal board", "goal-board")
    captured = KiroCrewConfig.load()
    _write_roster(home)

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert page_offer.was_asked(GONE) is False


def test_a_recreated_namesake_keeps_its_page_offer_record(tmp_path):
    from kiro_crew.dashboard import page_offer

    home = tmp_path / "home"
    _write_roster(home, **{GONE: {}})
    page_offer.record_asked(GONE, "Goal board", "goal-board")
    captured = KiroCrewConfig.load()
    _write_roster(home, **{GONE: {}})

    agents_mod._reclaim_deleted_member_dashboard_lock(GONE, captured)

    assert page_offer.was_asked(GONE) is True

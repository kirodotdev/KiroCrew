"""The crew name the user typed shows on every dashboard of that user. A Crew launch names
its sandbox ``<crew>-kc-<6 hex>`` while the List decides (the DevSpace lane's label shape), and a
dashboard that didn't launch it names its List-made row by that crew part. The guard still takes
only a Crew launch's displayName; an older launch's bare ``kc-`` tag still adopts."""

from __future__ import annotations

from collections import deque

import pytest
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_crews_from_list import _env, _items
from test_managed_env_engine import TAG, ft, launch, make  # noqa: F401  (ft, make: fixtures)
from test_managed_env_routes import call

from kiro_crew.managed_env.engine import crew_display_name, ssh_host_for
from kiro_crew.managed_env.guard import crew_name_of, crew_tag_of, is_crew_marker

CREW = "crew-sandbox-01"


def _switch(monkeypatch, on=True):
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: on)


# ---- the marker: a Crew launch's displayName, and nothing else -------------------------------


@pytest.mark.parametrize(
    "name,crew,tag",
    [
        ("kc-0a1b2c", "", "kc-0a1b2c"),  # an older launch: the bare tag
        ("crew-sandbox-01-kc-0a1b2c", "crew-sandbox-01", "kc-0a1b2c"),
        ("ab-kc-ffffff", "ab", "kc-ffffff"),
        ("a" * 32 + "-kc-000000", "a" * 32, "kc-000000"),
    ],
)
def test_a_crew_launchs_display_name_is_a_marker_with_its_parts(name, crew, tag):
    assert is_crew_marker(name) and crew_name_of(name) == crew and crew_tag_of(name) == tag


@pytest.mark.parametrize(
    "name",
    [
        "my-ide-box",  # the web app, the IDE
        "kc-0A1B2C",  # upper-case hex
        "kc-0a1b2c-1",  # the tag first
        "kc-0a1b2c-crew",
        "Crew-kc-0a1b2c",  # not a crew name: upper case
        "a--b-kc-0a1b2c",  # two hyphens
        "x-kc-0a1b2c",  # one letter
        "1ab-kc-0a1b2c",  # starts with a digit
        "ab--kc-0a1b2c",
        "crew sandbox-kc-0a1b2c",  # a space
        "a" * 33 + "-kc-0a1b2c",  # over 32
        "crew-kc-0a1b2c-x",
        "",
    ],
)
def test_anything_else_is_not_a_marker(name):
    assert not is_crew_marker(name) and crew_name_of(name) == "" and crew_tag_of(name) == ""


def test_the_display_name_is_the_crew_then_the_tag():
    assert crew_display_name(CREW, TAG) == f"{CREW}-{TAG}"
    assert crew_name_of(crew_display_name(CREW, TAG)) == CREW
    assert crew_tag_of(crew_display_name(CREW, TAG)) == TAG


# ---- home A: the launch names its sandbox ------------------------------------------------------


@pytest.mark.parametrize(
    "on,want",
    [
        (True, f"{CREW}-{TAG}"),
    ],
)
def test_the_launch_names_the_sandbox_with_the_crew(make, bff, monkeypatch, on, want):  # noqa: F811
    _switch(monkeypatch, on)
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["PENDING", "RUNNING"])
    launch(engine, crew=CREW)
    assert bff.sandboxes["env-001"]["displayName"] == want
    assert [r["displayName"] for r in guard.ledger()] == [want]


def test_the_rollback_by_tag_finds_the_named_sandbox(make, bff, monkeypatch):  # noqa: F811
    _switch(monkeypatch)
    engine, reg, guard = make()
    bff.get_statuses["env-001"] = deque(["PENDING", "RUNNING"])
    launch(engine, crew=CREW)
    assert engine.teardown(tag=TAG, profile=CREW, region="") is True
    assert bff.count("DeleteSandbox") == 1
    assert engine.teardown(tag="kc-999999", profile=CREW, region="") is False  # another tag


# ---- home B: the List names its row by the crew -----------------------------------------------


@pytest.fixture
def bff():
    from managed_env_fake_bff import FakeBff

    b = FakeBff()
    yield b
    b.close()


def test_another_dashboard_names_the_row_with_the_crew(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", f"{CREW}-{TAG}")  # home A's launch, seen from home B
    item = _items(routes)[sid]
    rows = [r for r in reg.list() if r.ssh_host == ssh_host_for(sid)]
    assert [r.name for r in rows] == [CREW]
    assert item["own"] is True and item["crew_marker"] is True and item["name"] == CREW
    assert guard.is_actionable(sid) and sid not in guard.own_ids()


def test_an_older_launchs_bare_tag_still_adopts_by_its_tag(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("STOPPED", "kc-dfc40d")
    _items(routes)
    assert [r.name for r in reg.list()] == ["kc-dfc40d"] and guard.is_actionable(sid)


@pytest.mark.parametrize("name", ["my-ide-box", "crew sandbox-kc-0a1b2c", "Crew-kc-0a1b2c"])
def test_a_box_crew_didnt_launch_is_still_refused(bff, tmp_path, monkeypatch, name):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", name)
    assert _items(routes)[sid]["own"] is False and reg.list() == []
    n = len(bff.requests)
    assert call(routes, "POST", f"/api/managed-environments/{sid}/ssh-session")[0] == 404
    assert len(bff.requests) == n


def test_a_row_this_home_launched_keeps_its_name(bff, tmp_path, monkeypatch):
    from test_managed_env_routes import own

    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    mine = own(bff, guard, reg, name=f"other-name-{TAG}", crew="kirocrew-main")
    _items(routes)
    assert [(r.name, r.ssh_host) for r in reg.list()] == [("kirocrew-main", ssh_host_for(mine))]

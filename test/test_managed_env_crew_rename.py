"""Rename on a crew's row. With the service's rename on, one UpdateSandbox sends
``<name>-<tag>`` and every dashboard of the user shows the name (their List renames their row).
With it off (the 400), the rename fails on this dashboard too: nothing is
renamed and one line says so. An unreadable switch (the 503) changes nothing and doesn't stop the
client. Two homes, one fake service."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_crews_from_list import _env, _items
from test_managed_env_engine import Row
from test_managed_env_routes import own

from kiro_crew.managed_env.engine import RENAME_TEXT, ssh_host_for
from kiro_crew.managed_env.guard import (
    RENAME_SWITCH_UNREADABLE_TEXT,
    GuardRefused,
)
from kiro_crew.managed_env.routes import register_sandbox_routes

TAG = "kc-dfc40d"


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def post(routes, path, body):
    async def go():
        app = web.Application()
        register_sandbox_routes(app, routes)
        async with TestClient(TestServer(app)) as c:
            r = await c.post(path, json=body)
            return r.status, await r.json()

    return asyncio.run(go())


def rename(routes, sid, name):
    return post(routes, f"/api/managed-environments/{sid}/rename", {"name": name})


def _homes(bff, tmp_path, monkeypatch, listed=TAG, crew="crew-sandbox-01"):
    """Home A launched the box (its ledger, its row named by the user); home B adopts it."""
    a = _env(bff, tmp_path / "a", monkeypatch)
    b = _env(bff, tmp_path / "b", monkeypatch)
    sid = own(bff, a[2], a[1], status="RUNNING", name=listed, crew=crew)
    _items(a[0])
    _items(b[0])
    return a, b, sid


def _names(reg, sid):
    return [r.name for r in reg.list() if r.ssh_host == ssh_host_for(sid)]


def _sent(bff):
    return [r["body"] for r in bff.requests if r["op"] == "UpdateSandbox"]


def test_the_operators_box_renamed_on_home_a_shows_on_home_b(bff, tmp_path, monkeypatch):
    # 4d1d7936's case: listed as the bare tag kc-dfc40d, its crew name only in home A's row
    (ra, rega, _, *_), (rb, regb, *_), sid = _homes(bff, tmp_path, monkeypatch)
    assert _names(regb, sid) == [TAG]
    assert rename(ra, sid, "crew-sandbox-01") == (
        200,
        {"renamed": "everywhere", "name": "crew-sandbox-01", "line": ""},
    )
    assert _sent(bff) == [{"execEnvironmentId": sid, "displayName": f"crew-sandbox-01-{TAG}"}]
    assert bff.sandboxes[sid]["displayName"] == f"crew-sandbox-01-{TAG}"
    _items(rb)  # home B's next List
    assert _names(regb, sid) == ["crew-sandbox-01"] and _names(rega, sid) == ["crew-sandbox-01"]
    _items(ra)  # and home A's List keeps it
    assert _names(rega, sid) == ["crew-sandbox-01"] and len(_sent(bff)) == 1


def test_a_bare_tag_list_never_renames_a_hand_named_row(bff, tmp_path, monkeypatch):
    # 4d1d7936: listed as the bare tag kc-dfc40d, its rows named
    # crew-sandbox-01 by hand (a registry write, no Rename), in homes that never noted a name
    (ra, rega, ga, *_), (rb, regb, gb, *_), sid = _homes(bff, tmp_path, monkeypatch)
    for reg in (rega, regb):
        row = next(r for r in reg.list() if r.ssh_host == ssh_host_for(sid))
        reg.update(row.id, name="crew-sandbox-01")
    assert ga.row_names() == {} and gb.row_names() == {}  # a bare tag carries no crew name
    for _ in range(2):  # the first List after the swap, and the next
        _items(ra)
        _items(rb)
    assert _names(rega, sid) == ["crew-sandbox-01"] and _names(regb, sid) == ["crew-sandbox-01"]
    assert ga.row_names() == {} and gb.row_names() == {} and _sent(bff) == []


def test_a_named_launch_renamed_again_shows_everywhere(bff, tmp_path, monkeypatch):
    (ra, rega, *_), (rb, regb, *_), sid = _homes(
        bff, tmp_path, monkeypatch, listed=f"crew-sandbox-01-{TAG}"
    )
    assert _names(regb, sid) == ["crew-sandbox-01"]
    assert rename(ra, sid, "my-crew")[1]["renamed"] == "everywhere"
    assert _sent(bff) == [{"execEnvironmentId": sid, "displayName": f"my-crew-{TAG}"}]
    _items(rb)
    assert _names(regb, sid) == ["my-crew"] and _names(rega, sid) == ["my-crew"]


def test_the_launching_home_follows_a_later_shared_rename(bff, tmp_path, monkeypatch):
    # home A's row keeps its own name at first sight (rows this home launched keep their
    # names), then follows when the shared name changes, here by a Rename on home B
    (ra, rega, *_), (rb, regb, *_), sid = _homes(
        bff, tmp_path, monkeypatch, listed=f"other-name-{TAG}"
    )
    assert _names(rega, sid) == ["crew-sandbox-01"] and _names(regb, sid) == ["other-name"]
    assert rename(rb, sid, "my-crew")[1]["renamed"] == "everywhere"
    _items(ra)
    assert _names(rega, sid) == ["my-crew"] and _names(regb, sid) == ["my-crew"]


def test_another_400_is_no_rename_and_no_local_name(bff, tmp_path, monkeypatch):
    (ra, rega, *_), _, sid = _homes(bff, tmp_path, monkeypatch)
    bff.queue(
        "UpdateSandbox",
        400,
        {"__type": "com.example.kiro#ValidationException", "message": "displayName is too long"},
    )
    status, body = rename(ra, sid, "my-crew")
    assert status == 200 and body["renamed"] == "no" and "too long" in body["line"]
    assert len(_sent(bff)) == 1 and _names(rega, sid) == ["crew-sandbox-01"]


def test_an_unreadable_rename_switch_changes_nothing_and_stops_nothing(bff, tmp_path, monkeypatch):
    (ra, rega, ga, *_), _, sid = _homes(bff, tmp_path, monkeypatch)
    bff.queue(
        "UpdateSandbox",
        503,
        {
            "__type": "com.example.kiro#ServiceUnavailableException",
            "message": RENAME_SWITCH_UNREADABLE_TEXT,
        },
    )
    assert rename(ra, sid, "my-crew") == (200, {"renamed": "no", "line": RENAME_TEXT["try_later"]})
    assert len(_sent(bff)) == 1 and _names(rega, sid) == ["crew-sandbox-01"]
    ga.check_not_stopped()  # the client goes on
    n = bff.count("ListSandboxes")
    _items(ra)
    assert bff.count("ListSandboxes") == n + 1


def test_any_other_5xx_on_a_rename_still_stops_the_client(bff, tmp_path, monkeypatch):
    (ra, _, ga, *_), _, sid = _homes(bff, tmp_path, monkeypatch)
    bff.queue(
        "UpdateSandbox",
        503,
        {"__type": "com.example.kiro#ServiceUnavailableException", "message": "later"},
    )
    assert rename(ra, sid, "my-crew")[1]["renamed"] == "no"
    with pytest.raises(GuardRefused):
        ga.check_not_stopped()


def test_a_box_not_the_callers_own_is_refused_with_no_call(bff, tmp_path, monkeypatch):
    routes, reg, guard, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")  # no record, not a Crew launch
    reg.rows["row-x"] = Row("row-x", "my-ide-box", ssh_host_for(sid))
    _items(routes)
    n = len(bff.requests)
    assert rename(routes, sid, "my-crew") == (404, {"error": "not a sandbox of this crew client"})
    assert len(bff.requests) == n and _names(reg, sid) == ["my-ide-box"]


def test_the_client_refuses_a_rename_of_a_box_not_its_own(bff, tmp_path, monkeypatch):
    routes, _, _, client, *_ = _env(bff, tmp_path, monkeypatch)
    sid = bff.add_other("RUNNING", "my-ide-box")
    client.list_sandboxes()
    with pytest.raises(GuardRefused):
        client.rename_sandbox(sid, f"my-crew-{TAG}")
    assert _sent(bff) == []


@pytest.mark.parametrize("name", ["My Crew", "a", "a--b", "1abc", "x" * 33, "crew_1", "", None])
def test_the_name_follows_the_crew_name_rule(bff, tmp_path, monkeypatch, name):
    (ra, rega, *_), _, sid = _homes(bff, tmp_path, monkeypatch)
    n = len(bff.requests)
    status, body = rename(ra, sid, name)
    assert status == 400 and "Crew name must be" in body["error"]
    assert len(bff.requests) == n and _names(rega, sid) == ["crew-sandbox-01"]


def test_a_box_whose_name_has_no_tag_gets_no_rename_and_no_call(bff, tmp_path, monkeypatch):
    # no dashboard-only rename anywhere
    (ra, rega, ga, *_), _, sid = _homes(bff, tmp_path, monkeypatch, listed="my-box")
    before = _names(rega, sid)
    names = ga.row_names()
    assert rename(ra, sid, "my-crew") == (200, {"renamed": "no", "line": RENAME_TEXT["no_shared"]})
    assert RENAME_TEXT["no_shared"] == "Rename failed: this crew has no shared name to rename."
    assert _sent(bff) == [] and _names(rega, sid) == before and "my-crew" not in before
    assert ga.row_names() == names
    _items(ra)
    assert _names(rega, sid) == before and _sent(bff) == []


def test_the_shared_name_already_sends_nothing(bff, tmp_path, monkeypatch):
    (ra, *_), _, sid = _homes(bff, tmp_path, monkeypatch, listed=f"crew-sandbox-01-{TAG}")
    assert rename(ra, sid, "crew-sandbox-01")[1]["renamed"] == "unchanged"
    assert _sent(bff) == []


def test_another_crews_name_here_is_refused(bff, tmp_path, monkeypatch):
    # a box with no tag gets no rename at all now, so the shared name's own row takes the case
    (ra, rega, *_), _, sid = _homes(bff, tmp_path, monkeypatch, listed=f"taken-name-{TAG}")
    rega.rows["row-other"] = Row("row-other", "taken-name", "crew-sbx-somewhere-else")
    assert rename(ra, sid, "taken-name") == (
        200,
        {"renamed": "no", "name": "taken-name", "line": RENAME_TEXT["taken"]},
    )
    assert rega.rows["row-other"].ssh_host == "crew-sbx-somewhere-else" and _sent(bff) == []


def test_a_taken_name_is_refused_before_the_shared_rename(bff, tmp_path, monkeypatch):
    # with the service's rename on (a 200 queued), a name another crew row
    # here has is refused first: 0 calls, so the box is never renamed everywhere but not here
    (ra, rega, ga, *_), (rb, regb, *_), sid = _homes(
        bff, tmp_path, monkeypatch, listed=f"crew-sandbox-01-{TAG}"
    )
    rega.rows["row-other"] = Row("row-other", "taken-name", "crew-sbx-somewhere-else")
    # the service with its switch on: a 200 that applies the name (consumed only by a call)
    bff.queue(
        "UpdateSandbox",
        200,
        {"execEnvironmentId": sid, "status": "RUNNING", "displayName": f"my-crew-{TAG}"},
    )
    names = ga.row_names()
    assert rename(ra, sid, "taken-name") == (200, {"renamed": "no", "line": RENAME_TEXT["taken"]})
    assert _sent(bff) == [] and bff.sandboxes[sid]["displayName"] == f"crew-sandbox-01-{TAG}"
    assert _names(rega, sid) == ["crew-sandbox-01"] and ga.row_names() == names
    assert rega.rows["row-other"].ssh_host == "crew-sbx-somewhere-else"
    _items(rb)
    assert _names(regb, sid) == ["crew-sandbox-01"]
    # a free name still renames shared
    assert rename(ra, sid, "my-crew")[1] == {"renamed": "everywhere", "name": "my-crew", "line": ""}
    assert len(_sent(bff)) == 1


def test_an_unreadable_registry_refuses_the_rename_with_no_call(bff, tmp_path, monkeypatch):
    # the checks' read fails closed, with a 200 queued
    (ra, rega, *_), _, sid = _homes(bff, tmp_path, monkeypatch, listed=f"crew-sandbox-01-{TAG}")
    bff.queue(
        "UpdateSandbox",
        200,
        {"execEnvironmentId": sid, "status": "RUNNING", "displayName": f"my-crew-{TAG}"},
    )

    def _unreadable():
        raise OSError("registry locked")

    monkeypatch.setattr(rega, "list", _unreadable)
    assert rename(ra, sid, "my-crew") == (200, {"renamed": "no", "line": RENAME_TEXT["no_row"]})
    assert _sent(bff) == [] and bff.sandboxes[sid]["displayName"] == f"crew-sandbox-01-{TAG}"


def test_no_row_here_is_refused_before_the_shared_rename(bff, tmp_path, monkeypatch):
    (ra, rega, *_), _, sid = _homes(bff, tmp_path, monkeypatch, listed=f"crew-sandbox-01-{TAG}")
    for rid in [r.id for r in rega.list() if r.ssh_host == ssh_host_for(sid)]:
        rega.rows.pop(rid)
    bff.queue(
        "UpdateSandbox",
        200,
        {"execEnvironmentId": sid, "status": "RUNNING", "displayName": f"my-crew-{TAG}"},
    )
    assert rename(ra, sid, "my-crew") == (200, {"renamed": "no", "line": RENAME_TEXT["no_row"]})
    assert _sent(bff) == [] and bff.sandboxes[sid]["displayName"] == f"crew-sandbox-01-{TAG}"


def test_the_rename_route_is_absent_with_the_switch_off(bff, tmp_path, monkeypatch):
    (ra, *_), _, sid = _homes(bff, tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    n = len(bff.requests)
    assert rename(ra, sid, "my-crew") == (404, {"error": "not found"})
    assert len(bff.requests) == n


def test_a_rename_never_touches_power_network_or_timeout(bff, tmp_path, monkeypatch):
    (ra, *_), _, sid = _homes(bff, tmp_path, monkeypatch)
    n = len(bff.requests)
    rename(ra, sid, "my-crew")
    assert [r["op"] for r in bff.requests[n:]] == ["UpdateSandbox"]
    assert set(_sent(bff)[0]) == {"execEnvironmentId", "displayName"}

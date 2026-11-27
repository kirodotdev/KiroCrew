"""Adding a managed environment another client of the same account made.

The add reads the List once for the current sign-in; only an exact listed id is recorded,
under that sign-in. Get, Stop, Resume and the ssh session then take it; Delete never does.
"""

from __future__ import annotations

import json
import stat
import sys

import pytest
from managed_env_fake_box import FakeBox, FakeRegistry
from managed_env_fake_service import FakeService
from test_managed_env_routes import call

from kiro_crew.managed_env.client import Credential, SandboxClient
from kiro_crew.managed_env.engine import SandboxLaunchEngine
from kiro_crew.managed_env.guard import ADOPTED_NAME, SandboxGuard
from kiro_crew.managed_env.remote import ssh_host_for
from kiro_crew.managed_env.routes import SandboxRoutes
from kiro_crew.managed_env.settings import LaneSettings

BASE = "/api/managed-environments"


@pytest.fixture
def svc():
    f = FakeService()
    yield f
    f.close()


@pytest.fixture
def lane(svc, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import _shared

    async def owner(request, operation):
        return None

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", owner)
    settings = LaneSettings(endpoint=svc.endpoint, ssh_region="ex-test-1")
    state = {"sign_in": "a", "ssh_block": True}
    client = SandboxClient(
        lambda: Credential("tok-secret", state["sign_in"]),
        SandboxGuard(tmp_path / "s"),
        settings=lambda: settings,
    )
    reg = FakeRegistry()
    engine = SandboxLaunchEngine(
        client=client,
        registry_factory=lambda: reg,
        run=FakeBox(),
        settings=lambda: settings,
        ssh_block=lambda: state["ssh_block"],
    )
    routes = SandboxRoutes(lambda: engine, lane_enabled=lambda: True)
    return routes, client, reg, state, tmp_path / "s"


def adopt(routes, sid, name="my-crew"):
    """POST the add with a JSON body (``call`` sends none)."""
    import asyncio

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.managed_env.routes import register_sandbox_routes

    async def go():
        app = web.Application()
        register_sandbox_routes(app, routes)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(f"{BASE}/{sid}/adopt", json={"name": name})
            return resp.status, await resp.json()

    return asyncio.run(go())


def test_an_add_of_a_listed_id_records_it_writes_the_crew_row_and_takes_actions(lane, svc):
    routes, client, reg, _, _ = lane
    sid = svc.add("RUNNING", "made-in-kiro-web")
    status, body = adopt(routes, sid)
    assert status == 200 and body == {"status": "RUNNING", "ssh_host": ssh_host_for(sid)}
    assert svc.ops() == ["ListSandboxes"]  # nothing else ran: nothing on the box either
    [row] = reg.list()
    assert row.name == "my-crew" and row.ssh_host == ssh_host_for(sid)
    assert row.provisioner_id == "kiro_managed_env"
    _, listed = call(routes, "GET", f"{BASE}/list")
    assert listed["sandboxes"] == [
        {
            "sandbox_id": sid,
            "status": "RUNNING",
            "own": False,
            "display_name": "made-in-kiro-web",
            "adopted": True,
            "ssh_host": ssh_host_for(sid),
        }
    ]
    assert call(routes, "POST", f"{BASE}/{sid}/ssh-session")[0] == 200
    assert call(routes, "POST", f"{BASE}/{sid}/stop") == (200, {"status": "STOPPING"})
    assert call(routes, "POST", f"{BASE}/{sid}/resume")[0] == 200
    assert client.get_sandbox(sid)["execEnvironmentId"] == sid
    # Add again: the row is updated, not doubled.
    assert adopt(routes, sid)[0] == 200 and len(reg.list()) == 1


def test_delete_refuses_an_added_id(lane, svc):
    routes, _, _, _, _ = lane
    sid = svc.add("RUNNING")
    assert adopt(routes, sid)[0] == 200
    status, body = call(routes, "POST", f"{BASE}/{sid}/delete")
    assert status == 409 and body["code"] == "not_own_id"
    assert "DeleteSandbox" not in svc.ops() and svc.sandboxes[sid]["status"] == "RUNNING"


def test_an_id_the_list_does_not_show_is_refused_and_nothing_recorded(lane, svc):
    routes, client, reg, _, state_dir = lane
    status, body = adopt(routes, "env-404")
    assert status == 409 and body["code"] == "not_listed"
    assert svc.ops() == ["ListSandboxes"] and reg.list() == []
    assert not (state_dir / ADOPTED_NAME).exists()
    assert call(routes, "POST", f"{BASE}/env-404/stop")[1]["code"] == "not_own_id"


def test_a_display_name_never_matches_an_id(lane, svc):
    routes, _, reg, _, _ = lane
    svc.add("RUNNING", "env-777")  # its name looks like an id; its id is env-001
    assert adopt(routes, "env-777")[1]["code"] == "not_listed"
    assert reg.list() == []


@pytest.mark.parametrize("status", ["DELETED", "DELETING", "FAILED"])
def test_an_environment_on_its_way_out_is_not_added(lane, svc, status):
    routes, _, reg, _, _ = lane
    sid = svc.add(status)
    assert adopt(routes, sid)[1]["code"] == "not_listed"
    assert reg.list() == []


def test_after_a_token_rotation_an_added_id_works_through_one_live_recheck(lane, svc):
    routes, client, _, state, state_dir = lane
    sid = svc.add("RUNNING")
    assert adopt(routes, sid)[0] == 200
    state["sign_in"] = "a-rotated"  # same person, the refresh issued a new refresh token
    sent = len(svc.requests)
    assert call(routes, "POST", f"{BASE}/{sid}/stop") == (200, {"status": "STOPPING"})
    assert svc.ops()[sent:] == ["ListSandboxes", "StopSandbox"]
    assert json.loads((state_dir / ADOPTED_NAME).read_text(encoding="utf-8")) == {
        "ids": {sid: "a-rotated"}
    }
    # Rebound: the next call needs no recheck.
    assert call(routes, "POST", f"{BASE}/{sid}/resume")[0] == 200
    assert svc.ops()[-1] == "ResumeSandbox" and svc.ops().count("ListSandboxes") == 2
    # The List route makes the same check, so a rotated row reads added again.
    state["sign_in"] = "a-rotated-twice"
    _, listed = call(routes, "GET", f"{BASE}/list")
    assert listed["sandboxes"][0]["adopted"] is True


def test_a_different_account_whose_list_lacks_the_id_is_refused_and_the_record_kept(lane, svc):
    routes, client, _, state, state_dir = lane
    sid = svc.add("RUNNING")
    assert adopt(routes, sid)[0] == 200
    state["sign_in"] = "b"
    sent = len(svc.requests)
    for action in ("stop", "resume", "ssh-session"):
        svc.answer("ListSandboxes", 200, {"sandboxes": []})  # account b lists none of a's
        status, body = call(routes, "POST", f"{BASE}/{sid}/{action}")
        assert status == 409 and body["code"] == "not_own_id"
    # Only the rechecks went out with b's bearer; no action on the environment.
    assert svc.ops()[sent:] == ["ListSandboxes"] * 3
    assert svc.sandboxes[sid]["status"] == "RUNNING"
    assert json.loads((state_dir / ADOPTED_NAME).read_text(encoding="utf-8")) == {"ids": {sid: "a"}}
    # A listed id on its way out is not rebound either.
    svc.answer(
        "ListSandboxes", 200, {"sandboxes": [{"execEnvironmentId": sid, "status": "DELETING"}]}
    )
    assert call(routes, "POST", f"{BASE}/{sid}/stop")[1]["code"] == "not_own_id"


def test_a_sign_in_that_changes_between_list_pages_adds_nothing(lane, svc):
    routes, client, _, _, state_dir = lane
    svc.page_size = 1
    svc.add("RUNNING")
    sid = svc.add("RUNNING")  # on the second page
    seen = iter(["a", "b"])
    client._credential = lambda: Credential("tok-secret", next(seen))
    assert adopt(routes, sid)[1]["code"] == "not_listed"
    assert not (state_dir / ADOPTED_NAME).exists()


@pytest.mark.parametrize("name", ["", "Bad Name", "x", None])
def test_a_bad_crew_name_is_refused_before_any_call(lane, svc, name):
    routes, _, reg, _, _ = lane
    sid = svc.add("RUNNING")
    status, body = adopt(routes, sid, name)
    assert status == 400 and body["code"] == "bad_crew_name"
    assert svc.requests == [] and reg.list() == []


def test_an_add_needs_the_ssh_settings_first(lane, svc):
    routes, _, reg, state, state_dir = lane
    state["ssh_block"] = False
    status, body = adopt(routes, svc.add("RUNNING"))
    assert status == 409 and body["code"] == "ssh_config_missing"
    assert svc.requests == [] and reg.list() == []
    assert not (state_dir / ADOPTED_NAME).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_added_ids_file_is_0600_and_holds_no_token(lane, svc):
    routes, _, _, _, state_dir = lane
    sid = svc.add("RUNNING")
    assert adopt(routes, sid)[0] == 200
    path = state_dir / ADOPTED_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"ids": {sid: "a"}}
    assert "tok-secret" not in path.read_text(encoding="utf-8")


def test_a_damaged_added_ids_file_refuses_added_ids_and_keeps_own_ones(lane, svc):
    routes, client, _, _, state_dir = lane
    own = client.create_sandbox("crew-a-kc-0a1b2c")["execEnvironmentId"]
    svc.sandboxes[own]["status"] = "RUNNING"
    other = svc.add("RUNNING")
    (state_dir / ADOPTED_NAME).write_text("[", encoding="utf-8")
    assert call(routes, "POST", f"{BASE}/{other}/stop")[1]["code"] == "state_corrupt"
    assert call(routes, "POST", f"{BASE}/{own}/stop") == (200, {"status": "STOPPING"})
    # The List still shows every environment; the damaged file adds none.
    _, listed = call(routes, "GET", f"{BASE}/list")
    assert {i["sandbox_id"]: "adopted" in i for i in listed["sandboxes"]} == {
        own: False,
        other: False,
    }

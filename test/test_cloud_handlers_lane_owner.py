"""The managed-environment lane's generic cloud routes are the machine owner's alone.

A Shared Crew teammate passes the dashboard owner gate (``_guard``). On the
``kiro_managed_env`` lane the cloud routes then refuse a request that passed only as a
teammate (``teammate_access.teammate_passes_owner_gate``) with 403 ``owner_only``. The
built-in EC2 lane keeps the owner gate alone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import make_mocked_request
from test_cloud_handlers import _body, _posix_host, _req, _state  # noqa: F401
from test_shared_crew_teammate_owner_access import OWNER, _publish

from kiro_crew.cloud import launch_job as lj
from kiro_crew.dashboard import handlers_cloud as hc
from kiro_crew.managed_env import PROVISIONER_ID


def _as(who, method, path, *, state, body=None, match_info=None):
    """A request carrying the real claims the SSM gate and token auth publish for *who*."""
    from aiohttp import web

    app = web.Application()
    app["state"] = state
    req = make_mocked_request(method, path, app=app, match_info=match_info or {})
    _publish(req, who)
    if body is not None:

        async def _json():
            return body

        req.json = _json  # type: ignore[assignment]
    return req


@pytest.mark.asyncio
async def test_a_real_teammate_is_refused_on_the_lane_and_unchanged_on_ec2(tmp_path, monkeypatch):
    from kiro_crew.shared_crew import owner
    from kiro_crew.shared_crew.teammate_access import teammate_passes_owner_gate

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(owner, "_owner_subject", OWNER)
    lane = type("P", (), {"id": PROVISIONER_ID, "kind": PROVISIONER_ID, "posix_only": False})()
    monkeypatch.setattr(hc, "_provisioners", lambda: [lane])
    monkeypatch.setattr(hc, "_engine", lambda *a, **k: pytest.fail("no engine for a teammate"))
    state = _state(tmp_path)
    state.owner_id = OWNER
    body = {"provider_id": PROVISIONER_ID, "profile": "crew-a", "region": "", "size_key": ""}
    req = _as("ssm", "POST", "/api/cloud/launch", state=state, body=body)
    assert teammate_passes_owner_gate(req)
    resp = await hc.api_cloud_launch_create(req)
    assert (resp.status, _body(resp)["code"]) == (403, "owner_only")
    assert list(state.cloud_launch_store.root.glob("*.json")) == []  # no job written

    job = state.cloud_launch_store.create(
        profile="crew-a", region="", size_key="", provider_id=PROVISIONER_ID
    )
    job.tag = "kc-0a1b2c"
    state.cloud_launch_store.save(job)
    tag = {"tag": "kc-0a1b2c"}
    resp = await hc.api_cloud_destroy(
        _as("ssm", "DELETE", "/api/cloud/kc-0a1b2c", state=state, match_info=tag)
    )
    assert (resp.status, _body(resp)["code"]) == (403, "owner_only")

    from kiro_crew.dashboard.handlers_cloud_signin_skip import api_cloud_launch_signin_skip

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers_cloud_signin_skip.request_skip",
        lambda *a: pytest.fail("a teammate's skip reached the launch"),
    )
    path = f"/api/cloud/launch/{job.id}/signin/skip"
    resp = await api_cloud_launch_signin_skip(
        _as("ssm", "POST", path, state=state, match_info={"id": job.id})
    )
    assert (resp.status, _body(resp)["code"]) == (403, "owner_only")

    # The machine owner passes the same lane check and reaches the lane's own answer.
    resp = await hc.api_cloud_destroy(
        _as("owner", "DELETE", "/api/cloud/kc-0a1b2c", state=state, match_info=tag)
    )
    assert (resp.status, _body(resp)["code"]) == (409, "lane_delete_elsewhere")

    # EC2 is unchanged: the same teammate's delete goes on to the EC2 path.
    reached: list[str] = []
    monkeypatch.setattr(hc.ec2, "destroy", lambda *a, **k: reached.append("ec2.destroy") or {})
    monkeypatch.setattr(hc.ec2, "describe", lambda *a, **k: {"instance_id": "i-0abc"})
    monkeypatch.setattr(hc, "_start_teardown_watch", lambda *a, **k: None)
    ec2 = state.cloud_launch_store.create(profile="dev", region="us-east-1", size_key="balanced")
    ec2.tag = "kc-ec2aaa"
    state.cloud_launch_store.save(ec2)
    resp = await hc.api_cloud_destroy(
        _as("ssm", "DELETE", "/api/cloud/kc-ec2aaa", state=state, match_info={"tag": "kc-ec2aaa"})
    )
    assert resp.status == 200, _body(resp)
    assert reached == ["ec2.destroy"]


@pytest.fixture
def registered_chain(tmp_path, monkeypatch):
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.testing.ids import UNALLOCATABLE_PID

    state = _state(tmp_path)
    reg = InstancesRegistry()
    reg.add(name="parent", ssh_host="parent.example")
    reg.add(
        name="child",
        ssh_host="child.example",
        via_instance_id="parent",
        via_remote_id="child",
        via_remote_port=5476,
    )
    for row in reg.list():
        reg.update(
            row.id,
            was_connected=True,
            forwarder_pid=UNALLOCATABLE_PID,
            forwarder_start="recorded",
        )
    state.instances_registry = reg
    connected = {"parent", "child"}
    events = []

    async def disconnect(cid, *, keep_intent):
        assert keep_intent is True
        assert {row.id for row in reg.list()} == {"parent", "child"}
        events.append(cid)
        connected.remove(cid)

    state.instances_manager = SimpleNamespace(disconnect=disconnect)

    def teardown(**kwargs):
        events.append("teardown")
        reg.remove_cascade("parent")
        return True

    state.cloud_launch_engine = SimpleNamespace(teardown=Mock(side_effect=teardown))
    unregister = Mock(wraps=hc.connect_mod.unregister_instance)
    monkeypatch.setattr(hc.connect_mod, "unregister_instance", unregister)
    return state, reg, connected, events, unregister


@pytest.mark.asyncio
async def test_owner_generic_destroy_on_the_lane_is_refused_and_deletes_nothing(
    registered_chain, monkeypatch
):
    """The lane deletes from its own Remote Crew section; the generic route touches nothing."""
    state, reg, connected, events, unregister = registered_chain
    before = reg.list()
    audited = Mock()
    monkeypatch.setattr(hc, "_audit", audited)
    job = state.cloud_launch_store.create(
        profile="dev", region="us-east-1", size_key="", provider_id=PROVISIONER_ID
    )
    job.tag = "kc-chain"
    job.instance_id = "parent.example"
    state.cloud_launch_store.save(job)

    resp = await hc.api_cloud_destroy(
        _req("DELETE", "/api/cloud/kc-chain", state=state, match_info={"tag": "kc-chain"})
    )

    assert resp.status == 409, _body(resp)
    assert _body(resp)["code"] == "lane_delete_elsewhere"
    assert "Delete environment on its Remote Crew row" in _body(resp)["error"]
    assert "Nothing was deleted" in _body(resp)["error"]
    assert events == []
    assert connected == {"parent", "child"}
    assert reg.list() == before
    state.cloud_launch_engine.teardown.assert_not_called()
    unregister.assert_not_called()
    audited.assert_called_once_with(
        "destroy", "denied", request_id="kc-chain", error=f"lane {PROVISIONER_ID} deletes elsewhere"
    )


@pytest.mark.asyncio
async def test_generic_destroy_of_another_lane_keeps_main_behaviour(registered_chain):
    """Only the managed-environment lane refuses the generic delete; other lanes are unchanged."""
    state, reg, connected, events, unregister = registered_chain
    job = state.cloud_launch_store.create(
        profile="dev", region="us-east-1", size_key="", provider_id="microvm"
    )
    job.tag = "kc-chain"
    job.instance_id = "parent.example"
    state.cloud_launch_store.save(job)

    resp = await hc.api_cloud_destroy(
        _req("DELETE", "/api/cloud/kc-chain", state=state, match_info={"tag": "kc-chain"})
    )

    assert resp.status == 200, _body(resp)
    assert events == ["teardown"]
    state.cloud_launch_engine.teardown.assert_called_once()


@pytest.fixture
def lane_and_ec2_jobs(tmp_path, monkeypatch):
    """A real-teammate setup holding one managed-environment job and one EC2 job."""
    from kiro_crew.shared_crew import owner

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(owner, "_owner_subject", OWNER)
    state = _state(tmp_path)
    state.owner_id = OWNER
    lane = state.cloud_launch_store.create(
        profile="crew-a", region="", size_key="", provider_id=PROVISIONER_ID
    )
    ec2 = state.cloud_launch_store.create(profile="dev", region="us-east-1", size_key="balanced")
    return state, lane, ec2


@pytest.mark.asyncio
async def test_a_real_teammate_cannot_read_a_lane_job_and_reads_ec2_unchanged(lane_and_ec2_jobs):
    from kiro_crew.shared_crew.teammate_access import teammate_passes_owner_gate

    state, lane, ec2 = lane_and_ec2_jobs

    def get(who, job_id):
        path = f"/api/cloud/launch/{job_id}"
        return _as(who, "GET", path, state=state, match_info={"id": job_id})

    req = get("ssm", lane.id)
    assert teammate_passes_owner_gate(req)
    resp = await hc.api_cloud_launch_get(req)
    assert (resp.status, _body(resp)["code"]) == (403, "owner_only")

    resp = await hc.api_cloud_launch_get(get("owner", lane.id))
    assert resp.status == 200, _body(resp)
    assert _body(resp)["id"] == lane.id

    resp = await hc.api_cloud_launch_get(get("ssm", ec2.id))
    assert resp.status == 200, _body(resp)
    assert _body(resp)["id"] == ec2.id


@pytest.mark.asyncio
async def test_a_real_teammate_list_omits_lane_jobs_and_keeps_ec2(lane_and_ec2_jobs):
    state, lane, ec2 = lane_and_ec2_jobs

    resp = await hc.api_cloud_launch_list(_as("ssm", "GET", "/api/cloud/launch", state=state))
    assert resp.status == 200, _body(resp)
    assert [j["id"] for j in _body(resp)["jobs"]] == [ec2.id]

    resp = await hc.api_cloud_launch_list(_as("owner", "GET", "/api/cloud/launch", state=state))
    assert resp.status == 200, _body(resp)
    assert {j["id"] for j in _body(resp)["jobs"]} == {lane.id, ec2.id}


def _running(state, **kw):
    """A non-terminal job a worker in this process owns, with a live device code."""
    job = state.cloud_launch_store.create(**kw)
    job.status = lj.AWAITING_SIGNIN
    job.signin = lj.SigninPrompt(url="https://x/verify-lane", code="LANE-CODE", ports=[54123])
    state.cloud_launch_store.save(job)
    state.cloud_launch_store.adopt(job.id)
    return job


@pytest.fixture
def owner_lane_running(tmp_path, monkeypatch):
    """A real-teammate setup while the owner's managed-environment launch is signing in."""
    from kiro_crew.shared_crew import owner

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(owner, "_owner_subject", OWNER)
    state = _state(tmp_path)
    state.owner_id = OWNER
    lane = _running(state, profile="crew-a", region="", size_key="", provider_id=PROVISIONER_ID)
    return state, lane


def _assert_no_lane_job(body, lane):
    assert "job" not in body and "signin" not in body
    text = json.dumps(body)
    assert lane.id not in text and "verify-lane" not in text and "LANE-CODE" not in text


_EC2_BODY = {"profile": "dev", "region": "us-east-1", "size_key": "balanced"}


@pytest.mark.asyncio
async def test_a_teammate_ec2_launch_colliding_with_the_lane_gets_no_lane_job(owner_lane_running):
    state, lane = owner_lane_running

    resp = await hc.api_cloud_launch_create(
        _as("ssm", "POST", "/api/cloud/launch", state=state, body=dict(_EC2_BODY))
    )
    assert (resp.status, _body(resp)["code"]) == (409, "launch_already_running")
    _assert_no_lane_job(_body(resp), lane)

    resp = await hc.api_cloud_launch_create(
        _as("owner", "POST", "/api/cloud/launch", state=state, body=dict(_EC2_BODY))
    )
    assert (resp.status, _body(resp)["code"]) == (409, "launch_already_running")
    assert _body(resp)["job"]["id"] == lane.id
    assert _body(resp)["job"]["signin"]["code"] == "LANE-CODE"
    assert len(state.cloud_launch_store.list()) == 1


@pytest.mark.asyncio
async def test_a_teammate_ec2_signin_restart_during_the_lane_launch_gets_no_lane_job(
    owner_lane_running,
):
    state, lane = owner_lane_running
    crew = state.cloud_launch_store.create(**_EC2_BODY)
    crew.status = lj.DONE
    crew.instance_id = "i-0ec2crew"
    crew.step(lj.STEP_CONNECT).state = lj.STEP_DONE
    state.cloud_launch_store.save(crew)

    def restart(who):
        path = f"/api/cloud/launch/{crew.id}/signin/restart"
        return hc.api_cloud_launch_signin_restart(
            _as(who, "POST", path, state=state, match_info={"id": crew.id})
        )

    resp = await restart("ssm")
    assert (resp.status, _body(resp)["code"]) == (409, "launch_already_running")
    _assert_no_lane_job(_body(resp), lane)

    resp = await restart("owner")
    assert (resp.status, _body(resp)["code"]) == (409, "launch_already_running")
    assert _body(resp)["job"]["id"] == lane.id


@pytest.mark.asyncio
async def test_a_teammate_ec2_collision_with_an_ec2_launch_still_names_it(tmp_path, monkeypatch):
    from kiro_crew.shared_crew import owner

    monkeypatch.setenv("KIROCREW_SHARED_CREW_PREVIEW", "1")
    monkeypatch.setattr(owner, "_owner_subject", OWNER)
    state = _state(tmp_path)
    state.owner_id = OWNER
    running = _running(state, **_EC2_BODY)

    resp = await hc.api_cloud_launch_create(
        _as("ssm", "POST", "/api/cloud/launch", state=state, body=dict(_EC2_BODY))
    )
    assert (resp.status, _body(resp)["code"]) == (409, "launch_already_running")
    assert _body(resp)["job"]["id"] == running.id
    assert _body(resp)["job"]["signin"]["code"] == "LANE-CODE"

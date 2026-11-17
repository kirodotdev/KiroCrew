"""A cloud crew stopped from the panel reads as Stopped, not as a broken tunnel.

``POST /api/cloud/{tag}/stop`` marks the crew's Instances row ``cloud_stopped`` and
closes its tunnel; ``.../start`` clears the mark. While it is set, every connect
path refuses the crew and the startup revive skips it, so nothing dials an
instance the user just parked.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.cloud import launch_job as lj
from kiro_crew.dashboard import handlers_cloud as hc
from kiro_crew.dashboard import handlers_instances as handlers
from kiro_crew.instances.registry import Instance, InstancesRegistry, InstanceStoppedError
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState, TunnelStatus

IID = "i-0abc123456789def0"
TAG = "kc-3f9a"


@pytest.fixture(autouse=True)
def _posix_host(_floor_monkeypatch):
    _floor_monkeypatch.setattr(hc.sys, "platform", "linux")


class _Tunnel:
    built: list[str] = []

    def __init__(self, iid, ssh_host, lp, rp, **_kwargs):
        _Tunnel.built.append(iid)
        self.pid = None
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        self.status.state = TunnelState.CONNECTED
        return True

    async def stop(self):
        self.status.state = TunnelState.STOPPED


async def _mint(host, **_kwargs):
    return "TOK"


class _Manager:
    """Records the locked teardown a stop asks for and applies its write."""

    def __init__(self):
        self.reconfigured: list[str] = []

    async def reconfigure(self, instance_id, apply, needs_teardown=None):
        self.reconfigured.append(instance_id)
        return apply()


def _cloud_state(tmp_path: Path, reg, mgr=None):
    store = lj.LaunchJobStore(root=tmp_path / "launch-jobs")
    job = store.create(profile="", region="", size_key="balanced")
    job.tag = TAG
    job.instance_id = IID
    store.save(job)
    return SimpleNamespace(
        owner_id="owner-1",
        cloud_launch_sync=True,
        cloud_launch_store=store,
        instances_registry=reg,
        instances_manager=mgr,
    )


def _cloud_req(op, state):
    app = web.Application()
    app["state"] = state
    req = make_mocked_request("POST", f"/api/cloud/{TAG}/{op}", app=app, match_info={"tag": TAG})
    req["user"] = "owner-1"
    req["app"] = ""
    return req


def _ssm_registry(tmp_path: Path) -> InstancesRegistry:
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(name="Cloud", connection_method="ssm", ssm_target=IID, instance_id="cloud")
    reg.add(name="Other", ssh_host="other", instance_id="other")
    return reg


def test_flag_defaults_off_and_round_trips(tmp_path):
    assert Instance.from_dict({"id": "a", "name": "A"}).cloud_stopped is False
    assert (
        Instance.from_dict({"id": "a", "name": "A", "cloud_stopped": "true"}).cloud_stopped is False
    )
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(name="A", ssh_host="a", instance_id="a")
    reg.update("a", cloud_stopped=True)
    again = InstancesRegistry(path=tmp_path / "instances.json").get("a")
    assert again is not None and again.to_dict()["cloud_stopped"] is True


@pytest.mark.asyncio
async def test_stop_marks_the_crew_and_tears_its_tunnel_down_and_start_clears_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(hc.ec2, "stop", lambda *a, **k: {"action": "stop", "instance_id": IID})
    monkeypatch.setattr(hc.ec2, "start", lambda *a, **k: {"action": "start", "instance_id": IID})
    reg = _ssm_registry(tmp_path)
    mgr = _Manager()
    state = _cloud_state(tmp_path, reg, mgr)

    stopped = await hc.api_cloud_stop(_cloud_req("stop", state))
    assert stopped.status == 200
    assert reg.get("cloud").cloud_stopped is True
    assert mgr.reconfigured == ["cloud"], "the stop left the tunnel to keep redialling"
    assert reg.get("other").cloud_stopped is False

    started = await hc.api_cloud_start(_cloud_req("start", state))
    assert started.status == 200
    assert reg.get("cloud").cloud_stopped is False
    assert mgr.reconfigured == ["cloud"], "a start has nothing to tear down"


@pytest.mark.asyncio
async def test_stop_and_start_reach_every_row_sharing_the_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.ec2, "stop", lambda *a, **k: {"action": "stop", "instance_id": IID})
    monkeypatch.setattr(hc.ec2, "start", lambda *a, **k: {"action": "start", "instance_id": IID})
    reg = _ssm_registry(tmp_path)
    reg.add(name="Twin", connection_method="ssm", ssm_target=IID, instance_id="twin")
    mgr = _Manager()
    state = _cloud_state(tmp_path, reg, mgr)

    await hc.api_cloud_stop(_cloud_req("stop", state))
    assert reg.get("cloud").cloud_stopped is True
    assert reg.get("twin").cloud_stopped is True
    assert sorted(mgr.reconfigured) == ["cloud", "twin"]
    assert reg.get("other").cloud_stopped is False

    await hc.api_cloud_start(_cloud_req("start", state))
    assert reg.get("cloud").cloud_stopped is False
    assert reg.get("twin").cloud_stopped is False


@pytest.mark.asyncio
async def test_the_row_marked_is_the_instance_aws_acted_on(tmp_path, monkeypatch):
    acted_on = "i-0def456789abc0123"
    monkeypatch.setattr(hc.ec2, "stop", lambda *a, **k: {"action": "stop", "instance_id": acted_on})
    reg = _ssm_registry(tmp_path)
    reg.add(name="Same tag", connection_method="ssm", ssm_target=acted_on, instance_id="same")
    state = _cloud_state(tmp_path, reg, _Manager())

    await hc.api_cloud_stop(_cloud_req("stop", state))
    assert reg.get("same").cloud_stopped is True
    assert reg.get("cloud").cloud_stopped is False, "the job's own instance was not stopped"


@pytest.mark.asyncio
async def test_an_unsaved_flag_is_reported_not_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(hc.ec2, "stop", lambda *a, **k: {"action": "stop", "instance_id": IID})
    reg = _ssm_registry(tmp_path)

    def _disk_full(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr(reg, "update", _disk_full)
    resp = await hc.api_cloud_stop(_cloud_req("stop", _cloud_state(tmp_path, reg, _Manager())))
    assert resp.status == 500
    body = json.loads(resp.body.decode())
    assert body["code"] == "cloud_state_not_saved"
    assert body["action_completed"] is True


@pytest.mark.asyncio
async def test_a_failed_stop_leaves_the_crew_unmarked(tmp_path, monkeypatch):
    def _refused(*_a, **_k):
        raise hc.AWSError("denied")

    monkeypatch.setattr(hc.ec2, "stop", _refused)
    reg = _ssm_registry(tmp_path)
    state = _cloud_state(tmp_path, reg, _Manager())
    resp = await hc.api_cloud_stop(_cloud_req("stop", state))
    assert resp.status == 502
    assert reg.get("cloud").cloud_stopped is False


@pytest.fixture
def ssh_env(tmp_path: Path, monkeypatch):
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm
    from kiro_crew.config import loader

    monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"instances": {"enabled": True}}), encoding="utf-8"
    )
    loader._invalidate_config_cache()
    _Tunnel.built = []
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
    mgr = SshTunnelManager(reg, base_port=53400, mint_token=_mint, tunnel_factory=_Tunnel)
    return reg, mgr


class _Req:
    def __init__(self, state, *, match):
        self.app = {"state": state}
        self.headers: dict = {}
        self.match_info = match
        self.query: dict = {}
        self._attrs = {"app": "", "user": "owner"}

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


@pytest.mark.asyncio
async def test_connect_refuses_a_stopped_crew_with_its_own_code(ssh_env):
    reg, mgr = ssh_env
    reg.update("cd-1", cloud_stopped=True)
    with pytest.raises(InstanceStoppedError):
        await mgr.connect("cd-1")
    state = SimpleNamespace(owner_id="owner", instances_registry=reg, instances_manager=mgr)
    r = await handlers.api_instances_connect(_Req(state, match={"id": "cd-1"}))
    assert r.status == 409
    body = json.loads(r.body.decode())
    assert body["code"] == "instance_stopped"
    assert "start it" in body["error"]
    assert _Tunnel.built == []


@pytest.mark.asyncio
async def test_startup_revive_skips_a_stopped_crew(ssh_env):
    import kiro_crew.dashboard.server as server

    reg, mgr = ssh_env
    reg.update("cd-1", was_connected=True, cloud_stopped=True)
    await server._revive_intended_instances(reg, mgr)
    assert _Tunnel.built == []
    assert reg.get("cd-1").was_connected is True
    await mgr.shutdown()

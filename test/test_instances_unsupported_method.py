"""A crew whose ``connection_method`` this build does not know is refused, not dialled.

Records load without full validation, so a hand-edited or newer-build value
(``outbound``) reaches the gateway. Every transport path must refuse it with the
typed ``connection_method_unsupported`` reason instead of treating it as SSH and
dialling whatever ``ssh_host`` the record holds.

Driven through the real ``SshTunnelManager`` (fake tunnel factory and mint) and
the real connect route.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.dashboard import handlers_instances as handlers
from kiro_crew.instances.registry import Instance, InstancesRegistry
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState, TunnelStatus
from kiro_crew.instances.validation import SshValidationError, UnsupportedConnectionMethodError

_TASK = "0123456789abcdef0123456789abcdef"


class _Tunnel:
    """Fake forwarder: records that it was built, starts CONNECTED."""

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


class _Req:
    def __init__(self, state, *, match=None, query=None):
        self.app = {"state": state}
        self.headers: dict = {}
        self.match_info = match or {}
        self.query = query or {}
        self._attrs = {"app": "", "user": "owner"}

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


class _State:
    owner_id = "owner"

    def __init__(self, registry, manager):
        self.instances_registry = registry
        self.instances_manager = manager


def _set_method_on_disk(path: Path, instance_id: str, method: str | None) -> None:
    """Rewrite one record's method the way a hand edit or a newer build would."""
    data = json.loads(path.read_text())
    rows = data["instances"] if isinstance(data, dict) else data
    for row in rows:
        if row["id"] == instance_id:
            if method is None:
                row.pop("connection_method", None)
            else:
                row["connection_method"] = method
    path.write_text(json.dumps(data))


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm
    from kiro_crew.config import loader

    monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": True}}))
    loader._invalidate_config_cache()
    _Tunnel.built = []
    path = tmp_path / "instances.json"
    InstancesRegistry(path=path).add(name="CD", ssh_host="stale-host", instance_id="cd-1")
    _set_method_on_disk(path, "cd-1", "outbound")
    reg = InstancesRegistry(path=path)
    mgr = SshTunnelManager(reg, base_port=53400, mint_token=_mint, tunnel_factory=_Tunnel)
    return reg, mgr, _State(reg, mgr)


def test_the_record_loads_with_the_unknown_method(env):
    """The premise: the registry hands the gateway a method it cannot dial."""
    reg, _, _ = env
    inst = reg.get("cd-1")
    assert inst is not None and inst.connection_method == "outbound"


@pytest.mark.parametrize("method", ["outbound", "OUTBOUND", " tailscale ", "ssh2"])
def test_resolve_transport_refuses_an_unknown_method(method):
    mgr = SshTunnelManager.__new__(SshTunnelManager)
    inst = Instance(id="x", name="X", ssh_host="stale-host", connection_method=method)
    with pytest.raises(UnsupportedConnectionMethodError) as exc:
        mgr._resolve_transport(inst)
    assert exc.value.code == "connection_method_unsupported"
    # Every caller that already catches transport-validation errors refuses it too.
    assert isinstance(exc.value, SshValidationError)


@pytest.mark.parametrize("method", ["ssh", "SSH", "", "  "])
def test_an_absent_or_ssh_method_still_resolves_to_ssh(method):
    mgr = SshTunnelManager.__new__(SshTunnelManager)
    inst = Instance(id="x", name="X", ssh_host="box", connection_method=method)
    assert mgr._resolve_transport(inst).method == "ssh"


@pytest.mark.parametrize(
    ("method", "target"),
    [
        ("ssm", "i-0123456789abcdef0"),
        ("fargate", f"ecs:crews_{_TASK}_{_TASK}-1234567890"),
    ],
)
def test_ssm_and_fargate_still_resolve(method, target):
    mgr = SshTunnelManager.__new__(SshTunnelManager)
    inst = Instance(id="x", name="X", connection_method=method, ssm_target=target)
    assert mgr._resolve_transport(inst).method == method


@pytest.mark.asyncio
async def test_connect_refuses_and_spawns_nothing(env):
    _, mgr, _ = env
    with pytest.raises(UnsupportedConnectionMethodError):
        await mgr.connect("cd-1")
    assert _Tunnel.built == []
    assert mgr.status("cd-1") is None


@pytest.mark.asyncio
async def test_connect_route_answers_with_the_typed_refusal(env):
    _, _, state = env
    r = await handlers.api_instances_connect(_Req(state, match={"id": "cd-1"}))
    body = json.loads(r.body.decode())
    assert r.status == 409
    assert body["code"] == "connection_method_unsupported"
    assert "outbound" in body["error"]
    assert "token" not in body
    assert _Tunnel.built == []


@pytest.mark.asyncio
async def test_a_record_without_a_method_still_connects_over_ssh(env, tmp_path):
    reg, mgr, _ = env
    _set_method_on_disk(tmp_path / "instances.json", "cd-1", None)
    inst = reg.get("cd-1")
    assert inst is not None and inst.connection_method == "ssh"
    assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
    assert _Tunnel.built == ["cd-1"]


@pytest.mark.asyncio
async def test_diagnose_reports_the_method_and_runs_no_ssh_probe(env, monkeypatch):
    import kiro_crew.instances.ssh_tunnel_manager as stm

    _, mgr, _ = env
    dialled: list[str] = []

    async def _ssh_probe(host, *args, **kwargs):
        dialled.append(host)
        raise AssertionError("the SSH ladder must not run for an unknown method")

    monkeypatch.setattr(stm, "diagnose_instance", _ssh_probe)
    diag = await mgr.diagnose("cd-1")
    assert diag is not None
    assert diag["code"] == "connection_method_unsupported"
    assert diag["ok"] is False
    assert "outbound" in diag["reason"]
    assert dialled == []

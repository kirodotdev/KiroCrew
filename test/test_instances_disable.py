"""Per-crew enable/disable.

A disabled crew keeps its record and its connection intent, but no tunnel is
opened for it: disabling tears the live tunnel down, every connect path is
refused, and the startup revive skips it. Enabling it again lets it reconnect.

Driven through the real ``SshTunnelManager`` (with a fake tunnel factory and
mint) and the real handlers, so the teardown runs through the manager's own
``reconfigure`` lock rather than a stub of it. A transport edit shares that
locked path, so its race against the route's pre-lock read is pinned here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.dashboard import handlers_instances as handlers
from kiro_crew.instances.registry import Instance, InstanceDisabledError, InstancesRegistry
from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager, TunnelState, TunnelStatus


class _Tunnel:
    """Fake forwarder: starts CONNECTED, records its stop."""

    built: list[str] = []

    def __init__(self, iid, ssh_host, lp, rp, **_kwargs):
        _Tunnel.built.append(iid)
        self.pid = None
        self.stopped = False
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        self.status.state = TunnelState.CONNECTED
        return True

    async def stop(self):
        self.stopped = True
        self.status.state = TunnelState.STOPPED


async def _mint(host, **_kwargs):
    return "TOK"


class _Req:
    def __init__(self, state, *, match=None, body=None, query=None, user="owner"):
        self.app = {"state": state}
        self.headers: dict = {}
        self.match_info = match or {}
        self.query = query or {}
        self._body = body
        self._attrs = {"app": ""}
        if user is not None:
            self._attrs["user"] = user

    def get(self, key, default=None):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]

    async def json(self):
        return self._body


class _State:
    owner_id = "owner"

    def __init__(self, registry, manager):
        self.instances_registry = registry
        self.instances_manager = manager


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm
    from kiro_crew.config import loader

    # Hermetic ports: both namespaces that probe must answer "free".
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
    return reg, mgr, _State(reg, mgr)


def _body(resp):
    return json.loads(resp.body.decode())


def test_record_defaults_to_enabled_and_round_trips(tmp_path):
    """A record written before the field existed reads as enabled."""
    assert Instance.from_dict({"id": "a", "name": "A"}).disabled is False
    assert Instance.from_dict({"id": "a", "name": "A", "disabled": "false"}).disabled is False
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(name="A", ssh_host="a", instance_id="a")
    reg.update("a", disabled=True)
    again = InstancesRegistry(path=tmp_path / "instances.json").get("a")
    assert again is not None and again.disabled is True
    assert again.to_dict()["disabled"] is True


@pytest.mark.asyncio
async def test_connect_refuses_a_disabled_crew_and_spawns_nothing(env):
    reg, mgr, _ = env
    reg.update("cd-1", disabled=True)
    with pytest.raises(InstanceDisabledError):
        await mgr.connect("cd-1")
    assert _Tunnel.built == []
    assert mgr.status("cd-1") is None


@pytest.mark.asyncio
async def test_disabling_tears_the_tunnel_down_and_keeps_intent(env):
    reg, mgr, state = env
    assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
    r = await handlers.api_instances_update(
        _Req(state, match={"id": "cd-1"}, body={"disabled": True})
    )
    assert r.status == 200
    assert _body(r)["disabled"] is True
    assert _body(r)["status"]["state"] == "disconnected"
    assert mgr.status("cd-1") is None
    inst = reg.get("cd-1")
    # Intent kept, so enabling brings the crew back where it was.
    assert inst is not None and inst.disabled is True and inst.was_connected is True


@pytest.mark.asyncio
async def test_connect_route_answers_409_for_a_disabled_crew(env):
    reg, mgr, state = env
    reg.update("cd-1", disabled=True)
    r = await handlers.api_instances_connect(_Req(state, match={"id": "cd-1"}))
    assert r.status == 409
    assert _body(r)["code"] == "instance_disabled"
    assert _Tunnel.built == []


@pytest.mark.asyncio
async def test_connect_route_names_the_crew_that_is_off(env):
    """A chained crew is refused for its parent's switch, so say which crew is off.

    The refusal names crews by id only, never a host.
    """
    reg, mgr, state = env
    reg.add(
        name="Child",
        ssh_host="child-host",
        instance_id="kid",
        remote_port=7778,
        via_instance_id="cd-1",
        via_remote_port=7999,
        via_remote_id="kid",
    )
    reg.update("cd-1", disabled=True)
    r = await handlers.api_instances_connect(_Req(state, match={"id": "kid"}))
    assert r.status == 409
    body = _body(r)
    assert body["code"] == "instance_disabled"
    assert "'cd-1'" in body["error"] and "enable it to connect" in body["error"]
    assert "cd-1-alias" not in body["error"] and "child-host" not in body["error"]
    assert _Tunnel.built == []


@pytest.mark.asyncio
async def test_a_repeated_disable_closes_a_forward_the_record_already_calls_off(env):
    """The route reads the record before the manager lock, so an enable and a
    connect can land between that read and its write. A record that says off over
    a live forward is what that leaves, and a repeated disable must still close it.
    """
    reg, mgr, state = env
    try:
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        reg.update("cd-1", disabled=True)
        r = await handlers.api_instances_update(
            _Req(state, match={"id": "cd-1"}, body={"disabled": True})
        )
        assert r.status == 200 and _body(r)["disabled"] is True
        assert _body(r)["status"]["state"] == "disconnected"
        assert mgr.status("cd-1") is None
    finally:
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_a_revert_raced_by_an_edit_and_a_connect_still_tears_down(env, monkeypatch):
    """The route's pre-lock read must not decide a transport edit's teardown either.

    A PATCH setting a field back to what that read showed, after another edit and a
    connect moved it, still closes the forward the other edit built: otherwise the
    record names one machine while the tunnel serves another.
    """
    reg, mgr, state = env
    before_the_race = reg.get("cd-1")
    real_get = reg.get
    locked_at_read: list[bool] = []

    def get(instance_id):
        # The route's own read comes first, before the lock, and sees the record
        # as it was before the other edit landed.
        locked_at_read.append(mgr._lock.locked())
        return before_the_race if len(locked_at_read) == 1 else real_get(instance_id)

    try:
        reg.update("cd-1", ssh_host="cd-2-alias")
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        monkeypatch.setattr(reg, "get", get)
        r = await handlers.api_instances_update(
            _Req(state, match={"id": "cd-1"}, body={"ssh_host": "cd-1-alias"})
        )
        assert r.status == 200 and _body(r)["ssh_host"] == "cd-1-alias"
        assert _body(r)["status"]["state"] == "disconnected"
        assert mgr.status("cd-1") is None, "the cd-2-alias forward outlived the edit"
        assert locked_at_read[:2] == [False, True], "the teardown was decided outside the lock"
    finally:
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_an_edit_that_moves_no_transport_field_leaves_the_tunnel_and_its_self_heal(
    env, monkeypatch
):
    """Re-sending a transport field's stored value moves nothing, so the forward stays
    up and the reconfiguration barrier never cancels a self-heal it overlaps."""
    reg, mgr, state = env
    cancelled: list[str] = []

    async def cancel_recovery(instance_id):
        cancelled.append(instance_id)

    monkeypatch.setattr(mgr, "_cancel_recovery", cancel_recovery)
    try:
        assert (await mgr.connect("cd-1")).state == TunnelState.CONNECTED
        r = await handlers.api_instances_update(
            _Req(state, match={"id": "cd-1"}, body={"name": "Renamed", "ssh_host": "cd-1-alias"})
        )
        assert r.status == 200 and _body(r)["name"] == "Renamed"
        status = mgr.status("cd-1")
        assert status is not None and status.state == TunnelState.CONNECTED
        assert _Tunnel.built == ["cd-1"] and cancelled == []
    finally:
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_a_repeated_disable_of_an_idle_crew_opens_and_forgets_nothing(env):
    """The locked teardown re-run on a crew that is already off changes nothing else.

    Nothing is spawned, no record moves (its intent and its chained crew's stay),
    and a crew it does not carry keeps its forward.
    """
    reg, mgr, state = env
    reg.add(name="B", ssh_host="host-b", instance_id="b", remote_port=7778)
    reg.add(
        name="Child",
        ssh_host="child-host",
        instance_id="kid",
        remote_port=7779,
        via_instance_id="cd-1",
        via_remote_port=7999,
        via_remote_id="kid",
    )
    try:
        assert (await mgr.connect("b")).state == TunnelState.CONNECTED
        reg.update("cd-1", was_connected=True, disabled=True)
        reg.update("kid", was_connected=True)
        before = {row.id: row.to_dict() for row in reg.list()}
        r = await handlers.api_instances_update(
            _Req(state, match={"id": "cd-1"}, body={"disabled": True})
        )
        assert r.status == 200 and _body(r)["disabled"] is True
        assert {row.id: row.to_dict() for row in reg.list()} == before
        assert _Tunnel.built == ["b"]
        status_b = mgr.status("b")
        assert status_b is not None and status_b.state == TunnelState.CONNECTED
    finally:
        await mgr.shutdown()


@pytest.mark.asyncio
async def test_auto_warm_on_a_disabled_crew_is_a_quiet_decline(env):
    """The viewport's connected-only probe is not an error for a disabled crew."""
    reg, mgr, state = env
    reg.update("cd-1", disabled=True)
    r = await handlers.api_instances_connect(
        _Req(state, match={"id": "cd-1"}, query={"only_if_connected": "1"})
    )
    assert r.status == 200
    assert _body(r)["code"] == "instance_not_connected"


@pytest.mark.asyncio
async def test_enabling_lets_the_crew_connect_again(env):
    reg, mgr, state = env
    reg.update("cd-1", disabled=True)
    r = await handlers.api_instances_update(
        _Req(state, match={"id": "cd-1"}, body={"disabled": False})
    )
    assert r.status == 200 and _body(r)["disabled"] is False
    r = await handlers.api_instances_connect(_Req(state, match={"id": "cd-1"}))
    assert r.status == 200 and _body(r)["state"] == "connected"
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_a_non_boolean_flag_is_refused(env):
    reg, mgr, state = env
    r = await handlers.api_instances_update(
        _Req(state, match={"id": "cd-1"}, body={"disabled": "yes"})
    )
    assert r.status == 400
    inst = reg.get("cd-1")
    assert inst is not None and inst.disabled is False


@pytest.mark.asyncio
async def test_only_the_owner_may_toggle(env):
    reg, mgr, state = env
    r = await handlers.api_instances_update(
        _Req(state, match={"id": "cd-1"}, body={"disabled": True}, user="someone-else")
    )
    assert r.status == 403
    inst = reg.get("cd-1")
    assert inst is not None and inst.disabled is False


@pytest.mark.asyncio
async def test_startup_revive_skips_a_disabled_crew(env):
    import kiro_crew.dashboard.server as server

    reg, mgr, _ = env
    reg.add(name="B", ssh_host="host-b", instance_id="b", remote_port=7778)
    reg.update("cd-1", was_connected=True, disabled=True)
    reg.update("b", was_connected=True)
    await server._revive_intended_instances(reg, mgr)
    assert mgr.status("cd-1") is None
    status_b = mgr.status("b")
    assert status_b is not None and status_b.state == TunnelState.CONNECTED
    await mgr.shutdown()


@pytest.mark.asyncio
async def test_a_chained_crew_under_a_disabled_parent_is_refused(env):
    """Its forward dials the parent's host, so the parent's off switch covers it."""
    import kiro_crew.dashboard.server as server

    reg, mgr, _ = env
    reg.add(
        name="Child",
        ssh_host="child",
        instance_id="kid",
        remote_port=7778,
        via_instance_id="cd-1",
        via_remote_port=7999,
        via_remote_id="kid",
    )
    reg.update("kid", was_connected=True)
    reg.update("cd-1", disabled=True)
    with pytest.raises(InstanceDisabledError):
        await mgr.connect("kid")
    await server._revive_intended_instances(reg, mgr)
    assert _Tunnel.built == []
    assert mgr.status("kid") is None

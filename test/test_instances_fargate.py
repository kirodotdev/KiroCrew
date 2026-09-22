"""The ``fargate`` connection method at the instances layer.

A Fargate crew is reached over the same SSM port-forward an EC2 crew is, aimed
at an ECS task instead of a box. What differs is everything the box-shaped code
does AFTER the forward is up: there is no dashboard on the task, so no token can
be minted, refreshed or validated, no ``kirocrew restart`` can be sent, and the
card shows a turn URL rather than opening a pane. These tests pin each of those
absences at the registry, tunnel-manager, handler and diagnostics layers, plus
the one presence: the turn URL the card copies.
"""

from __future__ import annotations

import asyncio
import json

import pytest

_TASK = "0123456789abcdef0123456789abcdef"
_ECS_TARGET = f"ecs:crews_{_TASK}_{_TASK}-1234567890"
_EC2_TARGET = "i-0123456789abcdef0"


def _patch_port_probe(monkeypatch) -> None:
    """Both port probes answer "free" so allocation is host-independent."""
    import kiro_crew.instances.port_allocator as pa
    import kiro_crew.instances.ssh_tunnel_manager as stm

    monkeypatch.setattr(stm, "_is_port_free", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(pa, "_is_port_free", lambda port, host="127.0.0.1": True)


class _FakeTunnel:
    """Records the transport kwargs the manager chose; never spawns anything."""

    start_results: list[bool] = []

    def __init__(
        self,
        iid,
        ssh_host,
        lp,
        rp,
        *,
        connect_timeout_secs=0,
        compression=True,
        probe_failure_threshold=0,
        on_exit=None,
        transport="ssh",
        ssm_target="",
        aws_profile="",
        aws_region="",
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self._S = TunnelState
        self.pid = None
        self.transport = transport
        self.ssm_target = ssm_target
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.connect_timeout_secs = connect_timeout_secs
        self.start_result = self.start_results.pop(0) if self.start_results else True
        self.status = TunnelStatus(instance_id=iid, local_port=lp, remote_port=rp)

    async def start(self):
        self.status.state = self._S.CONNECTED if self.start_result else self._S.ERROR
        if not self.start_result:
            self.status.error = "boom"
        return self.start_result

    async def stop(self):
        self.status.state = self._S.STOPPED


def _forbid_mints(monkeypatch):
    """Every mint seam records a call; the tests assert the list stays empty."""
    import kiro_crew.instances.ssh_tunnel_manager as mod

    minted: list[str] = []

    async def ssm_mint(*a, **k):
        minted.append("ssm")
        return "SSM_TOKEN"

    async def ssh_mint(*a, **k):
        minted.append("ssh")
        return "SSH_TOKEN"

    monkeypatch.setattr(mod, "mint_remote_token_ssm", ssm_mint)
    monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: True)
    return minted, ssh_mint


def _mgr(tmp_path, monkeypatch):
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

    _patch_port_probe(monkeypatch)
    minted, ssh_mint = _forbid_mints(monkeypatch)
    _FakeTunnel.start_results = []
    reg = InstancesRegistry(path=tmp_path / "instances.json")
    mgr = SshTunnelManager(reg, base_port=53700, mint_token=ssh_mint, tunnel_factory=_FakeTunnel)
    return reg, mgr, minted


def _add_fargate(reg, **overrides):
    fields = dict(
        name="Fargate crew",
        connection_method="fargate",
        ssm_target=_ECS_TARGET,
        aws_profile="dev",
        aws_region="eu-west-2",
        instance_id="fg",
        remote_port=8080,
    )
    fields.update(overrides)
    return reg.add(**fields)


class TestRegistry:
    def test_fargate_is_a_connection_method_and_forwards_over_ssm(self):
        from kiro_crew.instances.registry import CONNECTION_METHODS, SSM_TRANSPORT_METHODS

        assert "fargate" in CONNECTION_METHODS
        assert SSM_TRANSPORT_METHODS == {"ssm", "fargate"}
        assert SSM_TRANSPORT_METHODS < set(CONNECTION_METHODS)

    def test_a_fargate_record_stores_and_round_trips_an_ecs_target(self, tmp_path):
        from kiro_crew.instances.registry import Instance, InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        inst = _add_fargate(reg)
        assert inst.connection_method == "fargate" and inst.ssm_target == _ECS_TARGET
        again = Instance.from_dict(json.loads(json.dumps(inst.to_dict())))
        assert again.connection_method == "fargate" and again.ssm_target == _ECS_TARGET
        again.validate()

    def test_a_fargate_record_refuses_an_ec2_id(self, tmp_path):
        """The ssm arm admits ``i-...``; the fargate arm must not.

        An EC2 id stored under ``fargate`` would open a forward to a box and then
        show its dashboard port as a turn URL. The arm validates with the same
        splitter connect_fargate reads with, so the stored target is one that
        lane can open.
        """
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError, match="ECS task target"):
            _add_fargate(reg, ssm_target=_EC2_TARGET)
        with pytest.raises(InvalidInstanceError, match="ECS task target"):
            _add_fargate(reg, ssm_target="")

    def test_a_fargate_record_checks_its_aws_coordinates(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError, match="aws_profile"):
            _add_fargate(reg, aws_profile="dev;rm")
        with pytest.raises(InvalidInstanceError, match="aws_region"):
            _add_fargate(reg, aws_region="not a region")

    def test_an_ssm_record_refuses_an_ecs_target(self, tmp_path):
        """The mirror of the EC2-id refusal above, at the ``ssm`` arm.

        ``ssm_target_matches`` admits the ECS shape (it is the shared transport
        charset), so without its own check the ``ssm`` arm would store a target
        whose connect forwards and then dispatches ``aws ssm send-command`` at a
        task with no SSM agent. The refusal names the method that stores it.
        """
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError) as exc:
            reg.add(name="Misfiled crew", connection_method="ssm", ssm_target=_ECS_TARGET)
        assert "fargate" in str(exc.value)
        assert reg.list() == []

    def test_an_ssm_record_migrates_to_fargate_with_an_ecs_target(self, tmp_path):
        """update() sets every change, then validates the whole record.

        Switching the method and the target in one call is therefore a fargate
        record by the time the arm runs, and it persists as one.
        """
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        inst = reg.add(name="EC2 crew", connection_method="ssm", ssm_target=_EC2_TARGET)
        updated = reg.update(inst.id, connection_method="fargate", ssm_target=_ECS_TARGET)
        assert updated.connection_method == "fargate" and updated.ssm_target == _ECS_TARGET
        reloaded = InstancesRegistry(path=tmp_path / "instances.json").get(inst.id)
        assert reloaded is not None
        assert reloaded.connection_method == "fargate" and reloaded.ssm_target == _ECS_TARGET

    def test_an_ssm_record_cannot_take_an_ecs_target_without_changing_method(self, tmp_path):
        """The same real update() path, with the method left at ``ssm``."""
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        inst = reg.add(name="EC2 crew", connection_method="ssm", ssm_target=_EC2_TARGET)
        with pytest.raises(InvalidInstanceError) as exc:
            reg.update(inst.id, ssm_target=_ECS_TARGET)
        assert "fargate" in str(exc.value)
        # The refused edit was never written.
        reloaded = InstancesRegistry(path=tmp_path / "instances.json").get(inst.id)
        assert reloaded is not None
        assert reloaded.connection_method == "ssm" and reloaded.ssm_target == _EC2_TARGET


class TestTunnelManager:
    @pytest.mark.asyncio
    async def test_connect_forwards_over_ssm_and_exposes_the_turn_url(self, tmp_path, monkeypatch):
        from kiro_crew.cloud.connect import FARGATE_TURN_PATH
        from kiro_crew.instances.constants import DEFAULT_SSM_CONNECT_TIMEOUT_SECS

        reg, mgr, minted = _mgr(tmp_path, monkeypatch)
        _add_fargate(reg)
        status = await mgr.connect("fg")

        tunnel = mgr._tunnels["fg"]
        # The child is the SSM port-forward, aimed at the ECS task.
        assert tunnel.transport == "ssm"
        assert tunnel.ssm_target == _ECS_TARGET
        assert tunnel.aws_profile == "dev" and tunnel.aws_region == "eu-west-2"
        assert tunnel.connect_timeout_secs == DEFAULT_SSM_CONNECT_TIMEOUT_SECS
        assert status.state.value == "connected"
        assert status.turn_url == f"http://127.0.0.1:{status.local_port}{FARGATE_TURN_PATH}"
        assert status.to_dict()["turn_url"] == status.turn_url
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_nothing_is_minted_at_the_instances_layer(self, tmp_path, monkeypatch):
        """The connect-layer sibling of this test lives in test_cloud_connect.

        Both mint seams record every call. A fargate connect must leave them
        untouched, hold no token, schedule no refresh, and report no TTL: any one
        of those would be the manager dispatching ``kirocrew token`` at a task
        that has no ``kirocrew``.
        """
        reg, mgr, minted = _mgr(tmp_path, monkeypatch)
        _add_fargate(reg)
        await mgr.connect("fg")

        assert minted == []
        assert mgr.get_token("fg") == ""
        assert "fg" not in mgr._refresh_tasks
        assert mgr.token_ttl_remaining("fg") is None
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_an_on_demand_refresh_is_refused_without_minting(self, tmp_path, monkeypatch):
        reg, mgr, minted = _mgr(tmp_path, monkeypatch)
        _add_fargate(reg)
        await mgr.connect("fg")

        assert await mgr.refresh_token("fg") is None
        assert minted == []
        assert mgr.get_token("fg") == ""
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_self_heal_tier_two_re_forwards_instead_of_minting(self, tmp_path, monkeypatch):
        """Tier 1 fails, tier 2 must rebuild again rather than re-mint."""
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        reg, mgr, minted = _mgr(tmp_path, monkeypatch)
        _add_fargate(reg)
        await mgr.connect("fg")
        first = mgr._tunnels["fg"]
        first.status.state = TunnelState.ERROR
        # Tier 1's rebuild fails, tier 2's succeeds.
        _FakeTunnel.start_results = [False, True]

        await mgr._recover("fg")

        current = mgr._tunnels["fg"]
        assert current is not first
        assert current.status.state == TunnelState.CONNECTED
        assert current.transport == "ssm" and current.ssm_target == _ECS_TARGET
        assert current.status.turn_url.endswith("/v1/chat/completions")
        assert minted == []
        assert mgr.get_token("fg") == ""
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_restart_remote_is_refused_before_any_command(self, tmp_path, monkeypatch):
        import kiro_crew.instances.ssh_tunnel_manager as mod

        dispatched = []

        async def run_ssm(*a, **k):
            dispatched.append(a)
            return 0, ""

        monkeypatch.setattr(mod, "run_remote_kirocrew_ssm", run_ssm)
        reg, mgr, _minted = _mgr(tmp_path, monkeypatch)
        _add_fargate(reg)

        result = await mgr.restart_remote("fg")
        assert result["ok"] is False
        assert "no Kiro Crew gateway" in result["message"]
        assert dispatched == []

    def test_resolve_transport_refuses_an_ec2_id_under_fargate(self, tmp_path, monkeypatch):
        """The registry arm refuses it on write; this is the read-side guard.

        A record edited on disk to carry ``i-...`` under ``fargate`` would
        otherwise open a forward to a box.
        """
        from kiro_crew.instances.registry import Instance
        from kiro_crew.instances.validation import SsmValidationError

        _reg, mgr, _minted = _mgr(tmp_path, monkeypatch)
        inst = Instance(id="fg", name="x", connection_method="fargate", ssm_target=_EC2_TARGET)
        with pytest.raises(SsmValidationError, match="ECS task target"):
            mgr._resolve_transport(inst)
        params = mgr._resolve_transport(
            Instance(id="fg", name="x", connection_method="fargate", ssm_target=_ECS_TARGET)
        )
        assert params.method == "fargate" and params.forwards_over_ssm
        assert params.tunnel_kwargs()["transport"] == "ssm"
        assert params.turn_url(5599) == "http://127.0.0.1:5599/v1/chat/completions"

    def test_resolve_transport_refuses_an_ecs_target_under_ssm(self, tmp_path, monkeypatch):
        """The registry arm refuses it on write; this is the read-side mirror.

        A record filed as ``ssm`` with an ECS target before the registry refused
        the shape still exists on disk. Without this guard it would forward and
        fail at the mint with a generic error instead of naming the fix.
        """
        from kiro_crew.instances.registry import Instance
        from kiro_crew.instances.validation import SsmValidationError

        _reg, mgr, _minted = _mgr(tmp_path, monkeypatch)
        inst = Instance(id="legacy", name="x", connection_method="ssm", ssm_target=_ECS_TARGET)
        with pytest.raises(SsmValidationError) as exc:
            mgr._resolve_transport(inst)
        assert "fargate" in str(exc.value)
        # The plain EC2 id is still accepted by the same arm.
        params = mgr._resolve_transport(
            Instance(id="ec2", name="x", connection_method="ssm", ssm_target=_EC2_TARGET)
        )
        assert params.method == "ssm" and params.ssm_target == _EC2_TARGET

    @pytest.mark.asyncio
    async def test_connect_refuses_a_stored_ssm_record_with_an_ecs_target(
        self, tmp_path, monkeypatch
    ):
        """Public connect path for a pre-refusal record written straight to disk.

        ``reg.add`` now refuses this shape, so the record is planted the way an
        older build left it. Connect must answer an ERROR status naming the
        fargate method, spawn no forwarder and mint nothing.
        """
        reg, mgr, minted = _mgr(tmp_path, monkeypatch)
        spawned: list[str] = []

        class _RecordingTunnel(_FakeTunnel):
            def __init__(self, iid, *a, **k):
                spawned.append(iid)
                super().__init__(iid, *a, **k)

        mgr._tunnel_factory = _RecordingTunnel
        reg.path.write_text(
            json.dumps(
                {
                    "instances": [
                        {
                            "id": "legacy",
                            "name": "Legacy",
                            "connection_method": "ssm",
                            "ssm_target": _ECS_TARGET,
                            "aws_profile": "dev",
                            "aws_region": "eu-west-2",
                            "remote_port": 5476,
                        }
                    ],
                    "last_active_id": "",
                }
            ),
            encoding="utf-8",
        )
        assert reg.get("legacy") is not None  # the record loads; only connect refuses it

        status = await mgr.connect("legacy")

        assert status.state.value == "error"
        assert "fargate" in (status.error or "")
        assert "invalid ssm settings" in (status.error or "")
        assert "fargate" in (mgr.last_error("legacy") or "")
        assert spawned == []
        assert "legacy" not in mgr._tunnels
        assert minted == []
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_an_ssm_status_carries_no_turn_url(self, tmp_path, monkeypatch):
        """The handler keys on ``turn_url``; only a fargate status may carry it."""
        reg, mgr, _minted = _mgr(tmp_path, monkeypatch)
        reg.add(
            name="EC2",
            connection_method="ssm",
            ssm_target=_EC2_TARGET,
            instance_id="ec2",
            remote_port=5476,
        )
        status = await mgr.connect("ec2")
        assert status.state.value == "connected"
        assert status.turn_url == ""
        assert "turn_url" not in status.to_dict()
        params = mgr._resolve_transport(reg.get("ec2"))
        assert params.turn_url(5599) == ""
        await mgr.shutdown()

    def test_connect_timeout_is_the_ssm_default(self, tmp_path, monkeypatch):
        from kiro_crew.instances.constants import DEFAULT_SSM_CONNECT_TIMEOUT_SECS

        _reg, mgr, _minted = _mgr(tmp_path, monkeypatch)
        assert mgr._connect_timeout_for("fargate") == DEFAULT_SSM_CONNECT_TIMEOUT_SECS
        assert mgr._connect_timeout_for("fargate") == mgr._connect_timeout_for("ssm")


class _Req:
    """Request double for the instances handlers.

    ``_guard`` demands the positively-identified owner, and the predicate reads the
    request as a MAPPING as well as calling ``.get`` -- ``"app" in request`` then
    ``request["app"]`` -- so all three reads come from one claims dict. ``app`` is
    "" because a browser session carries no app token.
    """

    def __init__(self, state, *, match=None, query=None):
        self.app = {"state": state}
        self.headers = {}
        self.match_info = match or {}
        self.query = query or {}
        self._claims = {"user": "owner", "app": ""}

    def get(self, key, default=None):
        return self._claims.get(key, default)

    def __contains__(self, key):
        return key in self._claims

    def __getitem__(self, key):
        return self._claims[key]


class _State:
    """Dashboard-state stand-in; ``owner_id`` matches ``_Req``'s caller because
    ``_guard`` compares the subject against it."""

    owner_id = "owner"

    def __init__(self, registry, manager):
        self.instances_registry = registry
        self.instances_manager = manager


class TestConnectHandler:
    def _enable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps({"instances": {"enabled": True}}))
        from kiro_crew.config import loader

        loader._invalidate_config_cache()

    def test_a_fargate_connect_answers_with_the_turn_url_and_no_token(self, tmp_path, monkeypatch):
        """The token dance is skipped, so a lane with no token is not a 502.

        Without the branch the handler asks for a token, finds none, tries a
        re-mint, and reports "token expired and re-mint failed" for a forward
        that is up. And a ``token`` key in this body is what an Open button would
        consume, so its absence is asserted too.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState, TunnelStatus

        self._enable(tmp_path, monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        _add_fargate(reg)
        turn_url = "http://127.0.0.1:7778/v1/chat/completions"
        touched = []

        class FakeMgr:
            async def connect(self, iid, *, rebuild=False, only_if_connected=False):
                return TunnelStatus(
                    iid, TunnelState.CONNECTED, local_port=7778, remote_port=8080, turn_url=turn_url
                )

            def get_token(self, iid):
                touched.append("get_token")
                return ""

            async def token_validates(self, local_port, token):
                touched.append("token_validates")
                return False

            async def refresh_token(self, iid):
                touched.append("refresh_token")
                return None

        state = _State(reg, FakeMgr())
        r = asyncio.run(handlers.api_instances_connect(_Req(state, match={"id": "fg"})))
        body = json.loads(r.body.decode())
        assert r.status == 200, body
        assert body["turn_url"] == turn_url
        assert "token" not in body
        assert touched == []


class TestDiagnostics:
    def _ready(self, monkeypatch, ready: bool, reason: str):
        from types import SimpleNamespace

        from kiro_crew.cloud import ssm as cloud_ssm

        seen = []

        def fake(cluster, task_id, profile="", region=""):
            seen.append((cluster, task_id, profile, region))
            return SimpleNamespace(ready=ready, reason=reason)

        monkeypatch.setattr(cloud_ssm, "task_exec_readiness", fake)
        return seen

    def test_a_task_that_is_not_ready_stops_the_ladder_with_its_own_reason(self, monkeypatch):
        from kiro_crew.instances import diagnostics as d

        seen = self._ready(monkeypatch, False, "exec channel is off; relaunch")
        result = asyncio.run(
            d.diagnose_instance_fargate(
                _ECS_TARGET, 5599, aws_profile="dev", aws_region="eu-west-2"
            )
        )
        assert result.code == d.SSM_UNREACHABLE
        assert result.reason == "exec channel is off; relaunch"
        assert result.probes == [{"name": "task_exec_ready", "ok": False}]
        # The ladder reads the target through the same splitter connect does.
        assert seen == [("crews", _TASK, "dev", "eu-west-2")]

    def test_no_forward_is_not_connected_and_a_dead_forward_is_tunnel_down(self, monkeypatch):
        from kiro_crew.instances import diagnostics as d

        self._ready(monkeypatch, True, "")
        result = asyncio.run(d.diagnose_instance_fargate(_ECS_TARGET, 0))
        assert result.code == d.NOT_CONNECTED
        assert [p["name"] for p in result.probes] == ["task_exec_ready"]

        async def dead(port):
            return False

        monkeypatch.setattr(d, "_probe_local_forward", dead)
        result = asyncio.run(d.diagnose_instance_fargate(_ECS_TARGET, 5599))
        assert result.code == d.TUNNEL_DOWN
        assert [p["name"] for p in result.probes] == ["task_exec_ready", "local_forward"]

        async def alive(port):
            return True

        monkeypatch.setattr(d, "_probe_local_forward", alive)
        result = asyncio.run(d.diagnose_instance_fargate(_ECS_TARGET, 5599))
        assert result.code == d.OK and result.ok
        assert "dashboard" not in result.reason

    def test_a_non_ecs_target_is_unknown_and_reaches_no_aws_call(self, monkeypatch):
        from kiro_crew.instances import diagnostics as d

        seen = self._ready(monkeypatch, True, "")
        result = asyncio.run(d.diagnose_instance_fargate(_EC2_TARGET, 5599))
        assert result.code == d.UNKNOWN
        assert "not an ECS task target" in result.reason
        assert seen == [] and result.probes == []

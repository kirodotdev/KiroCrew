"""An SSM crew whose ``ssm_target`` is an ``asg:``/``param:`` resolver.

The row names where the instance id is found, the hub looks it up each time it
builds the forward (never storing the answer), and a lookup that names no single
instance fails closed with its own reason.
"""

from __future__ import annotations

import pytest
from closed_peer_helpers import refused_peer_connects  # noqa: F401
from instances_helpers import _FakeTunnel, _patch_port_probe

pytestmark = pytest.mark.usefixtures("refused_peer_connects")

_ID_A = "i-0123456789abcdef0"
_ID_B = "i-0fedcba9876543210"


class TestResolverShape:
    def test_both_shapes_split_and_stay_out_of_the_dialled_charset(self):
        from kiro_crew.instances import validation

        assert validation.split_ssm_resolver("asg:crew-asg_1.a") == ("asg", "crew-asg_1.a")
        assert validation.split_ssm_resolver("param:/crews/a/instance-id") == (
            "param",
            "/crews/a/instance-id",
        )
        for value in ("asg:crew-asg", "param:/crews/a/instance-id"):
            assert not validation.ssm_target_matches(value)
            with pytest.raises(validation.SsmValidationError):
                validation.validate_ssm_target(value)

    @pytest.mark.parametrize(
        "value",
        ["asg:", "asg:-x", "asg:crew asg", "asg:crew\n", "param:", "param:-x", " asg:crew", _ID_A],
    )
    def test_anything_else_is_no_resolver(self, value):
        from kiro_crew.instances import validation

        assert validation.split_ssm_resolver(value) is None


class TestResolverRegistry:
    def test_an_ssm_row_stores_a_resolver_as_typed(self, tmp_path):
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="Asg", connection_method="ssm", ssm_target="asg:crew-asg", instance_id="a")
        reg.add(name="Param", connection_method="ssm", ssm_target="param:/c/id", instance_id="p")
        reloaded = InstancesRegistry(path=tmp_path / "instances.json")
        assert reloaded.get("a").ssm_target == "asg:crew-asg"
        assert reloaded.get("p").ssm_target == "param:/c/id"

    @pytest.mark.parametrize(
        ("method", "value"),
        [("ssm", "asg:-x"), ("ssm", "param:/c/id --profile admin"), ("fargate", "asg:crew-asg")],
    )
    def test_a_malformed_or_misplaced_resolver_is_refused(self, tmp_path, method, value):
        from kiro_crew.instances.registry import InstancesRegistry, InvalidInstanceError

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        with pytest.raises(InvalidInstanceError):
            reg.add(name="Bad", connection_method=method, ssm_target=value)


class TestResolveSsmTarget:
    def _stub(self, monkeypatch, rc, out, err=""):
        calls = []

        def fake_run_aws(args, profile="", region="", **_kw):
            calls.append((list(args), profile, region))
            return rc, out, err

        monkeypatch.setattr("kiro_crew.cloud.aws.run_aws", fake_run_aws)
        return calls

    def test_a_group_with_one_inservice_instance_names_it(self, monkeypatch):
        from kiro_crew.cloud import ssm

        calls = self._stub(monkeypatch, 0, f'["{_ID_A}"]\n')
        assert ssm.resolve_ssm_target("asg", "crew-asg", "dev", "eu-west-2") == _ID_A
        args, profile, region = calls[0]
        assert args[:4] == [
            "autoscaling",
            "describe-auto-scaling-groups",
            "--auto-scaling-group-names",
            "crew-asg",
        ]
        assert (profile, region) == ("dev", "eu-west-2")

    @pytest.mark.parametrize(
        ("rc", "out", "says"),
        [
            (0, "[]\n", "has 0 InService instances"),
            (0, f'["{_ID_A}", "{_ID_B}"]\n', "has 2 InService instances"),
            (0, "null\n", "there is no Auto Scaling group named 'crew-asg'"),
            (254, "", "could not read Auto Scaling group 'crew-asg'"),
        ],
    )
    def test_a_group_that_names_no_single_instance_fails_closed(self, monkeypatch, rc, out, says):
        from kiro_crew.cloud import ssm
        from kiro_crew.instances.validation import SsmTargetUnresolvedError

        self._stub(monkeypatch, rc, out, "AccessDenied")
        with pytest.raises(SsmTargetUnresolvedError, match=says):
            ssm.resolve_ssm_target("asg", "crew-asg")

    def test_a_parameter_is_read_without_decryption(self, monkeypatch):
        from kiro_crew.cloud import ssm

        calls = self._stub(monkeypatch, 0, f"{_ID_A}\n")
        assert ssm.resolve_ssm_target("param", "/crews/a/id") == _ID_A
        args = calls[0][0]
        assert args[:4] == ["ssm", "get-parameter", "--name", "/crews/a/id"]
        assert "--with-decryption" not in args

    def test_a_parameter_holding_no_instance_id_is_refused_without_quoting_it(self, monkeypatch):
        from kiro_crew.cloud import ssm
        from kiro_crew.instances.validation import SsmTargetUnresolvedError

        self._stub(monkeypatch, 0, "hunter2-not-an-id\n")
        with pytest.raises(SsmTargetUnresolvedError) as excinfo:
            ssm.resolve_ssm_target("param", "/crews/a/id")
        assert "hunter2" not in str(excinfo.value)
        assert "/crews/a/id" in str(excinfo.value)


def _manager(tmp_path, monkeypatch, answers, dialled, minted, refused=()):
    """A manager whose resolver answers from *answers* and whose SSM mint records its target.

    The mint fails for every instance id in *refused* (a list the test may change).
    """
    import kiro_crew.instances.ssh_tunnel_manager as mod
    from kiro_crew.instances.registry import InstancesRegistry
    from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager
    from kiro_crew.instances.validation import SsmTargetUnresolvedError

    _patch_port_probe(monkeypatch)
    monkeypatch.setattr("kiro_crew.cloud.ssm.session_manager_plugin_installed", lambda: True)

    def fake_resolve(kind, name, profile="", region=""):
        answer = answers[0]
        if isinstance(answer, str):
            return answer
        raise SsmTargetUnresolvedError(
            f"Auto Scaling group {name!r} has {answer} InService instances"
        )

    monkeypatch.setattr("kiro_crew.cloud.ssm.resolve_ssm_target", fake_resolve)

    async def fake_ssm_mint(target, **_kw):
        from kiro_crew.instances.token_mint import TokenMintError

        minted.append(target)
        if target in refused:
            raise TokenMintError(f"{target} has not registered with SSM yet")
        return f"TOK-{target}"

    monkeypatch.setattr(mod, "mint_remote_token_ssm", fake_ssm_mint)

    def factory(iid, ssh_host, lp, rp, **kw):
        tunnel = _FakeTunnel(iid, ssh_host, lp, rp, **kw)
        # The real child carries its dialled target on its status.
        tunnel.status.ssm_target = kw.get("ssm_target", "")
        dialled.append(tunnel.ssm_target)
        return tunnel

    reg = InstancesRegistry(path=tmp_path / "instances.json")
    reg.add(
        name="Asg crew",
        connection_method="ssm",
        ssm_target="asg:crew-asg",
        aws_profile="dev",
        aws_region="eu-west-2",
        instance_id="asg-crew",
        remote_port=53611,
    )
    return reg, SshTunnelManager(reg, base_port=53600, tunnel_factory=factory)


class TestResolverForward:
    @pytest.mark.asyncio
    async def test_connect_dials_the_instance_the_group_names_and_stores_nothing(
        self, tmp_path, monkeypatch
    ):
        dialled, minted = [], []
        reg, mgr = _manager(tmp_path, monkeypatch, [_ID_A], dialled, minted)
        await mgr.connect("asg-crew")
        assert dialled == [_ID_A]
        assert minted == [_ID_A]
        assert reg.get("asg-crew").ssm_target == "asg:crew-asg"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_connect_fails_closed_while_the_group_has_two(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        dialled, minted = [], []
        _reg, mgr = _manager(tmp_path, monkeypatch, [2], dialled, minted)
        status = await mgr.connect("asg-crew")
        assert status.state == TunnelState.ERROR
        assert "could not look up the SSM target" in status.error
        assert "2 InService instances" in status.error
        assert dialled == [] and minted == []
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_self_heal_retries_an_unresolved_group_then_reaches_the_replacement(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        answers, dialled, minted = [_ID_A], [], []
        _reg, mgr = _manager(tmp_path, monkeypatch, answers, dialled, minted)
        await mgr.connect("asg-crew")
        retries = []
        mgr._retry_recovery = lambda iid, delay: retries.append(iid)

        # Mid-replacement: two InService instances. Nothing is dialled; a retry is armed.
        answers[0] = 2
        mgr._tunnels["asg-crew"].status.state = TunnelState.ERROR
        await mgr._recover("asg-crew")
        assert retries == ["asg-crew"]
        assert dialled == [_ID_A]

        # The replacement is the group's one InService instance: the rebuild dials it,
        # and the credential comes from it, not from the instance it replaced.
        answers[0] = _ID_B
        await mgr._recover("asg-crew")
        assert dialled[-1] == _ID_B
        assert mgr.status("asg-crew").state == TunnelState.CONNECTED
        assert minted[-1] == _ID_B
        assert mgr.get_token("asg-crew") == f"TOK-{_ID_B}"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_connect_during_self_heals_lookup_keeps_its_tunnel(self, tmp_path, monkeypatch):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        answers, dialled, minted = [_ID_A], [], []
        _reg, mgr = _manager(tmp_path, monkeypatch, answers, dialled, minted)
        await mgr.connect("asg-crew")
        mgr._tunnels["asg-crew"].status.state = TunnelState.ERROR
        answers[0] = _ID_B
        lookup = mgr._with_resolved_target

        async def lookup_while_the_user_reconnects(inst):
            # The user's Retry lands while the recovery waits on its lookup.
            del mgr._with_resolved_target
            await mgr.connect("asg-crew")
            return await lookup(inst)

        mgr._with_resolved_target = lookup_while_the_user_reconnects
        await mgr._recover("asg-crew")
        tunnel = mgr._tunnels["asg-crew"]
        assert tunnel.status.state == TunnelState.CONNECTED and not tunnel.stopped
        assert dialled == [_ID_A, _ID_B]
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_failed_mint_on_the_new_instance_is_retried_not_reused(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        answers, dialled, minted, refused = [_ID_A], [], [], [_ID_B]
        _reg, mgr = _manager(tmp_path, monkeypatch, answers, dialled, minted, refused)
        await mgr.connect("asg-crew")
        retries = []
        mgr._retry_recovery = lambda iid, delay: retries.append(iid)
        mgr._tunnels["asg-crew"].status.state = TunnelState.ERROR

        # The replacement is InService but its agent has not registered: no mint.
        answers[0] = _ID_B
        await mgr._recover("asg-crew")
        assert retries == ["asg-crew"]
        assert mgr.status("asg-crew").state != TunnelState.CONNECTED
        assert not mgr.get_token("asg-crew")
        assert dialled == [_ID_A]

        # Once it has, the next attempt mints there and rebuilds to it.
        refused.clear()
        await mgr._recover("asg-crew")
        assert dialled[-1] == _ID_B
        assert mgr.status("asg-crew").state == TunnelState.CONNECTED
        assert mgr.get_token("asg-crew") == f"TOK-{_ID_B}"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_refresh_racing_the_move_leaves_no_old_credential_on_the_new_forward(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.instances.ssh_tunnel_manager import TunnelState

        answers, dialled, minted = [_ID_A], [], []
        _reg, mgr = _manager(tmp_path, monkeypatch, answers, dialled, minted)
        await mgr.connect("asg-crew")
        old = mgr._tunnels["asg-crew"]
        old.status.state = TunnelState.ERROR
        answers[0] = _ID_B
        stop, refreshed = old.stop, []

        async def stop_after_a_refresh():
            # The owner's refresh lands while the forward to A is going down. Recorded,
            # not asserted: the rebuild suppresses whatever stop() raises.
            refreshed.append(await mgr._refresh_token_once("asg-crew"))
            await stop()

        old.stop = stop_after_a_refresh
        await mgr._recover("asg-crew")
        # The race happened: the refresh minted on A, and stored, after the recovery minted on B.
        assert refreshed == [True]
        assert minted == [_ID_A, _ID_B, _ID_A]
        tunnel = mgr._tunnels["asg-crew"]
        assert tunnel.status.ssm_target == _ID_B
        assert tunnel.status.state == TunnelState.CONNECTED
        assert mgr.get_token("asg-crew") == f"TOK-{_ID_B}"
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_remote_verb_runs_where_the_open_forward_leads(self, tmp_path, monkeypatch):
        answers, dialled, minted = [_ID_A], [], []
        _reg, mgr = _manager(tmp_path, monkeypatch, answers, dialled, minted)
        await mgr.connect("asg-crew")
        answers[0] = _ID_B
        _inst, params = await mgr._remote_command_target("asg-crew", "restart")
        assert params.ssm_target == _ID_A
        await mgr.disconnect("asg-crew")
        _inst, params = await mgr._remote_command_target("asg-crew", "restart")
        assert params.ssm_target == _ID_B
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_a_token_refresh_mints_on_the_instance_the_open_forward_reaches(
        self, tmp_path, monkeypatch
    ):
        answers, dialled, minted = [_ID_A], [], []
        _reg, mgr = _manager(tmp_path, monkeypatch, answers, dialled, minted)
        await mgr.connect("asg-crew")
        # The group already names a replacement, but the open forward still reaches A.
        answers[0] = _ID_B
        assert await mgr._refresh_token_once("asg-crew")
        assert minted == [_ID_A, _ID_A]
        await mgr.shutdown()


class TestResolverDiagnosis:
    @pytest.mark.asyncio
    async def test_an_unresolved_group_has_its_own_code(self, monkeypatch):
        from kiro_crew.instances import diagnostics
        from kiro_crew.instances.validation import SsmTargetUnresolvedError

        def fake_resolve(kind, name, profile="", region=""):
            raise SsmTargetUnresolvedError(
                "Auto Scaling group 'crew-asg' has 0 InService instances"
            )

        monkeypatch.setattr("kiro_crew.cloud.ssm.resolve_ssm_target", fake_resolve)
        result = await diagnostics.diagnose_instance_ssm("asg:crew-asg", 5476, 0)
        assert result.code == "ssm_target_unresolved"
        assert "0 InService instances" in result.reason
        assert result.probes == [{"name": "ssm_target_resolver", "ok": False}]

    @pytest.mark.asyncio
    async def test_the_ladder_probes_the_instance_the_lookup_names(self, monkeypatch):
        from kiro_crew.instances import diagnostics

        probed = []

        async def fake_probe(target, profile="", region=""):
            probed.append(target)
            return diagnostics._NODE_MISSING

        monkeypatch.setattr(
            "kiro_crew.cloud.ssm.resolve_ssm_target", lambda kind, name, p="", r="": _ID_A
        )
        monkeypatch.setattr(diagnostics, "_probe_ssm_managed", fake_probe)
        result = await diagnostics.diagnose_instance_ssm("param:/crews/a/id", 5476, 0)
        assert probed == [_ID_A]
        assert result.code == "ssm_target_missing"
        assert result.probes[0] == {"name": "ssm_target_resolver", "ok": True}
        # The remedy is never to type a fixed id over the resolver.
        assert _ID_A in result.reason
        assert "set SSM target to the new instance id" not in result.reason


class TestResolverIsNoLaunchedCrew:
    def test_a_resolver_never_correlates_to_a_launch_job(self, monkeypatch, tmp_path):
        import kiro_crew.cloud.launch_job as ljmod
        from kiro_crew.cloud import connect

        store = ljmod.LaunchJobStore(root=tmp_path / "launch-jobs")
        monkeypatch.setattr(ljmod, "LaunchJobStore", lambda *a, **k: store)
        job = store.create(profile="dev", region="eu-west-2", size_key="light")
        job.instance_id = _ID_A
        store.save(job)
        assert connect.is_launched_instance(_ID_A) is True
        assert connect.is_launched_instance("asg:crew-asg") is False
        assert connect.is_launched_instance("param:/crews/a/id") is False

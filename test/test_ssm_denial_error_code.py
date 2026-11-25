"""An SSM forward IAM refused carries ``error_code`` so the dashboard can word it.

The raw AWS text stays in ``error`` (ARNs and account IDs are identifiers, never
redacted); the code reaches the status the dashboard reads, both from a live
tunnel and from the reason retained after the failed tunnel is dropped.
"""

from __future__ import annotations

import types

import pytest
from instances_helpers import _FakeTunnel, _patch_port_probe

from kiro_crew.instances.ssh_tunnel_manager_child import SSM_START_SESSION_DENIED

DENIAL = (
    "aws: [ERROR]: An error occurred (AccessDeniedException) when calling the "
    "StartSession operation: User: arn:aws:sts::000000000000:assumed-role/no-ssm/s "
    "is not authorized to perform: ssm:StartSession on resource: "
    "arn:aws:ec2:us-east-1:000000000000:instance/i-0123456789abcdef0"
)


def _ssm_tunnel(stderr: str):
    from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

    t = _SshTunnel("ec2", "", 7777, 7777, transport="ssm", ssm_target="i-0123456789abcdef0")
    t._stderr_buf = stderr
    return t


def test_a_denied_start_session_names_its_code_and_keeps_the_aws_text() -> None:
    t = _ssm_tunnel(DENIAL)
    t.status.error = t._exit_error(255)
    d = t.status.to_dict()
    assert d["error_code"] == SSM_START_SESSION_DENIED
    assert "arn:aws:sts::000000000000:assumed-role/no-ssm/s" in d["error"]


@pytest.mark.parametrize(
    "stderr",
    [
        "The security token included in the request is expired",
        "SessionManagerPlugin is not found",
        # A role profile whose AssumeRole is refused: a denial, but not StartSession's.
        "An error occurred (AccessDenied) when calling the AssumeRole operation: User: "
        "arn:aws:iam::000000000000:user/me is not authorized to perform: sts:AssumeRole",
    ],
)
def test_another_ssm_failure_names_no_code(stderr: str) -> None:
    t = _ssm_tunnel(stderr)
    t.status.error = t._exit_error(255)
    assert "error_code" not in t.status.to_dict()


class _DeniedTunnel(_FakeTunnel):
    async def start(self):
        ok = await super().start()
        self.status.error = "IAM denied ssm:StartSession for this target: " + DENIAL
        self.status.error_code = SSM_START_SESSION_DENIED
        return ok


class TestRetainedCode:
    def _mgr(self, tmp_path, monkeypatch, factory):
        from kiro_crew.instances.registry import InstancesRegistry
        from kiro_crew.instances.ssh_tunnel_manager import SshTunnelManager

        async def mint(host, **_kw):
            return "SECRET_TOK"

        _patch_port_probe(monkeypatch)
        reg = InstancesRegistry(path=tmp_path / "instances.json")
        reg.add(name="CD", ssh_host="cd-1", instance_id="cd-1")
        return SshTunnelManager(reg, base_port=53400, mint_token=mint, tunnel_factory=factory)

    @pytest.mark.asyncio
    async def test_the_status_after_the_failed_tunnel_is_dropped_keeps_the_code(
        self, tmp_path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers_instances import _status_for

        def denied(*a, **k):
            t = _DeniedTunnel(*a, **k)
            t.start_result = False
            return t

        mgr = self._mgr(tmp_path, monkeypatch, denied)
        await mgr.connect("cd-1")
        assert mgr.status("cd-1") is None
        d = _status_for(types.SimpleNamespace(instances_manager=mgr), "cd-1")
        assert d["state"] == "error"
        assert d["error_code"] == SSM_START_SESSION_DENIED
        assert DENIAL in d["error"]

    @pytest.mark.asyncio
    async def test_a_later_reason_does_not_inherit_the_code(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers_instances import _status_for

        def denied(*a, **k):
            t = _DeniedTunnel(*a, **k)
            t.start_result = False
            return t

        mgr = self._mgr(tmp_path, monkeypatch, denied)
        await mgr.connect("cd-1")
        mgr._last_error["cd-1"] = "token mint failed: nope"
        d = _status_for(types.SimpleNamespace(instances_manager=mgr), "cd-1")
        assert d["error"] == "token mint failed: nope"
        assert "error_code" not in d

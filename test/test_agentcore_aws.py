"""IaC-installed AgentCore extra — boto3 is mocked; no live AWS."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time

import pytest

from kiro_crew.platform.interfaces import SessionPrincipal


def _principal(*, jwt: str | None = None) -> SessionPrincipal:
    return SessionPrincipal(
        surface="dashboard",
        subject="dashboard+owner",
        session_key="agent:main:main",
        user_jwt=jwt,
    )


def _jwt(*, exp: float | None = None) -> str:
    payload = {"exp": int(exp if exp is not None else time.time() + 600)}
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"hdr.{body}.sig"


def test_importing_the_module_does_not_import_boto3() -> None:
    sys.modules.pop("kiro_crew.platform.agentcore_aws", None)
    before = "boto3" in sys.modules
    import kiro_crew.platform.agentcore_aws as aws_mod

    assert aws_mod.ENV_WORKLOAD
    if not before:
        assert "boto3" not in sys.modules


def test_importing_the_module_does_not_import_sigv4() -> None:
    sys.modules.pop("kiro_crew.platform.agentcore_aws", None)
    sys.modules.pop("kiro_crew.platform.agentcore_sigv4", None)
    import kiro_crew.platform.agentcore_aws as aws_mod

    aws_mod.AwsAgentIdentityProvider().gateway_mcp_spec()
    assert "kiro_crew.platform.agentcore_sigv4" not in sys.modules


def test_opted_in_requires_name_and_flag_or_posture(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.delenv(aws_mod.ENV_WORKLOAD, raising=False)
    monkeypatch.delenv(aws_mod.ENV_AWS, raising=False)
    monkeypatch.delenv(aws_mod.ENV_POSTURE, raising=False)
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    assert aws_mod.opted_in() is False

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    assert aws_mod.opted_in() is False

    monkeypatch.setenv(aws_mod.ENV_AWS, "1")
    assert aws_mod.opted_in() is True

    monkeypatch.delenv(aws_mod.ENV_AWS)
    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    assert aws_mod.opted_in() is True

    monkeypatch.setenv(aws_mod.ENV_POSTURE, "none")
    assert aws_mod.opted_in() is False


def test_opted_in_from_policy_posture_without_env_name(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform.governance import parse_policy

    monkeypatch.delenv(aws_mod.ENV_WORKLOAD, raising=False)
    monkeypatch.delenv(aws_mod.ENV_AWS, raising=False)
    monkeypatch.delenv(aws_mod.ENV_POSTURE, raising=False)
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": True, "posture": "login"}},
        }
    )
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: ceiling)
    monkeypatch.setattr(aws_mod, "authored_workload_name", lambda: "")
    assert aws_mod.opted_in() is True
    assert aws_mod.resolved_workload_name() == ""


def test_opted_in_ignores_home_file_when_fleet_ceiling_disables(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform.governance import parse_policy

    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: "login")
    fleet = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": False}},
        }
    )
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: fleet)
    assert aws_mod.opted_in() is False


def test_resolved_workload_name_launch_env_uses_rfc_default(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.delenv(aws_mod.ENV_WORKLOAD, raising=False)
    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: None)
    monkeypatch.setattr(aws_mod, "authored_workload_name", lambda: "")
    assert aws_mod.resolved_workload_name() == aws_mod.DEFAULT_WORKLOAD_NAME


def test_resolved_workload_name_prefers_policy_over_env(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew")
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setattr(aws_mod, "authored_workload_name", lambda: "kirocrew-e2e")
    assert aws_mod.resolved_workload_name() == "kirocrew-e2e"
    monkeypatch.setattr(aws_mod, "authored_workload_name", lambda: "")
    assert aws_mod.resolved_workload_name() == "kirocrew"


def test_normalize_agentcore_workload_name() -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    assert aws_mod.normalize_agentcore_workload_name("") == ""
    assert aws_mod.normalize_agentcore_workload_name("  kirocrew-e2e  ") == "kirocrew-e2e"
    with pytest.raises(ValueError):
        aws_mod.normalize_agentcore_workload_name("ab")
    with pytest.raises(ValueError):
        aws_mod.normalize_agentcore_workload_name("has space")


def test_try_aws_returns_none_when_boto3_missing(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    monkeypatch.setenv(aws_mod.ENV_AWS, "1")
    monkeypatch.setattr(aws_mod, "extra_available", lambda: False)
    assert aws_mod.try_aws_agent_identity() is None


def test_status_has_no_token_material(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "https://gw.example.test/mcp")
    status = aws_mod.AwsAgentIdentityProvider().status()
    dumped = json.dumps(status)
    assert "workloadAccessToken" not in dumped
    assert "Authorization" not in dumped
    assert not any(k.lower() in {"token", "secret", "bearer"} for k in status)
    assert status["adapter"] == "aws"
    assert status["credentialKind"] == "m2m"
    assert status["vaultedOwnerToken"] is False
    assert status["gatewayUrlConfigured"] is True


def test_gateway_mcp_spec_requires_https(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    provider = aws_mod.AwsAgentIdentityProvider()
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.delenv(aws_mod.ENV_GATEWAY_URL, raising=False)
    monkeypatch.delenv(aws_mod.ENV_POSTURE, raising=False)
    monkeypatch.setattr(aws_mod, "authored_gateway_url", lambda: "")
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: None)
    assert provider.gateway_mcp_spec() is None
    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "http://insecure.example/mcp")
    assert provider.gateway_mcp_spec() is None
    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "https://gw.example.test/mcp")
    assert provider.gateway_mcp_spec() == {"url": "https://gw.example.test/mcp"}


def test_gateway_mcp_spec_workload_returns_https_hostname(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    provider = aws_mod.AwsAgentIdentityProvider()
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "https://gw.example.test/mcp")
    monkeypatch.setattr(aws_mod, "authored_gateway_url", lambda: "")
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: None)
    # This PR does not start a SigV4 proxy. Workload still returns the
    # https hostname; a later PR rewrites it onto localhost.
    assert provider.gateway_mcp_spec() == {"url": "https://gw.example.test/mcp"}


def test_resolved_gateway_url_prefers_policy_over_env(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "https://env.example.test/mcp")
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setattr(aws_mod, "authored_gateway_url", lambda: "https://policy.example.test/mcp")
    assert aws_mod.resolved_gateway_url() == "https://policy.example.test/mcp"
    monkeypatch.setattr(aws_mod, "authored_gateway_url", lambda: "")
    assert aws_mod.resolved_gateway_url() == "https://env.example.test/mcp"


def test_resolved_posture_prefers_policy_over_env(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: "login")
    assert aws_mod.resolved_posture() == "login"
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: None)
    assert aws_mod.resolved_posture() == "workload"


def test_resolved_identity_uses_fleet_ceiling_not_home_peek(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform.governance import parse_policy

    monkeypatch.setenv(aws_mod.ENV_POSTURE, "workload")
    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "https://env.example.test/mcp")
    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "env-workload")
    monkeypatch.setattr(aws_mod, "authored_posture", lambda: "login")
    monkeypatch.setattr(aws_mod, "authored_gateway_url", lambda: "https://home.example.test/mcp")
    monkeypatch.setattr(aws_mod, "authored_workload_name", lambda: "home-workload")
    fleet = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": False}},
        }
    )
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: fleet)
    assert aws_mod.resolved_posture() == ""
    assert aws_mod.resolved_gateway_url() == ""
    assert aws_mod.resolved_workload_name() == ""


def test_resolved_identity_reads_composed_ceiling_fields(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform.governance import parse_policy

    monkeypatch.setattr(aws_mod, "authored_posture", lambda: "workload")
    monkeypatch.setattr(aws_mod, "authored_gateway_url", lambda: "https://home.example.test/mcp")
    monkeypatch.setattr(aws_mod, "authored_workload_name", lambda: "home-workload")
    fleet = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {
                "agentcore": {
                    "enabled": True,
                    "posture": "login",
                    "gateway_url": "https://fleet.example.test/mcp",
                    "workload_name": "fleet-workload",
                }
            },
        }
    )
    monkeypatch.setattr(aws_mod, "_effective_governance_ceiling", lambda: fleet)
    assert aws_mod.resolved_posture() == "login"
    assert aws_mod.resolved_gateway_url() == "https://fleet.example.test/mcp"
    assert aws_mod.resolved_workload_name() == "fleet-workload"


def test_vend_workload_access_token_uses_standalone_api(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    calls: list[tuple[str, dict]] = []

    class _Client:
        def get_workload_access_token(self, **kwargs):
            calls.append(("standalone", kwargs))
            return {"workloadAccessToken": "wat-m2m"}

        def get_workload_access_token_for_jwt(self, **kwargs):
            calls.append(("jwt", kwargs))
            return {"workloadAccessToken": "wat-user"}

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    monkeypatch.setattr(aws_mod, "_client", lambda: _Client())
    token = asyncio.run(aws_mod.AwsAgentIdentityProvider().vend_workload_access_token(_principal()))
    assert token == "wat-m2m"
    assert calls == [("standalone", {"workloadName": "kirocrew-kc-abc"})]


def test_vend_workload_access_token_for_jwt(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    class _Client:
        def get_workload_access_token_for_jwt(self, **kwargs):
            return {"workloadAccessToken": "wat-user"}

        def get_workload_access_token(self, **kwargs):
            raise AssertionError("standalone path must not run when a user JWT is present")

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    monkeypatch.setattr(aws_mod, "_client", lambda: _Client())
    jwt = _jwt()
    token = asyncio.run(
        aws_mod.AwsAgentIdentityProvider().vend_workload_access_token(_principal(jwt=jwt))
    )
    assert token == "wat-user"


def test_inbound_token_is_operator_jwt_not_wat(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_GATEWAY_URL, "https://gw.example.test/mcp")
    jwt = _jwt(exp=1_800_000_000)
    inbound = asyncio.run(
        aws_mod.AwsAgentIdentityProvider().vend_gateway_inbound_token(_principal(jwt=jwt))
    )
    assert inbound is not None
    assert inbound.scheme == "bearer"
    assert inbound.token == jwt
    assert inbound.expires_at == 1_800_000_000.0
    assert inbound.audience == "https://gw.example.test/mcp"


def test_inbound_token_absent_without_user_jwt() -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    inbound = asyncio.run(
        aws_mod.AwsAgentIdentityProvider().vend_gateway_inbound_token(_principal())
    )
    assert inbound is None


def test_vend_returns_none_when_client_missing(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    monkeypatch.setattr(aws_mod, "_client", lambda: None)
    token = asyncio.run(aws_mod.AwsAgentIdentityProvider().vend_workload_access_token(_principal()))
    assert token is None


def test_bootstrap_attaches_adapter_only_when_opted_in(monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform import bootstrap
    from kiro_crew.platform.defaults import DefaultAgentIdentityProvider

    monkeypatch.setattr(aws_mod, "opted_in", lambda: False)
    ctx = bootstrap.build_default_context(KiroCrewConfig.load())
    # build_default_context itself stays Default; bootstrap_context does the swap.
    assert isinstance(ctx.agent_identity, DefaultAgentIdentityProvider)

    class _Fake(aws_mod.AwsAgentIdentityProvider):
        pass

    monkeypatch.setattr(aws_mod, "try_aws_agent_identity", lambda: _Fake())
    monkeypatch.setattr(bootstrap, "plugin_entry_points", lambda: ())
    monkeypatch.setattr(bootstrap, "resolve_profile", lambda *a, **k: "standalone")
    bootstrap._reset_boot_state()
    ctx = bootstrap.bootstrap_context(KiroCrewConfig.load())
    assert isinstance(ctx.agent_identity, _Fake)


def test_workload_identity_name_from_env(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setenv(aws_mod.ENV_WORKLOAD, "kirocrew-kc-abc")
    monkeypatch.setattr(
        aws_mod,
        "_workload_arn",
        lambda name: f"arn:aws:bedrock-agentcore:us-east-1:1:workload-identity/{name}",
    )
    ident = aws_mod.AwsAgentIdentityProvider().workload_identity()
    assert ident is not None
    assert ident.name == "kirocrew-kc-abc"
    assert ident.name in ident.arn


def test_ensure_extra_skips_pip_when_already_installed(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setattr(aws_mod, "extra_available", lambda: True)

    def _fail_run(*_a, **_k):
        raise AssertionError("pip must not run when boto3 is already importable")

    monkeypatch.setattr(aws_mod.subprocess, "run", _fail_run)
    assert aws_mod.ensure_extra() == aws_mod.EXTRA_CODE_OK


def test_ensure_extra_refuses_without_install_channel(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setattr(aws_mod, "extra_available", lambda: False)
    monkeypatch.setattr(aws_mod, "pip_install_channel_available", lambda: False)

    def _fail_run(*_a, **_k):
        raise AssertionError("pip must not run without an install channel")

    monkeypatch.setattr(aws_mod.subprocess, "run", _fail_run)
    assert aws_mod.ensure_extra() == aws_mod.EXTRA_CODE_NO_CHANNEL


def test_ensure_extra_installs_editable_checkout(monkeypatch, tmp_path) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    root = tmp_path / "checkout"
    root.mkdir()
    (root / "setup.cfg").write_text("[metadata]\nname = kirocrew\n", encoding="utf-8")
    monkeypatch.setenv(aws_mod.ENV_PROJECT_DIR, str(root))
    monkeypatch.setattr(aws_mod, "extra_available", lambda: False)
    monkeypatch.setattr(aws_mod, "pip_install_channel_available", lambda: True)
    seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, **_kwargs):
        seen.append(list(argv))
        monkeypatch.setattr(aws_mod, "extra_available", lambda: True)
        return _Result()

    monkeypatch.setattr(aws_mod.subprocess, "run", _run)
    assert aws_mod.ensure_extra() == aws_mod.EXTRA_CODE_OK
    assert seen == [[aws_mod.sys.executable, "-m", "pip", "install", "-e", f"{root}[agentcore]"]]


def test_ensure_extra_installs_wheel_when_no_checkout(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.delenv(aws_mod.ENV_PROJECT_DIR, raising=False)
    monkeypatch.setattr(aws_mod, "extra_available", lambda: False)
    monkeypatch.setattr(aws_mod, "pip_install_channel_available", lambda: True)
    seen: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(argv, **_kwargs):
        seen.append(list(argv))
        monkeypatch.setattr(aws_mod, "extra_available", lambda: True)
        return _Result()

    monkeypatch.setattr(aws_mod.subprocess, "run", _run)
    assert aws_mod.ensure_extra() == aws_mod.EXTRA_CODE_OK
    assert seen == [[aws_mod.sys.executable, "-m", "pip", "install", aws_mod.EXTRA_REQ_WHEEL]]


def test_ensure_extra_reports_install_failed(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.delenv(aws_mod.ENV_PROJECT_DIR, raising=False)
    monkeypatch.setattr(aws_mod, "extra_available", lambda: False)
    monkeypatch.setattr(aws_mod, "pip_install_channel_available", lambda: True)

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "nope"

    monkeypatch.setattr(aws_mod.subprocess, "run", lambda *_a, **_k: _Result())
    assert aws_mod.ensure_extra() == aws_mod.EXTRA_CODE_FAILED


def test_bootstrap_does_not_pip_extra_when_opted_in(monkeypatch) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform import bootstrap

    calls: list[str] = []
    monkeypatch.setattr(aws_mod, "opted_in", lambda: True)
    monkeypatch.setattr(aws_mod, "extra_available", lambda: False)
    monkeypatch.setattr(
        aws_mod, "ensure_extra", lambda: calls.append("ensure") or aws_mod.EXTRA_CODE_OK
    )
    monkeypatch.setattr(aws_mod, "try_aws_agent_identity", lambda: None)
    monkeypatch.setattr(bootstrap, "plugin_entry_points", lambda: ())
    monkeypatch.setattr(bootstrap, "resolve_profile", lambda *a, **k: "standalone")
    bootstrap._reset_boot_state()
    bootstrap.bootstrap_context(KiroCrewConfig.load())
    assert calls == []


def test_normalize_agentcore_gateway_url() -> None:
    from kiro_crew.cloud import iam

    assert iam.normalize_agentcore_gateway_url("") == ""
    assert (
        iam.normalize_agentcore_gateway_url("https://gw.example.test/mcp")
        == "https://gw.example.test/mcp"
    )
    try:
        iam.normalize_agentcore_gateway_url("http://gw.example.test/mcp")
        raise AssertionError("http must be refused")
    except ValueError:
        pass
    try:
        iam.normalize_agentcore_gateway_url("https://user:pass@gw.example.test/mcp")
        raise AssertionError("credentials must be refused")
    except ValueError:
        pass
    try:
        iam.normalize_agentcore_gateway_url("https://" + "g" * iam.AGENTCORE_GATEWAY_URL_MAX)
        raise AssertionError("over-long URL must be refused")
    except ValueError:
        pass


def test_probe_workload_identity_discards_token(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    class _Client:
        def get_workload_access_token(self, **kwargs):
            assert kwargs["workloadName"] == "kirocrew-e2e"
            return {"workloadAccessToken": "must-not-leak"}

    monkeypatch.setattr(aws_mod, "resolved_posture", lambda: "workload")
    monkeypatch.setattr(aws_mod, "resolved_workload_name", lambda: "kirocrew-e2e")
    monkeypatch.setattr(aws_mod, "_client", lambda: _Client())
    probed = aws_mod.probe_workload_identity()
    assert probed == {"ok": True, "detail": "ok", "name": "kirocrew-e2e"}
    assert "must-not-leak" not in json.dumps(probed)


def test_probe_workload_identity_service_linked(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    class Linked(Exception):
        response = {"Error": {"Code": "AccessDeniedException"}}

        def __str__(self) -> str:
            return "WorkloadIdentity is linked to a service"

    class _Client:
        def get_workload_access_token(self, **kwargs):
            raise Linked()

    monkeypatch.setattr(aws_mod, "resolved_posture", lambda: "workload")
    monkeypatch.setattr(aws_mod, "resolved_workload_name", lambda: "kirocrew-e2e-n9pk1rdrea")
    monkeypatch.setattr(aws_mod, "_client", lambda: _Client())
    probed = aws_mod.probe_workload_identity()
    assert probed["ok"] is False
    assert probed["detail"] == aws_mod.IDENTITY_PROBE_SERVICE_LINKED


def test_probe_workload_identity_skips_login(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod

    monkeypatch.setattr(aws_mod, "resolved_posture", lambda: "login")
    monkeypatch.setattr(aws_mod, "resolved_workload_name", lambda: "kirocrew-e2e")
    monkeypatch.setattr(
        aws_mod,
        "_client",
        lambda: (_ for _ in ()).throw(AssertionError("login must not vend a WAT")),
    )
    probed = aws_mod.probe_workload_identity()
    assert probed == {
        "ok": True,
        "detail": aws_mod.IDENTITY_PROBE_SKIP_LOGIN,
        "name": "kirocrew-e2e",
    }


def _runtime_ctx(*, agent_identity: object | None = None):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Ctx:
        governance: object | None = None
        agent_identity: object | None = None

    return _Ctx(agent_identity=agent_identity)


def test_apply_agentcore_runtime_swaps_ceiling_and_adapter(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance as gov_mod

    ceiling = object()
    adapter = object()
    captured: dict[str, object] = {}
    ctx = _runtime_ctx(agent_identity=object())

    monkeypatch.setattr(gov_mod, "load_security_policy", lambda: ceiling)
    monkeypatch.setattr(ctx_mod, "current_context", lambda: ctx)
    monkeypatch.setattr(ctx_mod, "set_context", lambda next_ctx: captured.update(ctx=next_ctx))
    monkeypatch.setattr(aws_mod, "opted_in", lambda: True)
    monkeypatch.setattr(aws_mod, "try_aws_agent_identity", lambda: adapter)

    assert aws_mod.apply_agentcore_runtime() is True
    applied = captured["ctx"]
    assert getattr(applied, "governance") is ceiling
    assert getattr(applied, "agent_identity") is adapter


def test_apply_agentcore_runtime_off_uses_default_adapter(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance as gov_mod
    from kiro_crew.platform.defaults import DefaultAgentIdentityProvider

    ceiling = object()
    captured: dict[str, object] = {}
    ctx = _runtime_ctx(agent_identity=object())

    monkeypatch.setattr(gov_mod, "load_security_policy", lambda: ceiling)
    monkeypatch.setattr(ctx_mod, "current_context", lambda: ctx)
    monkeypatch.setattr(ctx_mod, "set_context", lambda next_ctx: captured.update(ctx=next_ctx))
    monkeypatch.setattr(aws_mod, "opted_in", lambda: False)

    assert aws_mod.apply_agentcore_runtime() is True
    applied = captured["ctx"]
    assert getattr(applied, "governance") is ceiling
    assert isinstance(getattr(applied, "agent_identity"), DefaultAgentIdentityProvider)


def test_apply_agentcore_runtime_missing_extra_keeps_ceiling(monkeypatch) -> None:
    from kiro_crew.platform import agentcore_aws as aws_mod
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance as gov_mod

    ceiling = object()
    previous = object()
    captured: dict[str, object] = {}
    ctx = _runtime_ctx(agent_identity=previous)

    monkeypatch.setattr(gov_mod, "load_security_policy", lambda: ceiling)
    monkeypatch.setattr(ctx_mod, "current_context", lambda: ctx)
    monkeypatch.setattr(ctx_mod, "set_context", lambda next_ctx: captured.update(ctx=next_ctx))
    monkeypatch.setattr(aws_mod, "opted_in", lambda: True)
    monkeypatch.setattr(aws_mod, "try_aws_agent_identity", lambda: None)

    assert aws_mod.apply_agentcore_runtime() is False
    applied = captured["ctx"]
    assert getattr(applied, "governance") is ceiling
    assert getattr(applied, "agent_identity") is previous

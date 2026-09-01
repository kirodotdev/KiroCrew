"""AgentCore unattended Gateway policy — login never attaches; user/OBO fail closed."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy
from kiro_crew.platform.interfaces import InboundToken, SessionPrincipal

_GATEWAY_URL = "https://gateway.example.test/mcp"
_TOKEN = "sltok-unattended-must-not-leak"


class _CompanionIdentity(DefaultAgentIdentityProvider):
    def __init__(
        self,
        *,
        spec: dict[str, Any] | None = None,
        token: InboundToken | None = None,
        kind: str = "m2m",
        vaulted: bool = False,
    ) -> None:
        self._spec = spec
        self._token = token
        self._kind = kind
        self._vaulted = vaulted

    def enabled(self) -> bool:
        return True

    def gateway_mcp_spec(self) -> dict[str, object] | None:
        return self._spec

    def status(self) -> dict[str, object]:
        return {"credentialKind": self._kind, "vaultedOwnerToken": self._vaulted}

    async def vend_gateway_inbound_token(self, principal: SessionPrincipal) -> InboundToken | None:
        return self._token


def _ceiling(*, posture: str) -> Any:
    return parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": True, "posture": posture}},
        }
    )


def _install(
    *,
    posture: str,
    kind: str = "m2m",
    vaulted: bool = False,
    token: InboundToken | None = None,
) -> None:
    base = build_default_context(KiroCrewConfig())
    set_context(
        dataclasses.replace(
            base,
            agent_identity=_CompanionIdentity(
                spec={"url": _GATEWAY_URL},
                token=token
                or InboundToken(
                    scheme="bearer",
                    token=_TOKEN,
                    expires_at=4_000_000_000.0,
                    audience="g",
                ),
                kind=kind,
                vaulted=vaulted,
            ),
            governance=_ceiling(posture=posture),
        )
    )


def _cron() -> SessionPrincipal:
    return SessionPrincipal(surface="cron", subject="cron+owner", session_key="cron:job1")


def _dashboard() -> SessionPrincipal:
    return SessionPrincipal(
        surface="dashboard", subject="dashboard+alice", session_key="dashboard:1"
    )


@pytest.mark.asyncio
async def test_login_cron_never_attaches_gateway() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    _install(posture="login", kind="user")
    path = await attach_gateway_inbound(_cron())
    assert path is None
    assert inbound_sidecar_path("cron:job1").exists() is False
    assert session_gateway_servers("cron:job1") == []


@pytest.mark.asyncio
async def test_login_dashboard_still_attaches_when_vend_works() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        stage_session_gateway,
    )

    _install(posture="login", kind="user")
    stage_session_gateway("dashboard:1", "dashboard", "alice")
    path = await attach_gateway_inbound(_dashboard())
    assert path == inbound_sidecar_path("dashboard:1")
    assert path is not None and path.exists()


@pytest.mark.asyncio
async def test_workload_user_without_vault_retracts_gateway() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )
    from kiro_crew.sel import sel

    _install(posture="workload", kind="user", vaulted=False)
    assert await attach_gateway_inbound(_cron()) is None
    sidecar = json.loads(inbound_sidecar_path("cron:job1").read_text(encoding="utf-8"))
    assert sidecar["denied"] is True
    assert _TOKEN not in json.dumps(sidecar)
    injected = session_gateway_servers("cron:job1")
    from kiro_crew.platform.agentcore_gateway import ACP_DENIED_PLACEHOLDER_URL, acp_http_server

    assert injected == [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]
    events = [
        e for e in sel().recent(limit=50) if e.get("operation") == "agentcore.unattended_denied"
    ]
    assert events
    assert events[0].get("outcome") == "denied"
    assert _TOKEN not in json.dumps(events)


@pytest.mark.asyncio
async def test_workload_m2m_cron_keeps_agent_file_gateway() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    _install(posture="workload", kind="m2m")
    assert await attach_gateway_inbound(_cron()) is None
    assert inbound_sidecar_path("cron:job1").exists() is False
    # Companion https spec is the unsigned hostname — keep the agent-file
    # Gateway rather than injecting it unsigned onto session/new.
    assert session_gateway_servers("cron:job1") == []


@pytest.mark.asyncio
async def test_workload_m2m_cron_injects_live_loopback_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )
    from kiro_crew.platform.agentcore_sigv4 import proxy_auth_headers

    listen = "http://127.0.0.1:18765/mcp"
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.workload_proxy_auth_token",
        lambda: "proxy-test-token",
    )
    base = build_default_context(KiroCrewConfig())
    set_context(
        dataclasses.replace(
            base,
            agent_identity=_CompanionIdentity(spec={"url": listen}, kind="m2m"),
            governance=_ceiling(posture="workload"),
        )
    )
    assert await attach_gateway_inbound(_cron()) is None
    assert inbound_sidecar_path("cron:job1").exists() is False
    from kiro_crew.platform.agentcore_gateway import acp_http_server

    assert session_gateway_servers("cron:job1") == [
        acp_http_server(listen, headers=proxy_auth_headers("cron:job1"))
    ]


@pytest.mark.asyncio
async def test_workload_user_with_vault_allows_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    _install(posture="workload", kind="user", vaulted=True)
    assert await attach_gateway_inbound(_cron()) is None
    assert inbound_sidecar_path("cron:job1").exists() is False
    assert session_gateway_servers("cron:job1") == []


@pytest.mark.asyncio
async def test_unknown_credential_kind_fail_closed_for_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="workload", kind="", vaulted=False)
    await attach_gateway_inbound(_cron())
    injected = session_gateway_servers("cron:job1")
    assert injected and injected[0].get("disabled") is True


@pytest.mark.asyncio
async def test_unattended_deny_never_logs_token(caplog: pytest.LogCaptureFixture) -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound
    from kiro_crew.sel import sel

    _install(posture="login", kind="user")
    caplog.set_level(logging.DEBUG)
    await attach_gateway_inbound(_cron())
    assert _TOKEN not in caplog.text
    blob = json.dumps(sel().recent(limit=50))
    assert _TOKEN not in blob


@pytest.mark.asyncio
async def test_taskrunner_prefix_is_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="login", kind="user")
    principal = SessionPrincipal(
        surface="taskrunner",
        subject="taskrunner+owner",
        session_key="taskrunner:run1",
    )
    await attach_gateway_inbound(principal)
    assert session_gateway_servers("taskrunner:run1") == []


@pytest.mark.asyncio
async def test_meetings_prefix_is_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="login", kind="user")
    principal = SessionPrincipal(
        surface="meetings",
        subject="meetings+owner",
        session_key="meetings-note-taker-standup",
    )
    await attach_gateway_inbound(principal)
    assert session_gateway_servers("meetings-note-taker-standup") == []


@pytest.mark.asyncio
async def test_channel_agent_prefix_is_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="login", kind="user")
    principal = SessionPrincipal(
        surface="channel",
        subject="channel+agent-1",
        session_key="channel:ch1:agent-1",
    )
    await attach_gateway_inbound(principal)
    assert session_gateway_servers("channel:ch1:agent-1") == []


@pytest.mark.asyncio
async def test_hook_prefix_is_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="login", kind="user")
    principal = SessionPrincipal(
        surface="hook",
        subject="hook+owner",
        session_key="hook:review:pr-1",
    )
    await attach_gateway_inbound(principal)
    assert session_gateway_servers("hook:review:pr-1") == []


@pytest.mark.asyncio
async def test_workflow_prefixes_are_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="login", kind="user")
    for key in ("wf:run:0", "wf-pool:run-1:0", "wf-author:draft", "wf-unpooled:run:0"):
        principal = SessionPrincipal(
            surface="workflow",
            subject="workflow+owner",
            session_key=key,
        )
        await attach_gateway_inbound(principal)
        assert session_gateway_servers(key) == []


@pytest.mark.asyncio
async def test_workload_unattended_inject_without_attach_is_audited() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        ACP_DENIED_PLACEHOLDER_URL,
        acp_http_server,
        session_gateway_servers,
    )
    from kiro_crew.sel import sel

    _install(posture="workload", kind="user", vaulted=False)
    injected = session_gateway_servers("cron:job1")
    assert injected == [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]
    events = [
        e for e in sel().recent(limit=50) if e.get("operation") == "agentcore.unattended_denied"
    ]
    assert events
    assert events[0].get("outcome") == "denied"
    assert "cron:job1" in json.dumps(events)
    assert "user_without_vault" in json.dumps(events)
    assert _TOKEN not in json.dumps(events)


@pytest.mark.asyncio
async def test_login_unattended_inject_without_attach_is_audited() -> None:
    from kiro_crew.platform.agentcore_gateway import session_gateway_servers
    from kiro_crew.sel import sel

    _install(posture="login", kind="user")
    assert session_gateway_servers("cron:job1") == []
    events = [
        e for e in sel().recent(limit=50) if e.get("operation") == "agentcore.unattended_denied"
    ]
    assert events
    assert events[0].get("outcome") == "denied"
    assert "cron:job1" in json.dumps(events)
    assert _TOKEN not in json.dumps(events)


@pytest.mark.asyncio
async def test_background_and_heartbeat_keys_are_unattended() -> None:
    from kiro_crew.platform.agentcore_gateway import attach_gateway_inbound, session_gateway_servers

    _install(posture="login", kind="user")
    for key in ("_bg", "_hb"):
        principal = SessionPrincipal(
            surface="dashboard",
            subject="dashboard+owner",
            session_key=key,
        )
        await attach_gateway_inbound(principal)
        assert session_gateway_servers(key) == []


@pytest.mark.asyncio
async def test_unattended_bind_attach_error_writes_deny_sidecar(monkeypatch) -> None:
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
        session_gateway_servers,
    )
    from kiro_crew.sel import sel

    _install(posture="workload", kind="user", vaulted=False)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("attach exploded")

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_gateway.attach_gateway_inbound",
        _boom,
    )
    await prepare_session_gateway("cron:job1", surface="cron", raw_id="owner")
    sidecar = json.loads(inbound_sidecar_path("cron:job1").read_text(encoding="utf-8"))
    assert sidecar["denied"] is True
    assert sidecar.get("reason") == "attach_failed"
    assert _TOKEN not in json.dumps(sidecar)
    injected = session_gateway_servers("cron:job1")
    from kiro_crew.platform.agentcore_gateway import ACP_DENIED_PLACEHOLDER_URL, acp_http_server

    assert injected == [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]
    events = [
        e for e in sel().recent(limit=50) if e.get("operation") == "agentcore.unattended_denied"
    ]
    assert events
    assert events[0].get("outcome") == "denied"
    assert _TOKEN not in json.dumps(events)


@pytest.mark.asyncio
async def test_dashboard_bind_attach_error_does_not_write_deny_sidecar(monkeypatch) -> None:
    from kiro_crew.platform.agentcore_gateway import inbound_sidecar_path, prepare_session_gateway

    _install(posture="workload", kind="user", vaulted=False)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("attach exploded")

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_gateway.attach_gateway_inbound",
        _boom,
    )
    await prepare_session_gateway("dashboard:1", surface="dashboard", raw_id="alice")
    assert inbound_sidecar_path("dashboard:1").exists() is False


@pytest.mark.asyncio
async def test_unbound_prepare_recycles_live_session_before_clear() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
    )

    _install(posture="login", kind="user", vaulted=False)
    path = inbound_sidecar_path("dashboard:1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"name": "agentcore-gateway", "url": "https://gw.example.test/mcp"}),
        encoding="utf-8",
    )
    removed: list[str] = []

    class _Sessions:
        async def remove(self, key: str) -> None:
            removed.append(key)

    await prepare_session_gateway("dashboard:1", sessions=_Sessions())
    assert removed == ["dashboard:1"]
    assert path.exists() is False


@pytest.mark.asyncio
async def test_unbound_prepare_recycles_when_principal_live_sidecar_absent() -> None:
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
    )
    from kiro_crew.platform.interfaces import SessionPrincipal

    _install(posture="login", kind="user", vaulted=False)
    path = inbound_sidecar_path("dashboard:1")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    removed: list[str] = []

    class _Sessions:
        def get_principal(self, key: str) -> object:
            return SessionPrincipal(
                surface="dashboard",
                subject="dashboard+alice",
                session_key=key,
            )

        async def remove(self, key: str) -> None:
            removed.append(key)

    await prepare_session_gateway("dashboard:1", sessions=_Sessions())
    assert removed == ["dashboard:1"]
    assert path.exists() is False


@pytest.mark.asyncio
async def test_bound_attach_recycles_when_sidecar_changes(monkeypatch) -> None:
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
    )

    _install(posture="login", kind="user", vaulted=False)
    path = inbound_sidecar_path("dashboard:1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"old": True}), encoding="utf-8")
    removed: list[str] = []

    class _Sessions:
        async def remove(self, key: str) -> None:
            removed.append(key)

    async def _attach(_principal: object) -> object:
        path.write_text(json.dumps({"new": True}), encoding="utf-8")
        return path

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_gateway.attach_gateway_inbound",
        _attach,
    )
    await prepare_session_gateway(
        "dashboard:1",
        surface="dashboard",
        raw_id="alice",
        sessions=_Sessions(),
    )
    assert removed == ["dashboard:1"]


@pytest.mark.asyncio
async def test_bound_attach_error_clears_prior_sidecar_and_recycles(
    monkeypatch,
) -> None:
    from kiro_crew.platform.agentcore_gateway import (
        inbound_sidecar_path,
        prepare_session_gateway,
    )

    _install(posture="login", kind="user", vaulted=False)
    path = inbound_sidecar_path("dashboard:1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"old_bearer": True}), encoding="utf-8")
    removed: list[str] = []

    class _Sessions:
        async def remove(self, key: str) -> None:
            removed.append(key)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("attach exploded")

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_gateway.attach_gateway_inbound",
        _boom,
    )
    await prepare_session_gateway(
        "dashboard:1",
        surface="dashboard",
        raw_id="alice",
        sessions=_Sessions(),
    )
    assert path.exists() is False
    assert removed == ["dashboard:1"]


def test_unknown_session_key_defaults_to_unattended() -> None:
    """A caller-named session is not a human ChannelTurn."""
    from kiro_crew.platform.agentcore_gateway import is_unattended_session

    assert is_unattended_session("custom") is True
    assert is_unattended_session("wf-custom-run") is True
    assert is_unattended_session("agent:main:main") is True
    assert is_unattended_session("") is True
    assert is_unattended_session("_host") is True
    assert is_unattended_session("channel:notes:writer") is True
    assert is_unattended_session("meetings-crew-abc") is True
    assert is_unattended_session("dashboard:1") is True
    assert is_unattended_session("cli_chat") is False
    assert is_unattended_session("slack:T1:C1:111.222") is True
    assert is_unattended_session("discord:guild:user") is True
    assert is_unattended_session("slack:forged") is True

    from kiro_crew.platform.agentcore_gateway import stage_session_gateway

    stage_session_gateway("dashboard:1")
    assert is_unattended_session("dashboard:1") is True
    stage_session_gateway("dashboard:1", "dashboard", None)
    assert is_unattended_session("dashboard:1") is True
    stage_session_gateway("dashboard:1", None, "alice")
    assert is_unattended_session("dashboard:1") is True
    stage_session_gateway("dashboard:1", "dashboard", "alice")
    assert is_unattended_session("dashboard:1") is False
    stage_session_gateway("slack:T1:C1:111.222", "slack", "U1")
    assert is_unattended_session("slack:T1:C1:111.222") is False


@pytest.mark.asyncio
async def test_workload_user_forged_channel_key_retracts_without_vault() -> None:
    """ctx.agent(session='slack:forged') must not skip the vault check."""
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    _install(posture="workload", kind="user", vaulted=False)
    principal = SessionPrincipal(
        surface="workflow",
        subject="workflow+owner",
        session_key="slack:forged",
    )
    assert await attach_gateway_inbound(principal) is None
    sidecar = json.loads(inbound_sidecar_path("slack:forged").read_text(encoding="utf-8"))
    assert sidecar["denied"] is True
    injected = session_gateway_servers("slack:forged")
    from kiro_crew.platform.agentcore_gateway import ACP_DENIED_PLACEHOLDER_URL, acp_http_server

    assert injected == [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]


@pytest.mark.asyncio
async def test_workload_user_custom_session_retracts_without_vault() -> None:
    """ctx.agent(session='custom') must not inherit interactive user credentials."""
    from kiro_crew.platform.agentcore_gateway import (
        attach_gateway_inbound,
        inbound_sidecar_path,
        session_gateway_servers,
    )

    _install(posture="workload", kind="user", vaulted=False)
    principal = SessionPrincipal(surface="workflow", subject="workflow+owner", session_key="custom")
    assert await attach_gateway_inbound(principal) is None
    sidecar = json.loads(inbound_sidecar_path("custom").read_text(encoding="utf-8"))
    assert sidecar["denied"] is True
    assert _TOKEN not in json.dumps(sidecar)
    injected = session_gateway_servers("custom")
    from kiro_crew.platform.agentcore_gateway import ACP_DENIED_PLACEHOLDER_URL, acp_http_server

    assert injected == [acp_http_server(ACP_DENIED_PLACEHOLDER_URL, disabled=True)]

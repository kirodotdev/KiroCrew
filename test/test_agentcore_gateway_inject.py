"""AgentCore Gateway session/new inject — never persisted to the agent file."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from kiro_crew.agent import _merge_edition_mcp
from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.agentcore_gateway import (
    GATEWAY_SERVER_NAME,
    sanitize_gateway_spec,
    session_gateway_servers,
    strip_secret_spec_keys,
)
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.context import reset_context, set_context
from kiro_crew.platform.defaults import DefaultAgentIdentityProvider
from kiro_crew.platform.governance import parse_policy


class _ForcedOn(DefaultAgentIdentityProvider):
    def __init__(self, spec: dict[str, Any] | None) -> None:
        self._spec = spec

    def enabled(self) -> bool:
        return True

    def gateway_mcp_spec(self) -> dict[str, object] | None:
        return self._spec


def _install(*, posture: str, spec: dict[str, Any] | None) -> None:
    base = build_default_context(KiroCrewConfig())
    ceiling = parse_policy(
        {
            "version": 1,
            "boot": {"fail_closed": True},
            "capabilities": {"agentcore": {"enabled": True, "posture": posture}},
        }
    )
    set_context(
        dataclasses.replace(
            base,
            agent_identity=_ForcedOn(spec),
            governance=ceiling,
        )
    )


def test_sanitize_drops_authorization_headers() -> None:
    cleaned = sanitize_gateway_spec(
        {
            "url": "https://gw.example.test/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "Authorization": "Bearer secret",
        }
    )
    assert cleaned == {"url": "https://gw.example.test/mcp"}
    stripped = strip_secret_spec_keys({"url": "https://x", "headers": {"a": "b"}})
    assert "headers" not in stripped


_MANAGED_GATEWAY_URL = "https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def test_rebuild_never_writes_gateway_into_agent_file() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp = {GATEWAY_SERVER_NAME: {"url": _MANAGED_GATEWAY_URL}}
        _merge_edition_mcp(mcp)
        assert GATEWAY_SERVER_NAME not in mcp
    finally:
        reset_context()


def test_rebuild_preserves_operator_url_only_gateway() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        custom = {"url": "https://stale.example.test/mcp"}
        mcp = {GATEWAY_SERVER_NAME: custom}
        _merge_edition_mcp(mcp)
        assert mcp[GATEWAY_SERVER_NAME] == custom
    finally:
        reset_context()


def test_rebuild_retracts_managed_gateway_under_login_posture() -> None:
    try:
        _install(posture="login", spec={"url": "https://gw.example.test/mcp"})
        mcp = {GATEWAY_SERVER_NAME: {"url": _MANAGED_GATEWAY_URL}}
        _merge_edition_mcp(mcp)
        assert GATEWAY_SERVER_NAME not in mcp
    finally:
        reset_context()


def test_rebuild_keeps_non_dict_gateway_entry() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp = {GATEWAY_SERVER_NAME: "npx-operator-string"}
        _merge_edition_mcp(mcp)
        assert mcp[GATEWAY_SERVER_NAME] == "npx-operator-string"
    finally:
        reset_context()


def test_rebuild_keeps_operator_command_named_gateway() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        custom = {"command": "npx", "args": ["-y", "my-gateway"]}
        mcp = {GATEWAY_SERVER_NAME: custom}
        _merge_edition_mcp(mcp)
        assert mcp[GATEWAY_SERVER_NAME] == custom
    finally:
        reset_context()


def test_session_injects_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform.agentcore_sigv4 import (
        PROXY_AGENT_HEADER,
        PROXY_AUTH_HEADER,
        PROXY_SESSION_HEADER,
        bound_proxy_auth_token,
    )

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.workload_proxy_auth_token",
        lambda: "proxy-test-token",
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        servers = session_gateway_servers("agent:main:main", agent="researcher")
        assert len(servers) == 1
        assert servers[0]["url"] == "http://127.0.0.1:18765/mcp"
        assert servers[0]["type"] == "http"
        digest = bound_proxy_auth_token("proxy-test-token", "agent:main:main", "researcher")
        assert servers[0]["headers"] == [
            {"name": PROXY_AUTH_HEADER, "value": digest},
            {"name": PROXY_SESSION_HEADER, "value": "agent:main:main"},
            {"name": PROXY_AGENT_HEADER, "value": "researcher"},
        ]
    finally:
        reset_context()


def test_session_withholds_loopback_without_proxy_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.workload_proxy_auth_token",
        lambda: None,
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert session_gateway_servers("agent:main:main") == []
    finally:
        reset_context()


def test_session_never_injects_unsigned_https() -> None:
    try:
        _install(posture="workload", spec={"url": "https://gw.example.test/mcp"})
        assert session_gateway_servers("agent:main:main") == []
    finally:
        reset_context()


def test_agentcore_gateway_inject_is_kiro_only() -> None:
    from kiro_crew.acp.types import (
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_KAS,
        ACP_BACKEND_KIRO,
        ACP_BACKENDS_AGENTCORE_GATEWAY,
    )

    assert ACP_BACKENDS_AGENTCORE_GATEWAY == frozenset({ACP_BACKEND_KIRO})
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_AGENTCORE_GATEWAY
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_AGENTCORE_GATEWAY
    from kiro_crew.acp.client import AcpClient

    source = AcpClient._pooled_mcp_servers.__code__.co_names
    assert "ACP_BACKENDS_AGENTCORE_GATEWAY" in source
    from kiro_crew.acp.runtime import _mcp_servers_for_session

    runtime_source = _mcp_servers_for_session.__code__.co_names
    assert "ACP_BACKENDS_AGENTCORE_GATEWAY" in runtime_source
    assert "session_gateway_servers" in runtime_source
    assert "crew_agent" in _mcp_servers_for_session.__code__.co_varnames


def test_runtime_session_new_injects_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.acp.runtime import _mcp_servers_for_session
    from kiro_crew.acp.types import ACP_BACKEND_KIRO
    from kiro_crew.platform.agentcore_gateway import GATEWAY_SERVER_NAME
    from kiro_crew.platform.agentcore_sigv4 import PROXY_AUTH_HEADER

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4.workload_proxy_auth_token",
        lambda: "proxy-test-token",
    )
    monkeypatch.setattr(
        "kiro_crew.acp.runtime.pooled_session_servers",
        lambda *_a, **_k: [],
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        servers = _mcp_servers_for_session(
            None, "kirocrew", session_key="agent:main:main", backend=ACP_BACKEND_KIRO
        )
        assert any(item.get("name") == GATEWAY_SERVER_NAME for item in servers)
        headers = next(
            item.get("headers") or [] for item in servers if item.get("name") == GATEWAY_SERVER_NAME
        )
        assert any(pair.get("name") == PROXY_AUTH_HEADER for pair in headers)
    finally:
        reset_context()


def test_session_empty_without_session_key() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:9/mcp"})
        assert session_gateway_servers("") == []
    finally:
        reset_context()


def test_session_inject_audits_identity_decision() -> None:
    from kiro_crew.platform.agentcore_gateway import _identity_on
    from kiro_crew.sel import sel

    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert _identity_on("agent:main:main") is True
        events = [
            event
            for event in sel().recent(limit=50)
            if event.get("operation") == "agentcore.gateway_inject"
            and event.get("caller_identity") == "agent:main:main"
        ]
        assert events
        assert events[0].get("outcome") == "allowed"
    finally:
        reset_context()


def test_runtime_gateway_inject_uses_session_crew_agent() -> None:
    """Shared-runtime children must not inherit the parent runtime's permit."""
    import inspect

    from kiro_crew.acp.runtime import AcpRuntime

    create = inspect.getsource(AcpRuntime.create_session)
    load = inspect.getsource(AcpRuntime.load_session)
    assert 'crew_agent=_crew or ""' in create
    assert 'crew_agent=_crew or ""' in load
    assert "crew_agent=self._crew_agent" not in create
    assert "crew_agent=self._crew_agent" not in load


def test_shared_session_callers_pass_child_crew_agent() -> None:
    import inspect

    from kiro_crew.session import SessionManager
    from kiro_crew.subagent import SubagentManager

    shared = inspect.getsource(SubagentManager._create_shared_session)
    task = inspect.getsource(SessionManager.open_task_session)
    assert 'crew_agent=agent or ""' in shared
    assert 'crew_agent=agent or ""' in task

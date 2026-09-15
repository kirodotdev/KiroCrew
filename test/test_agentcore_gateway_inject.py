"""AgentCore Gateway session/new inject — never persisted to the agent file."""

from __future__ import annotations

import dataclasses
import json
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

    def status(self) -> dict[str, object]:
        return {"credentialKind": "m2m", "vaultedOwnerToken": False}


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
    """The Gateway is session-injected: with an installed Workload posture and a
    live gateway spec, the merge adds nothing under the reserved name, and an
    AgentCore URL already there (the operator's override) is withheld from the
    GENERATED config -- a session governance denies must not mount it."""
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp: dict = {}
        _merge_edition_mcp(mcp)
        assert GATEWAY_SERVER_NAME not in mcp
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


def test_rebuild_withholds_operator_gateway_url_under_login_posture(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Under Login the Gateway is reachable only after a person signs in; an
    AgentCore URL in the override would hand every session an unsigned remote.
    It is withheld from the generated config and the operator is told where it
    came from -- their override file itself is not modified."""
    try:
        _install(posture="login", spec={"url": "https://gw.example.test/mcp"})
        mcp = {GATEWAY_SERVER_NAME: {"url": _MANAGED_GATEWAY_URL}}
        with caplog.at_level("WARNING", logger="kiro_crew.agent"):
            _merge_edition_mcp(mcp)
        assert GATEWAY_SERVER_NAME not in mcp
        assert any(
            GATEWAY_SERVER_NAME in rec.getMessage() and "AgentCore Gateway" in rec.getMessage()
            for rec in caplog.records
        )
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


def test_rebuild_withholds_agentcore_gateway_entry_with_bearer() -> None:
    """Two properties at once: no bearer persists in the generated file, and no
    session mounts an unsigned Gateway outside the governed inject. Withholding
    the reserved-name AgentCore entry satisfies both."""
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp = {
            GATEWAY_SERVER_NAME: {
                "url": _MANAGED_GATEWAY_URL,
                "headers": {"Authorization": "Bearer leftover"},
                "Authorization": "Bearer leftover-2",
                "timeout": 30,
            }
        }
        _merge_edition_mcp(mcp)
        assert GATEWAY_SERVER_NAME not in mcp
    finally:
        reset_context()


def test_rebuild_withholds_gateway_url_under_any_server_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The governance question is what an entry REACHES, not what it is called: an
    AgentCore Gateway URL under a custom name (with bearer headers, even) is
    withheld from the generated config exactly like the reserved-name one, while
    an ordinary remote under a custom name is kept. The warning names the entry."""
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp = {
            "my-gateway": {
                "url": _MANAGED_GATEWAY_URL,
                "headers": {"Authorization": "Bearer leaked"},
            },
            "docs": {"url": "https://docs.example.test/mcp"},
            "kirocrew-core": {"command": "core"},
        }
        with caplog.at_level("WARNING", logger="kiro_crew.agent"):
            _merge_edition_mcp(mcp)
        assert "my-gateway" not in mcp
        assert mcp["docs"] == {"url": "https://docs.example.test/mcp"}
        assert mcp["kirocrew-core"] == {"command": "core"}
        assert any("my-gateway" in rec.getMessage() for rec in caplog.records)
    finally:
        reset_context()


def test_rebuild_never_adds_an_edition_extra_that_points_at_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.agent._extra_mcp_servers",
        lambda: {
            "vendor-gateway": {"url": _MANAGED_GATEWAY_URL},
            "vendor-docs": {"url": "https://vendor.example.test/mcp"},
        },
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp: dict = {}
        _merge_edition_mcp(mcp)
        assert "vendor-gateway" not in mcp
        assert mcp["vendor-docs"] == {"url": "https://vendor.example.test/mcp"}
    finally:
        reset_context()


def test_rebuild_preserves_non_agentcore_gateway_headers() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        custom = {
            "url": "https://stale.example.test/mcp",
            "headers": {"X-Operator": "keep"},
        }
        mcp = {GATEWAY_SERVER_NAME: custom}
        _merge_edition_mcp(mcp)
        assert mcp[GATEWAY_SERVER_NAME] == custom
    finally:
        reset_context()


def test_login_withhold_drops_command_only_edition_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.agent._extra_mcp_servers",
        lambda: {"edition-cli": {"command": "npx", "args": ["-y", "foo"]}},
    )
    try:
        _install(posture="login", spec={"url": "https://gw.example.test/mcp"})
        mcp: dict[str, Any] = {}
        _merge_edition_mcp(mcp)
        assert "edition-cli" not in mcp
    finally:
        reset_context()


def test_session_injects_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.platform.agentcore_sigv4 import (
        PROXY_AGENT_HEADER,
        PROXY_AUTH_HEADER,
        PROXY_GENERATION_HEADER,
        PROXY_SESSION_HEADER,
        bound_proxy_auth_token,
    )

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4._live_proxy",
        lambda: _fake_live_proxy(),
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        servers = session_gateway_servers("dashboard:1", agent="researcher")
        assert len(servers) == 1
        assert servers[0]["url"] == "http://127.0.0.1:18765/mcp"
        assert servers[0]["type"] == "http"
        by_name = {pair["name"]: pair["value"] for pair in servers[0]["headers"]}
        generation = by_name[PROXY_GENERATION_HEADER]
        assert len(generation) == 32
        # The digest is bound to the generation the inject registered.
        digest = bound_proxy_auth_token("proxy-test-token", "dashboard:1", "researcher", generation)
        assert by_name == {
            PROXY_AUTH_HEADER: digest,
            PROXY_SESSION_HEADER: "dashboard:1",
            PROXY_GENERATION_HEADER: generation,
            PROXY_AGENT_HEADER: "researcher",
        }
    finally:
        reset_context()


def test_session_withholds_loopback_without_proxy_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4._live_proxy",
        lambda: None,
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert session_gateway_servers("dashboard:1") == []
    finally:
        reset_context()


def test_denied_successor_retires_the_predecessor_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled predecessor's generation must not survive a successor the inject
    gates deny: the retire happens before the gates, and a replacement is minted
    only when every gate passes."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    proxy = _fake_live_proxy()
    monkeypatch.setattr(sigv4, "_PROXY", proxy)
    monkeypatch.setattr("kiro_crew.platform.agentcore_sigv4._live_proxy", lambda: proxy)
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert session_gateway_servers("agent:main:main", agent="researcher")
        predecessor = proxy.current_generation("agent:main:main")
        assert predecessor is not None

        # Successor 1: a login posture -> the workload inject denies it, and the
        # predecessor's generation is gone with it.
        _install(posture="login", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert session_gateway_servers("agent:main:main", agent="researcher") == []
        assert proxy.current_generation("agent:main:main") is None

        # Successor 2 (workload again, but the listener is down): still denied,
        # and no generation is minted for a denied inject.
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert session_gateway_servers("agent:main:main", agent="researcher")
        monkeypatch.setattr("kiro_crew.platform.agentcore_sigv4._live_proxy", lambda: None)
        assert session_gateway_servers("agent:main:main", agent="researcher") == []
        assert proxy.current_generation("agent:main:main") is None

        # A granted successor mints a FRESH generation, never the predecessor's.
        monkeypatch.setattr("kiro_crew.platform.agentcore_sigv4._live_proxy", lambda: proxy)
        assert session_gateway_servers("agent:main:main", agent="researcher")
        fresh = proxy.current_generation("agent:main:main")
        assert fresh is not None and fresh != predecessor
    finally:
        reset_context()


def test_session_never_injects_unsigned_https() -> None:
    try:
        _install(posture="workload", spec={"url": "https://gw.example.test/mcp"})
        assert session_gateway_servers("dashboard:1") == []
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
        "kiro_crew.platform.agentcore_sigv4._live_proxy",
        lambda: _fake_live_proxy(),
    )
    monkeypatch.setattr(
        "kiro_crew.acp.runtime.pooled_session_servers",
        lambda *_a, **_k: [],
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        servers = _mcp_servers_for_session(
            None, "kirocrew", session_key="dashboard:1", backend=ACP_BACKEND_KIRO
        )
        assert any(item.get("name") == GATEWAY_SERVER_NAME for item in servers)
        headers = next(
            item.get("headers") or [] for item in servers if item.get("name") == GATEWAY_SERVER_NAME
        )
        assert any(pair.get("name") == PROXY_AUTH_HEADER for pair in headers)
    finally:
        reset_context()


def test_workload_discards_persisted_login_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover login bearer must not reach session/new after a posture flip."""
    from kiro_crew.platform.agentcore_gateway import inbound_sidecar_path

    monkeypatch.setattr(
        "kiro_crew.platform.agentcore_sigv4._live_proxy",
        lambda: _fake_live_proxy(),
    )
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        path = inbound_sidecar_path("dashboard:1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "url": "https://gw.example.test/mcp",
                    "headers": {"Authorization": "Bearer leftover-login-jwt"},
                }
            ),
            encoding="utf-8",
        )
        servers = session_gateway_servers("dashboard:1")
        assert len(servers) == 1
        assert servers[0]["url"] == "http://127.0.0.1:18765/mcp"
        dumped = str(servers)
        assert "leftover-login-jwt" not in dumped
        assert "https://gw.example.test/mcp" not in dumped
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

    from kiro_crew.session_allocation import SessionAllocationService
    from kiro_crew.subagent_manager.run import RunEventCoordinator

    shared = inspect.getsource(RunEventCoordinator._create_shared_session_impl)
    task = inspect.getsource(SessionAllocationService.open_task_session)
    assert 'crew_agent=agent or ""' in shared
    assert "session_key=session_key" in shared
    assert 'crew_agent=agent or ""' in task


def test_gateway_url_is_withheld_regardless_of_a_co_present_command() -> None:
    """A ``command`` beside a Gateway ``url`` does not make the entry a plain
    stdio server: the question is what it REACHES, and the bearer headers it
    carries would persist all the same. Withheld like any other Gateway entry."""
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        mcp = {
            "mixed": {
                "command": "npx",
                "args": ["-y", "some-bridge"],
                "url": _MANAGED_GATEWAY_URL,
                "headers": {"Authorization": "Bearer leaked"},
            },
            "plain": {"command": "core"},
        }
        _merge_edition_mcp(mcp)
        assert "mixed" not in mcp
        assert mcp["plain"] == {"command": "core"}
    finally:
        reset_context()


def test_final_map_governance_pass_withholds_gateway_entries_from_any_source() -> None:
    """Every agent-config writer (host install/rebuild, fork refresh, worker and
    conductor installers, app-agent materialization) runs its FINAL server map
    through ``strip_ungoverned_auto_approve``; a Gateway URL that reaches that
    map from an app manifest, a per-agent policy or the ambient mcp.json copy
    -- i.e. AFTER ``_merge_edition_mcp`` ran -- is withheld there."""
    from kiro_crew.platform.governance import (
        is_agentcore_gateway_entry,
        strip_ungoverned_auto_approve,
        withhold_agentcore_gateway_entries,
    )

    servers = {
        "app:gw": {"url": _MANAGED_GATEWAY_URL, "headers": {"Authorization": "Bearer x"}},
        "app:mixed": {"command": "bridge", "url": _MANAGED_GATEWAY_URL},
        "app:docs": {"url": "https://docs.example.test/mcp"},
        "app:tools": {"command": "tools"},
        "weird": "not-an-object",
    }
    kept = withhold_agentcore_gateway_entries(servers)
    assert set(kept) == {"app:docs", "app:tools", "weird"}
    assert set(strip_ungoverned_auto_approve(servers)) == {"app:docs", "app:tools", "weird"}
    assert is_agentcore_gateway_entry(servers["app:mixed"]) is True
    assert is_agentcore_gateway_entry(servers["app:docs"]) is False
    assert is_agentcore_gateway_entry("not-an-object") is False
    # Both writers' local aliases go through the same pass.
    from kiro_crew import agent as agent_mod
    from kiro_crew.apps import bridges

    assert "app:gw" not in agent_mod._strip_ungoverned_auto_approve(servers)
    assert "app:gw" not in bridges._strip_ungoverned_auto_approve(servers)


def test_current_posture_is_the_effective_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-only (CloudFormation) configuration reaches the inject gate; a loaded
    ceiling that disables AgentCore is not undone by leftover env."""
    from kiro_crew.platform import agentcore_aws
    from kiro_crew.platform.agentcore_gateway import _current_posture

    monkeypatch.setattr(agentcore_aws, "_effective_governance_ceiling", lambda: None)
    monkeypatch.setattr(agentcore_aws, "authored_posture", lambda: None)
    monkeypatch.setenv(agentcore_aws.ENV_POSTURE, "workload")
    assert _current_posture() == "workload"
    monkeypatch.setenv(agentcore_aws.ENV_POSTURE, "bogus")
    assert _current_posture() is None

    class _Disabled:
        pass

    monkeypatch.setenv(agentcore_aws.ENV_POSTURE, "workload")
    monkeypatch.setattr(agentcore_aws, "_effective_governance_ceiling", lambda: _Disabled())
    monkeypatch.setattr("kiro_crew.platform.governance.agentcore_posture", lambda _gov: None)
    assert _current_posture() is None


def _fake_live_proxy():
    """A never-started proxy with a known per-boot token: registers real generations."""
    from kiro_crew.platform import agentcore_sigv4 as sigv4

    proxy = sigv4.GatewaySigV4Proxy(
        "https://x.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp", region="us-west-2"
    )
    proxy.client_token = "proxy-test-token"
    return proxy

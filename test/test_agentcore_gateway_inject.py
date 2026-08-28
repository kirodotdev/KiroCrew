"""AgentCore Gateway URL-only rebuild + live session/new inject."""

from __future__ import annotations

import dataclasses
from typing import Any

from kiro_crew.config import KiroCrewConfig
from kiro_crew.platform.agentcore_gateway import (
    GATEWAY_SERVER_NAME,
    rebuild_gateway_contribution,
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


def test_rebuild_contributes_workload_url_only() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        assert rebuild_gateway_contribution() == {
            GATEWAY_SERVER_NAME: {"url": "http://127.0.0.1:18765/mcp"}
        }
    finally:
        reset_context()


def test_rebuild_skips_login_posture() -> None:
    try:
        _install(posture="login", spec={"url": "https://gw.example.test/mcp"})
        assert rebuild_gateway_contribution() == {}
    finally:
        reset_context()


def test_session_injects_loopback_only() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:18765/mcp"})
        servers = session_gateway_servers("agent:main:main")
        assert len(servers) == 1
        assert servers[0]["url"] == "http://127.0.0.1:18765/mcp"
        assert servers[0]["type"] == "http"
        assert servers[0]["headers"] == []
    finally:
        reset_context()


def test_session_never_injects_unsigned_https() -> None:
    try:
        _install(posture="workload", spec={"url": "https://gw.example.test/mcp"})
        assert session_gateway_servers("agent:main:main") == []
    finally:
        reset_context()


def test_session_empty_without_session_key() -> None:
    try:
        _install(posture="workload", spec={"url": "http://127.0.0.1:9/mcp"})
        assert session_gateway_servers("") == []
    finally:
        reset_context()

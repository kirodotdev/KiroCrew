"""Member context uses ordinary MCP projection on new, resumed and rekeyed sessions."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
)
from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER


class _RequestCaptured(Exception):
    """Stop at the transport boundary without starting a provider process."""


@pytest.fixture
def broker_overlay(tmp_path):
    overlay = tmp_path / "mcp-gateway" / "agents"
    overlay.mkdir(parents=True)
    for agent in ("kirocrew", "another-agent"):
        (overlay / f"{agent}.json").write_text(
            json.dumps(
                {
                    "name": agent,
                    "mcpServers": {
                        "builder": {
                            _WRAPPER_MARKER: True,
                            "command": "broker-stub",
                            "args": ["--socket", str(overlay.parent / "gateway.sock")],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
    return overlay


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE])
@pytest.mark.parametrize("entry", ["new", "load", "reset-and-rekey"])
async def test_client_session_requests_preserve_ordinary_broker_routing(
    tmp_path,
    monkeypatch,
    broker_overlay,
    backend,
    entry,
):
    socket = broker_overlay.parent / "gateway.sock"
    client = AcpClient(
        work_dir=tmp_path,
        agent="kirocrew",
        acp_backend=backend,
        mcp_gateway_overlay=broker_overlay,
        mcp_gateway_socket=socket,
    )

    # Model the direct Claude projection after spawn; its resolver is exercised
    # separately below. Kiro reads its original agent spec without an injection.
    # The claude mirror now places the pooled broker stubs INTO the projection
    # (held to the spec's `tools` allowlist), so the post-spawn cache carries the
    # stub for a session using the broker; the shared _pooled_mcp_servers append is
    # inert for mirrored backends and must not re-add it at the call site.
    def _projected_cache() -> list:
        cache = [{"name": "direct", "command": "local-mcp", "args": [], "env": []}]
        if backend == ACP_BACKEND_CLAUDE:
            cache.append({"name": "builder", "command": "broker-stub", "args": [], "env": []})
        return cache

    client._session_mcp_cache = _projected_cache()
    claims = Mock()
    monkeypatch.setattr(client_mod, "schedule_claim", claims)
    if entry == "reset-and-rekey":
        client._reset_state()
        client.rekey("dashboard:next", channel_id="next-channel")
        client._agent = "another-agent"
        client._session_mcp_cache = _projected_cache()
        assert claims.call_args.args[0] == str(socket)

    sent = []

    async def capture(method, params):
        if method in {METHOD_SESSION_NEW, METHOD_SESSION_LOAD}:
            sent.append((method, params))
            raise _RequestCaptured
        return 1

    monkeypatch.setattr(client, "_send_request", capture)
    monkeypatch.setattr(
        client,
        "_wait_for_response",
        AsyncMock(
            return_value={
                "agentCapabilities": {"loadSession": True},
            }
        ),
    )
    if entry == "load":
        (tmp_path / "prior.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(client_mod, "kiro_sessions_dir", lambda: tmp_path)
        client._resume_session_id = "prior"
        operation = client._initialize_session()
    else:
        operation = client._new_session_following_substitution()
    with pytest.raises(_RequestCaptured):
        await asyncio.wait_for(operation, timeout=5)
    assert len(sent) == 1
    assert sent[0][0] == (METHOD_SESSION_LOAD if entry == "load" else METHOD_SESSION_NEW)
    entries = sent[0][1]["mcpServers"]
    assert any(e["command"] == "broker-stub" for e in entries)
    assert any(e["command"] == "local-mcp" for e in entries) is (backend == ACP_BACKEND_CLAUDE)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new", "load"])
async def test_runtime_session_requests_preserve_broker_routing(
    tmp_path,
    monkeypatch,
    broker_overlay,
    entry,
):
    runtime = AcpRuntime(
        work_dir=tmp_path,
        agent="kirocrew",
        mcp_gateway_overlay=broker_overlay,
        mcp_gateway_socket=broker_overlay.parent / "gateway.sock",
    )
    runtime._initialized = True
    runtime._can_load_session = True
    runtime._session_start_timeout = 5
    sent = []

    async def capture(method, params, timeout=None):
        sent.append((method, params))
        raise _RequestCaptured

    monkeypatch.setattr(runtime, "_send_and_await", capture)
    operation = (
        runtime.create_session(agent="another-agent")
        if entry == "new"
        else runtime.load_session(str(tmp_path / "prior.json"), "prior", agent="another-agent")
    )
    with pytest.raises(_RequestCaptured):
        await asyncio.wait_for(operation, timeout=5)
    assert len(sent) == 1
    assert sent[0][0] == (METHOD_SESSION_NEW if entry == "new" else METHOD_SESSION_LOAD)
    entries = sent[0][1]["mcpServers"]
    assert any(e["command"] == "broker-stub" for e in entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("pooled", [False, True])
async def test_kas_projection_preserves_direct_or_pooled_tools(
    tmp_path, monkeypatch, broker_overlay, pooled
):
    agents = tmp_path / "original-agents"
    agents.mkdir()
    (agents / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "prompt": "Test agent",
                "tools": ["@builder"],
                "mcpServers": {
                    "builder": {"command": "local-mcp", "args": ["serve"], "env": {"K": "V"}}
                },
            }
        ),
        encoding="utf-8",
    )
    import kiro_crew.config.paths as paths_mod

    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: True)
    runtime = AcpRuntime(
        work_dir=tmp_path,
        acp_backend=ACP_BACKEND_KAS,
        mcp_gateway_overlay=broker_overlay if pooled else None,
        member_context=True,
    )
    extras = await asyncio.wait_for(runtime._kas_custom_agents("kirocrew"), timeout=5)
    projected = extras.custom_agents
    assert projected
    server = projected[0].get("mcpServers", {}).get("builder")
    assert (server is not None) is (not pooled)
    if not pooled:
        assert server["command"] == "local-mcp"
        assert server["args"] == ["serve"]
        # KAS deliberately strips credential-bearing env during projection.
        assert "env" not in server


@pytest.mark.parametrize("constructor", [AcpClient, AcpRuntime])
@pytest.mark.parametrize("backend", ["", "codex", "claude", "kas"])
def test_memory_does_not_add_backend_admission(constructor, backend, tmp_path):
    instance = constructor(work_dir=tmp_path, acp_backend=backend)
    assert instance._process is None

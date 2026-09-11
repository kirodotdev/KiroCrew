"""Private providers retain direct MCP tools across every session entry point."""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp import runtime as runtime_mod
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
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE])
@pytest.mark.parametrize("entry", ["new", "load", "reset-and-rekey"])
async def test_client_session_requests_do_not_restore_private_broker_routing(
    tmp_path,
    monkeypatch,
    broker_overlay,
    private,
    backend,
    entry,
):
    socket = broker_overlay.parent / "gateway.sock"
    client = AcpClient(
        work_dir=tmp_path,
        agent="kirocrew",
        acp_backend=backend,
        private_memory=private,
        mcp_gateway_overlay=broker_overlay,
        mcp_gateway_socket=socket,
    )
    # Model the direct Claude projection after spawn; its resolver is exercised
    # separately below. Kiro reads its original agent spec without an injection.
    client._session_mcp_cache = [{"name": "direct", "command": "local-mcp", "args": [], "env": []}]
    claims = Mock()
    monkeypatch.setattr(client_mod, "schedule_claim", claims)
    if entry == "reset-and-rekey":
        client._reset_state()
        client.rekey("dashboard:next", channel_id="next-channel")
        client._agent = "another-agent"
        client._session_mcp_cache = [
            {"name": "direct", "command": "local-mcp", "args": [], "env": []}
        ]
        assert claims.call_args.args[0] == (None if private else str(socket))

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
    assert any(e["command"] == "broker-stub" for e in entries) is (not private)
    assert any(e["command"] == "local-mcp" for e in entries) is (backend == ACP_BACKEND_CLAUDE)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("entry", ["new", "load"])
async def test_runtime_session_requests_keep_private_tools_out_of_the_broker(
    tmp_path,
    monkeypatch,
    broker_overlay,
    private,
    entry,
):
    runtime = AcpRuntime(
        work_dir=tmp_path,
        agent="kirocrew",
        private_memory=private,
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
    assert any(e["command"] == "broker-stub" for e in entries) is (not private)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_kas_projection_preserves_direct_private_server(
    tmp_path, monkeypatch, broker_overlay, private
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
    monkeypatch.setattr(runtime_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(runtime_mod, "ensure_agent_materialized", lambda _agent: True)
    runtime = AcpRuntime(
        work_dir=tmp_path,
        acp_backend=ACP_BACKEND_KAS,
        private_memory=private,
        mcp_gateway_overlay=broker_overlay,
    )
    projected = await asyncio.wait_for(runtime._kas_custom_agents("kirocrew"), timeout=5)
    assert projected
    server = projected[0].get("mcpServers", {}).get("builder")
    assert (server is not None) is private
    if private:
        assert server["command"] == "local-mcp"
        assert server["args"] == ["serve"]
        # KAS deliberately strips credential-bearing env during projection.
        assert "env" not in server


@pytest.mark.parametrize("constructor", [AcpClient, AcpRuntime])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize(
    "configured,effective", [("", "codex"), ("codex", ""), ("codex", "claude"), ("codex", "kas")]
)
def test_private_backend_override_is_checked_before_allocation(
    tmp_path,
    monkeypatch,
    constructor,
    private,
    configured,
    effective,
):
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import UnknownMemoryStore

    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        lambda: SimpleNamespace(
            agent=SimpleNamespace(acp_backend=configured, member_acp_backend=configured),
        ),
    )
    if private and effective == "codex":
        with pytest.raises(UnknownMemoryStore, match="Codex ACP.*Use Kiro, Claude Code or KAS"):
            constructor(work_dir=tmp_path, acp_backend=effective, private_memory=private)
    else:
        instance = constructor(work_dir=tmp_path, acp_backend=effective, private_memory=private)
        assert instance._process is None


class _SandboxLaunchReached(Exception):
    """The real preparation accepted the endpoint, before any OS launch."""


@pytest.fixture
def launch_boundary(monkeypatch):
    from kiro_crew import sandbox
    from kiro_crew.config.paths import config_dir

    home = config_dir()
    monkeypatch.setattr(sandbox, "_private_memory_roots", lambda: [str(home.resolve())])
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **kwargs: "namespace")
    monkeypatch.setattr(sandbox, "kiro_internal_sandbox_enabled", lambda: False)
    monkeypatch.delenv("KIROCREW_MCP_SOCKET", raising=False)
    monkeypatch.delenv("MC_MCP_SOCKET", raising=False)
    accepted = Mock(side_effect=_SandboxLaunchReached)
    monkeypatch.setattr(sandbox, "namespace_argv", accepted)
    spawned = AsyncMock(
        side_effect=AssertionError("A provider process must never start in this test")
    )
    for module in (client_mod, runtime_mod):
        monkeypatch.setattr(
            module, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value=sys.executable)
        )
        monkeypatch.setattr(module, "ensure_agent_materialized", lambda _agent: True)
        monkeypatch.setattr(
            module, "assert_voice_runtime_outside_agent_workspace", lambda _path: None
        )
        monkeypatch.setattr(module, "apply_pod_bundle_spawn", lambda argv, **kwargs: (argv, False))
        monkeypatch.setattr(module, "create_subprocess_limited", spawned)
    return home, accepted, spawned


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("placement", ["reserved", "outside"])
async def test_public_factory_checks_persisted_socket_with_no_stubbed_servers(
    tmp_path,
    launch_boundary,
    private,
    placement,
):
    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
    from kiro_crew.history import ConversationLog
    from kiro_crew.member_memory_auth import bind_private_session_store
    from kiro_crew.memory_stores import provision_member_memory

    home, accepted, spawned = launch_boundary
    endpoint = (home / "mcp-gateway" if placement == "reserved" else tmp_path) / "custom.sock"

    def build_provider():
        config = KiroCrewConfig.load()
        config.agent.sandbox = "standard"
        config.agent.acp_backend = ACP_BACKEND_KIRO
        config.agent.member_acp_backend = ACP_BACKEND_KIRO
        config.mcp_gateway.stub_servers = []
        config.mcp_gateway._stub_roster = []
        config.mcp_gateway.stub_overrides = {}
        config.mcp_gateway.socket_path = str(endpoint)
        key = "dashboard:legacy"
        if private:
            config.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
            store = provision_member_memory(config, "writer")
            key = "dashboard:member-writer"
        config.save()
        if private:
            ConversationLog().update_metadata(key, {"memory_store": store})
            bind_private_session_store(key, store)
        loaded = KiroCrewConfig.load()
        assert loaded.mcp_gateway.stub_servers == []
        assert loaded.mcp_gateway.socket_path == str(endpoint)
        return loaded.create_provider_factory()(key, agent="kirocrew", cwd=str(tmp_path))

    provider = await asyncio.to_thread(build_provider)
    await asyncio.wait_for(provider.prepare_private_memory(), timeout=5)
    assert provider._client._mcp_gateway_socket is None
    assert provider._client._private_memory is private
    if private and placement == "outside":
        with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
            await asyncio.wait_for(provider._client._spawn(), timeout=5)
        accepted.assert_not_called()
    else:
        with pytest.raises(_SandboxLaunchReached):
            await asyncio.wait_for(provider._client._spawn(), timeout=5)
        accepted.assert_called_once()
    spawned.assert_not_awaited()
    assert not endpoint.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("constructor", [AcpClient, AcpRuntime])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("variable", ["KIROCREW_MCP_SOCKET", "MC_MCP_SOCKET"])
@pytest.mark.parametrize("placement", ["reserved", "outside"])
async def test_private_spawn_checks_child_socket_overrides_before_launch(
    tmp_path,
    monkeypatch,
    launch_boundary,
    constructor,
    private,
    variable,
    placement,
):
    from kiro_crew import sandbox

    home, accepted, spawned = launch_boundary
    endpoint = (home / "mcp-gateway" if placement == "reserved" else tmp_path) / "child.sock"
    instance = constructor(
        work_dir=tmp_path,
        sandbox_mode="standard",
        private_memory=private,
        extra_env={variable: str(endpoint), "UNRELATED_TOKEN": "synthetic-secret"},
    )
    captured = []
    original = sandbox.wrap_argv

    def prepare(argv, **kwargs):
        captured.append(kwargs)
        return original(argv, **kwargs)

    module = client_mod if constructor is AcpClient else runtime_mod
    monkeypatch.setattr(module, "wrap_argv", prepare)
    operation = instance._spawn() if constructor is AcpClient else instance._spawn_admitted()
    if private and placement == "outside":
        with pytest.raises(RuntimeError, match="reserved mcp-gateway"):
            await asyncio.wait_for(operation, timeout=5)
        accepted.assert_not_called()
    else:
        with pytest.raises(_SandboxLaunchReached):
            await asyncio.wait_for(operation, timeout=5)
        accepted.assert_called_once()
    assert len(captured) == 1
    assert captured[0].get("private_mcp_gateway_socket_overrides", ()) == (
        (str(endpoint),) if private else ()
    )
    assert "synthetic-secret" not in json.dumps(captured)
    spawned.assert_not_awaited()
    assert not endpoint.exists()

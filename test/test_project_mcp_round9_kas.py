"""KAS project MCP holds and cards follow the registered agent batch."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_acp_runtime import _start_reader, _stop_reader, kas_readiness_wire  # noqa: F401

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.project_mcp_files import KIRO_PROJECT_MCP_FILE


@pytest.fixture(params=[True, False], ids=["helper-reads", "all-opt-out"])
def project_batch(request, monkeypatch, tmp_path):
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp import kas_mcp_json_legacy
    from kiro_crew.config import paths

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(paths, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(kas_mcp_json_legacy, "legacy_mcp_json", lambda *_a: False)
    parent = {
        "name": "delegator",
        "prompt": "Delegate to the helper.",
        "tools": ["fs_read", "use_subagent"],
        "includeMcpJson": False,
        "toolsSettings": {"subagent": {"availableAgents": ["helper"]}},
    }
    helper = {
        "name": "helper",
        "prompt": "Read the project.",
        "tools": ["*"],
        "includeMcpJson": request.param,
    }
    for spec in (parent, helper):
        (agents / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")
    folder = tmp_path / "project"
    path = folder / KIRO_PROJECT_MCP_FILE
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"mcpServers": {"project-server": {"command": "project-tool"}}}),
        encoding="utf-8",
    )
    return folder, request.param


async def _extras(folder):
    from kiro_crew.acp.harness.kas import KasHarness

    extras = await KasHarness().session_extras("delegator", work_dir=folder, session_cwd=folder)
    assert extras.custom_agents is not None
    assert {a["id"] for a in extras.custom_agents} == {"delegator", "helper"}
    return extras


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
async def test_session_holds_match_registered_agents(
    project_batch, kas_readiness_wire, monkeypatch, resume  # noqa: F811
):
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW
    from kiro_crew.agent_sdk.project_mcp_holds import held_project_servers
    from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, decode_target_args

    folder, helper_reads = project_batch
    extras = await _extras(folder)
    held = held_project_servers(
        ACP_BACKEND_KAS, "delegator", folder, registered_agents=extras.custom_agents
    )
    assert [s.name for s in held] == (["project-server"] if helper_reads else [])
    wire = kas_readiness_wire
    runtime = wire.runtime
    monkeypatch.setattr(runtime, "_kas_custom_agents", AsyncMock(return_value=extras))
    monkeypatch.setattr(runtime_mod, "pooled_session_servers", lambda *_a, **_kw: [])
    kwargs = {"cwd": folder, "agent": "delegator", "session_key": ""}
    start = (
        runtime.load_session("", "ready-session", **kwargs)
        if resume
        else runtime.create_session(mcp_servers=[], **kwargs)
    )
    reader = await _start_reader(runtime)
    task = asyncio.create_task(start)
    try:
        request = await wire.take(wire.sent)
        assert request["method"] == (METHOD_SESSION_LOAD if resume else METHOD_SESSION_NEW)
        servers = {s["name"]: s for s in request["params"]["mcpServers"]}
        assert ("project-server" in servers) is helper_reads
        if helper_reads:
            [packed] = [
                a for a in servers["project-server"]["args"] if a.startswith(STUB_FLAGS_FLAG)
            ]
            assert "--refuse" in decode_target_args(packed.split("=", 1)[1])
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _stop_reader(reader)


@pytest.mark.asyncio
async def test_cards_match_registered_agents(project_batch, monkeypatch):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.dashboard.handlers import change_cards_propose
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards

    folder, helper_reads = project_batch
    extras = await _extras(folder)
    client = SimpleNamespace(cwd=str(folder), kas_registered_agents=extras.custom_agents)
    slot = SimpleNamespace(key="chat-1", project=str(folder))
    monkeypatch.setattr(
        capabilities, "capabilities_of", lambda _client: SimpleNamespace(backend=ACP_BACKEND_KAS)
    )
    propose = AsyncMock()
    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    await offer_project_mcp_cards(object(), slot, client, "session-1", "delegator")
    assert [c.kwargs["params"]["server"] for c in propose.await_args_list] == (
        ["project-server"] if helper_reads else []
    )

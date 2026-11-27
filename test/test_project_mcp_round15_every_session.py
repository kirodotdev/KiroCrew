"""Every model probe and direct-client session holds unapproved project servers."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, decode_target_args
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE


@pytest.fixture
def project(monkeypatch, tmp_path):
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    folder = tmp_path / "project"
    for file in (KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE):
        path = folder / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"mcpServers": {"project-server": {"command": "project-tool"}}}),
            encoding="utf-8",
        )
    spec = {"includeMcpJson": True}
    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda *_a, **_kw: spec)
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda *_a, **_kw: (spec, None))
    return str(folder)


def assert_refusal(params):
    servers = {entry["name"]: entry for entry in params["mcpServers"]}
    assert "project-server" in servers, "unapproved project server missing refusal stub"
    packed = [a for a in servers["project-server"]["args"] if a.startswith(STUB_FLAGS_FLAG)]
    assert len(packed) == 1
    assert "--refuse" in decode_target_args(packed[0].split("=", 1)[1])


def client_wire(monkeypatch, project, backend=""):
    client = AcpClient(agent="worker", acp_backend=backend)
    monkeypatch.setattr(client, "_process", SimpleNamespace(stdin=True))
    monkeypatch.setattr(client, "_session_work_dir", AsyncMock(return_value=project))
    monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [])
    frames = []

    async def write(data, *_a, **_kw):
        frames.append(json.loads(data))

    monkeypatch.setattr(client, "_write_request_bounded", write)
    return client, frames


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "kas", "claude"], ids=["kiro", "kas", "claude"])
async def test_runtime_entitlement_probe_holds_project_servers(monkeypatch, project, backend):
    runtime, _, _ = _make_runtime()
    monkeypatch.setattr(runtime, "_acp_backend", backend)
    monkeypatch.setattr(runtime, "_agent", "worker")
    monkeypatch.setattr(runtime, "_session_work_dir", AsyncMock(return_value=project))
    send = AsyncMock(return_value={"models": {"availableModels": [{"modelId": "auto"}]}})
    monkeypatch.setattr(runtime, "_send_and_await", send)

    assert await runtime.probe_advertised_models()

    method, params = send.call_args.args
    assert method == "session/new"
    assert params["cwd"] == project
    if backend == "claude":
        assert params["_meta"]["claudeCode"]["options"]["settings"]["disabledMcpjsonServers"] == [
            "project-server"
        ]
    else:
        assert_refusal(params)
    assert runtime._session_inits_in_flight == 0


@pytest.mark.asyncio
async def test_direct_client_entitlement_probe_holds_project_servers(monkeypatch, project):
    client, frames = client_wire(monkeypatch, project)
    monkeypatch.setattr(client, "_spawn", AsyncMock())
    monkeypatch.setattr(client, "_admit_scope", AsyncMock(return_value=30))
    monkeypatch.setattr(client, "_initialize_params", lambda: {})
    monkeypatch.setattr(
        client,
        "_wait_for_response",
        AsyncMock(return_value={"models": {"availableModels": [{"modelId": "auto"}]}}),
    )

    rows, _ = await client._entitlement_probe_answer()

    assert rows
    [frame] = [f for f in frames if f["method"] == "session/new"]
    assert_refusal(frame["params"])


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "kas", "claude"], ids=["kiro", "kas", "claude"])
@pytest.mark.parametrize("method", ["session/new", "session/load", "session/resume"])
async def test_direct_session_envelopes_hold_project_servers(monkeypatch, project, backend, method):
    """The client start, restore, substitution retry and probe share this transport."""
    client, frames = client_wire(monkeypatch, project, backend)
    params = {
        "cwd": project,
        "mcpServers": [],
        "_meta": {"claudeCode": {"options": {"settings": {"model": "auto"}}}},
    }

    await client._send_request(method, params)
    await client._send_request(method, frames[0]["params"])

    assert params["mcpServers"] == []
    assert frames[0]["params"] == frames[1]["params"]
    sent = frames[0]["params"]
    if backend == "claude":
        assert sent["_meta"]["claudeCode"]["options"]["settings"] == {
            "model": "auto",
            "disabledMcpjsonServers": ["project-server"],
        }
    else:
        assert_refusal(sent)
        assert len(sent["mcpServers"]) == 1


@pytest.mark.asyncio
async def test_held_array_is_built_off_loop_and_failure_does_not_stop_start(monkeypatch, project):
    original = holds.with_held_servers

    def checked(*args):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return original(*args)

    monkeypatch.setattr(holds, "with_held_servers", checked)
    params = {"cwd": project, "mcpServers": []}
    assert_refusal(await holds.hold_session_params("", "worker", params))

    def failed(*_a):
        raise OSError("stub unavailable")

    monkeypatch.setattr(holds, "_held_element", failed)
    result = await holds.hold_session_params("", "worker", params)
    [fallback] = result["mcpServers"]
    assert fallback["name"] == "project-server"
    assert fallback["args"] == ["-I", "-c", ""]
    assert fallback["env"] == []
    runtime = SimpleNamespace(acp_backend="")
    assert await holds.hold_project_servers(runtime, [], "worker", project) == [fallback]


@pytest.mark.asyncio
async def test_unrelated_requests_and_backends_do_not_read_project_files(monkeypatch, project):
    def unexpected(*_a, **_kw):
        pytest.fail("unrelated request read the project MCP file")

    monkeypatch.setattr(holds, "project_hold", unexpected)
    client, frames = client_wire(monkeypatch, project)
    await client._send_request("session/set_model", {"modelId": "auto"})
    params = {"cwd": project, "mcpServers": []}
    assert await holds.hold_session_params("codex", "worker", params) is params
    assert frames[0]["params"] == {"modelId": "auto"}


@pytest.mark.asyncio
async def test_kas_probe_does_not_inherit_an_unregistered_agents_opt_out(monkeypatch, project):
    from kiro_crew.acp.harness import kas

    spec = {"includeMcpJson": False, "mcpServers": {"project-server": {"command": "own-tool"}}}
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda *_a, **_kw: (spec, None))
    params = {"cwd": project, "mcpServers": []}
    assert_refusal(await holds.hold_session_params("kas", "worker", params))


@pytest.mark.asyncio
async def test_claude_unreadable_probe_keeps_existing_inline_settings(monkeypatch, project):
    from kiro_crew.project_mcp_approvals import FolderCheck

    monkeypatch.setattr(
        holds, "project_hold", lambda *_a, **_kw: FolderCheck(unreadable=(CLAUDE_PROJECT_MCP_FILE,))
    )
    params = {
        "cwd": project,
        "mcpServers": [],
        "_meta": {"claudeCode": {"options": {"settings": {"model": "auto"}}}},
    }
    result = await holds.hold_session_params("claude", "worker", params)
    assert result["_meta"]["claudeCode"]["options"] == {
        "settings": {"model": "auto"},
        "strictMcpConfig": True,
    }
    assert "strictMcpConfig" not in params["_meta"]["claudeCode"]["options"]


@pytest.mark.asyncio
async def test_kas_parking_load_holds_project_servers_in_a_different_stored_mode(
    monkeypatch, project
):
    runtime, _, _ = _make_runtime()
    monkeypatch.setattr(runtime, "_acp_backend", "kas")
    monkeypatch.setattr(runtime, "_agent", "worker")
    send = AsyncMock(return_value={"modes": {"currentModeId": "vibe", "availableModes": []}})
    monkeypatch.setattr(runtime, "_send_and_await", send)
    monkeypatch.setattr(runtime, "release_session", AsyncMock(return_value=True))
    params = {
        "cwd": project,
        "sessionId": "stored-session",
        "mcpServers": [],
        "_meta": {"kiro": {"customAgents": [{"id": "worker", "includeMcpJson": False}]}},
    }

    await runtime._park_in_start_agent("stored-session", params, 30)

    method, sent = send.call_args.args
    assert method == "session/load"
    assert_refusal(sent)
    assert params["mcpServers"] == []
    assert "stored-session" not in runtime._parked_copies


@pytest.mark.asyncio
async def test_claude_fork_holds_project_servers_and_keeps_fork_point(monkeypatch, project):
    """The adapter starts the fork's ``.mcp.json`` servers before the next load applies."""
    client, frames = client_wire(monkeypatch, project, "claude")
    monkeypatch.setattr(client, "_agent_version", "0.87.0")
    monkeypatch.setattr(
        client, "_wait_for_response", AsyncMock(return_value={"sessionId": "forked"})
    )

    capabilities = {"sessionCapabilities": {"fork": {}}}
    assert await client._fork_session("source", "msg_1", capabilities) == "forked"

    [frame] = frames
    assert frame["method"] == "session/fork"
    meta = frame["params"]["_meta"]
    assert meta["jetbrains"] == {"air": {"fork": {"version": 1, "messageId": "msg_1"}}}
    assert meta["claudeCode"]["options"]["settings"]["disabledMcpjsonServers"] == ["project-server"]


@pytest.mark.asyncio
async def test_claude_fork_refuses_an_unsendable_project_server_before_wire(monkeypatch, project):
    from pathlib import Path

    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    path = Path(project) / CLAUDE_PROJECT_MCP_FILE
    path.write_text(json.dumps({"mcpServers": {chr(0xD800): {"command": "x"}}}), "utf-8")
    client, frames = client_wire(monkeypatch, project, "claude")
    params = {
        "sessionId": "source",
        "cwd": project,
        "mcpServers": [],
        "_meta": {"jetbrains": {"air": {"fork": {"version": 1, "messageId": "msg_1"}}}},
    }

    with pytest.raises(AcpToolGateUnroutable) as caught:
        await client._send_request("session/fork", params)

    assert str(caught.value) == (
        f"{path} names an MCP server whose name Kiro Crew can't use. "
        "Rename it in that file, then start a new chat."
    )
    assert caught.value.transient is False
    assert frames == []

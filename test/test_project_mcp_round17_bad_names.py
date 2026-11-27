"""Refuse a session before its host can read an unsendable project server name."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime
from test_claude_shared_runtime import harness  # noqa: F401
from test_project_mcp_round15_every_session import client_wire

from kiro_crew.acp.harness import SessionExtras
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.agent_sdk import project_mcp_holds as holds
from kiro_crew.mcp_gateway.hashing import STUB_FLAGS_FLAG, decode_target_args
from kiro_crew.project_mcp_files import CLAUDE_PROJECT_MCP_FILE, KIRO_PROJECT_MCP_FILE


@pytest.fixture
def project(tmp_path, monkeypatch):
    from kiro_crew.acp import session_mcp
    from kiro_crew.acp.harness import kas

    folder = tmp_path / "project"
    write_project(folder, bad=True)
    spec = {"includeMcpJson": True}
    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda *_a, **_kw: spec)
    monkeypatch.setattr(kas, "resolve_projected_spec", lambda *_a, **_kw: (spec, None))
    return str(folder)


def write_project(folder, *, bad):
    servers = {"ordinary": {"command": "ordinary-tool"}}
    if bad:
        servers[chr(0xD800)] = {"command": "bad-tool"}
    for file in (KIRO_PROJECT_MCP_FILE, CLAUDE_PROJECT_MCP_FILE):
        path = Path(folder) / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def notice(project, backend):
    file = CLAUDE_PROJECT_MCP_FILE if backend == "claude" else KIRO_PROJECT_MCP_FILE
    return (
        f"{Path(project) / file} names an MCP server whose name Kiro Crew can't use. "
        "Rename it in that file, then start a new chat."
    )


def assert_refusal(error, project, backend):
    assert str(error) == notice(project, backend)
    assert error.transient is False
    assert error.user_worded is True


def assert_held(params, backend):
    if backend == "claude":
        assert params["_meta"]["claudeCode"]["options"]["settings"]["disabledMcpjsonServers"] == [
            "ordinary"
        ]
        return
    servers = {s["name"]: s for s in params["mcpServers"]}
    assert "ordinary" in servers
    server = servers["ordinary"]
    [packed] = [a for a in server["args"] if a.startswith(STUB_FLAGS_FLAG + "=")]
    assert "--refuse" in decode_target_args(packed.split("=", 1)[1])
    assert server["command"] != "ordinary-tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "kas", "claude"], ids=["kiro", "kas", "claude"])
@pytest.mark.parametrize("method", ["session/new", "session/load", "session/resume"])
async def test_direct_refuses_before_wire(monkeypatch, project, backend, method):
    client, frames = client_wire(monkeypatch, project, backend)
    params = {"cwd": project, "mcpServers": []}
    with pytest.raises(AcpToolGateUnroutable) as caught:
        await client._send_request(method, params)
    assert_refusal(caught.value, project, backend)
    assert frames == []
    assert params["mcpServers"] == []

    write_project(project, bad=False)
    await client._send_request(method, params)
    assert len(frames) == 1
    assert_held(frames[0]["params"], backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
@pytest.mark.parametrize("backend", ["", "kas", "claude"], ids=["kiro", "kas", "claude"])
async def test_runtime_refuses_before_wire(
    monkeypatch, project, backend, resume, harness  # noqa: F811
):
    runtime, _, process = _make_runtime()
    monkeypatch.setattr(runtime, "_acp_backend", backend)
    monkeypatch.setattr(runtime, "_work_dir", Path(project))
    monkeypatch.setattr(runtime, "_can_load_session", True)
    monkeypatch.setattr(runtime, "_agent", "worker")
    monkeypatch.setattr(runtime, "_kas_custom_agents", AsyncMock(return_value=SessionExtras()))
    monkeypatch.setattr(runtime, "_agent_version", "0.94.0")
    send = AsyncMock(return_value={"sessionId": "started", "modes": {"currentModeId": "default"}})
    monkeypatch.setattr(runtime, "_send_and_await", send)
    if resume:
        start = runtime.load_session("", "stored", cwd=project, agent="worker")
    else:
        start = runtime.create_session(cwd=project, agent="worker", mcp_servers=[])
    with pytest.raises(AcpToolGateUnroutable) as caught:
        await start
    assert_refusal(caught.value, project, backend)
    send.assert_not_awaited()
    process.stdin.write.assert_not_called()
    assert runtime._session_inits_in_flight == 0

    write_project(project, bad=False)
    if resume:
        handle = await runtime.load_session("", "stored", cwd=project, agent="worker")
    else:
        handle = await runtime.create_session(cwd=project, agent="worker", mcp_servers=[])
    assert handle is not None
    assert_held(send.call_args_list[0].args[1], backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "kas", "claude"], ids=["kiro", "kas", "claude"])
async def test_runtime_probe_refusal_returns_no_result(monkeypatch, project, backend):
    runtime, _, _ = _make_runtime()
    monkeypatch.setattr(runtime, "_acp_backend", backend)
    monkeypatch.setattr(runtime, "_agent", "worker")
    monkeypatch.setattr(runtime, "_session_work_dir", AsyncMock(return_value=project))
    send = AsyncMock(return_value={"models": {"availableModels": [{"modelId": "auto"}]}})
    monkeypatch.setattr(runtime, "_send_and_await", send)
    assert await runtime.probe_advertised_models() == []
    send.assert_not_awaited()
    assert runtime._session_inits_in_flight == 0

    write_project(project, bad=False)
    assert await runtime.probe_advertised_models(force=True)
    assert_held(send.call_args.args[1], backend)


@pytest.mark.asyncio
async def test_direct_probe_api_absorbs_refusal(monkeypatch, project):
    client, frames = client_wire(monkeypatch, project)
    monkeypatch.setattr(client, "_session_id", "existing")
    monkeypatch.setattr(client, "_entitlement_probe_client", lambda: client)
    monkeypatch.setattr(client, "_spawn", AsyncMock())
    monkeypatch.setattr(client, "shutdown", AsyncMock())
    monkeypatch.setattr(client, "_admit_scope", AsyncMock(return_value=30))
    monkeypatch.setattr(client, "_initialize_params", lambda: {})
    monkeypatch.setattr(
        client,
        "_wait_for_response",
        AsyncMock(return_value={"models": {"availableModels": [{"modelId": "auto"}]}}),
    )
    assert await client._probe_advertised_models() == ([], 0.0)
    assert [f["method"] for f in frames] == ["initialize"]
    client.shutdown.assert_awaited_once()

    write_project(project, bad=False)
    rows, _ = await client._probe_advertised_models()
    assert rows
    [frame] = [f for f in frames if f["method"] == "session/new"]
    assert_held(frame["params"], "")


@pytest.mark.asyncio
async def test_kas_parking_refuses_before_wire(monkeypatch, project):
    runtime, _, _ = _make_runtime()
    monkeypatch.setattr(runtime, "_acp_backend", "kas")
    monkeypatch.setattr(runtime, "_agent", "worker")
    send = AsyncMock(return_value={"modes": {"currentModeId": "vibe", "availableModes": []}})
    monkeypatch.setattr(runtime, "_send_and_await", send)
    monkeypatch.setattr(runtime, "release_session", AsyncMock(return_value=True))
    params = {
        "cwd": project,
        "sessionId": "stored",
        "mcpServers": [],
        "_meta": {"kiro": {"customAgents": [{"id": "worker", "includeMcpJson": False}]}},
    }
    with pytest.raises(AcpToolGateUnroutable) as caught:
        await runtime._park_in_start_agent("stored", params, 30)
    assert_refusal(caught.value, project, "kas")
    send.assert_not_awaited()
    assert runtime._parked_copies == set()
    runtime.release_session.assert_not_awaited()

    write_project(project, bad=False)
    await runtime._park_in_start_agent("stored", params, 30)
    assert_held(send.call_args.args[1], "kas")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["session/new", "session/load", "session/resume"])
async def test_claude_existing_strict_config_still_starts(monkeypatch, project, method):
    client, frames = client_wire(monkeypatch, project, "claude")
    params = {
        "cwd": project,
        "mcpServers": [],
        "_meta": {"claudeCode": {"options": {"strictMcpConfig": True}}},
    }
    await client._send_request(method, params)
    assert len(frames) == 1
    assert frames[0]["params"] == params


def test_kas_checks_even_without_opt_in_or_agent(monkeypatch, project):
    from kiro_crew.acp import session_mcp

    monkeypatch.setattr(session_mcp, "agent_spec_snapshot", lambda *_a, **_kw: {})
    assert holds.unsendable_project_server("kas", None, project) == notice(project, "kas")
    monkeypatch.setattr(
        session_mcp, "agent_spec_snapshot", lambda *_a, **_kw: {"includeMcpJson": False}
    )
    assert holds.unsendable_project_server("", "worker", project) == ""
    assert holds.unsendable_project_server("kas", "worker", project) == notice(project, "kas")


def test_approved_bad_name_is_still_refused(project):
    from kiro_crew.project_mcp_approvals import approve, check_folder

    for server in check_folder(project).pending:
        approve(server.folder, server.file, server.name, server.fingerprint)
    assert check_folder(project).pending == []
    assert holds.unsendable_project_server("kas", "worker", project) == notice(project, "kas")


@pytest.mark.asyncio
async def test_card_skips_bad_name_and_names_file_once(monkeypatch, project):
    from kiro_crew.agent_sdk import capabilities
    from kiro_crew.dashboard import state as dashboard_state
    from kiro_crew.dashboard.handlers import change_cards_propose
    from kiro_crew.dashboard.project_mcp_cards import offer_project_mcp_cards

    propose = AsyncMock()
    rows = []
    monkeypatch.setattr(change_cards_propose, "propose_in_slot", propose)
    monkeypatch.setattr(
        dashboard_state,
        "append_and_surface",
        lambda _state, _slot, role, text, *_a: rows.append((role, text)),
    )
    monkeypatch.setattr(capabilities, "capabilities_of", lambda _: SimpleNamespace(backend=""))
    slot = SimpleNamespace(key="chat-bad-name", project=project)
    await offer_project_mcp_cards(object(), slot, SimpleNamespace(cwd=project), "sk", "worker")
    assert rows == [("notice", f"⚠ {notice(project, '')}")]
    assert [call.kwargs["params"]["server"] for call in propose.call_args_list] == ["ordinary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    ["runtime-new", "runtime-load", "direct-new", "direct-load", "direct-resume", "parking"],
)
async def test_chat_api_shows_start_refusal_once(monkeypatch, tmp_path, project, entry):
    from test_dashboard_chat import TestRunChatTransientRetry

    from kiro_crew.dashboard.chat import _run_chat

    suite = TestRunChatTransientRetry()
    state = suite._make_state(tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
    client = suite._client(AsyncMock())
    suite._wire_sessions(state, client)
    direct, frames = client_wire(monkeypatch, project, "kas")
    runtime, _, process = _make_runtime()
    monkeypatch.setattr(runtime, "_acp_backend", "kas")
    monkeypatch.setattr(runtime, "_work_dir", Path(project))
    monkeypatch.setattr(runtime, "_can_load_session", True)
    monkeypatch.setattr(runtime, "_agent", "worker")
    monkeypatch.setattr(runtime, "_kas_custom_agents", AsyncMock(return_value=SessionExtras()))
    send = AsyncMock()
    monkeypatch.setattr(runtime, "_send_and_await", send)

    async def start(*_a, **_kw):
        if entry == "runtime-new":
            await runtime.create_session(cwd=project, agent="worker", mcp_servers=[])
        elif entry == "runtime-load":
            await runtime.load_session("", "stored", cwd=project, agent="worker")
        elif entry == "parking":
            await runtime._park_in_start_agent("stored", {"cwd": project}, 30)
        else:
            await direct._send_request("session/" + entry.removeprefix("direct-"), {"cwd": project})
        pytest.fail("session was not refused")

    state.sessions.get_or_create = AsyncMock(side_effect=start)
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    await _run_chat(state, slot, "hello")
    await suite._drain_bg(state)
    texts = suite._err_texts(slot)
    assert len(texts) == 1
    assert notice(project, "kas") in texts[0]
    state.sessions.get_or_create.assert_awaited_once()
    send.assert_not_awaited()
    process.stdin.write.assert_not_called()
    assert frames == []

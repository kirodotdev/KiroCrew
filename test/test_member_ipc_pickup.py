"""The runtime issues agent-socket pickups once, on the array a session start sends.

A member session's create and resume append the session-control and panel entries
after the mirror's projection, replacing a same-name projected element, so issuing
on the projection alone left the dashboard element the harness starts without one.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp.harness import claude as claude_harness
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.runtime_session_setup import _MirroredSessionMcp
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW
from kiro_crew.agent_ipc_wire import GATEWAY_PID_ENV, PICKUP_ENV
from kiro_crew.dashboard import agent_ipc

_ARGV = ["/opt/bin/claude-agent-acp"]


def _element(name: str, verb: str) -> dict:
    return {"name": name, "command": "kirocrew", "args": [verb], "env": [], "type": "stdio"}


def _env(element: dict) -> dict[str, str]:
    return {pair["name"]: pair["value"] for pair in element.get("env") or []}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "load"])
@pytest.mark.parametrize("backend", ["codex", "claude", ""])
@pytest.mark.parametrize("member", ["none", "panel-off", "panel-on"])
async def test_each_sent_crew_server_gets_one_pickup(monkeypatch, tmp_path, path, backend, member):
    import kiro_crew.members as members

    store = agent_ipc.CapabilityStore()
    monkeypatch.setattr(agent_ipc, "STORE", store)
    monkeypatch.setattr(agent_ipc, "agent_socket_bound", lambda: True)
    monkeypatch.setattr(members, "crew_panel_enabled", lambda: member == "panel-on")
    # The claude host prepares each session against its installed adapter; pinned at
    # the resolver so the case does not depend on one being installed on this host.
    monkeypatch.setattr(claude_harness, "_resolve_claude_argv", AsyncMock(return_value=_ARGV))
    monkeypatch.setattr(claude_harness, "_claude_adapter_installed_version", lambda _argv: "0.94.0")
    rt, _, _ = _make_runtime()
    rt._acp_backend = backend
    rt._can_load_session = True
    stub_token = tmp_path.name
    # What the codex mirror projects: both Crew servers as direct managed launches.
    projected = [_element("kirocrew-core", "mcp-core"), _element("kirocrew-dashboard", "mcp")]
    monkeypatch.setattr(
        rt,
        "_mirrored_session_mcp",
        AsyncMock(return_value=_MirroredSessionMcp(projected, frozenset(), stub_token, None)),
    )
    sent = []

    async def send(method, params, timeout=None):
        sent.append((method, params))
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "member-new", "modes": {"currentModeId": "kirocrew"}}
        if method == METHOD_SESSION_LOAD:
            return {"modes": {"currentModeId": "kirocrew"}, "models": []}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", send)
    monkeypatch.setattr(AcpSessionHandle, "apply_session_permission_routing", AsyncMock())
    monkeypatch.setattr(AcpSessionHandle, "drain_init", AsyncMock())
    key = {} if member == "none" else {"member_session_key": "dashboard_member-test"}
    if path == "create":
        await rt.create_session(cwd=str(tmp_path), agent="kirocrew", **key)
        method = METHOD_SESSION_NEW
    else:
        await rt.load_session(
            str(tmp_path / "session.json"), "member-old", cwd=str(tmp_path), agent="kirocrew", **key
        )
        method = METHOD_SESSION_LOAD
    params = next(params for name, params in sent if name == method)
    sent_servers = {e["name"]: e for e in params["mcpServers"]}
    if member == "panel-on":
        # Not a pickup server: the panel entry is appended and sent as it was built.
        assert PICKUP_ENV not in _env(sent_servers["kirocrew-panel"])
    if backend != "codex":
        # A harness that reads the secret keeps its whole array as it was.
        assert not any(PICKUP_ENV in _env(e) for e in params["mcpServers"])
        assert not store._pickups
        return
    if member != "none":
        # The element sent is the member entry, which carries its own identity.
        assert _env(sent_servers["kirocrew-dashboard"]).get("KIROCREW_SESSION_KEY")
    for name in ("kirocrew-core", "kirocrew-dashboard"):
        env = _env(sent_servers[name])
        assert len(env[PICKUP_ENV]) == 64, name
        assert env[GATEWAY_PID_ENV] == str(os.getpid())
        (pickup,) = [p for p in store._pickups.values() if p.server == name]
        assert pickup.session_token == stub_token
        assert pickup.runtime_pid() == rt._pid
    # One pickup per sent server: none was minted for an element the member entry
    # replaced, where nothing could ever redeem it.
    assert len(store._pickups) == 2

"""Late member mounts share the runtime's IPC identity on create and resume."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime

from kiro_crew.acp import ipc_pickup
from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.runtime_session_setup import _MirroredSessionMcp
from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW
from kiro_crew.agent_ipc_wire import GATEWAY_PID_ENV, PICKUP_ENV
from kiro_crew.dashboard import agent_ipc
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "load"])
@pytest.mark.parametrize("backend", ["codex", ""])
@pytest.mark.parametrize("panel_enabled", [False, True])
async def test_late_member_mounts_receive_runtime_pickups(
    monkeypatch, tmp_path, path, backend, panel_enabled
):
    import kiro_crew.members as members

    store = agent_ipc.CapabilityStore()
    monkeypatch.setattr(agent_ipc, "STORE", store)
    monkeypatch.setattr(agent_ipc, "agent_socket_bound", lambda: True)
    # The PANEL half must come from PRODUCTION, not from this test: it is a late
    # member mount, which is what this change is about, so a constant that stopped
    # naming it would silently take the member's webview tools away again.
    assert "kirocrew-panel" in ipc_pickup.PICKUP_SERVERS
    # Only the dashboard half is supplied here, because it ships separately
    # (#35579 adds ``kirocrew-dashboard`` to the same constant). Isolating it keeps
    # this test about the late mounts rather than about that dependency's merge
    # order.
    monkeypatch.setattr(
        ipc_pickup,
        "PICKUP_SERVERS",
        ipc_pickup.PICKUP_SERVERS | {"kirocrew-dashboard"},
    )
    monkeypatch.setattr(members, "crew_panel_enabled", lambda: panel_enabled)
    rt, _, _ = _make_runtime()
    rt._acp_backend = backend
    rt._can_load_session = True
    stub_token = tmp_path.name
    monkeypatch.setattr(
        rt,
        "_mirrored_session_mcp",
        AsyncMock(return_value=_MirroredSessionMcp([], frozenset(), stub_token, None)),
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
    if path == "create":
        await rt.create_session(
            cwd=str(tmp_path), agent="kirocrew", member_session_key="dashboard_member-test"
        )
        method = METHOD_SESSION_NEW
    else:
        await rt.load_session(
            str(tmp_path / "session.json"),
            "member-old",
            cwd=str(tmp_path),
            agent="kirocrew",
            member_session_key="dashboard_member-test",
        )
        method = METHOD_SESSION_LOAD
    params = next(params for name, params in sent if name == method)
    servers = {
        entry["name"]: entry
        for entry in params["mcpServers"]
        if entry["name"] in {"kirocrew-dashboard", "kirocrew-panel"}
    }
    expected = {"kirocrew-dashboard"}
    if panel_enabled:
        expected.add("kirocrew-panel")
    assert set(servers) == expected
    for name, entry in servers.items():
        env = {pair["name"]: pair["value"] for pair in entry["env"]}
        if backend == "":
            assert PICKUP_ENV not in env
            continue
        assert PICKUP_ENV in env and GATEWAY_PID_ENV in env
        assert env[STUB_SESSION_TOKEN_ENV] == stub_token
        assert env[GATEWAY_PID_ENV] == str(os.getpid())
        assert len(env[PICKUP_ENV]) == 64
        pickup = next(value for value in store._pickups.values() if value.server == name)
        assert pickup.session_token == stub_token
        assert pickup.runtime_pid() == rt._pid
    assert len(store._pickups) == (0 if backend == "" else len(expected))

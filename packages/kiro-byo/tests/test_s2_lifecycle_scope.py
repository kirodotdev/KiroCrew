"""S2-03: Saved Agent visibility is enforced at the actual execution boundary."""
from __future__ import annotations

import io
from unittest.mock import patch

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection, from_session_params
from kiro_byo.native_tools import NativeToolRuntime
from kiro_byo.providers.base import ToolCall


def test_projected_agent_missing_tools_is_explicitly_restricted():
    agent = from_session_params({"_meta": {"kiro": {
        "modeId": "worker", "customAgents": [{"id": "worker", "prompt": "summarize"}],
    }}})
    assert agent.active
    assert not agent.tools_declared  # provenance retained for diagnostics
    assert agent.allowed_native_names({"read", "write", "shell"}) == set()
    assert not agent.allows_any_mcp()


def test_model_issued_native_call_cannot_bypass_saved_agent_visibility(tmp_path):
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    server._runtime_event = lambda *args, **kwargs: None
    server._emit_tool_call = lambda *args, **kwargs: None
    statuses = []
    server._emit_tool_status = lambda _s, _id, *, status, text="": statuses.append((status, text))
    session = Session(
        "restricted", str(tmp_path), "model", native=NativeToolRuntime(tmp_path),
        agent=AgentProjection(agent_id="readonly", tools=("read",), tools_declared=True),
    )
    call = ToolCall("write1", "write", '{"path":"unauthorized.txt","content":"X"}')
    try:
        with patch.object(server, "_request_tool_permission", side_effect=AssertionError("should not prompt")):
            result, _elapsed, interruption = server._execute_native_tool(
                session, call, call.arguments())
        assert interruption == ""
        assert "not visible" in result
        assert statuses[-1][0] == "refused"
        assert not (tmp_path / "unauthorized.txt").exists()
    finally:
        session.native.close()
        server._executor.shutdown(wait=True)


def test_mcp_auto_approval_is_not_a_tool_visibility_grant():
    agent = AgentProjection(agent_id="read_only", tools=("read",),
                            tools_declared=True, allowed_tools=("@kirocrew-core",))
    assert not agent.allows_mcp(server_name="kirocrew-core", tool_name="browser",
                                public_name="mcp__kirocrew-core__browser")


def test_restore_current_crew_envelope_without_agents_does_not_revive_old_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    previous = Session("member-retired", str(workspace), "model", agent=AgentProjection(
        agent_id="retired", prompt="old member prompt", tools=("@mcp",),
        tools_declared=True))
    server._persist_session(previous)
    from kiro_byo.config import load_config
    # Older tests restore a @staticmethod as a plain class function. Pin the
    # static call contract here so the test is independent of execution order.
    with (patch.object(server, "_config", side_effect=load_config),
          patch.object(AcpServer, "_mcp_from_params", staticmethod(AcpServer._mcp_from_params))):
        restored, _ = server._restore_session(
            "member-retired", {"cwd": str(workspace), "mcpServers": [],
                               "_meta": {"kiro": {"role": "member"}}}, replay=False)
    try:
        assert not restored.agent.active
        assert restored.agent.prompt == ""
        assert restored.agent.allowed_native_names({"read"}) == {"read"}
    finally:
        if restored.mcp: restored.mcp.close()
        if restored.native: restored.native.close()
        server._executor.shutdown(wait=True)

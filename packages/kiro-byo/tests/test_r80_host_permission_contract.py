"""The ACP host gate must beat Saved-Agent allowedTools without backend-ID spoofing."""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from kiro_byo.acp_server import AcpServer
from kiro_byo.acp_session_state import Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.native_tools import NativeToolRuntime


def _config():
    m = ModelSpec(provider_id="p", provider_name="test", model_id="m", name="M")
    p = ProviderSpec(id="p", name="test", type="openai-compatible",
                     base_url="https://example.invalid/v1", api_key="",
                     models=(m,), default_model="m")
    return KiroByoConfig(providers=(p,), default_model="M")


def test_acp_advertises_host_gate_and_rejects_downgrade(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    cfg = _config()
    monkeypatch.setattr(AcpServer, "_config", staticmethod(lambda: cfg))
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    session = Session("gate-test", str(tmp_path), "M")
    server._put_session(session)
    try:
        options = server._config_options(session, cfg)
        gate = next(x for x in options if x["id"] == "host_tool_approval")
        assert gate["currentValue"] == "unconfigured"
        assert {x["value"] for x in gate["options"]} == {"required", "unconfigured"}
        server._handle_session_set_config_option_request(
            10, {"sessionId": session.session_id, "configId": "host_tool_approval", "value": "required"}
        )
        assert session.host_tool_approval_required
        assert any(x.get("id") == 10 and x.get("result", {}).get("configOptions")
                   for x in map(json.loads, out.getvalue().splitlines()))
        before = len(out.getvalue())
        server._handle_session_set_config_option_request(
            11, {"sessionId": session.session_id, "configId": "host_tool_approval", "value": "unconfigured"}
        )
        assert session.host_tool_approval_required
        assert any(x.get("id") == 11 and x.get("error")
                   for x in map(json.loads, out.getvalue()[before:].splitlines()))
        assert not Session("new", str(tmp_path), "M").host_tool_approval_required
    finally:
        server._executor.shutdown(wait=True)


def test_native_allowedtools_cannot_bypass_host_required_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    file = tmp_path / "readme.txt"
    file.write_text("sensitive-test-data", encoding="utf-8")
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    session = Session("native-test", str(tmp_path), "M",
                      agent=AgentProjection(allowed_tools=("*",)),
                      native=NativeToolRuntime(str(tmp_path)))
    calls = []
    monkeypatch.setattr(server, "_request_tool_permission", lambda *args: calls.append(args) or False)
    tool = SimpleNamespace(call_id="n1", name="read")
    try:
        legacy = server._execute_native_tool(session, tool, {"path": str(file)})
        assert "sensitive-test-data" in legacy[0]
        assert not calls
        session.host_tool_approval_required = True
        blocked = server._execute_native_tool(session, tool, {"path": str(file)})
        assert "Permission denied" in blocked[0]
        assert len(calls) == 1
    finally:
        server._executor.shutdown(wait=True)


def test_mcp_allowedtools_cannot_bypass_host_required_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    server = AcpServer(stdin=StringIO(), stdout=StringIO())
    tool = SimpleNamespace(call_id="m1", name="project_tool")
    binding = SimpleNamespace(server_name="crew", tool_name="project_tool", annotations={})
    dispatched = []
    mcp = SimpleNamespace(bindings={"project_tool": binding},
                          is_live_binding=lambda name: True,
                          is_crew_member_mount=lambda name: False,
                          call=lambda name, args: dispatched.append(name) or "ran")
    session = Session("mcp-test", str(tmp_path), "M",
                      agent=AgentProjection(allowed_tools=("*",)), mcp=mcp)
    requests = []
    monkeypatch.setattr(server, "_mcp_visible_to_agent", lambda *args: True)
    monkeypatch.setattr(server, "_request_tool_permission", lambda *args: requests.append(args) or False)
    try:
        legacy = server._execute_mcp_tool(session, tool, {})
        assert legacy[0] == "ran" and dispatched == ["project_tool"] and not requests
        session.host_tool_approval_required = True
        blocked = server._execute_mcp_tool(session, tool, {})
        assert "Permission denied" in blocked[0]
        assert len(requests) == 1 and dispatched == ["project_tool"]
    finally:
        server._executor.shutdown(wait=True)

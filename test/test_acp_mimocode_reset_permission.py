"""A MiMo Code permission event that arrives after ``_reset_state``.

``_reset_state`` drops the MCP array cache and the process adapter together. A late
permission request still asks which servers the session placed, which resolves the
array cold with no adapter kept. That resolve records what the projection found on the
read-back, and it must answer exactly as it does with an adapter kept.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.acp import session_mcp
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.types import AcpEvent
from kiro_crew.agent_sdk.backends import ACP_BACKEND_MIMOCODE


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", d)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda _name: None)
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    spec = {"name": "kirocrew", "mcpServers": {"srv": {"command": "/bin/x"}}, "tools": ["@srv"]}
    (d / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    return d


def _event(server: str) -> AcpEvent:
    return AcpEvent(
        kind="permission_request",
        request_id=server,
        mcp_server_name=server,
        tool_name=f"{server}_tool",
        mcp_identity_trusted=True,
    )


def test_a_late_permission_event_after_reset_resolves_the_array_cold(agents_dir, tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._process_adapter = process_adapter_for(ACP_BACKEND_MIMOCODE)
    live = (client._foreign_mcp_identity(_event("srv")), client._foreign_mcp_identity(_event("x")))
    assert live == (False, True), "the spec's server is placed; another is foreign"

    client._reset_state()
    assert client._session_mcp_cache is None, "the next read resolves the array cold"

    late = (client._foreign_mcp_identity(_event("srv")), client._foreign_mcp_identity(_event("x")))
    assert late == live

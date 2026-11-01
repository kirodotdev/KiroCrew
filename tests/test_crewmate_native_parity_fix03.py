"""Crew 0.10 member-DM routing and per-session MCP control regressions.

No Crew installation or network server is used. A real Windows Crew test is
still required for member identity, Saved Agent lifecycle and dispatch UX.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_byo.agent_projection import AgentProjection
from kiro_byo.acp_server import Session
from kiro_byo.mcp_client import McpError, McpManager
from kiro_byo.turn_runner import TurnRunnerMixin


def mount(name="kirocrew-dashboard", *, key="dashboard:member-atlas", argv="mcp-dashboard"):
    return {"name": name, "type": "stdio", "command": "kirocrew", "args": [argv],
            "env": [{"name": "KIROCREW_SESSION_KEY", "value": key},
                    {"name": "KIROCREW_SESSION_TOKEN", "value": "fake-no-auth-in-tests"}]}


def manager_with_tool(entries=None, *, name="kirocrew-dashboard", live=True):
    manager = McpManager(entries if entries is not None else [mount()])
    manager.servers[name].tools = [{"name": "dispatch_work",
                                    "description": "Dispatch task to Crew workers",
                                    "inputSchema": {"type": "object"}}]
    manager._rebuild_bindings()
    manager._ready_event.set()
    if live:
        manager._live_verified_servers.add(name)
    return manager


def as_session(manager, *, tools=(), excludes=(), allowed=()):
    session = Session("test-crewmate", ".", "test-model", mcp=manager)
    session.agent = AgentProjection(agent_id="saved-crewmate", tools=tools,
                                    tools_declared=True, excluded_tools=excludes,
                                    allowed_tools=allowed)
    return session


def tools(session):
    return {item["function"]["name"] for item in TurnRunnerMixin._active_tools_for_turn(session)}


def test_only_session_attested_member_mount_can_expand_saved_agent_mcp_visibility():
    m = manager_with_tool()
    try:
        session = as_session(m, tools=())
        tool = "mcp__kirocrew-dashboard__dispatch_work"
        assert m.is_crew_member_mount("kirocrew-dashboard")
        assert "mcp__kiro_byo__tool_search" in tools(session)
        session.loaded_tool_names.append(tool)
        assert tool in tools(session)
        assert TurnRunnerMixin._mcp_visible_to_agent(session, m.bindings[tool])
        assert session.agent.allowed_native_names({"read", "write", "shell"}) == set()
    finally:
        m.close()


@pytest.mark.parametrize("untrusted", [
    mount(key="chat-1"),
    mount(argv="mcp-other"),
    {"name": "kirocrew-dashboard", "type": "stdio", "command": "kirocrew", "args": ["mcp-dashboard"]},
])
def test_unattested_same_named_server_does_not_bypass_agent_tool_list(untrusted):
    m = manager_with_tool([untrusted])
    try:
        session = as_session(m)
        tool = "mcp__kirocrew-dashboard__dispatch_work"
        session.loaded_tool_names.append(tool)
        assert not m.is_crew_member_mount("kirocrew-dashboard")
        assert not TurnRunnerMixin._mcp_visible_to_agent(session, m.bindings[tool])
        assert tool not in tools(session)
    finally:
        m.close()


def test_member_mount_only_visible_after_live_handshake():
    m = manager_with_tool(live=False)
    try:
        session = as_session(m)
        tool = "mcp__kirocrew-dashboard__dispatch_work"
        session.loaded_tool_names.append(tool)
        assert tool not in tools(session)
        assert not m.is_live_binding(tool)
        assert m.host_capability_snapshot()["available_tools"] == []
        m._live_verified_servers.add("kirocrew-dashboard")
        assert m.is_live_binding(tool)
        assert tool in tools(session)
    finally:
        m.close()


def test_saved_agent_explicit_exclusions_always_override_host_mount():
    m = manager_with_tool()
    try:
        session = as_session(m, tools=("@mcp",), excludes=("@kirocrew-dashboard/*",))
        tool = "mcp__kirocrew-dashboard__dispatch_work"
        session.loaded_tool_names.append(tool)
        assert not TurnRunnerMixin._mcp_visible_to_agent(session, m.bindings[tool])
        assert tool not in tools(session)
    finally:
        m.close()


def test_trusted_member_mount_does_not_grant_other_servers():
    foreign = {"name": "stranger", "type": "stdio", "command": "stranger", "args": []}
    m = McpManager([mount(), foreign])
    try:
        binding = SimpleNamespace(server_name="stranger", tool_name="dispatch_work",
                                  public_name="mcp__stranger__dispatch_work")
        assert not TurnRunnerMixin._mcp_visible_to_agent(as_session(m), binding)
    finally:
        m.close()


def test_member_mount_denied_by_host_permission_and_never_auto_approved():
    m = manager_with_tool()
    try:
        session = as_session(m, allowed=("@kirocrew-dashboard/*",))
        tool = "mcp__kirocrew-dashboard__dispatch_work"
        class Runner(TurnRunnerMixin):
            def __init__(self):
                self.permission_calls = 0
            def _runtime_event(self, *args, **kwargs):
                pass
            def _emit_tool_status(self, *args, **kwargs):
                pass
            def _emit_tool_call(self, *args, **kwargs):
                pass
            def _request_tool_permission(self, *args, **kwargs):
                self.permission_calls += 1
                return False
        runner = Runner()
        # If this hits the actual server call, the test must fail.
        m.call = lambda *args, **kwargs: pytest.fail("unauthorized MCP dispatch")
        text, elapsed, reason = runner._execute_mcp_tool(
            session, SimpleNamespace(call_id="c1", name=tool), {"task": "oops"})
        assert runner.permission_calls == 1
        assert "denied" in text.lower()
        assert elapsed == 0.0 and reason == ""
    finally:
        m.close()


def test_duplicate_mcp_server_identity_is_rejected():
    with pytest.raises(McpError, match="duplicate MCP server name"):
        McpManager([mount(), mount()])


def test_host_panel_mount_stays_separate_from_dispatch_mount():
    m = manager_with_tool([mount(name="kirocrew-panel", argv="mcp-panel")], name="kirocrew-panel")
    try:
        assert m.is_crew_member_mount("kirocrew-panel")
        assert not m.is_crew_member_mount("kirocrew-dashboard")
        session = as_session(m)
        tool = "mcp__kirocrew-panel__dispatch_work"
        session.loaded_tool_names.append(tool)
        assert tool in tools(session)
    finally:
        m.close()


def test_live_member_dispatch_revoked_during_approval_never_calls_tool():
    m = manager_with_tool()
    try:
        session = as_session(m)
        tool = "mcp__kirocrew-dashboard__dispatch_work"
        class Runner(TurnRunnerMixin):
            def _runtime_event(self, *args, **kwargs):
                pass
            def _emit_tool_status(self, *args, **kwargs):
                pass
            def _request_tool_permission(self, *args, **kwargs):
                m._live_verified_servers.discard("kirocrew-dashboard")
                return True
        m.call = lambda *args, **kwargs: pytest.fail("revoked MCP dispatched")
        text, _, _ = Runner()._execute_mcp_tool(
            session, SimpleNamespace(call_id="c2", name=tool), {})
        assert "revoked" in text.lower() or "changed" in text.lower()
    finally:
        m.close()


def test_existing_agents_model_entitlement_helper_upgraded_in_place():
    from kiro_byo import crew_shared_patch, crew_patch_common as common
    from test_crew_shared_transform import _minimal_agents_source_for_member_backend
    rel = "src/kiro_crew/dashboard/handlers/agents.py"
    current = crew_shared_patch.patch_text(rel, _minimal_agents_source_for_member_backend())
    old_helper = '''def _kiro_byo_effective_member_backend(cfg: Any) -> str:
    default_backend = getattr(cfg.agent, "acp_backend", "")
    if default_backend == ACP_BACKEND_KIRO_BYO:
        return default_backend
    return getattr(cfg.agent, "member_acp_backend", "")
'''
    legacy = current.replace(common._KIRO_BYO_EFFECTIVE_MEMBER_BACKEND_HELPER, old_helper, 1)
    assert legacy != current
    assert crew_shared_patch.inspect(rel, legacy).state == "upgradeable"
    updated = crew_shared_patch.patch_text(rel, legacy)
    assert updated == current
    assert crew_shared_patch.inspect(rel, updated).state == "patched"


def test_member_session_without_full_agent_projection_is_not_unrestricted():
    foreign = {"name": "other", "type": "stdio", "command": "other", "args": []}
    m = McpManager([mount(), foreign])
    try:
        session = Session("unprojected-crewmate", ".", "test-model", mcp=m)
        assert not session.agent.active
        assert TurnRunnerMixin._native_visible_to_agent(session, {"read", "write", "shell"}) == set()
        member_binding = SimpleNamespace(server_name="kirocrew-dashboard", tool_name="dispatch_work",
                                         public_name="mcp__kirocrew-dashboard__dispatch_work")
        other_binding = SimpleNamespace(server_name="other", tool_name="read",
                                        public_name="mcp__other__read")
        assert TurnRunnerMixin._mcp_visible_to_agent(session, member_binding)
        assert not TurnRunnerMixin._mcp_visible_to_agent(session, other_binding)
    finally:
        m.close()


def test_regular_chat_without_member_mount_keeps_existing_default_tool_policy():
    m = McpManager([{"name": "other", "type": "stdio", "command": "other", "args": []}])
    try:
        session = Session("normal-chat", ".", "test-model", mcp=m)
        assert TurnRunnerMixin._native_visible_to_agent(session, {"read", "write"}) == {"read", "write"}
        binding = SimpleNamespace(server_name="other", tool_name="read", public_name="mcp__other__read")
        assert TurnRunnerMixin._mcp_visible_to_agent(session, binding)
    finally:
        m.close()

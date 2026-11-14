"""S2: Crew Saved Agent / Skills / MCP contract regression without live Windows host."""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection, from_session_params
from kiro_byo.mcp_client import McpManager
from kiro_byo.turn_runner import TurnRunnerMixin


def projection(agents, mode=None):
    meta = {"customAgents": agents}
    if mode is not None:
        meta["modeId"] = mode
    return from_session_params({"_meta": {"kiro": meta}})


def test_explicit_saved_agent_id_never_substitutes_other_agent():
    with pytest.raises(ValueError, match="did not project"):
        projection([{"id": "other", "prompt": "ignore earlier constraints", "tools": ["*"]}], mode="restricted")


def test_multiple_saved_agents_need_active_identity():
    with pytest.raises(ValueError, match="multiple saved Agents"):
        projection([{"id": "a", "prompt": "a"}, {"id": "b", "prompt": "b"}])
    selected = projection([{"id": "a", "prompt": "a"}, {"id": "b", "prompt": "b"}], mode="b")
    assert selected.agent_id == "b" and selected.prompt == "b"


def test_malformed_projected_agent_rejected_not_unrestricted():
    for meta in (
        {"customAgents": [{"id": "a", "prompt": 123}], "modeId": "a"},
        {"customAgents": [{"id": "a", "prompt": "hi", "tools": "*"}]},
        {"customAgents": {"id": "a", "prompt": "hi"}},
        {"customAgents": ["garbage"]},
        {"customAgents": [], "modeId": "unexpected"},
    ):
        with pytest.raises(ValueError):
            from_session_params({"_meta": {"kiro": meta}})
    assert not projection([], mode="default").active
    empty_prompt = projection([{"id": "no-prompt", "tools": []}], mode="no-prompt")
    assert empty_prompt.active and not empty_prompt.unrestricted
    assert empty_prompt.allowed_native_names({"write"}) == set()


def test_no_saved_agent_preserves_default_crew_session():
    assert not from_session_params({"cwd": "/tmp", "mcpServers": []}).active
    assert projection([{"id": "sole", "prompt": "valid"}]).agent_id == "sole"


def test_saved_agent_native_exclusion_globs_canonical_and_alias():
    agent = AgentProjection(agent_id="a", prompt="p", tools=("@builtin",),
                            tools_declared=True, excluded_tools=("web_*", "execute_bash*"))
    names = {"read", "write", "web_search", "web_fetch", "execute_shell_process", "shell"}
    assert agent.allowed_native_names(names) == {"read", "write"}


def test_native_visibility_glob_scoped_to_existing_supported_tools():
    agent = AgentProjection(agent_id="a", prompt="p", tools=("web_*",), tools_declared=True)
    assert agent.allowed_native_names({"web_search", "web_fetch", "write", "web_unknown"}) == {"web_search", "web_fetch"}


def test_native_read_tag_does_not_grant_same_named_mcp_tool():
    agent = AgentProjection(agent_id="a", prompt="p", tools=("read",), tools_declared=True)
    assert not agent.allows_mcp(server_name="thirdparty", tool_name="read", public_name="mcp__thirdparty__read")


def test_mcp_glob_whitelist_and_exclusions_never_expand_other_servers():
    agent = AgentProjection(agent_id="a", prompt="p", tools=("@kirocrew-core/*", "@git/read_*"),
                            tools_declared=True, excluded_tools=("@kirocrew-core/private_*", "@git/read_secret"))
    allows = lambda server, tool: agent.allows_mcp(
        server_name=server, tool_name=tool, public_name=f"mcp__{server}__{tool}")
    assert allows("kirocrew-core", "skill_search")
    assert not allows("kirocrew-core", "private_state")
    assert not allows("unknown", "skill_search")
    assert allows("git", "read_file")
    assert not allows("git", "read_secret")
    assert not allows("git", "delete_file")


def test_mcp_glob_exclude_overrides_mcp_wildcard():
    agent = AgentProjection(agent_id="a", prompt="p", tools=("@mcp",),
                            tools_declared=True, excluded_tools=("mcp__kirocrew-core__browser", "@other/*"))
    assert not agent.allows_mcp(server_name="kirocrew-core", tool_name="browser",
                                public_name="mcp__kirocrew-core__browser")
    assert not agent.allows_mcp(server_name="other", tool_name="ping", public_name="mcp__other__ping")
    assert agent.allows_mcp(server_name="kirocrew-core", tool_name="skill_search",
                            public_name="mcp__kirocrew-core__skill_search")


CORE = {"name": "kirocrew-core", "type": "stdio", "command": "not-executed", "args": []}


def crew_manager(*, verified=True):
    manager = McpManager([CORE])
    manager.servers["kirocrew-core"].tools = [
        {"name": "skill_search", "description": "Search permitted Skills", "inputSchema": {"type": "object"}},
        {"name": "browser", "description": "Browser", "inputSchema": {"type": "object"}},
    ]
    manager._rebuild_bindings()
    manager._ready_event.set()
    if verified:
        manager._live_verified_servers.add("kirocrew-core")
    return manager


def tool_names(session):
    return {item["function"]["name"] for item in TurnRunnerMixin._active_tools_for_turn(session)}


def test_skill_search_preloaded_only_with_live_crew_scope():
    manager = crew_manager()
    try:
        session = Session("s", ".", "m", mcp=manager)
        assert "mcp__kirocrew-core__skill_search" in tool_names(session)
        session.agent = AgentProjection(agent_id="restricted", prompt="p", tools=("read",), tools_declared=True)
        assert "mcp__kirocrew-core__skill_search" not in tool_names(session)
        session.agent = AgentProjection(agent_id="restricted", prompt="p", tools=("@mcp",),
                                         tools_declared=True, excluded_tools=("@kirocrew-core/skill_*") )
        assert "mcp__kirocrew-core__skill_search" not in tool_names(session)
        session.agent = AgentProjection(agent_id="permitted", prompt="p", tools=("@kirocrew-core/skill_search",),
                                         tools_declared=True)
        assert "mcp__kirocrew-core__skill_search" in tool_names(session)
        assert "mcp__kirocrew-core__browser" not in tool_names(session)
    finally:
        manager.close()


def test_skill_search_never_loaded_from_unverified_crew_catalog():
    manager = crew_manager(verified=False)
    try:
        assert "mcp__kirocrew-core__skill_search" not in tool_names(Session("s", ".", "m", mcp=manager))
    finally:
        manager.close()


def test_agent_projection_invalid_rejected_before_mcp_launch(monkeypatch, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    # Fail even before touching host-projected MCP commands or spawning a broker.
    params = {"cwd": str(cwd), "mcpServers": [CORE],
              "_meta": {"kiro": {"modeId": "agent-safe", "customAgents": [
                  {"id": "agent-other", "prompt": "You are not the requested agent."}]}}}
    with (patch.object(server, "_config"),
          patch.object(server, "_mcp_from_params") as mcp):
        # A production Kiro config is only consulted to prepare the model list.
        from kiro_byo.config import load_config
        with patch.object(server, "_config", side_effect=load_config):
            server._handle_session_new_request(99, params)
        mcp.assert_not_called()
    server._executor.shutdown(wait=True)
    assert len(server.sessions) == 0

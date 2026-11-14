"""S1-01: Crew browser visibility is a host capability, never a BYO grant."""
from __future__ import annotations

from unittest.mock import patch

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.mcp_client import McpManager
from kiro_byo.turn_runner import TurnRunnerMixin


def _manager(name: str = "kirocrew-core") -> McpManager:
    manager = McpManager([{
        "type": "stdio", "name": name, "command": "does-not-run",
        "args": [],
    }])
    manager.servers[name].tools = [
        {"name": "browser", "description": "Crew browser with native panel routing",
         "inputSchema": {"type": "object", "properties": {"op": {"type": "string"}}}},
        {"name": "other", "description": "Other core tool",
         "inputSchema": {"type": "object", "properties": {}}},
    ]
    manager._rebuild_bindings()
    # Unit fixture stands in for a completed authenticated Crew handshake.
    manager._live_verified_servers.add(name)
    return manager


def _names(session: Session) -> list[str]:
    return [spec["function"]["name"] for spec in TurnRunnerMixin._active_tools_for_turn(session)]


def test_browser_directly_advertised_only_from_crew_core() -> None:
    manager = _manager()
    try:
        session = Session("s", ".", "m", mcp=manager)
        names = _names(session)
        assert "mcp__kirocrew-core__browser" in names
        assert "mcp__kirocrew-core__other" not in names
        assert "mcp__kiro_byo__tool_search" in names
        assert session.loaded_tool_names == []  # no hidden mutation of search LRU
    finally:
        manager.close()


def test_browser_grant_respects_crew_saved_agent_tool_whitelist() -> None:
    manager = _manager()
    try:
        excluded = AgentProjection(agent_id="custom", tools_declared=True, tools=("@mcp",),
                                   excluded_tools=("@kirocrew-core/browser",))
        session = Session("s", ".", "m", mcp=manager, agent=excluded)
        assert "mcp__kirocrew-core__browser" not in _names(session)
        assert "mcp__kiro_byo__tool_search" in _names(session)
        # An explicit [] must not expose the browser or the search tool.
        session.agent = AgentProjection(agent_id="custom", tools_declared=True, tools=())
        assert _names(session) == []
    finally:
        manager.close()


def test_third_party_browser_not_elevated() -> None:
    manager = _manager("untrusted-browser")
    try:
        session = Session("s", ".", "m", mcp=manager)
        assert "mcp__untrusted-browser__browser" not in _names(session)
        assert manager.crew_browser_tool_names() == ()
    finally:
        manager.close()


def test_first_browser_turn_waits_for_authenticated_host_not_schema_cache() -> None:
    manager = _manager()
    try:
        session = Session("s", ".", "m", mcp=manager)
        manager._live_verified_servers.discard("kirocrew-core")
        # A durable schema cache may report ready while this session's
        # authenticated broker is still warming. Browser intent must join it.
        with patch.object(manager, "catalog_ready", return_value=True), patch.object(
            manager, "wait_for_live_crew_catalog", return_value=False
        ) as wait:
            TurnRunnerMixin._prepare_crew_browser_for_turn(session, "用内置浏览器打开网页并点击按钮")
            wait.assert_called_once_with("kirocrew-core", timeout=8.0)
            TurnRunnerMixin._prepare_crew_browser_for_turn(session, "你好，介绍下浏览器技术")
            wait.assert_called_once()
    finally:
        manager.close()


def test_runtime_browser_instruction_only_when_tool_is_projected() -> None:
    from types import SimpleNamespace
    provider = SimpleNamespace(name="P")
    model = SimpleNamespace(name="M", model_id="m")
    with_browser = AcpServer._runtime_system_message(provider, model, crew_browser_available=True)["content"]
    without_browser = AcpServer._runtime_system_message(provider, model, crew_browser_available=False)["content"]
    assert "Crew browser is available" in with_browser
    assert "Crew browser is available" not in without_browser
    assert "must not be bypassed" in with_browser

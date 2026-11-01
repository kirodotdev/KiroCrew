"""S1-03: cached schemas are not session grants; refresh is observable and scoped."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_projection import AgentProjection
from kiro_byo.mcp_client import McpManager
from kiro_byo.mcp_transports import StdioMcpServer
from kiro_byo.turn_runner import TurnRunnerMixin

ENTRY = {"type": "stdio", "name": "kirocrew-core", "command": "not-executed", "args": []}
BROWSER = {"name": "browser", "description": "Crew-owned interactive browser", "inputSchema": {"type": "object"}}
MEMORY = {"name": "memory_search", "description": "Search memory", "inputSchema": {"type": "object"}}


def manager_with_cached_browser(verified: bool = False) -> McpManager:
    manager = McpManager([ENTRY])
    manager.servers["kirocrew-core"].tools = [BROWSER, MEMORY]
    manager._rebuild_bindings()
    if verified:
        manager._live_verified_servers.add("kirocrew-core")
    manager._ready_event.set()
    return manager


def names(session: Session) -> set[str]:
    return {row["function"]["name"] for row in TurnRunnerMixin._active_tools_for_turn(session)}


def test_cached_crew_browser_is_not_live_authorization() -> None:
    manager = manager_with_cached_browser()
    try:
        session = Session("cache", ".", "model", mcp=manager)
        assert "mcp__kirocrew-core__browser" not in names(session)
        assert "mcp__kirocrew-core__browser" not in manager.crew_browser_tool_names()
        assert "mcp__kirocrew-core__browser" not in {
            item.public_name for item in manager.search_tools("browser", verified_only=True)[0]
        }
        assert "not verified" in manager.call("mcp__kirocrew-core__browser", {})
        snapshot = manager.host_capability_snapshot()
        assert snapshot["pending_crew_servers"] == ["kirocrew-core"]
        assert snapshot["crew_browser"] is False
    finally:
        manager.close()


def test_live_broker_advertises_only_agent_granted_tools() -> None:
    manager = manager_with_cached_browser(verified=True)
    try:
        agent = AgentProjection(agent_id="agent", tools_declared=True, tools=("@mcp",),
                                excluded_tools=("@kirocrew-core/browser",))
        session = Session("live", ".", "model", mcp=manager, agent=agent)
        assert "mcp__kirocrew-core__browser" not in names(session)
        assert manager.host_capability_snapshot(allowed=lambda b: agent.allows_mcp(
            server_name=b.server_name, tool_name=b.tool_name, public_name=b.public_name
        ))["crew_browser"] is False
        assert "mcp__kirocrew-core__memory_search" in manager.host_capability_snapshot()["available_tools"]
    finally:
        manager.close()


def test_refresh_revokes_removed_tool_and_prunes_session_lru() -> None:
    manager = manager_with_cached_browser(verified=True)
    manager._last_host_refresh = 0
    try:
        session = Session("refresh", ".", "model", mcp=manager,
                          loaded_tool_names=["mcp__kirocrew-core__memory_search"])
        server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
        events = []
        server._runtime_event = lambda kind, session, data: events.append((kind, data))
        with patch.object(StdioMcpServer, "refresh_tools", autospec=True,
                          side_effect=lambda instance: setattr(instance, "tools", [BROWSER]) or [BROWSER]):
            server._sync_host_capabilities(session)
        assert session.loaded_tool_names == []
        assert "mcp__kirocrew-core__memory_search" not in names(session)
        assert "mcp__kirocrew-core__browser" in names(session)
        assert events[-1][0] == "crew.capabilities.changed"
        assert events[-1][1]["crew_browser"] is True
        assert "command" not in str(events[-1])
    finally:
        manager.close()


def test_refresh_failure_removes_stale_grant_without_reusing_disk_cache() -> None:
    manager = manager_with_cached_browser(verified=True)
    manager._last_host_refresh = 0
    try:
        with patch.object(StdioMcpServer, "refresh_tools", side_effect=RuntimeError("host offline")):
            manager.refresh_host_capabilities()
        assert manager.host_capability_snapshot()["verified_crew_servers"] == []
        assert manager.host_capability_snapshot()["failed_servers"] == ["kirocrew-core"]
        assert "mcp__kirocrew-core__browser" not in names(Session("offline", ".", "model", mcp=manager))
    finally:
        manager.close()


def test_snapshot_contains_no_broker_secret_or_server_command() -> None:
    entry = dict(ENTRY, env=[{"name": "KIROCREW_STUB_SESSION_TOKEN", "value": "secret-token-value"}])
    manager = McpManager([entry])
    try:
        snapshot = str(manager.host_capability_snapshot())
        assert "secret-token-value" not in snapshot
        assert "KIROCREW_STUB_SESSION_TOKEN" not in snapshot
        assert "not-executed" not in snapshot
    finally:
        manager.close()


def test_session_load_fresh_empty_agent_projection_does_not_revive_saved_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    saved = Session("old-agent", str(cwd), "model",
                    agent=AgentProjection(agent_id="retired", prompt="old agent rules",
                                          tools_declared=True, tools=("@mcp",)))
    server._persist_session(saved)
    # Crew has explicitly withdrawn the earlier saved agent projection.
    # Legacy control-plane tests restore a @staticmethod as a plain function
    # on the class. Guard our restore test against suite-order pollution.
    with (patch.object(server, "_config") as cfg,
          patch.object(AcpServer, "_mcp_from_params", staticmethod(AcpServer._mcp_from_params))):
        # _restore_session_impl still needs a valid config; use the regular
        # local test fixture loader instead of invoking any model/provider.
        from kiro_byo.config import load_config
        cfg.side_effect = load_config
        restored, _config = server._restore_session(
            "old-agent", {"cwd": str(cwd), "mcpServers": [],
                          "_meta": {"kiro": {"customAgents": []}}}, replay=False,
        )
    assert restored.agent.agent_id == ""
    assert restored.agent.prompt == ""
    assert restored.agent.tools == ()
    server._executor.shutdown(wait=True)


def test_live_broker_recovers_after_transient_refresh_failure() -> None:
    manager = manager_with_cached_browser(verified=True)
    manager._last_host_refresh = 0
    try:
        with patch.object(StdioMcpServer, "refresh_tools", side_effect=RuntimeError("temporary")):
            manager.refresh_host_capabilities()
        assert manager.host_capability_snapshot()["crew_browser"] is False
        manager._last_host_refresh = 0  # represent next allowed refresh boundary
        with patch.object(StdioMcpServer, "refresh_tools", autospec=True,
                          side_effect=lambda instance: setattr(instance, "tools", [BROWSER]) or [BROWSER]):
            manager.refresh_host_capabilities()
        assert manager.host_capability_snapshot()["crew_browser"] is True
        assert manager.host_capability_snapshot()["failed_servers"] == []
    finally:
        manager.close()

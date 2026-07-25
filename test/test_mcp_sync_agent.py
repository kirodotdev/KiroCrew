"""Tests for _sync_mcp_to_agent and _sync_mcp_to_agent_batch in mcp.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers.agents import (
    api_capability_mcp_install,
    api_capability_mcp_uninstall,
)


@pytest.fixture()
def mcp_env(tmp_path: Path):
    """Set up agent config and global mcp.json in tmp_path."""
    agent_cfg = tmp_path / "kirocrew.json"
    mcp_json = tmp_path / "mcp.json"

    agent_cfg.write_text(json.dumps({
        "name": "kirocrew",
        "mcpServers": {"builder-mcp": {"command": "builder-mcp"}},
        "tools": ["@builder-mcp"],
        "allowedTools": ["@builder-mcp"],
    }))
    mcp_json.write_text(json.dumps({"mcpServers": {
        "builder-mcp": {"command": "builder-mcp"},
        "slack-mcp": {"command": "slack-mcp", "args": []},
        "outlook-mcp": {"command": "outlook-mcp", "env": {"WRITES": "true"}},
    }}))

    with patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json), \
         patch("kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg):
        yield agent_cfg, mcp_json


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestSyncMcpToAgent:
    def test_enable_adds_server_and_tool_refs(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("slack-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert "slack-mcp" in cfg["mcpServers"]
        assert "@slack-mcp" in cfg["tools"]
        assert "@slack-mcp" in cfg["allowedTools"]

    def test_enable_preserves_existing_server(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("builder-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert cfg["mcpServers"]["builder-mcp"] == {"command": "builder-mcp"}

    def test_enable_strips_disabled_key(self, mcp_env):
        agent_cfg, mcp_json = mcp_env
        d = json.loads(mcp_json.read_text(encoding="utf-8"))
        d["mcpServers"]["slack-mcp"]["disabled"] = True
        mcp_json.write_text(json.dumps(d))
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("slack-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert "disabled" not in cfg["mcpServers"]["slack-mcp"]

    def test_enable_noop_when_already_present(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("builder-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert cfg["tools"].count("@builder-mcp") == 1

    def test_disable_removes_tool_refs(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("builder-mcp", enabled=False)
        cfg = _load(agent_cfg)
        assert "@builder-mcp" not in cfg["tools"]
        assert "@builder-mcp" not in cfg["allowedTools"]

    def test_remove_deletes_server_entry(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("builder-mcp", enabled=False, remove=True)
        cfg = _load(agent_cfg)
        assert "builder-mcp" not in cfg["mcpServers"]

    def test_enable_returns_early_on_missing_mcp_json(self, mcp_env):
        agent_cfg, mcp_json = mcp_env
        mcp_json.unlink()
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent
        _sync_mcp_to_agent("slack-mcp", enabled=True)
        cfg = _load(agent_cfg)
        assert "slack-mcp" not in cfg.get("mcpServers", {})


class TestSyncMcpToAgentBatch:
    def test_enable_adds_multiple_servers(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch
        _sync_mcp_to_agent_batch(["slack-mcp", "outlook-mcp"], enabled=True)
        cfg = _load(agent_cfg)
        assert "slack-mcp" in cfg["mcpServers"]
        assert "outlook-mcp" in cfg["mcpServers"]
        assert "@slack-mcp" in cfg["tools"]
        assert "@outlook-mcp" in cfg["allowedTools"]

    def test_disable_removes_multiple_tool_refs(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch
        _sync_mcp_to_agent_batch(["builder-mcp"], enabled=False)
        cfg = _load(agent_cfg)
        assert "@builder-mcp" not in cfg["tools"]

    def test_enable_with_missing_mcp_json_still_adds_tool_refs(self, mcp_env):
        """Post #15 fix: existing servers get tool refs even when mcp.json missing."""
        agent_cfg, mcp_json = mcp_env
        mcp_json.unlink()
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch
        _sync_mcp_to_agent_batch(["builder-mcp"], enabled=True)
        cfg = _load(agent_cfg)
        # builder-mcp already in mcpServers, should still get tool ref
        assert "@builder-mcp" in cfg["tools"]

    def test_enable_skips_invalid_spec(self, mcp_env):
        agent_cfg, mcp_json = mcp_env
        d = json.loads(mcp_json.read_text(encoding="utf-8"))
        d["mcpServers"]["bad-server"] = "not-a-dict"
        mcp_json.write_text(json.dumps(d))
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch
        _sync_mcp_to_agent_batch(["bad-server"], enabled=True)
        cfg = _load(agent_cfg)
        assert "bad-server" not in cfg["mcpServers"]

    def test_noop_returns_without_write(self, mcp_env):
        agent_cfg, _ = mcp_env
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent_batch
        _sync_mcp_to_agent_batch(["builder-mcp"], enabled=True)
        cfg = _load(agent_cfg)
        assert "@builder-mcp" in cfg["tools"]


class TestAimMcpInstallSync:

    @staticmethod
    def _mgr(ok: bool):
        from kiro_crew.platform.interfaces import CapabilityResult

        m = MagicMock()
        m.available.return_value = True
        m.install_mcp = AsyncMock(
            return_value=CapabilityResult(ok=ok, message="" if ok else "install failed")
        )
        m.uninstall_mcp = AsyncMock(
            return_value=CapabilityResult(ok=ok, message="" if ok else "uninstall failed")
        )
        return m

    @pytest.mark.asyncio
    async def test_install_calls_sync(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "meetings-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=True),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_install(req)

        assert resp.status == 200
        mock_sync.assert_called_once_with("meetings-mcp", True)

    @pytest.mark.asyncio
    async def test_install_no_sync_on_aim_failure(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "bad-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=False),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_install(req)

        assert resp.status == 500
        mock_sync.assert_not_called()

    @pytest.mark.asyncio
    async def test_uninstall_calls_sync_with_remove(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "meetings-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=True),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_uninstall(req)

        assert resp.status == 200
        mock_sync.assert_called_once_with("meetings-mcp", False, remove=True)

    @pytest.mark.asyncio
    async def test_uninstall_no_sync_on_aim_failure(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"server_id": "bad-mcp"})
        req.app = {"state": MagicMock()}
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents._capability_manager",
                return_value=self._mgr(ok=False),
            ),
            patch("kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent") as mock_sync,
        ):
            resp = await api_capability_mcp_uninstall(req)

        assert resp.status == 500
        mock_sync.assert_not_called()


class TestApiMcpSyncToolsUpdate:

    @pytest.mark.asyncio
    async def test_sync_adds_tools_for_discovered_servers(self, mcp_env):
        """api_mcp_sync should call _sync_mcp_to_agent_batch for new servers."""
        from kiro_crew.dashboard.handlers.mcp import api_mcp_sync

        agent_cfg, _ = mcp_env

        mock_server = MagicMock()
        mock_server.name = "aws-outlook-mcp"
        mock_server.command = "aws-outlook-mcp"
        mock_server.args = []
        mock_server.env = {}
        mock_server.is_remote = False

        req = MagicMock()
        req.app = {"state": MagicMock()}

        with (
            patch(
                "kiro_crew.mcp_discovery.discover_servers_to_sync",
                return_value=[mock_server],
            ),
            patch(
                "kiro_crew.mcp_discovery.sync_to_agent_config",
                return_value=True,
            ),
            patch("kiro_crew.mcp_discovery.register_servers_for_cc"),
            patch("kiro_crew.dashboard.handlers.mcp._get_mcp_lock") as mock_lock,
            patch("kiro_crew.dashboard.handlers.mcp._write_mcp_json"),
            patch(
                "kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent_batch",
            ) as mock_batch,
            patch(
                "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
                new_callable=AsyncMock,
                return_value=1,
            ),
        ):
            mock_lock.return_value = AsyncMock()
            resp = await api_mcp_sync(req)

        assert resp.status == 200
        mock_batch.assert_called_once_with(["aws-outlook-mcp"], enabled=True)

    @pytest.mark.asyncio
    async def test_sync_no_tools_update_when_nothing_discovered(self, mcp_env):
        """api_mcp_sync should not call _sync_mcp_to_agent_batch when empty."""
        from kiro_crew.dashboard.handlers.mcp import api_mcp_sync

        req = MagicMock()
        req.app = {"state": MagicMock()}

        with (
            patch(
                "kiro_crew.mcp_discovery.discover_servers_to_sync",
                return_value=[],
            ),
            patch(
                "kiro_crew.dashboard.handlers.mcp._sync_mcp_to_agent_batch",
            ) as mock_batch,
            patch(
                "kiro_crew.dashboard.handlers.sessions._reset_all_sessions",
                new_callable=AsyncMock,
                return_value=0,
            ),
        ):
            resp = await api_mcp_sync(req)

        assert resp.status == 200
        mock_batch.assert_not_called()

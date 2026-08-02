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

    agent_cfg.write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "mcpServers": {"builder-mcp": {"command": "builder-mcp"}},
                "tools": ["@builder-mcp"],
                "allowedTools": ["@builder-mcp"],
            }
        )
    )
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "builder-mcp": {"command": "builder-mcp"},
                    "slack-mcp": {"command": "slack-mcp", "args": []},
                    "outlook-mcp": {"command": "outlook-mcp", "env": {"WRITES": "true"}},
                }
            }
        )
    )

    with (
        patch("kiro_crew.dashboard.handlers.mcp._GLOBAL_MCP_JSON", mcp_json),
        patch(
            "kiro_crew.dashboard.handlers.agents._installed_agent_config", return_value=agent_cfg
        ),
    ):
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


class TestOffloadedSyncHoldsTheConfigLock:
    """`_sync_mcp_to_agent*` does a read-modify-write of kirocrew.json. Offloading
    it to a worker thread means two concurrent MCP requests can interleave, so
    every offloaded call must run inside `_get_config_lock()` — the event loop no
    longer serializes them for free.
    """

    def test_every_offloaded_sync_is_under_the_config_lock(self) -> None:
        from pathlib import Path

        lines = (
            Path("src/kiro_crew/dashboard/handlers/mcp.py").read_text(encoding="utf-8").splitlines()
        )
        offenders: list[str] = []
        for i, ln in enumerate(lines):
            if "asyncio.to_thread(" in ln and "_sync_mcp_to_agent" in ln:
                window = "\n".join(lines[max(0, i - 4) : i + 1])
                if "_get_config_lock()" not in window:
                    offenders.append(f"line {i + 1}: {ln.strip()[:70]}")
        assert offenders == [], "offloaded sync without config lock: " + "; ".join(offenders)


class TestSyncSharesTheFileLockWithBridges:
    """The dashboard sync and bridges' app-MCP registration both RMW kirocrew.json.
    They must share ONE file lock; the dashboard's in-process _get_config_lock does
    not coordinate with bridges' cross-process _mcp_lock, so the dashboard paths
    now acquire _mcp_lock too."""

    def test_both_sync_funcs_hold_the_mcp_file_lock(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import mcp

        for fn in (mcp._sync_mcp_to_agent, mcp._sync_mcp_to_agent_batch):
            src = inspect.getsource(fn)
            assert "_mcp_lock(target=_installed_agent_config())" in src, fn.__name__


class TestSyncStripsGovernedAutoApprove:
    """Copying a global MCP server into kirocrew.json must not carry a governed
    `autoApprove`: kiro-cli honours it on the copy and auto-approves the server
    without ever reaching the PreToolUse gate."""

    def test_single_sync_strips_autoapprove_when_governed(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import mcp

        src = inspect.getsource(mcp._sync_mcp_to_agent_unlocked)
        assert 'entry.pop("autoApprove", None)' in src
        assert "not may_skip_gate_now(tool_ref)" in src

    def test_batch_sync_strips_autoapprove_when_governed(self) -> None:
        import inspect

        from kiro_crew.dashboard.handlers import mcp

        src = inspect.getsource(mcp._sync_mcp_to_agent_batch_unlocked)
        assert '_entry.pop("autoApprove", None)' in src


class TestSyncStripsPreExistingGovernedAutoApprove:
    """A governed autoApprove must be stripped even when the alias ALREADY exists
    in kirocrew.json (re-enable, or a spec written before the ceiling): the copy
    branch only runs for a brand-new alias."""

    def test_existing_entry_autoapprove_is_stripped(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent, mcp_server_alias

        alias = mcp_server_alias("slack-mcp")
        cfg = _load(agent_cfg)
        cfg.setdefault("mcpServers", {})[alias] = {"command": "x", "autoApprove": ["danger"]}
        agent_cfg.write_text(json.dumps(cfg))
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: False)  # governed

        _sync_mcp_to_agent("slack-mcp", enabled=True)
        out = _load(agent_cfg)
        assert "autoApprove" not in out["mcpServers"][alias], "governed grant must be stripped"


class TestGovernedSyncAuditsWithheld:
    """A governed enable withholds auto-approve (mounts in `tools`, keeps the
    ref OUT of allowedTools). The SEL audit must record that WITHHELD decision,
    not a grant — logging mcp_tools_added there falsely reports the opposite.
    """

    class _SelRec:
        def __init__(self) -> None:
            self.events: list[dict] = []

        def log_api_access(self, **kw) -> None:
            self.events.append(kw)

        def ops(self) -> set[str]:
            return {e.get("operation") for e in self.events}

    def test_single_governed_emits_withheld_not_added(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent, mcp_server_alias

        rec = self._SelRec()
        monkeypatch.setattr(mcp, "sel", lambda: rec)
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: False)  # governed

        _sync_mcp_to_agent("slack-mcp", enabled=True)

        ref = f"@{mcp_server_alias('slack-mcp')}"
        out = _load(agent_cfg)
        assert ref in out.get("tools", [])  # mounted
        assert ref not in out.get("allowedTools", [])  # auto-approve withheld
        assert "mcp_auto_approve_withheld" in rec.ops()
        assert "mcp_tools_added" not in rec.ops()

    def test_single_ungoverned_still_emits_added(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import _sync_mcp_to_agent, mcp_server_alias

        rec = self._SelRec()
        monkeypatch.setattr(mcp, "sel", lambda: rec)
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: True)  # ungoverned

        _sync_mcp_to_agent("slack-mcp", enabled=True)

        ref = f"@{mcp_server_alias('slack-mcp')}"
        out = _load(agent_cfg)
        assert ref in out.get("allowedTools", [])
        assert "mcp_tools_added" in rec.ops()
        assert "mcp_auto_approve_withheld" not in rec.ops()

    def test_batch_governed_emits_withheld_not_added(self, mcp_env, monkeypatch):
        agent_cfg, _ = mcp_env
        import kiro_crew.dashboard.handlers.mcp as mcp
        from kiro_crew.dashboard.handlers.mcp import (
            _sync_mcp_to_agent_batch,
            mcp_server_alias,
        )

        rec = self._SelRec()
        monkeypatch.setattr(mcp, "sel", lambda: rec)
        monkeypatch.setattr(mcp, "may_skip_gate_now", lambda ref: False)  # governed

        _sync_mcp_to_agent_batch(["slack-mcp", "outlook-mcp"], enabled=True)

        out = _load(agent_cfg)
        for name in ("slack-mcp", "outlook-mcp"):
            ref = f"@{mcp_server_alias(name)}"
            assert ref in out.get("tools", [])
            assert ref not in out.get("allowedTools", [])
        assert "mcp_auto_approve_withheld" in rec.ops()
        assert "mcp_tools_added" not in rec.ops()


class TestCapabilityInstallOffloadsTheLockedSync:
    """_sync_mcp_to_agent takes bridges' synchronous _mcp_lock for a full
    kirocrew.json RMW. Called directly on the event loop it freezes the gateway
    when a concurrent app registration holds that lock, so every async caller
    MUST offload it to a worker thread."""

    def _assert_offloaded(self, fn: object) -> None:
        import inspect

        src = inspect.getsource(fn)  # type: ignore[arg-type]
        stripped = src.replace("to_thread(_sync_mcp_to_agent", "").replace(
            "to_thread(lambda: _sync_mcp_to_agent", ""
        )
        assert (
            "_sync_mcp_to_agent(" not in stripped
        ), f"{fn.__name__} calls _sync_mcp_to_agent on the event loop; offload it"

    def test_capability_install_uninstall_offload(self):
        from kiro_crew.dashboard.handlers import agents

        self._assert_offloaded(agents.api_capability_mcp_install)
        self._assert_offloaded(agents.api_capability_mcp_uninstall)

    def test_discover_capability_install_offloads(self):
        from kiro_crew.dashboard.handlers import mcp_discover

        self._assert_offloaded(mcp_discover._install_via_capability)

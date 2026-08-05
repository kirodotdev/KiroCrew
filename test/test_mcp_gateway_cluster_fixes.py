"""Regression tests for #926, #927, #928 (mcp-gateway cluster)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.mcp_gateway.rewriter import (
    _WRAPPER_MARKER,
    _WRAPPER_MARKER_LEGACY,
)
from kiro_crew.mcp_gateway.session_servers import (
    injection_server_names,
    pooled_session_servers,
)

# ── #926: enabling gateway at runtime rebuilds the provider factory ─────────


class TestEnableRebuildsFactory:
    """After toggling mcp_gateway.enabled, the provider factory must be
    rebuilt so new sessions resolve the overlay path from current config."""

    @pytest.mark.asyncio
    async def test_enable_calls_refresh_defaults(self) -> None:
        """_apply_mcp_gateway_enabled must call refresh_defaults() on the
        session manager so the captured overlay path is re-resolved."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
        orch._mcp_gateway_manager = None
        orch.dashboard_state = SimpleNamespace()
        orch.sessions = MagicMock()
        orch.sessions.refresh_defaults = AsyncMock()

        with patch.object(type(orch), "_init_mcp_gateway", new_callable=AsyncMock) as mock_init:
            mock_init.return_value = None
            with patch("kiro_crew.slack.gateway.KiroCrewConfig") as mock_cfg:
                mock_cfg.load.return_value = MagicMock(mcp_gateway=MagicMock(enabled=True))
                orch._cfg = mock_cfg.load.return_value
                result = await orch._apply_mcp_gateway_enabled(True)  # noqa: F841

        orch.sessions.refresh_defaults.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disable_also_calls_refresh_defaults(self) -> None:
        """Disabling must also rebuild the factory so new sessions stop
        injecting stubs pointing at a dead socket."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
        orch._mcp_gateway_manager = MagicMock(is_running=False)
        orch._mcp_gateway_manager.ping = AsyncMock(return_value=False)
        orch.dashboard_state = SimpleNamespace()
        orch.sessions = MagicMock()
        orch.sessions.refresh_defaults = AsyncMock()

        with patch.object(type(orch), "_stop_mcp_broker", new_callable=AsyncMock):
            with patch("kiro_crew.slack.gateway.KiroCrewConfig") as mock_cfg:
                mock_cfg.load.return_value = MagicMock(mcp_gateway=MagicMock(enabled=False))
                orch._cfg = mock_cfg.load.return_value
                await orch._apply_mcp_gateway_enabled(False)

        orch.sessions.refresh_defaults.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_crash_when_sessions_is_none(self) -> None:
        """Early boot: sessions may not be initialised yet."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
        orch._mcp_gateway_manager = None
        orch.dashboard_state = None
        orch.sessions = None

        with patch.object(type(orch), "_init_mcp_gateway", new_callable=AsyncMock):
            with patch("kiro_crew.slack.gateway.KiroCrewConfig") as mock_cfg:
                mock_cfg.load.return_value = MagicMock(mcp_gateway=MagicMock(enabled=True))
                orch._cfg = mock_cfg.load.return_value
                # Must not raise
                await orch._apply_mcp_gateway_enabled(True)


# ── #927: injection precedence is robustly tested ───────────────────────────


class TestInjectionPrecedenceRobust:
    """Non-skippable structural tests that verify the injection contract
    without requiring kiro-cli on PATH."""

    def test_injection_server_names_matches_pooled_output(self, tmp_path: Path) -> None:
        """injection_server_names returns the exact set that
        pooled_session_servers would inject, ensuring the duplicate-detection
        guard covers the full injection set."""
        overlay = tmp_path / "agents"
        overlay.mkdir()
        servers = {
            "pooled-a": {_WRAPPER_MARKER: True, "command": "/usr/bin/a", "args": [], "env": {}},
            "pooled-b": {_WRAPPER_MARKER: True, "command": "/usr/bin/b", "args": [], "env": {}},
            "unpooled": {"command": "npx", "args": ["-y", "foo"], "env": {}},
        }
        (overlay / "agent.json").write_text(
            json.dumps({"name": "agent", "mcpServers": servers}), encoding="utf-8"
        )
        names = injection_server_names(overlay, "agent")
        injected = pooled_session_servers(overlay, "agent")
        assert names == {"pooled-a", "pooled-b"}
        assert {e["name"] for e in injected} == names

    def test_injection_names_empty_when_disabled(self) -> None:
        assert injection_server_names(None, "agent") == frozenset()

    def test_injected_names_shadow_by_identity(self, tmp_path: Path) -> None:
        """Each injected entry's name MUST equal the server it shadows in
        the agent spec. This is the structural guarantee that makes override
        semantics sufficient: kiro-cli matches by name."""
        overlay = tmp_path / "agents"
        overlay.mkdir()
        servers = {
            "builder-mcp": {_WRAPPER_MARKER: True, "command": "/bin/stub", "args": [], "env": {}},
        }
        (overlay / "agent.json").write_text(
            json.dumps({"name": "agent", "mcpServers": servers}), encoding="utf-8"
        )
        (entry,) = pooled_session_servers(overlay, "agent")
        # The name in the injection output is what kiro-cli uses to decide
        # whether to suppress the spec's own copy. If this ever diverges,
        # pooling silently doubles every server.
        assert entry["name"] == "builder-mcp"


# ── #928: backward-compatible naming migration ──────────────────────────────


class TestNamingMigration:
    """New naming emits KIROCREW_* identifiers while still reading legacy
    MC_* / _mc_* values from overlays written by prior versions."""

    def test_new_marker_value_is_kirocrew_prefixed(self) -> None:
        assert "kirocrew" in _WRAPPER_MARKER.lower()
        assert "mc_mcp" not in _WRAPPER_MARKER.lower()

    def test_legacy_marker_value_preserved(self) -> None:
        assert _WRAPPER_MARKER_LEGACY == "_mc_mcp_gateway_wrapped"

    def test_legacy_overlay_still_pools(self, tmp_path: Path) -> None:
        """An overlay written by an old rewriter (with the legacy marker)
        must still be recognised as a poolable stub."""
        overlay = tmp_path / "agents"
        overlay.mkdir()
        servers = {
            "old-stub": {
                _WRAPPER_MARKER_LEGACY: True,
                "command": "/bin/stub",
                "args": [],
                "env": {},
            },
        }
        (overlay / "agent.json").write_text(
            json.dumps({"name": "agent", "mcpServers": servers}), encoding="utf-8"
        )
        injected = pooled_session_servers(overlay, "agent")
        assert len(injected) == 1
        assert injected[0]["name"] == "old-stub"

    def test_legacy_marker_stripped_from_injected_element(self, tmp_path: Path) -> None:
        """Neither the new nor legacy marker should appear in the shaped
        output that reaches kiro-cli."""
        overlay = tmp_path / "agents"
        overlay.mkdir()
        servers = {"stub": {_WRAPPER_MARKER_LEGACY: True, "command": "/bin/s", "args": [], "env": {}}}
        (overlay / "agent.json").write_text(
            json.dumps({"name": "agent", "mcpServers": servers}), encoding="utf-8"
        )
        (entry,) = pooled_session_servers(overlay, "agent")
        assert _WRAPPER_MARKER not in entry
        assert _WRAPPER_MARKER_LEGACY not in entry

    def test_gatewayd_resolver_accepts_legacy_env_key(self) -> None:
        """env_target_resolver must find a target under the old MC_MCP_TARGET_
        prefix when the new KIROCREW_MCP_TARGET_ is absent."""
        from kiro_crew.mcp_gateway.gatewayd import env_target_resolver
        from kiro_crew.mcp_gateway.pool import PoolKey

        pk = PoolKey(
            server_name="test-srv",
            agent_name="kirocrew",
            command_args_hash="abc123",
            effective_env_hash="e",
            work_dir="/tmp/w",
            binary_version="1",
            os_uid=1000,
            sandbox_mode="off",
            autoapprove_set_hash="a",
            approval_mode="interactive",
            trust_all_tools=False,
            user_identity="test",
            config_snapshot_hash="c",
        )
        env_patch = {"MC_MCP_TARGET_TEST_SRV": "/usr/bin/test-srv --stdio"}
        with patch.dict(os.environ, env_patch, clear=False):
            # Remove any new-style key that might exist
            os.environ.pop("KIROCREW_MCP_TARGET_TEST_SRV", None)
            os.environ.pop("KIROCREW_MCP_TARGET_TEST_SRV__abc123", None)
            result = env_target_resolver(pk)
        assert result is not None
        command, args, env, work_dir = result
        assert command == "/usr/bin/test-srv"
        assert args == ["--stdio"]

    def test_gatewayd_resolver_prefers_new_env_key(self) -> None:
        """When both new and legacy keys exist, the new one wins."""
        from kiro_crew.mcp_gateway.gatewayd import env_target_resolver
        from kiro_crew.mcp_gateway.pool import PoolKey

        pk = PoolKey(
            server_name="test-srv",
            agent_name="kirocrew",
            command_args_hash="abc123",
            effective_env_hash="e",
            work_dir="/tmp/w",
            binary_version="1",
            os_uid=1000,
            sandbox_mode="off",
            autoapprove_set_hash="a",
            approval_mode="interactive",
            trust_all_tools=False,
            user_identity="test",
            config_snapshot_hash="c",
        )
        env_patch = {
            "KIROCREW_MCP_TARGET_TEST_SRV": "/usr/bin/new-srv --stdio",
            "MC_MCP_TARGET_TEST_SRV": "/usr/bin/old-srv --stdio",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            os.environ.pop("KIROCREW_MCP_TARGET_TEST_SRV__abc123", None)
            os.environ.pop("MC_MCP_TARGET_TEST_SRV__abc123", None)
            result = env_target_resolver(pk)
        assert result is not None
        command, _, _, _ = result
        assert command == "/usr/bin/new-srv"

    def test_stub_accepts_kirocrew_mcp_socket_env(self) -> None:
        """The stub should prefer KIROCREW_MCP_SOCKET over MC_MCP_SOCKET."""
        from kiro_crew.mcp_gateway.stub import _parse_args

        with patch.dict(os.environ, {
            "KIROCREW_MCP_SOCKET": "/tmp/new.sock",
            "MC_MCP_SOCKET": "/tmp/old.sock",
        }, clear=False):
            args = _parse_args([
                "--server", "test", "--agent", "a",
                "--target-command", "/bin/x", "--work-dir", "/tmp",
            ])
        assert args.socket == "/tmp/new.sock"

    def test_stub_falls_back_to_legacy_mc_mcp_socket(self) -> None:
        """When only MC_MCP_SOCKET is set, it should still work."""
        from kiro_crew.mcp_gateway.stub import _parse_args

        env = dict(os.environ)
        env.pop("KIROCREW_MCP_SOCKET", None)
        env["MC_MCP_SOCKET"] = "/tmp/legacy.sock"
        with patch.dict(os.environ, env, clear=True):
            args = _parse_args([
                "--server", "test", "--agent", "a",
                "--target-command", "/bin/x", "--work-dir", "/tmp",
            ])
        assert args.socket == "/tmp/legacy.sock"

    def test_rewriter_emits_new_env_key_prefix(self, tmp_path: Path) -> None:
        """rewrite_agents must populate target_env with KIROCREW_MCP_TARGET_
        keys, not the legacy MC_MCP_TARGET_ prefix."""
        from kiro_crew.mcp_gateway.rewriter import rewrite_agents

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "test.json").write_text(json.dumps({
            "name": "test",
            "mcpServers": {
                "pooled": {"command": "/bin/srv", "args": ["--stdio"]},
            },
        }), encoding="utf-8")
        overlay_dir = tmp_path / "overlay"
        overlay_dir.mkdir()
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        _, target_env = rewrite_agents(
            source_dir=agents_dir,
            overlay_dir=overlay_dir,
            socket_path=Path("/tmp/gw.sock"),
            work_dir=work_dir,
            poolable_servers=frozenset({"pooled"}),
        )
        # Should have at least one KIROCREW_MCP_TARGET_ key
        assert any(k.startswith("KIROCREW_MCP_TARGET_") for k in target_env), (
            f"Expected KIROCREW_MCP_TARGET_ prefix in target_env keys: {list(target_env.keys())}"
        )
        # Should NOT have any MC_MCP_TARGET_ keys (new installations)
        assert not any(k.startswith("MC_MCP_TARGET_") for k in target_env)

    def test_idempotent_rewrite_upgrades_legacy_marker(self, tmp_path: Path) -> None:
        """Re-running the rewriter on an overlay that carries the legacy
        marker must upgrade it to the new marker."""
        from kiro_crew.mcp_gateway.rewriter import _rewrite_single_spec

        spec = {
            "name": "agent",
            "mcpServers": {
                "already": {
                    _WRAPPER_MARKER_LEGACY: True,
                    "command": "/bin/stub",
                    "args": [],
                    "env": {},
                },
            },
        }
        new_spec, wrapped = _rewrite_single_spec(
            spec=spec,
            stubs_dir=tmp_path,
            socket_path=Path("/tmp/gw.sock"),
            work_dir=tmp_path,
            sandbox_mode="standard",
            approval_mode="interactive",
            poolable_servers=frozenset(),
        )
        entry = new_spec["mcpServers"]["already"]
        assert entry.get(_WRAPPER_MARKER) is True
        assert _WRAPPER_MARKER_LEGACY not in entry
        assert wrapped == 1

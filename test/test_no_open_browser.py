"""Tests for the --no-open / auto_open_browser feature.

Covers:
- DashboardConfig.auto_open_browser default and JSON loading
- GatewayOrchestrator stores no_open flag
- run_gateway passes no_open to orchestrator
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import DashboardConfig, KiroCrewConfig


def _load_from_raw_string(content: str) -> KiroCrewConfig:
    """Write raw string content to a temp file and load."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(content)
        tmp = Path(f.name)

    with patch("kiro_crew.config.loader.config_path", return_value=tmp):
        return KiroCrewConfig.load()


class TestAutoOpenBrowserConfig:
    """Tests for dashboard.auto_open_browser config field."""

    def test_default_is_true(self) -> None:
        """DashboardConfig defaults auto_open_browser to True."""
        cfg = DashboardConfig()
        assert cfg.auto_open_browser is True

    def test_from_json_false(self) -> None:
        """Loading config with auto_open_browser=false reads the value."""
        content = json.dumps({"dashboard": {"auto_open_browser": False}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.auto_open_browser is False

    def test_from_json_true(self) -> None:
        """Loading config with auto_open_browser=true reads the value."""
        content = json.dumps({"dashboard": {"auto_open_browser": True}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.auto_open_browser is True

    def test_missing_key_defaults_true(self) -> None:
        """Missing auto_open_browser key defaults to True."""
        content = json.dumps({"dashboard": {}})
        cfg = _load_from_raw_string(content)
        assert cfg.dashboard.auto_open_browser is True

    def test_roundtrip_serialization(self) -> None:
        """auto_open_browser survives save/load roundtrip."""
        cfg = KiroCrewConfig()
        cfg.dashboard.auto_open_browser = False
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            tmp = Path(f.name)
        with patch("kiro_crew.config.loader.config_path", return_value=tmp):
            cfg.save()
            loaded = KiroCrewConfig.load()
        assert loaded.dashboard.auto_open_browser is False


class TestNoOpenCliFlag:
    """Tests for the --no-open flag propagation to GatewayOrchestrator."""

    def test_orchestrator_stores_no_open_true(self) -> None:
        """GatewayOrchestrator stores no_open=True."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg, no_open=True)
        assert orch._no_open is True

    def test_orchestrator_stores_no_open_false_by_default(self) -> None:
        """GatewayOrchestrator defaults no_open to False."""
        from kiro_crew.slack.gateway import GatewayOrchestrator

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={}):
            orch = GatewayOrchestrator(cfg)
        assert orch._no_open is False

    @pytest.mark.asyncio
    async def test_run_gateway_passes_no_open(self) -> None:
        """run_gateway forwards no_open to GatewayOrchestrator."""
        from kiro_crew.slack.gateway import run_gateway

        cfg = KiroCrewConfig()
        with (
            # run_gateway's real body applies the aggregate cgroup ceiling
            # (systemctl set-property) — a host-service mutation the rootdir
            # guard refuses; stub it like every other host mutation.
            patch("kiro_crew.slack.gateway.ensure_agents_slice_limits", return_value=True),
            patch("kiro_crew.slack.gateway.GatewayOrchestrator") as mock_orch_cls,
        ):
            mock_orch = MagicMock()
            mock_orch.run = AsyncMock()
            mock_orch_cls.return_value = mock_orch
            await run_gateway(cfg, no_open=True)
            mock_orch_cls.assert_called_once_with(
                cfg,
                no_dashboard=False,
                no_crons=False,
                no_open=True,
                port_override=None,
                json_ready=False,
                approval_mode=None,
                test_mode=False,
            )

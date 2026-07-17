"""Tests for kiro-cli Tool Search wiring in the ACP provider.

Tool Search (https://kiro.dev/docs/cli/mcp/tool-search/) loads MCP tool specs
on demand instead of sending every spec each turn. KiroCrew exposes it via the
``agent.tool_search`` config toggle and applies it by writing the kiro setting
into the per-session ``<work_dir>/.kiro/settings/cli.json`` overlay — the same
file used for the effort overlay. These tests cover the overlay writer and the
AcpProvider application logic (kiro-only, no-op for the Claude backend).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers.acp import (
    AcpProvider,
    _write_cli_overlay,
    _write_tool_search_overlay,
)


def _build_provider(backend: str) -> AcpProvider:
    """Build an AcpProvider with a mocked client (mirrors test_acp_provider.py)."""
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


def _cli_json(tmp_path):
    return tmp_path / ".kiro" / "settings" / "cli.json"


# ── Overlay writer ─────────────────────────────────────────────────────────


class TestWriteToolSearchOverlay:
    def test_enabled_sets_flag_and_zeroes_thresholds(self, tmp_path):
        _write_tool_search_overlay(tmp_path, True)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True
        assert data["toolSearch.minPct"] == 0
        assert data["toolSearch.minTokens"] == 0

    def test_disabled_sets_false_and_drops_thresholds(self, tmp_path):
        # Enable first (writes the zeroed thresholds), then disable.
        _write_tool_search_overlay(tmp_path, True)
        _write_tool_search_overlay(tmp_path, False)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is False
        # Forced-on thresholds must be removed so a later globally-enabled
        # Tool Search isn't silently forced always-on by leftover zeros.
        assert "toolSearch.minPct" not in data
        assert "toolSearch.minTokens" not in data

    def test_merge_safe_with_effort_overlay(self, tmp_path):
        # The effort overlay shares this cli.json file — writing tool search
        # must preserve the effort keys and vice versa.
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "high")
        _write_tool_search_overlay(tmp_path, True)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert (
            data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"]
            == "high"
        )
        assert data["toolSearch.enabled"] is True
        assert data["toolSearch.minPct"] == 0

    def test_effort_write_after_tool_search_preserves_both(self, tmp_path):
        # Reverse order: tool search first, then effort.
        _write_tool_search_overlay(tmp_path, True)
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "xhigh")
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True
        assert (
            data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"]
            == "xhigh"
        )

    def test_handles_corrupt_existing_json(self, tmp_path):
        cli = _cli_json(tmp_path)
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("{ this is not valid json", encoding="utf-8")
        _write_tool_search_overlay(tmp_path, True)
        data = json.loads(cli.read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True

    def test_idempotent(self, tmp_path):
        _write_tool_search_overlay(tmp_path, True)
        first = _cli_json(tmp_path).read_text(encoding="utf-8")
        _write_tool_search_overlay(tmp_path, True)
        second = _cli_json(tmp_path).read_text(encoding="utf-8")
        assert first == second


# ── Provider application logic ───────────────────────────────────────────────


class TestApplyToolSearchOverlay:
    def test_kiro_enabled_writes_overlay(self, tmp_path):
        provider = _build_provider(backend="")
        provider._client._work_dir = tmp_path
        provider._tool_search = True
        provider._apply_tool_search_overlay()
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True
        assert data["toolSearch.minPct"] == 0

    def test_kiro_disabled_writes_false(self, tmp_path):
        provider = _build_provider(backend="")
        provider._client._work_dir = tmp_path
        provider._tool_search = False
        provider._apply_tool_search_overlay()
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is False

    def test_claude_backend_skips(self, tmp_path):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client._work_dir = tmp_path
        provider._tool_search = True
        provider._apply_tool_search_overlay()
        assert not _cli_json(tmp_path).exists()

    def test_none_value_skips(self, tmp_path):
        provider = _build_provider(backend="")
        provider._client._work_dir = tmp_path
        provider._tool_search = None
        provider._apply_tool_search_overlay()
        assert not _cli_json(tmp_path).exists()


# ── Constructor wiring ───────────────────────────────────────────────────────


class TestInitWiring:
    def test_kiro_enabled_applies_on_init(self):
        with patch("kiro_crew.providers.acp.AcpClient") as mock_client, patch.object(
            AcpProvider, "_apply_tool_search_overlay"
        ) as ats:
            mock_client.return_value.backend = ""
            AcpProvider(acp_backend="", tool_search=True)
        ats.assert_called_once()

    def test_claude_backend_does_not_apply_on_init(self):
        with patch("kiro_crew.providers.acp.AcpClient") as mock_client, patch.object(
            AcpProvider, "_apply_tool_search_overlay"
        ) as ats:
            mock_client.return_value.backend = ACP_BACKEND_CLAUDE
            AcpProvider(acp_backend=ACP_BACKEND_CLAUDE, tool_search=True)
        ats.assert_not_called()


# ── Config plumbing ──────────────────────────────────────────────────────────


class TestConfigField:
    def test_default_is_true(self):
        from kiro_crew.config.loader import AgentConfig

        assert AgentConfig().tool_search is True

    def test_load_reads_false_from_config(self, tmp_path):
        import unittest.mock

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"agent": {"tool_search": False}}), encoding="utf-8"
        )
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path", return_value=cfg_file
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.agent.tool_search is False

    def test_load_defaults_true_when_absent(self, tmp_path):
        import unittest.mock

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"agent": {}}), encoding="utf-8")
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path", return_value=cfg_file
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.agent.tool_search is True


class TestSchemaEntry:
    """The Settings UI is auto-generated from the config schema; a boolean
    entry renders as a toggle. This locks in that agent.tool_search surfaces."""

    def test_tool_search_in_config_schema(self):
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        entry = next(
            (e for e in SCHEMA_REGISTRY if e.path == "agent.tool_search"), None
        )
        assert entry is not None, "agent.tool_search missing from config schema"
        assert entry.type == "boolean"
        assert entry.default_value is True
        assert entry.label == "MCP Tool Search"
        assert not entry.has_children

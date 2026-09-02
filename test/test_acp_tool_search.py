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

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers.acp import (
    TOOL_SEARCH_DEFAULT_MIN_PCT,
    TOOL_SEARCH_DEFAULT_MIN_TOKENS,
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
    def test_enabled_sets_flag_and_default_thresholds(self, tmp_path):
        _write_tool_search_overlay(tmp_path, True)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True
        # Defaults mirror kiro-cli's own activation thresholds, so a small tool
        # set is NOT deferred and never pays a tool_search round-trip.
        assert data["toolSearch.minPct"] == TOOL_SEARCH_DEFAULT_MIN_PCT
        assert data["toolSearch.minTokens"] == TOOL_SEARCH_DEFAULT_MIN_TOKENS

    def test_disabled_sets_false_and_drops_thresholds(self, tmp_path):
        # Enable first (writes the thresholds), then disable.
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
        assert data["toolSearch.minPct"] == TOOL_SEARCH_DEFAULT_MIN_PCT

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
        assert data["toolSearch.minPct"] == TOOL_SEARCH_DEFAULT_MIN_PCT

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


# ── Configurable thresholds ──────────────────────────────────────────────────


class TestConfigurableThresholds:
    """The thresholds decide WHEN deferral starts, and deferral is not free: a
    deferred tool's spec is absent from the model's tool list, so the first
    direct call fails and must be recovered with ``tool_search``. Forcing the
    thresholds to 0 imposed that cost on every install, including ones whose
    specs were nowhere near large enough for deferral to pay for itself."""

    def test_configured_values_are_written(self, tmp_path):
        _write_tool_search_overlay(tmp_path, True, 12, 1234)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.minPct"] == 12
        assert data["toolSearch.minTokens"] == 1234

    def test_zero_zero_still_defers_always(self, tmp_path):
        # Deliberately supported: an operator who wants unconditional deferral
        # (the previous hard-coded behaviour) can still ask for it.
        _write_tool_search_overlay(tmp_path, True, 0, 0)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.minPct"] == 0
        assert data["toolSearch.minTokens"] == 0

    def test_stale_forced_zeros_are_overwritten(self, tmp_path):
        # Migration: machines configured by an earlier build already carry
        # minPct/minTokens = 0 in cli.json. The writer must overwrite them, not
        # merely refrain from writing — otherwise the upgrade is a no-op and
        # deferral stays unconditional forever.
        cli = _cli_json(tmp_path)
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text(
            json.dumps(
                {
                    "toolSearch.enabled": True,
                    "toolSearch.minPct": 0,
                    "toolSearch.minTokens": 0,
                }
            ),
            encoding="utf-8",
        )
        _write_tool_search_overlay(tmp_path, True)
        data = json.loads(cli.read_text(encoding="utf-8"))
        assert data["toolSearch.minPct"] == TOOL_SEARCH_DEFAULT_MIN_PCT
        assert data["toolSearch.minTokens"] == TOOL_SEARCH_DEFAULT_MIN_TOKENS

    @pytest.mark.parametrize(
        "given,expected",
        [(-5, 0), (0, 0), (100, 100), (101, 100), (7, 7)],
    )
    def test_min_pct_is_clamped_to_a_percentage(self, tmp_path, given, expected):
        _write_tool_search_overlay(tmp_path, True, given, 1)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.minPct"] == expected

    def test_negative_min_tokens_is_floored(self, tmp_path):
        _write_tool_search_overlay(tmp_path, True, 5, -1)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.minTokens"] == 0

    @pytest.mark.parametrize("junk", ["abc", None, [], {}])
    def test_unusable_values_fall_back_to_defaults(self, tmp_path, junk):
        # A hand-edited config must not write a non-numeric value into a kiro
        # setting, which would make kiro-cli reject the whole overlay.
        _write_tool_search_overlay(tmp_path, True, junk, junk)
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.minPct"] == TOOL_SEARCH_DEFAULT_MIN_PCT
        assert data["toolSearch.minTokens"] == TOOL_SEARCH_DEFAULT_MIN_TOKENS

    def test_provider_passes_configured_values_through(self, tmp_path):
        provider = _build_provider(backend="")
        provider._client._work_dir = tmp_path
        provider._tool_search = True
        provider._tool_search_min_pct = 33
        provider._tool_search_min_tokens = 4444
        provider._apply_tool_search_overlay()
        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.minPct"] == 33
        assert data["toolSearch.minTokens"] == 4444

    def test_constructor_defaults_to_kiro_thresholds(self):
        with patch("kiro_crew.providers.acp.AcpClient") as mock_client, patch.object(
            AcpProvider, "_apply_tool_search_overlay"
        ):
            mock_client.return_value.backend = ""
            provider = AcpProvider(acp_backend="", tool_search=True)
        assert provider._tool_search_min_pct == TOOL_SEARCH_DEFAULT_MIN_PCT
        assert provider._tool_search_min_tokens == TOOL_SEARCH_DEFAULT_MIN_TOKENS


class TestThresholdConfigFields:
    def test_defaults_match_the_provider_constants(self):
        # The dataclass cannot import the provider module (circular), so the two
        # spellings of the default are pinned together here instead.
        from kiro_crew.config.loader import AgentConfig

        assert AgentConfig().tool_search_min_pct == TOOL_SEARCH_DEFAULT_MIN_PCT
        assert AgentConfig().tool_search_min_tokens == TOOL_SEARCH_DEFAULT_MIN_TOKENS

    def test_load_reads_configured_values(self, tmp_path):
        import unittest.mock

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps(
                {"agent": {"tool_search_min_pct": 9, "tool_search_min_tokens": 111}}
            ),
            encoding="utf-8",
        )
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path", return_value=cfg_file
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.agent.tool_search_min_pct == 9
        assert cfg.agent.tool_search_min_tokens == 111

    def test_load_survives_a_non_numeric_value(self, tmp_path):
        import unittest.mock

        from kiro_crew.config.loader import KiroCrewConfig

        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(
            json.dumps({"agent": {"tool_search_min_pct": "lots"}}), encoding="utf-8"
        )
        with unittest.mock.patch(
            "kiro_crew.config.loader.config_path", return_value=cfg_file
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.agent.tool_search_min_pct == TOOL_SEARCH_DEFAULT_MIN_PCT

    @pytest.mark.parametrize(
        "path,default",
        [
            ("agent.tool_search_min_pct", TOOL_SEARCH_DEFAULT_MIN_PCT),
            ("agent.tool_search_min_tokens", TOOL_SEARCH_DEFAULT_MIN_TOKENS),
        ],
    )
    def test_thresholds_surface_in_config_schema(self, path, default):
        from kiro_crew.config.schema import SCHEMA_REGISTRY

        entry = next((e for e in SCHEMA_REGISTRY if e.path == path), None)
        assert entry is not None, f"{path} missing from config schema"
        assert entry.type == "integer"
        assert entry.default_value == default

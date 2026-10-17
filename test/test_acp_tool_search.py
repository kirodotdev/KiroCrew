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
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import requires_symlinks
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.providers import acp as acp_provider
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

    @pytest.mark.parametrize(
        "contents",
        [b"{ this is not valid json", b'{"x": "\xff"}', b"[1, 2]"],
        ids=["malformed", "undecodable", "non-object"],
    )
    def test_unmergeable_existing_file_raises_and_is_left_unchanged(self, tmp_path, contents):
        # Resetting the file to {} to make room for the flag would destroy the
        # operator's settings and every other session's effort entries. The
        # same guard as _write_cli_overlay: the bytes stay, the write raises.
        cli = _cli_json(tmp_path)
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_bytes(contents)

        with pytest.raises(ValueError, match="left unchanged"):
            _write_tool_search_overlay(tmp_path, True)

        assert cli.read_bytes() == contents

    def test_unreadable_existing_file_raises_the_read_error(self, tmp_path, monkeypatch):
        _write_tool_search_overlay(tmp_path, True)
        cli = _cli_json(tmp_path)
        before = cli.read_bytes()
        real_read = acp_provider.safe_read_file_bytes_nolink

        def _flaky_read(raw, *args, **kwargs):
            if Path(raw).name == "cli.json":
                raise OSError("sharing violation")
            return real_read(raw, *args, **kwargs)

        def _failed_pinned_read(_settings_fd, _cli_json):
            raise OSError("sharing violation")

        with monkeypatch.context() as temporary_patches:
            temporary_patches.setattr(acp_provider, "safe_read_file_bytes_nolink", _flaky_read)
            temporary_patches.setattr(acp_provider, "_read_pinned_cli_json", _failed_pinned_read)
            with pytest.raises(OSError, match="sharing violation"):
                _write_tool_search_overlay(tmp_path, False)

        assert cli.read_bytes() == before

    @pytest.mark.parametrize(
        "link_kind", [pytest.param("symlink", marks=requires_symlinks), "hardlink"]
    )
    def test_linked_existing_file_is_refused_without_copying_contents(self, tmp_path, link_kind):
        cli = _cli_json(tmp_path)
        cli.parent.mkdir(parents=True)
        target = tmp_path / "outside.json"
        secret = b'{"token": "SECRET"}'
        target.write_bytes(secret)
        try:
            if link_kind == "symlink":
                cli.symlink_to(target)
            else:
                os.link(target, cli)
        except (OSError, NotImplementedError) as exc:
            pytest.fail(f"{link_kind} is unavailable: {exc}")

        with pytest.raises(OSError, match="link|hardlink|regular"):
            _write_tool_search_overlay(tmp_path, True)

        assert target.read_bytes() == secret
        assert not any(
            path != cli
            and path.is_file()
            and not path.is_symlink()
            and b"SECRET" in path.read_bytes()
            for path in cli.parent.iterdir()
        )

    def test_idempotent(self, tmp_path):
        _write_tool_search_overlay(tmp_path, True)
        first = _cli_json(tmp_path).read_text(encoding="utf-8")
        _write_tool_search_overlay(tmp_path, True)
        second = _cli_json(tmp_path).read_text(encoding="utf-8")
        assert first == second

    @staticmethod
    def _stamp_clock(monkeypatch):
        """Drive the stamp clock by hand, so a repeat write would take a different stamp."""
        from types import SimpleNamespace

        from kiro_crew import workspace_cli_settings

        clock = [1_790_000_000.0]
        monkeypatch.setattr(workspace_cli_settings, "time", SimpleNamespace(time=lambda: clock[0]))

        def advance(seconds):
            clock[0] += seconds

        return advance

    def test_repeat_write_over_an_owned_effort_leaves_bytes_and_mtime_unchanged(
        self, tmp_path, monkeypatch
    ):
        advance = self._stamp_clock(monkeypatch)
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "high")
        _write_tool_search_overlay(tmp_path, True)
        cli = _cli_json(tmp_path)
        before = cli.read_bytes()
        before_stat = cli.stat()
        advance(10)

        _write_tool_search_overlay(tmp_path, True)

        after_stat = cli.stat()
        assert cli.read_bytes() == before
        assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
            before_stat.st_ino,
            before_stat.st_mtime_ns,
        )
        data = json.loads(before)
        assert data["kirocrew.effortOwned"] == {"claude-opus-4.7": "high"}
        assert data["kirocrew.effortOwnedStamp"] == int(after_stat.st_mtime)

    def test_repeat_write_with_no_record_leaves_bytes_unchanged(self, tmp_path, monkeypatch):
        advance = self._stamp_clock(monkeypatch)
        _write_tool_search_overlay(tmp_path, True)
        cli = _cli_json(tmp_path)
        before = cli.read_bytes()
        advance(10)

        _write_tool_search_overlay(tmp_path, True)

        assert cli.read_bytes() == before

    @pytest.mark.parametrize(
        "contents",
        [
            pytest.param(None, id="absent"),
            pytest.param(b'{"unrelated.key": 42}', id="operator"),
            pytest.param(b'{"kirocrew.effortOwnedStamp": 1790000000}', id="stray-stamp"),
        ],
    )
    def test_write_with_no_record_stores_no_kirocrew_key(self, tmp_path, contents):
        cli = _cli_json(tmp_path)
        if contents is not None:
            cli.parent.mkdir(parents=True)
            cli.write_bytes(contents)

        _write_tool_search_overlay(tmp_path, True)

        data = json.loads(cli.read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True
        assert not [key for key in data if key.startswith("kirocrew.")]


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

    @pytest.mark.asyncio
    async def test_unmergeable_file_survives_construction_and_start(
        self, tmp_path, monkeypatch, caplog
    ):
        # A corrupt overlay must never stop a session from starting, and the
        # refused write must be visible: the file keeps its bytes and the
        # provider logs the skip instead of raising into __init__ or start().
        cli = _cli_json(tmp_path)
        cli.parent.mkdir(parents=True, exist_ok=True)
        contents = b"{ this is not valid json"
        cli.write_bytes(contents)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.acp"):
            provider = AcpProvider(acp_backend="", work_dir=tmp_path, tool_search=True)
            monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
            await provider.start()

        assert cli.read_bytes() == contents
        assert "tool-search overlay write failed" in caplog.text


# ── Constructor wiring ───────────────────────────────────────────────────────


class TestInitWiring:
    def test_kiro_does_not_apply_on_init(self):
        with (
            patch("kiro_crew.providers.acp.AcpClient") as mock_client,
            patch.object(AcpProvider, "_apply_tool_search_overlay") as ats,
        ):
            mock_client.return_value.backend = ""
            AcpProvider(acp_backend="", tool_search=True)
        ats.assert_not_called()

    @pytest.mark.asyncio
    async def test_kiro_applies_tool_search_only_during_start(self, tmp_path, monkeypatch):
        provider = AcpProvider(acp_backend="", work_dir=tmp_path, tool_search=True)

        assert not _cli_json(tmp_path).exists()

        monkeypatch.setattr(provider, "_start_kiro_runtime", AsyncMock())
        await provider.start()

        data = json.loads(_cli_json(tmp_path).read_text(encoding="utf-8"))
        assert data["toolSearch.enabled"] is True

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

"""Tests for the shared reasoning-effort vocabulary (effort.py) and the
ACP provider cli.json overlay helpers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.effort import (
    EFFORT_LEVELS,
    EFFORT_VALUES,
    is_valid_effort,
    model_supports_effort,
    resolve_effort_for_model,
)
from kiro_crew.providers.acp import (
    _clear_cli_overlay_effort,
    _read_cli_overlay,
    _write_cli_overlay,
)


class TestEffortVocabulary:
    def test_levels_include_xhigh_ordered(self):
        assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")

    def test_values_add_empty_sentinel(self):
        assert EFFORT_VALUES == frozenset({"", "low", "medium", "high", "xhigh", "max"})

    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_is_valid_effort_true(self, level: str):
        assert is_valid_effort(level)

    @pytest.mark.parametrize("bad", ["", "LOW", "ultra", " low", 5, None, ["max"]])
    def test_is_valid_effort_false(self, bad: object):
        assert not is_valid_effort(bad)


class TestModelSupportsEffort:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4.7",
            "claude-sonnet-4.6",
            "global.anthropic.claude-opus-4-8[1m]",
            "anthropic.claude-sonnet-4-20250514-v1:0",
            "claude-fable-5",
            "global.anthropic.claude-fable-5[1m]",
        ],
    )
    def test_opus_sonnet_fable_supported(self, model: str):
        assert model_supports_effort(model)

    @pytest.mark.parametrize(
        "model",
        [None, "", "auto", "amazon.nova-pro-v1:0", "deepseek-3.2"],
    )
    def test_unsupported(self, model: str | None):
        assert not model_supports_effort(model)

    def test_raw_haiku_id_never_supports_effort_even_with_registry_fold(self):
        # The registry has no Haiku Bedrock profile, so claude-haiku-4.5 (a kiro
        # id) is registered as a claude_code ALIAS of Sonnet 4.6 1M (the cheapest
        # VALID Bedrock fold — passing it through verbatim would crash a CC
        # session with -32603). But "Haiku never supports effort" is a HARD rule
        # that must win over the registry: model_supports_effort is provider-
        # agnostic, and a kiro/acp Haiku agent reaches it with the RAW
        # "claude-haiku-4.5" spelling (the kiro path does NOT translate). So the
        # raw id must report False, NOT inherit Sonnet's supports_effort flag.
        from kiro_crew import model_registry as mr

        # The fold itself is unchanged — claude_code translation -> Sonnet id.
        assert mr.to_provider_id("claude-haiku-4.5", "claude_code") == (
            "global.anthropic.claude-sonnet-4-6[1m]"
        )
        # The raw kiro Haiku id is correctly effort-INCAPABLE (haiku guard wins).
        assert model_supports_effort("claude-haiku-4.5") is False
        # On the claude_code path the value reaching here is the FOLDED Sonnet
        # provider id (translated at the factory boundary), which IS capable.
        assert model_supports_effort("global.anthropic.claude-sonnet-4-6[1m]") is True
        # A model the registry does NOT list still uses the substring heuristic.
        assert model_supports_effort("some-haiku-thing") is False


class TestResolveEffortForModel:
    def test_slot_override_wins(self):
        assert (
            resolve_effort_for_model(
                "claude-opus-4.7",
                slot_overrides={"claude-opus-4.7": "low"},
                defaults={"claude-opus-4.7": "max"},
            )
            == "low"
        )

    def test_falls_back_to_defaults(self):
        assert (
            resolve_effort_for_model("claude-opus-4.7", defaults={"claude-opus-4.7": "high"})
            == "high"
        )

    def test_defaults_accept_json_string(self):
        # Frontend setVariable only stores strings, so defaults may arrive
        # JSON-encoded.
        assert (
            resolve_effort_for_model("claude-opus-4.7", defaults='{"claude-opus-4.7": "xhigh"}')
            == "xhigh"
        )

    def test_none_when_model_incapable(self):
        # 'auto' is genuinely effort-incapable (registry maps it to ""). (Haiku
        # folds to Sonnet and IS effort-capable — see
        # TestModelSupportsEffort.test_haiku_4_5_folds_to_sonnet_and_supports_effort.)
        assert resolve_effort_for_model("auto", slot_overrides={"auto": "max"}) is None

    def test_none_when_no_level(self):
        assert resolve_effort_for_model("claude-opus-4.7") is None

    def test_malformed_defaults_ignored(self):
        assert resolve_effort_for_model("claude-opus-4.7", defaults="not json") is None
        assert resolve_effort_for_model("claude-opus-4.7", defaults=12345) is None


class TestCliOverlay:
    def test_write_then_read_roundtrip(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "xhigh")
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.7": "xhigh"}
        # Verify on-disk shape matches kiro-cli's expected format.
        cli = tmp_path / ".kiro" / "settings" / "cli.json"
        data = json.loads(cli.read_text())
        assert data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "xhigh"

    def test_write_merges_preserves_other_keys(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text(
            json.dumps(
                {
                    "chat.enableNotifications": True,
                    "chat.modelDefaults": {
                        "claude-opus-4.6": {"output_config": {"effort": "high"}}
                    },
                }
            )
        )
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        data = json.loads((settings_dir / "cli.json").read_text())
        # Existing unrelated setting preserved.
        assert data["chat.enableNotifications"] is True
        # Both models present.
        assert data["chat.modelDefaults"]["claude-opus-4.6"]["output_config"]["effort"] == "high"
        assert data["chat.modelDefaults"]["claude-opus-4.7"]["output_config"]["effort"] == "max"

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert _read_cli_overlay(tmp_path) == {}

    def test_read_malformed_returns_empty(self, tmp_path):
        settings_dir = tmp_path / ".kiro" / "settings"
        settings_dir.mkdir(parents=True)
        (settings_dir / "cli.json").write_text("{ not json")
        assert _read_cli_overlay(tmp_path) == {}

    def test_clear_removes_only_target_model(self, tmp_path):
        _write_cli_overlay(tmp_path, "claude-opus-4.7", "max")
        _write_cli_overlay(tmp_path, "claude-opus-4.6", "high")
        _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7")
        assert _read_cli_overlay(tmp_path) == {"claude-opus-4.6": "high"}

    def test_clear_missing_file_noop(self, tmp_path):
        _clear_cli_overlay_effort(tmp_path, "claude-opus-4.7")  # must not raise
        assert _read_cli_overlay(tmp_path) == {}


class TestFactoryEffortThreading:
    """The provider factory must thread the slot's reasoning_effort_override
    into effort_per_model for BOTH ACP backends — otherwise a cold start
    (or the handler's reset-then-respawn) never applies the persisted effort."""

    def _capture_provider_kwargs(self, provider_name: str, **factory_call):
        # Both factory branches lazily `from kiro_crew.providers.acp import
        # AcpProvider` (circular-import workaround). That import runs inside
        # create_provider_factory(), so patch the source module symbol BEFORE
        # building the factory, then capture the construction kwargs.
        cfg = KiroCrewConfig()
        cfg.agent.provider = provider_name
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(**factory_call)
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    @pytest.mark.parametrize(
        "provider_name,expected_key",
        [
            # kiro (acp) threads the raw model.
            ("acp", "claude-opus-4.7"),
        ],
    )
    def test_valid_effort_on_opus_threads_per_model(self, provider_name, expected_key):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="xhigh",
        )
        assert kwargs.get("effort_per_model") == {expected_key: "xhigh"}

    @pytest.mark.parametrize("provider_name", ["acp"])
    def test_effort_on_incapable_model_not_threaded(self, provider_name):
        # 'auto' supports no effort on the kiro backend (kiro errors on auto).
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="auto",
            reasoning_effort_override="high",
        )
        assert kwargs.get("effort_per_model") == {}

    @pytest.mark.parametrize("provider_name", ["acp"])
    def test_invalid_effort_not_threaded(self, provider_name):
        kwargs = self._capture_provider_kwargs(
            provider_name,
            session_key="dashboard:1",
            model_override="claude-opus-4.7",
            reasoning_effort_override="ultra",
        )
        assert kwargs.get("effort_per_model") == {}

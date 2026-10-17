"""An operator-declared served context window (``agent.model_context_tokens``).

``model_registry.model_window`` resolves a model id through the live window, the
Kiro-list cache, the static registry, the supplementary map and the ``[1m]``
heuristic. An id none of them lists resolves to ``None``, which the budget code reads as
the 1,000,000-token reference (``context_assembly.budget._effective_window``). A fresh
session has no live window, so its first prompt is budgeted against that reference no
matter what the model is served with. ``agent.model_context_tokens`` lets the operator
declare the window an exact model id is served with; the resolver consults it right after
the live window.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew import context as ctx
from kiro_crew import model_registry
from kiro_crew.config import live
from kiro_crew.config import sections as config_sections
from kiro_crew.config.live import ConfigChange, ConfigDeferred
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import AgentConfig

MODEL = "server-a/org/model-7b"
SIBLING = "server-b/org/model-7b"
WINDOW = 32_768
_APPLIER = "bind[agent.model_context_tokens]"
_SECTIONS_LOGGER = config_sections.logger.name


def _coerce(raw: object) -> dict[str, int]:
    return config_sections.coerce_model_context_tokens(raw)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _SECTIONS_LOGGER]


class TestCoerceModelContextTokens:
    def test_a_valid_map_is_kept_as_written(self) -> None:
        raw = {MODEL: WINDOW, SIBLING: 131_072}
        assert _coerce(raw) == raw

    def test_the_bounds_are_inclusive(self) -> None:
        raw = {"low": 1_024, "high": 10_000_000}
        assert _coerce(raw) == raw

    @pytest.mark.parametrize(
        "value",
        [True, False, 0, -1, 1_023, 10_000_001, 4_096.0, "4096", None, [4_096], 10**400],
    )
    def test_a_value_that_is_not_a_window_drops_its_entry_and_says_so(
        self, value: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_SECTIONS_LOGGER):
            assert _coerce({"bad": value, MODEL: WINDOW}) == {MODEL: WINDOW}
        messages = _warnings(caplog)
        assert len(messages) == 1
        assert "'bad'" in messages[0]

    @pytest.mark.parametrize("key", ["", "   ", " padded", "padded ", 7, None, "x" * 257])
    def test_a_key_that_is_not_a_model_id_drops_its_entry_and_says_so(
        self, key: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_SECTIONS_LOGGER):
            assert _coerce({key: WINDOW, MODEL: WINDOW}) == {MODEL: WINDOW}
        messages = _warnings(caplog)
        assert len(messages) == 1
        assert len(messages[0]) < 300, "an oversized key is quoted truncated, never whole"

    def test_a_key_at_the_id_length_cap_is_kept(self) -> None:
        key = "k" * model_registry.ADVERTISED_MODEL_ID_MAX_CHARS
        assert _coerce({key: WINDOW}) == {key: WINDOW}

    def test_a_key_is_taken_literally_never_as_a_path(self) -> None:
        raw = {"../../etc/passwd": WINDOW, "acme.v2/org/model-7b": WINDOW, "a::b/c": WINDOW}
        assert _coerce(raw) == raw

    def test_only_the_first_entries_up_to_the_cap_are_read(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        raw = {f"m{i}": 2_048 for i in range(70)}
        with caplog.at_level(logging.WARNING, logger=_SECTIONS_LOGGER):
            out = _coerce(raw)
        assert list(out) == [f"m{i}" for i in range(64)]
        messages = _warnings(caplog)
        assert len(messages) == 1, "the rest is one warning, not one per entry"
        assert "6 entries" in messages[0] and "64" in messages[0]

    @pytest.mark.parametrize("raw", [[], "x", 5, [MODEL]])
    def test_a_value_that_is_not_a_mapping_is_empty_and_says_so(
        self, raw: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger=_SECTIONS_LOGGER):
            assert _coerce(raw) == {}
        assert len(_warnings(caplog)) == 1

    def test_an_absent_map_is_empty_and_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger=_SECTIONS_LOGGER):
            assert _coerce(None) == {}
        assert _warnings(caplog) == []


class TestAgentConfigField:
    def test_the_default_is_empty(self) -> None:
        assert AgentConfig().model_context_tokens == {}

    def test_direct_construction_is_coerced(self) -> None:
        cfg = AgentConfig(model_context_tokens={MODEL: WINDOW, "bad": True})
        assert cfg.model_context_tokens == {MODEL: WINDOW}

    def test_a_config_file_round_trips_and_drops_a_bad_entry(self, tmp_path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.json"
        declared = {MODEL: WINDOW, "acme.v2/org/model-7b": 8_192}
        cfg_file.write_text(
            json.dumps({"agent": {"model_context_tokens": {**declared, "bad": 12}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
        cfg = KiroCrewConfig.load()
        assert cfg.agent.model_context_tokens == declared
        assert cfg.to_dict()["agent"]["model_context_tokens"] == declared


class TestModelWindowTier:
    def test_a_declared_window_is_the_resolved_window(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.model_window(MODEL) == WINDOW

    def test_a_declared_window_names_the_operator_as_its_source(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.window_source(MODEL) == "operator"

    def test_a_declared_window_counts_as_known(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.has_known_window(MODEL)

    def test_a_live_window_still_wins(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.model_window(MODEL, live_tokens=40_000) == 40_000

    def test_an_undeclared_id_stays_unknown(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.model_window(SIBLING) is None
        assert model_registry.window_source(SIBLING) == "unknown"
        assert not model_registry.has_known_window(SIBLING)

    def test_a_declared_window_outranks_the_kiro_list_cache(self, monkeypatch) -> None:
        monkeypatch.setitem(model_registry._KIRO_WINDOWS, MODEL, 262_144)
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.model_window(MODEL) == WINDOW

    def test_a_declared_window_outranks_the_static_registry_and_gives_it_back(self) -> None:
        registry_id = "opus-4.8"
        listed = model_registry.model_window(registry_id)
        assert listed is not None and listed != WINDOW
        model_registry.apply_operator_windows({registry_id: WINDOW})
        assert model_registry.model_window(registry_id) == WINDOW
        model_registry.apply_operator_windows({})
        assert model_registry.model_window(registry_id) == listed
        assert model_registry.window_source(registry_id) == "registry"

    def test_two_ids_for_the_same_weights_stay_distinct(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW, SIBLING: 131_072})
        assert model_registry.model_window(MODEL) == WINDOW
        assert model_registry.model_window(SIBLING) == 131_072

    @pytest.mark.parametrize(
        "variant", [MODEL + "-instruct", MODEL.upper(), " " + MODEL, "model-7b"]
    )
    def test_the_match_is_exact(self, variant: str) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        assert model_registry.model_window(variant) is None

    def test_applying_replaces_the_declared_windows(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW})
        model_registry.apply_operator_windows({SIBLING: 65_536})
        assert model_registry.model_window(MODEL) is None
        assert model_registry.model_window(SIBLING) == 65_536

    def test_an_empty_map_makes_every_declared_id_unknown_again(self) -> None:
        model_registry.apply_operator_windows({MODEL: WINDOW, SIBLING: 65_536})
        model_registry.apply_operator_windows({})
        assert model_registry.model_window(MODEL) is None
        assert model_registry.model_window(SIBLING) is None

    def test_the_caller_cannot_change_the_declared_windows_afterwards(self) -> None:
        declared = {MODEL: WINDOW}
        model_registry.apply_operator_windows(declared)
        declared[MODEL] = 1_024
        declared[SIBLING] = 1_024
        assert model_registry.model_window(MODEL) == WINDOW
        assert model_registry.model_window(SIBLING) is None


class _FreshClient:
    """A provider client whose session has completed no turn, so no live window."""

    def __init__(self, model: str) -> None:
        self.client = SimpleNamespace(_model=model)

    def context_window_tokens(self) -> int:
        return 0


def _config(tmp_path, monkeypatch, windows: dict[str, object]) -> KiroCrewConfig:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"agent": {"model": MODEL, "model_context_tokens": windows}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: cfg_file)
    return KiroCrewConfig.load()


def _boot(initial: KiroCrewConfig | None) -> None:
    """Arm the dashboard's config watch the way both server modes do at boot."""
    from kiro_crew.dashboard.server import _register_config_watch

    state = SimpleNamespace(
        workflow_service=None, channel_manager=None, set_dynamic_cards_enabled=MagicMock()
    )
    _register_config_watch(web.Application(), state, initial)


def _reload(old: KiroCrewConfig, new: KiroCrewConfig) -> None:
    """Hand the registered applier the change a config watcher tick would report."""
    applier = {s.name: s for s in live.watch().subscriptions()}[_APPLIER].callback()
    assert applier is not None
    applier(ConfigChange(old=old, new=new, changed=frozenset({"agent.model_context_tokens"})))


def _with_windows(windows: dict[str, int]) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.model_context_tokens = windows
    return cfg


class TestFirstPromptBudget:
    def test_a_fresh_session_is_budgeted_against_the_declared_window(
        self, tmp_path, monkeypatch
    ) -> None:
        _boot(_config(tmp_path, monkeypatch, {MODEL: WINDOW}))
        window = ctx.window_for_provider_client(_FreshClient(MODEL))
        assert window == WINDOW
        declared, reference = ctx._resolve_caps(window), ctx._resolve_caps(None)
        assert declared.history_fallback < reference.history_fallback
        assert declared.per_message < reference.per_message
        assert declared.compressed_history < reference.compressed_history
        assert declared.protected_context < reference.protected_context

    def test_a_model_that_was_not_declared_keeps_the_reference_budget(
        self, tmp_path, monkeypatch
    ) -> None:
        _boot(_config(tmp_path, monkeypatch, {SIBLING: WINDOW}))
        window = ctx.window_for_provider_client(_FreshClient(MODEL))
        assert window is None
        assert ctx._resolve_caps(window) == ctx._resolve_caps(None)

    def test_a_reload_replaces_the_declared_windows(self, tmp_path, monkeypatch) -> None:
        booted = _config(tmp_path, monkeypatch, {MODEL: WINDOW})
        _boot(booted)
        assert ctx.window_for_provider_client(_FreshClient(MODEL)) == WINDOW
        _reload(booted, _with_windows({SIBLING: 65_536}))
        assert ctx.window_for_provider_client(_FreshClient(MODEL)) is None
        assert ctx.window_for_provider_client(_FreshClient(SIBLING)) == 65_536

    def test_a_reload_to_an_empty_map_makes_the_model_unknown_again(
        self, tmp_path, monkeypatch
    ) -> None:
        booted = _config(tmp_path, monkeypatch, {MODEL: WINDOW})
        _boot(booted)
        assert ctx.window_for_provider_client(_FreshClient(MODEL)) == WINDOW
        _reload(booted, _with_windows({}))
        assert ctx.window_for_provider_client(_FreshClient(MODEL)) is None

    def test_a_degraded_agent_section_keeps_the_declared_windows(
        self, tmp_path, monkeypatch
    ) -> None:
        """A torn document holds the DEFAULT map; applying it would drop every declaration."""
        booted = _config(tmp_path, monkeypatch, {MODEL: WINDOW})
        _boot(booted)
        torn = replace(KiroCrewConfig(), _degraded_sections=frozenset({"agent"}))
        with pytest.raises(ConfigDeferred):
            _reload(booted, torn)
        assert ctx.window_for_provider_client(_FreshClient(MODEL)) == WINDOW

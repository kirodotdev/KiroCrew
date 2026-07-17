"""Tests for the claude_code model list assembled by /api/models.

The dropdown leads with the KiroCrew-curated set (Opus 4.8 1M/200k, Opus 4.7,
Sonnet 4.6, Haiku 4.5) so users always see clean, current defaults ahead of
whatever the adapter advertises (its set can be stale — Opus 4.1, Sonnet 4.5);
adapter extras are appended de-duped, plus the configured default.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew import model_registry
from kiro_crew.dashboard.handlers.agents import (
    _advertised_cc_models,
    _cc_models,
)

# Canonical registry rows now lead the dropdown (replaces _CC_CURATED_MODELS).
_REGISTRY_NAMES = [r["model_name"] for r in model_registry.display_list("claude_code")]


def _request_with_providers(providers: dict) -> MagicMock:
    """Fake aiohttp request whose sessions.active_providers() yields `providers`.

    Mirrors the real SessionManager API (active_providers()) so the test can't
    pass against an attribute the production object doesn't have.
    """
    sessions = SimpleNamespace(active_providers=lambda: list(providers.values()))
    state = SimpleNamespace(sessions=sessions)
    req = MagicMock()
    req.app.__getitem__.return_value = state
    return req


class _FakeProvider:
    def __init__(self, models):
        self._models = models

    def available_models(self):
        return self._models


class TestAdvertisedCcModels:
    def test_maps_modelid_name_description(self):
        # An unknown provider id (not in the registry) passes through unchanged.
        prov = _FakeProvider(
            [
                {"modelId": "claude-sonnet-4-6", "name": "Sonnet 4.6", "description": "Everyday"},
            ]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}))
        assert out == [
            {
                "model_name": "claude-sonnet-4-6",
                "display_name": "Sonnet 4.6",
                "description": "Everyday",
            }
        ]

    def test_known_provider_id_mapped_to_canonical_key(self):
        # A backend provider id that IS in the registry maps back to its
        # canonical key so it dedups against the registry rows.
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "Opus 4.8",
                    "description": "",
                },
            ]
        )
        out = _advertised_cc_models(_request_with_providers({"s": prov}))
        assert out[0]["model_name"] == "opus-4.8-1m"

    def test_empty_when_no_active_sessions(self):
        assert _advertised_cc_models(_request_with_providers({})) == []

    def test_skips_provider_without_accessor(self):
        out = _advertised_cc_models(_request_with_providers({"s": object()}))
        assert out == []


class TestCcModelsMerge:
    def test_registry_set_always_present_even_without_session(self):
        # No live provider → the full canonical registry set leads the list,
        # using canonical keys (the wire values) ordered default-first.
        out = _cc_models(_request_with_providers({}))
        names = [m["model_name"] for m in out]
        assert "opus-4.8-1m" in names
        assert "opus-4.8" in names
        assert names[: len(_REGISTRY_NAMES)] == _REGISTRY_NAMES
        assert names[0] == "opus-4.8-1m"

    def test_registry_leads_then_adapter_extras_appended(self):
        # Adapter advertises a stale set (Opus 4.1, Sonnet 4.5) the registry does
        # not list. Registry leads; unknown adapter extras pass through and are
        # appended after, never displacing the canonical defaults.
        prov = _FakeProvider(
            [
                {"modelId": "claude-opus-4-1", "name": "Opus 4.1", "description": ""},
                {"modelId": "claude-sonnet-4-5", "name": "Sonnet 4.5", "description": ""},
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names[: len(_REGISTRY_NAMES)] == _REGISTRY_NAMES
        assert "claude-opus-4-1" in names
        assert "claude-sonnet-4-5" in names
        assert names.index("claude-opus-4-1") >= len(_REGISTRY_NAMES)

    def test_no_duplicate_when_adapter_lists_known_model(self):
        # The adapter advertises provider ids that ARE in the registry; mapped
        # back to canonical keys they collapse to one row each (registry wins).
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-sonnet-4-6[1m]",
                    "name": "Sonnet 4.6",
                    "description": "",
                },
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "Opus 4.8",
                    "description": "",
                },
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        names = [m["model_name"] for m in out]
        assert names.count("opus-4.8-1m") == 1
        assert names.count("sonnet-4.6-1m") == 1

    def test_registry_row_keeps_friendly_display_name(self):
        # When the adapter advertises a known id, the registry row (friendly
        # display name) wins over the backend's terser name.
        prov = _FakeProvider(
            [
                {
                    "modelId": "global.anthropic.claude-opus-4-8[1m]",
                    "name": "Opus 4.8",
                    "description": "",
                },
            ]
        )
        out = _cc_models(_request_with_providers({"s": prov}))
        opus48 = next(m for m in out if m["model_name"] == "opus-4.8-1m")
        assert opus48["display_name"] == "Opus 4.8 (1M context)"

    def test_configured_default_force_included(self):
        out = _cc_models(_request_with_providers({}), configured_default="custom-model-xyz")
        names = [m["model_name"] for m in out]
        assert "custom-model-xyz" in names

    def test_configured_default_not_duplicated_if_already_present(self):
        out = _cc_models(
            _request_with_providers({}),
            configured_default="opus-4.8-1m",
        )
        names = [m["model_name"] for m in out]
        assert names.count("opus-4.8-1m") == 1

    def test_configured_default_auto_does_not_insert_blank_row(self):
        # cc_model="auto" round-trips to "" (auto's provider id is empty), which
        # must NOT be inserted as a blank-named row at the top of the dropdown —
        # the "auto" registry row already covers it.
        out = _cc_models(_request_with_providers({}), configured_default="auto")
        names = [m["model_name"] for m in out]
        assert "" not in names
        assert all(m["model_name"] for m in out)
        # the canonical "auto" row is still present, exactly once.
        assert names.count("auto") == 1

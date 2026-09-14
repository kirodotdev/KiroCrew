"""OpenCode's picker reads its own advertised namespace, never Kiro's catalog."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _request(*providers):
    return SimpleNamespace(
        app={
            "state": SimpleNamespace(
                sessions=SimpleNamespace(active_providers=lambda: list(providers))
            )
        }
    )


def _provider(backend, *model_ids):
    return SimpleNamespace(
        capabilities=capabilities_for(backend),
        available_models=lambda: [
            {"modelId": name, "name": name, "description": ""} for name in model_ids
        ],
    )


def _cache(backend, *model_ids):
    model_registry.refresh_advertised_models(model_registry_namespace(backend), model_ids)


def _names(rows):
    return [row["model_name"] for row in rows]


@pytest.fixture(params=[ACP_BACKEND_CODEX, ACP_BACKEND_OPENCODE])
def advertised_picker(request):
    backend = request.param
    picker = agents._codex_models if backend == ACP_BACKEND_CODEX else agents._opencode_models
    return backend, picker


class TestSharedAdvertisedModelPicker:
    def test_namespace_isolation_applies_to_live_and_cached_choices(self, advertised_picker):
        backend, picker = advertised_picker
        foreign_backend = (
            ACP_BACKEND_OPENCODE if backend == ACP_BACKEND_CODEX else ACP_BACKEND_CODEX
        )
        _cache(backend, "own/cached")
        _cache(foreign_backend, "foreign/cached")
        foreign = _provider(foreign_backend, "foreign/live")

        assert _names(picker(_request(foreign))) == ["auto", "own/cached"]
        assert _names(picker(_request(_provider(backend, "own/live"), foreign))) == [
            "auto",
            "own/live",
        ]

    def test_empty_newest_session_keeps_the_prior_live_catalog(self, advertised_picker):
        backend, picker = advertised_picker
        _cache(backend, "own/cached")

        rows = picker(_request(_provider(backend, "own/live"), _provider(backend)))

        assert _names(rows) == ["auto", "own/live"]

    def test_sentinel_aliases_fold_but_provider_ids_remain_distinct(self, advertised_picker):
        backend, picker = advertised_picker
        provider = _provider(
            backend,
            "Auto",
            "default",
            "provider/model.v1",
            "provider/model-v1",
            "provider/Model.v1",
            "provider/model.v1",
            "provider/default",
        )

        rows = picker(_request(provider), configured_default="provider/model.v1")

        assert _names(rows) == [
            "auto",
            "provider/model.v1",
            "provider/model-v1",
            "provider/Model.v1",
            "provider/default",
        ]
        assert rows[0]["display_name"] == "Auto"
        assert rows[0]["description"] == "Backend default"

    @pytest.mark.parametrize("default", ["default", " Auto "])
    def test_unknown_catalog_does_not_resurrect_a_sentinel_alias(self, advertised_picker, default):
        _backend, picker = advertised_picker

        assert _names(picker(_request(), configured_default=default)) == ["auto"]

    def test_configured_pin_retains_default_metadata(self, advertised_picker, monkeypatch):
        _backend, picker = advertised_picker
        monkeypatch.setattr(
            model_registry, "model_window", lambda name: 32768 if name == "own/pin" else None
        )

        rows = picker(_request(), configured_default="own/pin")

        assert rows == [
            {
                "model_name": "auto",
                "display_name": "Auto",
                "description": "Backend default",
                "context_window": model_registry.REFERENCE_WINDOW_TOKENS,
            },
            {
                "model_name": "own/pin",
                "display_name": "own/pin",
                "description": "Configured default",
                "context_window": 32768,
            },
        ]


class TestOpenCodeModelPicker:
    def test_live_catalog_keeps_metadata_and_provider_qualified_ids(self):
        provider = _provider(ACP_BACKEND_OPENCODE)
        provider.available_models = lambda: [
            {"modelId": "local/model:8b", "name": "Local model", "description": "Local runtime"},
            {"modelId": "provider/model.v1"},
            {"modelId": "provider/model-v1"},
        ]

        rows = agents._opencode_models(_request(provider))

        assert _names(rows) == ["auto", "local/model:8b", "provider/model.v1", "provider/model-v1"]
        assert rows[1]["display_name"] == "Local model"
        assert rows[1]["description"] == "Local runtime"
        assert all(isinstance(row["context_window"], int) for row in rows)

    def test_newest_live_session_wins_over_older_session_and_cache(self):
        _cache(ACP_BACKEND_OPENCODE, "provider/cached")
        older = _provider(ACP_BACKEND_OPENCODE, "provider/old")
        newer = _provider(ACP_BACKEND_OPENCODE, "provider/current")

        assert _names(agents._opencode_models(_request(older, newer))) == [
            "auto",
            "provider/current",
        ]

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
    def test_other_harness_sessions_and_caches_cannot_supply_models(self, backend):
        _cache(backend, "foreign/model")
        provider = _provider(backend, "foreign/live")

        assert _names(agents._opencode_models(_request(provider))) == ["auto"]

    def test_cold_dashboard_reads_only_the_opencode_cache(self):
        _cache(ACP_BACKEND_OPENCODE, "provider/cached")

        assert _names(agents._opencode_models(_request())) == ["auto", "provider/cached"]

    def test_empty_live_advertisement_falls_back_to_cache(self):
        _cache(ACP_BACKEND_OPENCODE, "provider/cached")

        assert _names(agents._opencode_models(_request(_provider(ACP_BACKEND_OPENCODE)))) == [
            "auto",
            "provider/cached",
        ]

    @pytest.mark.parametrize("default", ["", "auto", "provider/custom"])
    def test_unknown_catalog_retains_only_auto_and_explicit_pin(self, default):
        expected = ["auto", "provider/custom"] if default == "provider/custom" else ["auto"]

        assert _names(agents._opencode_models(_request(), configured_default=default)) == expected

    @pytest.mark.parametrize("live", [False, True])
    def test_known_catalog_never_resurrects_an_unadvertised_pin(self, live):
        providers = [_provider(ACP_BACKEND_OPENCODE, "provider/current")] if live else []
        if not live:
            _cache(ACP_BACKEND_OPENCODE, "provider/current")

        rows = agents._opencode_models(_request(*providers), configured_default="provider/stale")

        assert _names(rows) == ["auto", "provider/current"]

    def test_duplicate_ids_and_auto_are_offered_once(self):
        provider = _provider(ACP_BACKEND_OPENCODE, "auto", "provider/model", "provider/model")

        rows = agents._opencode_models(_request(provider), configured_default="provider/model")

        assert _names(rows) == ["auto", "provider/model"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [False, True])
    async def test_api_never_checks_kiro_readiness_or_spawns_a_catalog(self, monkeypatch, live):
        monkeypatch.setattr(
            agents.KiroCrewConfig,
            "load",
            staticmethod(
                lambda: SimpleNamespace(
                    agent=SimpleNamespace(acp_backend=ACP_BACKEND_OPENCODE, model="")
                )
            ),
        )
        readiness = AsyncMock(side_effect=AssertionError("OpenCode must not check Kiro readiness"))
        spawn = AsyncMock(side_effect=AssertionError("model picker must not spawn a subprocess"))
        wrap = MagicMock(side_effect=AssertionError("model picker must not prepare a subprocess"))
        monkeypatch.setattr(agents, "reject_if_kiro_unverified", readiness)
        monkeypatch.setattr(agents, "create_subprocess_limited", spawn)
        monkeypatch.setattr(agents, "_wrap_list_models_argv", wrap)
        providers = [_provider(ACP_BACKEND_OPENCODE, "provider/live")] if live else []

        response = await agents.api_models(_request(*providers))

        assert response.status == 200
        expected = ["auto", "provider/live"] if live else ["auto"]
        assert _names(json.loads(response.body)) == expected
        readiness.assert_not_awaited()
        spawn.assert_not_awaited()
        wrap.assert_not_called()

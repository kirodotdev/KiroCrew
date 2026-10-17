"""Advertised-selection dashboard pickers stay within their backend namespace."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew import model_registry
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.handlers import agents


@pytest.fixture(autouse=True)
def _cold_advertised_cache(monkeypatch):
    monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
    monkeypatch.setattr(model_registry, "persist_advertised_models", lambda: None)


def _request(*providers):
    state = SimpleNamespace(sessions=SimpleNamespace(active_providers=lambda: list(providers)))
    request = MagicMock()
    request.app = {"state": state}
    return request


def _provider(backend, models):
    provider = MagicMock()
    provider.capabilities = capabilities_for(backend)
    provider.available_models = MagicMock(return_value=models)
    return provider


def _names(rows):
    return [row["model_name"] for row in rows]


def test_pi_models_use_live_pi_advertisement_and_preserve_ids():
    provider = _provider(
        ACP_BACKEND_PI,
        [
            {"modelId": "vendor/model-one", "name": "Model One", "description": "First"},
            {"modelId": "other/model-two", "name": "Model Two", "description": "Second"},
        ],
    )

    rows = agents._advertised_backend_models(_request(provider), ACP_BACKEND_PI)

    assert _names(rows) == ["auto", "vendor/model-one", "other/model-two"]
    assert rows[1]["display_name"] == "Model One"
    assert rows[1]["description"] == "First"
    assert all(row["context_window"] > 0 for row in rows)


def test_pi_models_use_cached_advertisement_after_restart():
    namespace = model_registry_namespace(ACP_BACKEND_PI)
    model_registry.refresh_advertised_models(namespace, ["vendor/model-one", "vendor/model-two"])

    assert _names(agents._advertised_backend_models(_request(), ACP_BACKEND_PI)) == [
        "auto",
        "vendor/model-one",
        "vendor/model-two",
    ]


def test_pi_models_do_not_read_other_backend_cache_or_sessions():
    model_registry.refresh_advertised_models("acp", ["wrong/model"])

    wrong_backend = _provider(ACP_BACKEND_KIRO, [{"modelId": "wrong/model"}])
    assert _names(agents._advertised_backend_models(_request(wrong_backend), ACP_BACKEND_PI)) == [
        "auto"
    ]


@pytest.mark.parametrize(
    "backend", sorted(ACP_BACKENDS_ADVERTISED_MODEL_SELECTION - {ACP_BACKEND_CLAUDE})
)
@pytest.mark.parametrize("source", ["live", "cache", "cold"])
@pytest.mark.asyncio
async def test_api_models_routes_advertised_backends_without_spawning_kiro(
    monkeypatch, backend, source
):
    monkeypatch.setattr(
        agents.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(agent=SimpleNamespace(acp_backend=backend, model=""))),
    )

    async def _never_spawn(*_args, **_kwargs):
        raise AssertionError("Advertised model discovery must never launch kiro-cli")

    monkeypatch.setattr(agents, "reject_if_kiro_unverified", _never_spawn)
    namespace = model_registry_namespace(backend)
    providers = []
    for other_backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION | {ACP_BACKEND_KIRO}:
        if other_backend != backend:
            model_registry.refresh_advertised_models(
                model_registry_namespace(other_backend), ["foreign/model"]
            )
            providers.append(_provider(other_backend, [{"modelId": "foreign/model"}]))
    if source == "live":
        model_registry.refresh_advertised_models(namespace, ["stale/model"])
        providers.insert(0, _provider(backend, [{"modelId": "vendor/model-one"}]))
    elif source == "cache":
        model_registry.refresh_advertised_models(namespace, ["vendor/model-one"])

    response = await agents.api_models(_request(*providers))

    assert response.status == 200
    expected = ["auto"] if source == "cold" else ["auto", "vendor/model-one"]
    assert _names(json.loads(response.body)) == expected

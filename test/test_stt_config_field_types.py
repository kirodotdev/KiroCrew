"""Field-type contract for the STT config PUT handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core
from kiro_crew.config.loader import config_path


def _put(body: dict) -> MagicMock:
    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.json = AsyncMock(return_value=body)
    return request


@pytest.fixture(autouse=True)
def _stub_host_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the response-building tail deterministic and off the host."""
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda _provider: [])
    monkeypatch.setattr(core, "ensure_ffmpeg_in_path", lambda: None)
    monkeypatch.setattr(core, "_find_ffmpeg", lambda: None)
    monkeypatch.setattr(core, "_transcribe_extra_importable", lambda: True)
    monkeypatch.setattr(core, "_pip_install_channel_available", lambda: True)
    monkeypatch.setattr(core.platform_compat, "is_bundled_interpreter", lambda: False)
    monkeypatch.setattr(core, "is_available", lambda _stt: False)


def _stored_stt() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("stt", {})


async def _seed() -> dict:
    response = await core.api_stt_config(
        _put(
            {
                "model": next(iter(core._STT_MODEL_SIZES)),
                "provider": core._stt_providers()[0],
                "language_code": "fr-FR",
            }
        )
    )
    assert response.status == 200
    return _stored_stt()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [{"size": "small"}, ["small"], [{"size": "small"}]])
async def test_unhashable_model_is_ignored_not_a_500(model: object) -> None:
    before = await _seed()

    response = await core.api_stt_config(_put({"model": model}))

    assert response.status == 200
    assert _stored_stt() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", 3),
        ("model", True),
        ("model", None),
        ("provider", {"name": "local"}),
        ("provider", ["local"]),
        ("provider", 7),
        ("provider", None),
    ],
)
async def test_non_string_model_and_provider_are_ignored(field: str, value: object) -> None:
    before = await _seed()

    response = await core.api_stt_config(_put({field: value}))

    assert response.status == 200
    assert _stored_stt() == before


@pytest.mark.asyncio
async def test_wrong_typed_field_does_not_discard_valid_siblings() -> None:
    await _seed()

    response = await core.api_stt_config(
        _put({"model": {"size": "small"}, "language_code": "de-DE"})
    )

    assert response.status == 200
    assert _stored_stt()["language_code"] == "de-DE"


@pytest.mark.asyncio
async def test_valid_model_and_provider_still_persist() -> None:
    model = next(iter(core._STT_MODEL_SIZES))
    provider = core._stt_providers()[0]

    response = await core.api_stt_config(_put({"model": model, "provider": provider}))

    assert response.status == 200
    assert _stored_stt()["model"] == model
    assert _stored_stt()["provider"] == provider


@pytest.mark.asyncio
async def test_unknown_string_values_are_ignored() -> None:
    before = await _seed()

    response = await core.api_stt_config(
        _put({"model": "not-a-model", "provider": "not-a-provider"})
    )

    assert response.status == 200
    assert _stored_stt() == before

"""Dashboard model picker visibility configuration."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig


@pytest.fixture()
def cfg_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


@pytest.fixture()
def handler_app(cfg_file):
    from kiro_crew.dashboard.handlers.files import api_dashboard_config

    audit = MagicMock()
    with patch("kiro_crew.dashboard.handlers.files._sel", return_value=audit):
        app = web.Application()
        app.router.add_get("/api/dashboard/config", api_dashboard_config)
        app.router.add_put("/api/dashboard/config", api_dashboard_config)
        yield as_owner(app)


def test_default_is_empty():
    assert KiroCrewConfig().dashboard.model_picker_hidden_models == []
    assert KiroCrewConfig().dashboard.model_picker_configured is False


def test_loader_trims_deduplicates_and_ignores_auto(cfg_file):
    cfg_file.write_text(
        json.dumps(
            {
                "dashboard": {
                    "model_picker_hidden_models": [
                        " model-a ",
                        "model-a",
                        "",
                        "auto",
                        7,
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert KiroCrewConfig.load().dashboard.model_picker_hidden_models == ["model-a"]


def test_loader_rejects_non_array_shape_to_default(cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"model_picker_hidden_models": "model-a"}}),
        encoding="utf-8",
    )
    assert KiroCrewConfig.load().dashboard.model_picker_hidden_models == []


@pytest.mark.asyncio
async def test_dashboard_put_applies_normalized_delta_ids(handler_app):
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.put(
            "/api/dashboard/config",
            json={
                "model_picker_hidden_models_add": [
                    " model-a ",
                    "model-a",
                    "auto",
                    "",
                ]
            },
        )
        assert response.status == 200
        get_response = await client.get("/api/dashboard/config")
        assert get_response.status == 200
        body = await get_response.json()
        assert body["model_picker_hidden_models"] == ["model-a"]
        assert body["model_picker_configured"] is True


@pytest.mark.asyncio
async def test_dashboard_put_applies_model_visibility_delta_to_current_config(handler_app):
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.put(
            "/api/dashboard/config",
            json={"model_picker_hidden_models_add": ["model-a"]},
        )
        assert response.status == 200

        response = await client.put(
            "/api/dashboard/config",
            json={"model_picker_hidden_models_add": ["model-b"]},
        )
        assert response.status == 200

        response = await client.put(
            "/api/dashboard/config",
            json={"model_picker_hidden_models_remove": ["model-a"]},
        )
        assert response.status == 200

        get_response = await client.get("/api/dashboard/config")
        assert get_response.status == 200
        body = await get_response.json()
        assert body["model_picker_hidden_models"] == ["model-b"]
        assert body["model_picker_configured"] is True


@pytest.mark.asyncio
async def test_dashboard_put_ignores_read_only_hidden_model_projection(handler_app):
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.put(
            "/api/dashboard/config", json={"model_picker_hidden_models": ["model-a"]}
        )
        assert response.status == 200
        get_response = await client.get("/api/dashboard/config")
        body = await get_response.json()
        assert body["model_picker_hidden_models"] == []
        assert body["model_picker_configured"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["model_picker_hidden_models_add", "model_picker_hidden_models_remove"],
)
async def test_dashboard_put_rejects_invalid_delta_values(handler_app, field):
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.put("/api/dashboard/config", json={field: ["bad model"]})
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_model_picker_hidden_models"


@pytest.mark.asyncio
async def test_dashboard_put_rejects_oversized_list(handler_app):
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.put(
            "/api/dashboard/config",
            json={"model_picker_hidden_models_add": [f"model-{i}" for i in range(129)]},
        )
        assert response.status == 400


@pytest.mark.asyncio
async def test_configured_fact_requires_successful_model_save(handler_app, cfg_file):
    async with TestClient(TestServer(handler_app)) as client:

        async def configured():
            response = await client.get("/api/dashboard/config")
            assert response.status == 200
            return (await response.json())["model_picker_configured"]

        assert await configured() is False
        assert (
            not json.loads(cfg_file.read_text(encoding="utf-8"))
            .get("dashboard", {})
            .get("model_picker_configured", False)
        )

        # An unrelated save or a forged read-only projection cannot dismiss it.
        response = await client.put(
            "/api/dashboard/config",
            json={"quick_send": True, "model_picker_configured": True},
        )
        assert response.status == 200
        assert await configured() is False

        response = await client.put(
            "/api/dashboard/config", json={"model_picker_hidden_models_add": [1]}
        )
        assert response.status == 400
        assert await configured() is False

        with patch(
            "kiro_crew.config.loader.update_config_locked",
            side_effect=OSError("test write failure"),
        ):
            response = await client.put(
                "/api/dashboard/config", json={"model_picker_hidden_models_add": []}
            )
            assert response.status == 500
        assert await configured() is False

        # Choosing to show every model still completes configuration.
        response = await client.put(
            "/api/dashboard/config", json={"model_picker_hidden_models_add": []}
        )
        assert response.status == 200
        assert await configured() is True
        assert KiroCrewConfig.load().dashboard.model_picker_configured is True

        response = await client.put(
            "/api/dashboard/config",
            json={"model_picker_hidden_models_add": ["model-a"]},
        )
        assert response.status == 200
        response = await client.put(
            "/api/dashboard/config",
            json={
                "model_picker_hidden_models": [],
                "model_picker_configured": False,
            },
        )
        assert response.status == 200
        assert await configured() is True
        raw = json.loads(cfg_file.read_text(encoding="utf-8"))["dashboard"]
        assert raw["model_picker_hidden_models"] == ["model-a"]
        assert raw["model_picker_configured"] is True
        assert raw["quick_send"] is True


def test_full_config_serialization_does_not_complete_setup():
    from dataclasses import asdict

    cfg = KiroCrewConfig()
    assert asdict(cfg.dashboard)["model_picker_configured"] is False
    assert cfg.dashboard.model_picker_hidden_models == []
    assert cfg.dashboard.model_picker_configured is False


@pytest.mark.parametrize(
    "hidden,expected",
    [
        ([" model-a ", "auto"], True),
        ([], False),
        (["", " auto ", 7], False),
        ("model-a", False),
    ],
)
def test_existing_hidden_models_migrate_configured_fact(cfg_file, hidden, expected):
    cfg_file.write_text(
        json.dumps({"dashboard": {"model_picker_hidden_models": hidden}}),
        encoding="utf-8",
    )
    assert KiroCrewConfig.load().dashboard.model_picker_configured is expected


@pytest.mark.asyncio
async def test_migrated_customizer_stays_configured_after_restore(handler_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"model_picker_hidden_models": ["model-a"]}}),
        encoding="utf-8",
    )
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.get("/api/dashboard/config")
        assert (await response.json())["model_picker_configured"] is True
        response = await client.put(
            "/api/dashboard/config", json={"model_picker_hidden_models": []}
        )
        assert response.status == 200
        assert KiroCrewConfig.load().dashboard.model_picker_configured is True

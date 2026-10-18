"""Persistence and API contract for the session-card message preview preference."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.files import api_dashboard_config


@pytest.fixture()
def cfg_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=path):
        yield path


@pytest.fixture()
def handler_app(cfg_file):
    app = web.Application()
    app.router.add_put("/api/dashboard/config", api_dashboard_config)
    app.router.add_get("/api/dashboard/config", api_dashboard_config)
    with patch("kiro_crew.dashboard.handlers.sel"):
        yield as_owner(app)


def test_existing_install_keeps_previews(cfg_file):
    assert KiroCrewConfig().dashboard.session_card_message_preview is True
    assert KiroCrewConfig.load().dashboard.session_card_message_preview is True


def test_save_load_preserves_disabled_previews(cfg_file):
    cfg = KiroCrewConfig()
    cfg.dashboard.session_card_message_preview = False
    cfg.save()

    raw = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert raw["dashboard"]["session_card_message_preview"] is False
    assert KiroCrewConfig.load().dashboard.session_card_message_preview is False


@pytest.mark.parametrize("value", ["false", 0, None])
def test_invalid_file_value_keeps_previews_enabled(cfg_file, value):
    cfg_file.write_text(
        json.dumps({"dashboard": {"session_card_message_preview": value}}), encoding="utf-8"
    )
    assert KiroCrewConfig.load().dashboard.session_card_message_preview is True


def test_preference_is_available_in_settings_schema():
    from kiro_crew.config.schema import JSON_SCHEMA

    node = JSON_SCHEMA["properties"]["dashboard"]["properties"]["session_card_message_preview"]
    assert node["type"] == "boolean"
    assert node["default"] is True
    assert node["x-meta"]["label"]
    assert node["x-meta"]["help"]


@pytest.mark.asyncio
async def test_toggle_round_trip_preserves_other_dashboard_preferences(handler_app, cfg_file):
    cfg_file.write_text(
        json.dumps({"dashboard": {"session_card_source_links": False}}), encoding="utf-8"
    )
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.get("/api/dashboard/config")
        assert response.status == 200
        assert (await response.json())["session_card_message_preview"] is True

        for enabled in (False, True):
            response = await client.put(
                "/api/dashboard/config", json={"session_card_message_preview": enabled}
            )
            assert response.status == 200
            cfg = KiroCrewConfig.load()
            assert cfg.dashboard.session_card_message_preview is enabled
            assert cfg.dashboard.session_card_source_links is False
            response = await client.get("/api/dashboard/config")
            assert response.status == 200
            assert (await response.json())["session_card_message_preview"] is enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["false", 0, None])
async def test_put_rejects_non_boolean_without_writing(handler_app, cfg_file, value):
    # Finish load-time migrations before measuring whether a refused write changes config.
    cfg = KiroCrewConfig.load()
    cfg.dashboard.session_card_message_preview = False
    cfg.save()
    before = cfg_file.read_text(encoding="utf-8")
    async with TestClient(TestServer(handler_app)) as client:
        response = await client.put(
            "/api/dashboard/config", json={"session_card_message_preview": value}
        )
        assert response.status == 400
        assert (await response.json())["code"] == "invalid_session_card_message_preview"
    assert cfg_file.read_text(encoding="utf-8") == before

"""Goal suggestions are a live preference independent of monitor runtime policy."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from jsonschema import Draft7Validator

from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.schema import JSON_SCHEMA, requires_restart
from kiro_crew.goal import goal_suggestions_enabled


@pytest.fixture
def cfg_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.loader.config_path", lambda: path)
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_local_path", lambda: tmp_path / "config.local.json"
    )
    return path


@pytest.fixture
def config_app(cfg_file: Path) -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_config_schema,
        api_kirocrew_config,
        api_kirocrew_config_patch,
    )

    app = web.Application()
    app.router.add_get("/api/config/kirocrew", api_kirocrew_config)
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/schema", api_config_schema)
    return as_owner(app)


@pytest.mark.parametrize("document", [{}, {"monitoring": {"max_runtime_secs": 3600}}])
def test_missing_preference_defaults_to_suggestions_enabled(cfg_file: Path, document: dict) -> None:
    cfg_file.write_text(json.dumps(document), encoding="utf-8")

    assert KiroCrewConfig().monitoring.goal_suggestions is True
    loaded = KiroCrewConfig.load()
    assert loaded.monitoring.goal_suggestions is True
    assert loaded.to_dict()["monitoring"]["goal_suggestions"] is True


@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
def test_non_boolean_is_invalid_and_loads_the_default(cfg_file: Path, invalid: object) -> None:
    document = {
        "monitoring": {
            "goal_suggestions": invalid,
            "max_runtime_secs": 3600,
            "prefer_structured_arming": True,
        }
    }
    errors = list(Draft7Validator(JSON_SCHEMA).iter_errors(document))
    assert any(
        list(error.path) == ["monitoring", "goal_suggestions"] and error.validator == "type"
        for error in errors
    )

    cfg_file.write_text(json.dumps(document), encoding="utf-8")
    loaded = KiroCrewConfig.load()
    assert loaded.monitoring.goal_suggestions is True
    assert loaded.monitoring.max_runtime_secs == 3600
    assert loaded.monitoring.prefer_structured_arming is True


@pytest.mark.asyncio
async def test_schema_api_exposes_boolean_default_and_live_metadata(
    config_app: web.Application,
) -> None:
    async with TestClient(TestServer(config_app)) as client:
        response = await client.get("/api/config/schema")
        assert response.status == 200
        entries = (await response.json())["entries"]

    entry = next(entry for entry in entries if entry["path"] == "monitoring.goal_suggestions")
    assert entry["type"] == "boolean"
    assert entry["defaultValue"] is True
    assert entry["label"]
    assert entry["help"]
    assert entry["sensitive"] is False
    assert entry.get("requiresRestart", False) is False
    assert requires_restart("monitoring.goal_suggestions") is False


@pytest.mark.asyncio
async def test_saved_preference_reaches_live_reader_and_config_api(
    cfg_file: Path, config_app: web.Application
) -> None:
    cfg_file.write_text(
        json.dumps({"monitoring": {"max_runtime_secs": 3600, "prefer_structured_arming": True}}),
        encoding="utf-8",
    )
    boot = await asyncio.to_thread(KiroCrewConfig.load)
    watcher = live.watch()
    watcher.prime(boot)

    async with TestClient(TestServer(config_app)) as client:
        response = await client.get("/api/config/kirocrew")
        assert response.status == 200
        assert (await response.json())["monitoring"]["goal_suggestions"] is True

        for enabled in (False, True):
            loaded = await asyncio.to_thread(KiroCrewConfig.load)
            loaded.monitoring.goal_suggestions = enabled
            await asyncio.to_thread(loaded.save)
            saved = json.loads(await asyncio.to_thread(cfg_file.read_text, encoding="utf-8"))
            assert saved["monitoring"]["goal_suggestions"] is enabled

            change = await watcher.refresh_now()
            assert change is not None
            assert "monitoring.goal_suggestions" in change.changed
            current = live.current(boot, log_prefix="goal-suggestions-test")
            assert current.monitoring.goal_suggestions is enabled
            assert current.monitoring.max_runtime_secs == 3600
            assert current.monitoring.prefer_structured_arming is True
            assert boot.monitoring.goal_suggestions is True

            response = await client.get("/api/config/kirocrew")
            assert response.status == 200
            assert (await response.json())["monitoring"]["goal_suggestions"] is enabled


def test_live_reader_before_watcher_start_loads_saved_preference(cfg_file: Path) -> None:
    boot = KiroCrewConfig.load()
    loaded = KiroCrewConfig.load()
    loaded.monitoring.goal_suggestions = False
    loaded.save()

    assert live.snapshot() is None
    assert (
        live.current(boot, log_prefix="goal-suggestions-test").monitoring.goal_suggestions is False
    )


@pytest.mark.asyncio
async def test_owner_patch_round_trips_and_refreshes_goal_preference(
    cfg_file: Path, config_app: web.Application
) -> None:
    monitoring = {
        "goal_suggestions": True,
        "max_runtime_secs": 3600,
        "prefer_structured_arming": True,
    }
    cfg_file.write_text(
        json.dumps({"monitoring": monitoring, "auto_update": False}), encoding="utf-8"
    )
    boot = await asyncio.to_thread(KiroCrewConfig.load)
    watcher = live.watch()
    try:
        await watcher.start(initial=boot)
        async with TestClient(TestServer(config_app)) as client:
            for enabled in (False, True):
                response = await client.patch(
                    "/api/config/kirocrew",
                    json={"path": "monitoring.goal_suggestions", "value": enabled},
                )
                assert response.status == 200
                monitoring["goal_suggestions"] = enabled
                assert (await response.json())["monitoring"] == monitoring

                # The PATCH must refresh the real reader before replying.
                assert goal_suggestions_enabled() is enabled
                assert boot.monitoring.goal_suggestions is True
                saved = json.loads(await asyncio.to_thread(cfg_file.read_text, encoding="utf-8"))
                assert saved["monitoring"] == monitoring
                assert saved["auto_update"] is False

                response = await client.get("/api/config/kirocrew")
                assert response.status == 200
                assert (await response.json())["monitoring"] == monitoring
    finally:
        await watcher.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["false", "true", 0, 1, None, [], {}])
async def test_owner_patch_rejects_non_boolean_without_changing_config(
    cfg_file: Path, config_app: web.Application, invalid: object
) -> None:
    cfg_file.write_text(
        json.dumps(
            {
                "monitoring": {
                    "goal_suggestions": False,
                    "max_runtime_secs": 3600,
                    "prefer_structured_arming": True,
                },
                "auto_update": False,
            }
        ),
        encoding="utf-8",
    )
    loaded = await asyncio.to_thread(KiroCrewConfig.load)
    live.watch().prime(loaded)
    before = await asyncio.to_thread(cfg_file.read_bytes)

    async with TestClient(TestServer(config_app)) as client:
        response = await client.patch(
            "/api/config/kirocrew",
            json={"path": "monitoring.goal_suggestions", "value": invalid},
        )
        assert response.status == 400
        assert (await response.json())["error"] == "must be a boolean"

    assert await asyncio.to_thread(cfg_file.read_bytes) == before
    assert goal_suggestions_enabled() is False

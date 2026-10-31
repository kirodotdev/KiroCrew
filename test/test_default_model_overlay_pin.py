"""A Settings save of a key ``config.local.json`` sets is refused, not reported as saved.

``config.local.json`` deep-merges OVER ``config.json``, and the config PATCH route
writes the BASE file. Answering 200 for a key the overlay sets lets the Settings
picker show the pick until the next refetch puts the overlay's value back, so the
pick looks like it "reverts". The route answers 409 instead and writes nothing.
"""

from __future__ import annotations

import json
import os
import unittest.mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig, config_dir

OLD = "claude-sonnet-5"
NEW = "claude-sonnet-5.5"


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _patch_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = unittest.mock.MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


def _write_overlay(overlay: dict) -> None:
    local = config_dir() / "config.local.json"
    local.write_text(json.dumps(overlay), encoding="utf-8")
    os.chmod(local, 0o600)


def _base() -> dict:
    return json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))


@pytest.fixture
def _models(monkeypatch):
    (config_dir() / "config.json").write_text(
        json.dumps({"agent": {"model": OLD}}), encoding="utf-8"
    )
    from kiro_crew.dashboard.handlers import core

    monkeypatch.setattr(core, "_active_advertised_ids", lambda request, backend=None: [OLD, NEW])
    monkeypatch.setattr(
        core, "_revalidate_role_pin_evidence", unittest.mock.AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._model_rejected_reason",
        lambda m, provider=None: None,
    )


async def _patch(path: str, value: object) -> tuple[int, dict]:
    async with TestClient(TestServer(_patch_app())) as c:
        resp = await c.patch("/api/config/kirocrew", json={"path": path, "value": value})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_a_default_model_the_overlay_sets_is_refused_and_not_written(_models):
    _write_overlay({"agent": {"model": OLD}})
    status, body = await _patch("agent.model", NEW)
    assert status == 409
    assert body["code"] == "config_overlay_owned"
    assert body["overlay_override"] is True
    assert "config.local.json" in body["error"]
    assert _base()["agent"]["model"] == OLD
    assert KiroCrewConfig.load().agent.model == OLD


@pytest.mark.asyncio
async def test_a_nested_role_model_the_overlay_sets_is_refused(_models):
    _write_overlay({"agent": {"role_models": {"subagent": OLD}}})
    status, body = await _patch("agent.role_models.subagent", NEW)
    assert status == 409
    assert body["code"] == "config_overlay_owned"
    assert "role_models" not in _base()["agent"]


@pytest.mark.asyncio
async def test_an_overlay_on_another_key_of_the_section_does_not_block(_models):
    _write_overlay({"agent": {"fallback_model": OLD}})
    status, body = await _patch("agent.model", NEW)
    assert status == 200, body
    assert _base()["agent"]["model"] == NEW
    assert KiroCrewConfig.load().agent.model == NEW


@pytest.mark.asyncio
async def test_without_an_overlay_the_save_lands(_models):
    status, body = await _patch("agent.model", NEW)
    assert status == 200, body
    assert body["agent"]["model"] == NEW
    assert _base()["agent"]["model"] == NEW

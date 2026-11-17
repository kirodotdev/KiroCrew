"""The Settings row for ``agent.session_control`` writes through the config PATCH route."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.session_control_parts.caller_access import session_control_enabled


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


@pytest.mark.asyncio
async def test_an_owner_patch_switches_session_control_off_and_on(tmp_path) -> None:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agent": {}}), encoding="utf-8")
    app = web.Application(middlewares=[_owner_identity])
    app["state"] = SimpleNamespace(owner_id="")
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        async with TestClient(TestServer(app)) as client:
            for value in (False, True):
                resp = await client.patch(
                    "/api/config/kirocrew",
                    json={"path": "agent.session_control", "value": value},
                )
                assert resp.status == 200, await resp.text()
                stored = json.loads(cfg_path.read_text(encoding="utf-8"))
                assert stored["agent"]["session_control"] is value
                assert session_control_enabled() is value

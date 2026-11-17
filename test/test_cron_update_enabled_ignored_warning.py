"""``PATCH /api/crons/{id}`` names an ``enabled`` field it ignores.

Pausing lives on ``POST /api/crons/{id}/enable``; the update route never reads
``enabled``. A body that sets it beside another field answered a bare ok while
the job kept running, and a body with only ``enabled`` answered "no fields to
update". Both keep their status; the answer now says where pausing lives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import api_cron_update

_WARNING = (
    "'enabled' is not changed by this route; pause or resume the job with "
    "POST /api/crons/{id}/enable"
)


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _app(crons: object) -> web.Application:
    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(
        crons=crons,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_route("*", "/api/crons/{job_id}", api_cron_update)
    return app


@pytest.mark.asyncio
async def test_enabled_beside_another_field_is_answered_with_a_warning(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", every_secs=300)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.patch(
            f"/api/crons/{job.id}", json={"name": "renamed", "enabled": False}
        )
        body = json.loads(await resp.text())
    assert resp.status == 200, body
    assert body.get("warnings") == [_WARNING]
    stored = svc.get_job(job.id)
    assert stored.name == "renamed"
    assert stored.enabled is True


@pytest.mark.asyncio
async def test_enabled_alone_is_refused_with_the_route_to_use(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", every_secs=300)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.patch(f"/api/crons/{job.id}", json={"enabled": False})
        body = json.loads(await resp.text())
    assert resp.status == 400, body
    assert body["error"] == f"no fields to update; {_WARNING}"


@pytest.mark.asyncio
async def test_a_body_without_enabled_warns_nothing(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", every_secs=300)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.patch(f"/api/crons/{job.id}", json={"name": "renamed"})
        body = json.loads(await resp.text())
    assert resp.status == 200, body
    assert "warnings" not in body

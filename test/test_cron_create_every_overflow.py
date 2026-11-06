"""``POST /api/crons`` answers 400 ``invalid_every`` for an infinite ``every``.

JSON ``1e999`` parses as an infinite float, and ``int()`` of it raises
``OverflowError``, which is not a ``ValueError``. The route refused a string or
list ``every`` with its own 400, but an infinite one escaped as a 500.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers.cron import api_crons_create


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
    app.router.add_post("/api/crons", api_crons_create)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["1e999", "-1e999"])
async def test_an_infinite_every_is_a_400_not_a_500(tmp_path, raw: str) -> None:
    svc = CronService(base_dir=tmp_path)
    body = '{"name": "n", "message": "m", "every": ' + raw + "}"
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post(
            "/api/crons", data=body, headers={"Content-Type": "application/json"}
        )
        text = await resp.text()

    assert resp.status == 400, text
    assert json.loads(text)["code"] == "invalid_every"
    assert svc.list_jobs(include_disabled=True) == []


@pytest.mark.asyncio
async def test_a_whole_every_still_creates_the_job(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.post("/api/crons", json={"name": "n", "message": "m", "every": 600})
        payload = json.loads(await resp.text())

    assert resp.status == 200, payload
    (job,) = svc.list_jobs(include_disabled=True)
    assert job.schedule.every_secs == 600

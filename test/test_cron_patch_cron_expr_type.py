"""``PATCH /api/crons/{id}`` answers 400 for a non-string ``cron``.

``validate_cron_expr`` handed the value to croniter, which calls ``.lower()``
on it, so a JSON number, list or object raised ``AttributeError`` and the
request answered 500. A string expression is unchanged.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.cron_service.schedule import validate_cron_expr
from kiro_crew.dashboard.handlers.cron import api_cron_update


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _app(crons: CronService) -> web.Application:
    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(
        crons=crons,
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        has_slot=MagicMock(return_value=False),
        _folders=[],
    )
    app.router.add_patch("/api/crons/{job_id}", api_cron_update)
    return app


async def _patch(crons: CronService, job_id: str, body: dict[str, Any]) -> tuple[int, str]:
    async with TestClient(TestServer(_app(crons))) as client:
        resp = await client.patch(f"/api/crons/{job_id}", json=body)
        return resp.status, await resp.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5, ["* * * * *"], {"a": 1}])
async def test_a_non_string_cron_is_a_400_and_writes_nothing(tmp_path, value: object) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)

    status, text = await _patch(crons, job.id, {"cron": value})

    assert status == 400, text
    assert "Invalid cron expression" in json.loads(text)["error"]
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.schedule.kind == "every"
    assert saved.schedule.every_secs == 3600


@pytest.mark.asyncio
async def test_a_string_cron_still_saves(tmp_path) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)

    status, text = await _patch(crons, job.id, {"cron": "0 9 * * *"})

    assert status == 200, text
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.schedule.cron_expr == "0 9 * * *"


@pytest.mark.parametrize("value", [5, 1.5, ["* * * * *"], {"a": 1}, None])
def test_the_predicate_reads_a_non_string_as_invalid(value: object) -> None:
    assert validate_cron_expr(value) is False  # type: ignore[arg-type]

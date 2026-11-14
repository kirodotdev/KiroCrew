"""``PATCH /api/crons/{id}`` names an empty schedule it leaves unchanged.

The store reads an empty ``every`` or ``cron`` as "leave unchanged", so the route
answers ok for a schedule edit that never happened. The answer stays 200, as it
is today; its ``warnings`` list now says the schedule was not changed.
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
@pytest.mark.parametrize(
    "patch_body, warning",
    [
        ({"every": 0}, "'every' is empty or zero, so the schedule was left unchanged"),
        ({"every": None}, "'every' is empty or zero, so the schedule was left unchanged"),
        ({"cron": ""}, "'cron' is empty, so the schedule was left unchanged"),
    ],
)
async def test_an_empty_schedule_is_answered_with_a_warning(tmp_path, patch_body, warning) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", cron_expr="0 9 * * *")
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.patch(f"/api/crons/{job.id}", json=patch_body)
        body = json.loads(await resp.text())
        assert resp.status == 200, body
        assert body.get("warnings") == [warning]
    assert svc.get_job(job.id).schedule.cron_expr == "0 9 * * *"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch_body, kind",
    [({"cron": "0 10 * * *", "every": 0}, "cron"), ({"every": 3600, "cron": ""}, "every")],
)
async def test_an_empty_field_beside_a_real_schedule_warns_nothing(
    tmp_path, patch_body, kind
) -> None:
    """The store applies whichever schedule field is set, so the schedule did change."""
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", cron_expr="0 9 * * *")
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.patch(f"/api/crons/{job.id}", json=patch_body)
        body = json.loads(await resp.text())
    assert "warnings" not in body, body
    stored = svc.get_job(job.id).schedule
    assert stored.kind == kind
    assert (stored.cron_expr, stored.every_secs) != ("0 9 * * *", None)


@pytest.mark.asyncio
async def test_a_real_schedule_change_warns_nothing(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job("n", "m", cron_expr="0 9 * * *")
    async with TestClient(TestServer(_app(svc))) as client:
        resp = await client.patch(f"/api/crons/{job.id}", json={"cron": "0 10 * * *"})
        body = json.loads(await resp.text())
        assert resp.status == 200, body
        assert "warnings" not in body
    assert svc.get_job(job.id).schedule.cron_expr == "0 10 * * *"

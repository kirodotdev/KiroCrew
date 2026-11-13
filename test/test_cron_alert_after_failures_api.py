"""``alert_after_failures`` over the dashboard's cron REST surface.

The Schedule page edit form shows and changes a job's alert threshold, so the
list payload carries the stored value and the PATCH route validates it before
the store sees it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronJob
from kiro_crew.cron_service.model import _AUTO_PAUSE_MAX
from kiro_crew.dashboard.handlers.cron import api_cron_update, api_crons

pytestmark = pytest.mark.asyncio


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    # The dashboard owner's cookie session, the caller the Schedule page is.
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


def _job(**over: object) -> CronJob:
    fields: dict[str, object] = {"id": "j1", "name": "poller", "message": "Check it."}
    fields.update(over)
    return CronJob(**fields)  # type: ignore[arg-type]


async def _listed(job: CronJob) -> dict:
    state = MagicMock()
    state.has_slot.return_value = False
    state.crons.list_jobs_async = AsyncMock(return_value=[job])
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    request = MagicMock()
    request.app = {"state": state}
    resp = await api_crons(request)
    return json.loads(resp.body)["jobs"][0]


async def test_a_default_job_reports_one() -> None:
    assert (await _listed(_job()))["alert_after_failures"] == 1


async def test_a_custom_threshold_is_reported() -> None:
    assert (await _listed(_job(alert_after_failures=3)))["alert_after_failures"] == 3


async def test_the_value_reaches_the_store() -> None:
    update = AsyncMock(return_value=_job(alert_after_failures=3))
    app = _app(SimpleNamespace(update_job_async=update))
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/crons/j1", json={"alert_after_failures": 3})
        assert resp.status == 200
    assert update.await_args.kwargs["alert_after_failures"] == 3


async def test_an_unrelated_patch_does_not_send_the_threshold() -> None:
    update = AsyncMock(return_value=_job())
    app = _app(SimpleNamespace(update_job_async=update))
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch("/api/crons/j1", json={"name": "renamed"})
        assert resp.status == 200
    assert "alert_after_failures" not in update.await_args.kwargs


@pytest.mark.parametrize("bad", [0, -1, _AUTO_PAUSE_MAX + 1, "two", 2.5, True, None, [2]])
async def test_a_bad_value_is_a_400_and_nothing_is_written(bad: object) -> None:
    update = AsyncMock(return_value=_job())
    app = _app(SimpleNamespace(update_job_async=update))
    async with TestClient(TestServer(app)) as client:
        resp = await client.patch(
            "/api/crons/j1", json={"alert_after_failures": bad, "name": "renamed"}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_alert_after_failures"
    update.assert_not_awaited()

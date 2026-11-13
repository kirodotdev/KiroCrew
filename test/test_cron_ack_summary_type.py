"""``POST /api/crons/{id}/ack`` answers 400 for a ``summary`` it cannot store.

The store keeps ``summary[:500]``. A JSON null, number, boolean or object has no
slice, so the request raised and answered 500. A string and a list slice, so
both keep saving as they did.
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
from kiro_crew.dashboard.handlers.cron import api_cron_ack


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
        _notification_log=[],
    )
    app.router.add_post("/api/crons/{job_id}/ack", api_cron_ack)
    return app


async def _ack(crons: CronService, job_id: str, body: dict[str, Any]) -> tuple[int, str]:
    async with TestClient(TestServer(_app(crons))) as client:
        resp = await client.post(f"/api/crons/{job_id}/ack", json=body)
        return resp.status, await resp.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("summary", [None, 5, 1.5, True, {"a": 1}])
async def test_a_summary_with_no_slice_is_a_400_and_writes_nothing(
    tmp_path, summary: object
) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)

    status, text = await _ack(crons, job.id, {"summary": summary})

    assert status == 400, text
    assert json.loads(text)["code"] == "invalid_summary"
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.acked_items == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "stored"),
    [
        ({"summary": "seen it"}, "seen it"),
        ({}, "acknowledged"),
        ({"summary": ["a", "b"]}, ["a", "b"]),
    ],
)
async def test_a_string_list_or_absent_summary_still_saves(
    tmp_path, body: dict[str, Any], stored: object
) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)

    status, text = await _ack(crons, job.id, body)

    assert status == 200, text
    assert json.loads(text)["ok"] is True
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.acked_items == [stored]

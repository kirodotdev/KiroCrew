"""``PATCH /api/crons/{id}`` answers 400 for a non-string text field.

Covers ``agent`` / ``agent_id``, ``channel`` and ``timezone``.

The route normalized these fields with ``(value or "").strip()``, so a JSON
number, list or object raised ``AttributeError`` and the request answered 500.
``model`` on the same route already refused a non-string with its own 400.
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
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("agent", 5, "invalid_agent"),
        ("agent", ["a"], "invalid_agent"),
        ("agent_id", {"a": 1}, "invalid_agent"),
        ("timezone", 5, "invalid_timezone"),
        ("timezone", ["UTC"], "invalid_timezone"),
        ("channel", 5, "invalid_channel"),
        ("channel", ["C1"], "invalid_channel"),
    ],
)
async def test_a_non_string_field_is_a_400_and_writes_nothing(
    tmp_path, field: str, value: object, code: str
) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)

    status, text = await _patch(crons, job.id, {field: value})

    assert status == 400, text
    assert json.loads(text)["code"] == code
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.agent_id == ""
    assert saved.timezone == ""
    assert not saved.channel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "agent", "tz"),
    [
        ({"agent": "  helper  "}, "helper", ""),
        ({"agent_id": None}, "", ""),
        ({"timezone": " UTC "}, "", "UTC"),
        ({"timezone": None}, "", ""),
        ({"agent": 0}, "", ""),
        ({"agent": "helper", "agent_id": 5}, "helper", ""),
    ],
)
async def test_a_string_or_null_field_still_saves(
    tmp_path, body: dict[str, Any], agent: str, tz: str
) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)

    status, text = await _patch(crons, job.id, body)

    assert status == 200, text
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.agent_id == agent
    assert saved.timezone == tz

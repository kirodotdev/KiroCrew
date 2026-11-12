"""A stored ``acked_items`` that is not a list loads as no items.

``acked_item_ids`` beside it is already read that way. ``acked_items`` was read
as stored, so a ``null`` (an imported archive, a hand edit) or a number made
the next ack of that job raise and answer 500, and a string or object was
split into its characters or keys. A list keeps loading as it is.
"""

from __future__ import annotations

import json
from pathlib import Path
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


def _store_with(tmp_path: Path, acked: Any) -> tuple[CronService, str]:
    """A store whose one job carries *acked* as its saved ``acked_items``."""
    job_id = CronService(base_dir=tmp_path).add_job(name="n", message="m", every_secs=3600).id
    (path,) = [p for p in tmp_path.rglob("*.json") if job_id in p.read_text(encoding="utf-8")]
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data["jobs"] if isinstance(data, dict) else data
    for record in records:
        if record.get("id") == job_id:
            record["acked_items"] = acked
    path.write_text(json.dumps(data), encoding="utf-8")
    return CronService(base_dir=tmp_path), job_id


@pytest.mark.asyncio
@pytest.mark.parametrize("acked", [None, 5])
async def test_an_ack_on_a_null_or_number_acked_items_saves(tmp_path, acked: Any) -> None:
    crons, job_id = _store_with(tmp_path, acked)

    async with TestClient(TestServer(_app(crons))) as client:
        resp = await client.post(f"/api/crons/{job_id}/ack", json={"summary": "seen it"})
        text = await resp.text()

    assert resp.status == 200, text
    (saved,) = crons.list_jobs(include_disabled=True)
    assert saved.acked_items == ["seen it"]


@pytest.mark.parametrize("acked", [None, 5, {"a": 1}, "seen"])
def test_a_non_list_acked_items_loads_as_no_items(tmp_path, acked: Any) -> None:
    crons, _ = _store_with(tmp_path, acked)
    (job,) = crons.list_jobs(include_disabled=True)
    assert job.acked_items == []


def test_a_list_acked_items_loads_as_stored(tmp_path) -> None:
    crons, _ = _store_with(tmp_path, ["one", "two"])
    (job,) = crons.list_jobs(include_disabled=True)
    assert job.acked_items == ["one", "two"]

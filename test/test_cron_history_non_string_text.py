"""The cron history pages skip a row with a non-string text field and serve the rest.

``GET /api/crons/{id}/history``, ``GET /api/crons/{id}/history/{run_id}`` and
``GET /api/crons/history`` redact each run's ``summary``, ``error`` and
``trace`` with string scanners. A history line whose text field holds a number,
a list or an object (a hand edit, another writer) raised there, so one such row
turned the whole page into a 500. The readers skip that row, as they skip a line
that is not JSON, and serve the rest; the row stays on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard.handlers.cron import (
    api_cron_history,
    api_cron_history_all,
    api_cron_history_detail,
)


@web.middleware
async def _owner(request: web.Request, handler):  # type: ignore[no-untyped-def]
    request["user"] = "local-app"
    request["app"] = ""
    return await handler(request)


def _app(crons: CronService) -> web.Application:
    app = web.Application(middlewares=[_owner])
    app["state"] = SimpleNamespace(crons=crons, push_refresh=MagicMock())
    app.router.add_get("/api/crons/history", api_cron_history_all)
    app.router.add_get("/api/crons/{job_id}/history", api_cron_history)
    app.router.add_get("/api/crons/{job_id}/history/{run_id}", api_cron_history_detail)
    return app


async def _seed_with_bad_row(crons: CronService, job_id: str, row: dict[str, Any]) -> None:
    """One good run, then *row* written as a raw line to the job file and the index."""
    history = crons.get_history()
    await history.append(CronRunRecord(run_id="seed", job_id=job_id, summary="seed"))
    for path in (history._job_path(job_id), history._index_path):
        with Path(path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"job_id": job_id, **row}) + "\n")


async def _get(crons: CronService, url: str) -> tuple[int, Any]:
    async with TestClient(TestServer(_app(crons))) as client:
        resp = await client.get(url)
        return resp.status, json.loads(await resp.text())


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["summary", "error", "trace"])
@pytest.mark.parametrize("value", [5, ["a"], {"a": 1}])
async def test_a_non_string_text_row_is_skipped_and_the_rest_served(
    tmp_path, field: str, value: object
) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)
    await _seed_with_bad_row(crons, job.id, {"run_id": "bad", "status": "ok", field: value})

    status, body = await _get(crons, f"/api/crons/{job.id}/history")
    assert status == 200, body
    assert [r["run_id"] for r in body["runs"]] == ["seed"]

    status, body = await _get(crons, "/api/crons/history")
    assert status == 200, body
    assert [r["run_id"] for r in body["runs"]] == ["seed"]

    status, body = await _get(crons, f"/api/crons/{job.id}/history/bad")
    assert status == 404, body

    lines = Path(crons.get_history()._job_path(job.id)).read_text(encoding="utf-8")
    assert '"bad"' in lines


@pytest.mark.asyncio
async def test_a_string_text_row_is_still_served(tmp_path) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)
    await _seed_with_bad_row(crons, job.id, {"run_id": "ok", "status": "ok", "summary": "done"})

    status, body = await _get(crons, f"/api/crons/{job.id}/history")
    assert status == 200, body
    assert [r["summary"] for r in body["runs"]] == ["done", "seed"]
    status, body = await _get(crons, f"/api/crons/{job.id}/history/ok")
    assert status == 200, body
    assert body["summary"] == "done"

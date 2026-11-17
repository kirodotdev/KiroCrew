"""A script cron's idle Skip() runs can be hidden from its run history.

A run that ends in ``Skip()`` writes a row with no output, the same shape as an
``ok`` run that said nothing, so a job polling every few minutes buries the few
runs that acted under pages of idle rows. Such a run carries ``idle`` on its
history row, and ``GET /api/crons/{id}/history?hide_idle=1`` leaves those rows
out before paging and counts them in ``idle_hidden``. A manual run, a failed run
and any run with output stay.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cron_gateway_integration_helpers import _make_gw, _make_script_job, _run_script_callback

from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard.handlers import cron as h

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.sel as sel_mod

    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: MagicMock())


@pytest.mark.parametrize(("status", "idle"), [("skip", True), ("ok", False)])
async def test_only_a_skip_marks_the_run_idle(status: str, idle: bool) -> None:
    job = _make_script_job()
    await _run_script_callback(_make_gw(), job, {"status": status})
    assert job.idle_run is idle


async def test_only_a_successful_skip_writes_an_idle_row(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    svc._history.append = AsyncMock()
    outcome = {"idle": True, "fail": False}

    async def run(job):
        job.idle_run = outcome["idle"]
        if outcome["fail"]:
            job.last_status = "error"
            job.last_error = "boom"

    svc._on_job = run
    job = svc.add_job("poller", "go", every_secs=120)

    async def row_is_idle() -> bool:
        await svc._run_job_isolated(job, svc._claim_run(job.id, "scheduled"))
        record: CronRunRecord = svc._history.append.await_args.args[0]
        return record.idle

    assert await row_is_idle()
    # The flag is this run's alone: the next run that acted is not idle.
    outcome["idle"] = False
    assert not await row_is_idle()
    # A run that failed after its Skip is a failure, not an idle run.
    outcome.update(idle=True, fail=True)
    assert not await row_is_idle()


def _server(svc: CronService) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = "local-app"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(crons=svc, owner_id="", conversation_log=None)
    app.router.add_get("/api/crons/{job_id}/history", h.api_cron_history)
    return app


async def test_hide_idle_filters_before_paging_and_counts_what_it_hid(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = await svc.add_job_async("poller", "task", every_secs=120)
    # Oldest first: (summary, trigger, status, idle).
    rows = [
        ("acted", "scheduled", "success", False),
        ("idle-1", "scheduled", "success", True),
        ("failed", "scheduled", "failure", False),
        ("idle-2", "scheduled", "success", True),
        ("manual-idle", "manual", "success", True),
        ("idle-3", "scheduled", "success", True),
        ("idle-4", "scheduled", "success", True),
    ]
    base = time.time()
    for i, (summary, trigger, status, idle) in enumerate(rows):
        await svc.get_history().append(
            CronRunRecord(
                job_id=job.id,
                trigger=trigger,
                started_at=base + i,
                finished_at=base + i,
                status=status,
                summary=summary,
                idle=idle,
            )
        )

    async with TestClient(TestServer(_server(svc))) as client:

        async def page(query: str) -> dict:
            resp = await client.get(f"/api/crons/{job.id}/history?{query}")
            assert resp.status == 200, await resp.text()
            return json.loads(await resp.text())

        hidden = await page("hide_idle=1&limit=2")
        assert [r["summary"] for r in hidden["runs"]] == ["manual-idle", "failed"]
        assert (hidden["total"], hidden["idle_hidden"]) == (3, 4)
        last = await page("hide_idle=1&limit=2&offset=2")
        assert [r["summary"] for r in last["runs"]] == ["acted"]
        # Off by default: every run, nothing hidden.
        shown = await page("limit=10")
        assert [r["summary"] for r in shown["runs"]] == [r[0] for r in reversed(rows)]
        assert (shown["total"], shown["idle_hidden"]) == (7, 0)

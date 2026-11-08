"""The cron history routes fold ``limit`` and ``offset`` into a real page.

Both ``GET /api/crons/{id}/history`` and ``GET /api/crons/history`` end in a
Python list slice, where a negative bound counts from the END: unclamped,
``offset=-3`` would return the three OLDEST runs and ``limit=-1`` every run but
one. The routes fold ``offset`` to ``>= 0`` and ``limit`` to ``[1, 200]``.

The history is a real ``CronService`` in ``tmp_path``. The caller is the owner,
and the app-scoped branch of the all-history route is covered once as well,
because it slices on its own.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers import cron as h
from kiro_crew.dashboard.handlers import cron_runs

pytestmark = pytest.mark.asyncio

APP = "app-a"
RUNS = 5


@pytest.fixture
def svc(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path)


@pytest.fixture(autouse=True)
def _quiet_sel(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    import kiro_crew.sel as sel_mod

    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: MagicMock())
    _floor_monkeypatch.setattr(
        token_auth, "_app_api_allowlist", lambda name: ("/api/crons",) if name == APP else ()
    )


async def _seed(svc: CronService, created_by: str = "") -> str:
    """One job with ``RUNS`` runs, oldest first. Each run's summary is ``run-<i>``."""
    job = await svc.add_job_async("job", "task", every_secs=3600, created_by=created_by)
    base = time.time()
    for i in range(RUNS):
        await svc.get_history().append(
            CronRunRecord(
                job_id=job.id, started_at=base + i, finished_at=base + i, summary=f"run-{i}"
            )
        )
    return job.id


def _server(svc: CronService, app_claim: str) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = "local-app"
        request["app"] = app_claim
        if app_claim:
            denied = token_auth._enforce_app_scope(request, app_claim, request.path)
            if denied is not None:
                return denied
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(crons=svc, owner_id="", conversation_log=None)
    app.router.add_get("/api/crons/history", h.api_cron_history_all)
    app.router.add_get("/api/crons/{job_id}/history", h.api_cron_history)
    return app


async def _summaries(svc: CronService, path: str, app_claim: str = "") -> list[str]:
    async with TestClient(TestServer(_server(svc, app_claim))) as client:
        resp = await client.get(path)
        assert resp.status == 200, await resp.text()
        body = json.loads(await resp.text())
    assert body["total"] == RUNS
    return [r["summary"] for r in body["runs"]]


NEWEST_FIRST = [f"run-{i}" for i in reversed(range(RUNS))]


@pytest.mark.parametrize("route", ["job", "all"])
@pytest.mark.parametrize(
    ("query", "want"),
    [
        # A negative offset reads the first page, not the oldest runs.
        ("offset=-3&limit=2", NEWEST_FIRST[:2]),
        # A negative or zero limit is a one-run page, not "all but one".
        ("limit=-1", NEWEST_FIRST[:1]),
        ("limit=0", NEWEST_FIRST[:1]),
        # In-range paging is unchanged.
        ("offset=1&limit=2", NEWEST_FIRST[1:3]),
        # A non-integer keeps the default page.
        ("offset=x&limit=y", NEWEST_FIRST),
    ],
)
async def test_owner_history_pages_are_folded(svc, route, query, want) -> None:
    job_id = await _seed(svc)
    path = f"/api/crons/{job_id}/history" if route == "job" else "/api/crons/history"
    assert await _summaries(svc, f"{path}?{query}") == want


async def test_limit_is_capped(svc, monkeypatch: pytest.MonkeyPatch) -> None:
    # The constant lives where ``_history_page`` reads it, not on the facade.
    monkeypatch.setattr(cron_runs, "_HISTORY_PAGE_MAX", 3)
    job_id = await _seed(svc)
    assert await _summaries(svc, f"/api/crons/{job_id}/history?limit=100000") == NEWEST_FIRST[:3]
    assert await _summaries(svc, "/api/crons/history?limit=100000") == NEWEST_FIRST[:3]


async def test_app_scoped_all_history_is_folded(svc) -> None:
    # The app branch filters to its own runs and then slices on its own.
    await _seed(svc, created_by=f"app:{APP}")
    got = await _summaries(svc, "/api/crons/history?offset=-3&limit=-1", app_claim=APP)
    assert got == NEWEST_FIRST[:1]

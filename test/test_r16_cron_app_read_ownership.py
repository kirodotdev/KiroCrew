"""An app token may read only the cron jobs its own app created.

App Kit lets an app token declare ``/api/crons`` (``docs/app-kit/api-reference.md``),
and a bare grant reaches every child path. The mutation routes confine that
token to its own jobs through ``handlers.cron._refuse_foreign_app_job`` (pinned
by ``test/test_r12_cron_app_ownership_gate.py``). These tests pin the same rule
on the routes that READ a job or surface its result:

* ``GET /api/crons/{id}/history`` and ``GET /api/crons/{id}/history/{run_id}``
* ``GET /api/crons/{id}/script``
* ``POST /api/crons/{id}/to-chat``

A job the app does not own -- the person's, another app's, or one that does not
exist -- gets the owner gate's 403 with none of its data. ``GET /api/crons/history``
lists every job, so it filters to the app's own runs instead of refusing.

The cron store and its history are a real ``CronService`` in ``tmp_path``; the
app-token scope check is the real ``_enforce_app_scope``. Only the script-file
read and the chat-tab injection are replaced, so no file outside ``tmp_path`` is
read and no slot is minted.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronRunRecord
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers import cron as h

pytestmark = pytest.mark.asyncio

APP_A = "app-a"
APP_B = "app-b"
# A standalone-local owner: ``owner_id`` unset, subject in the local set.
OWNER_SUBJECT = "local-app"
MARK = "private-marker"
SCRIPT_BODY = f"def run(ctx):\n    ctx.notify('{MARK}')\n"


@pytest.fixture
def sel_calls(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    _floor_monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


@pytest.fixture(autouse=True)
def _grant(_floor_monkeypatch: pytest.MonkeyPatch, sel_calls: MagicMock) -> None:
    # Both apps' manifests declare /api/crons, as App Kit allows.
    _floor_monkeypatch.setattr(
        token_auth,
        "_app_api_allowlist",
        lambda name: ("/api/crons",) if name in (APP_A, APP_B) else (),
    )


@pytest.fixture
def svc(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path)


@pytest.fixture
def script_reads(monkeypatch: pytest.MonkeyPatch, svc: CronService) -> list[str]:
    """Every job id whose script source the handler went on to read."""
    reads: list[str] = []
    real_get = svc.get_job_async

    async def get_job_async(job_id: str):
        job = await real_get(job_id)
        return None if job is None else SimpleNamespace(id=job.id, script=f"{job.id}.py")

    def read_sync(script: str):
        reads.append(script.removesuffix(".py"))
        return {"source": SCRIPT_BODY, "truncated": False, "sha256": "0" * 64}, None

    monkeypatch.setattr(svc, "get_job_async", get_job_async)
    monkeypatch.setattr(h, "_read_script_source_sync", read_sync)
    return reads


@pytest.fixture
def injected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every job id whose last result to-chat pushed into a chat tab."""
    seen: list[str] = []
    monkeypatch.setattr(h, "app_holds_gateway_key", lambda *a, **k: False)
    monkeypatch.setattr(h, "prefetch_cron_dismissed", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        h, "inject_cron_result_to_dashboard", lambda _state, job, *a, **k: seen.append(job.id)
    )
    return seen


async def _seed(svc: CronService) -> dict[str, tuple[str, str]]:
    """One job per principal, each with one recorded run. Returns key -> (job, run)."""
    out: dict[str, tuple[str, str]] = {}
    for key, created_by in (("owner", ""), ("a", f"app:{APP_A}"), ("b", f"app:{APP_B}")):
        job = await svc.add_job_async(
            f"job-{key}", f"task {key}", every_secs=3600, created_by=created_by
        )
        rec = CronRunRecord(
            job_id=job.id,
            started_at=time.time(),
            finished_at=time.time(),
            summary=f"{MARK}-summary-{key}",
            trace=f"{MARK}-trace-{key}",
        )
        await svc.get_history().append(rec)
        out[key] = (job.id, rec.run_id)
    out["missing"] = ("job-missing", "run-missing")
    return out


def _server(svc: CronService, app_claim: str) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler):
        # What token_auth_middleware publishes for a verified caller.
        request["user"] = OWNER_SUBJECT
        request["app"] = app_claim
        if app_claim:
            denied = token_auth._enforce_app_scope(request, app_claim, request.path)
            if denied is not None:
                return denied
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(
        crons=svc,
        owner_id="",
        conversation_log=None,
        push_slots_update=MagicMock(),
        has_slot=lambda _key: False,
    )
    app.router.add_get("/api/crons", h.api_crons)
    app.router.add_get("/api/crons/history", h.api_cron_history_all)
    app.router.add_get("/api/crons/{job_id}/history", h.api_cron_history)
    app.router.add_get("/api/crons/{job_id}/history/{run_id}", h.api_cron_history_detail)
    app.router.add_get("/api/crons/{job_id}/script", h.api_cron_script_source)
    app.router.add_post("/api/crons/{job_id}/to-chat", h.api_cron_to_chat)
    return app


async def _call(svc: CronService, app_claim: str, method: str, path: str):
    async with TestClient(TestServer(_server(svc, app_claim))) as client:
        resp = await client.request(method, path)
        text = await resp.text()
    return resp.status, text


ROUTES = {
    "history": ("GET", "/api/crons/{job}/history", "crons.history"),
    "history_detail": ("GET", "/api/crons/{job}/history/{run}", "crons.history_detail"),
    "script": ("GET", "/api/crons/{job}/script", "crons.script_source"),
    "to_chat": ("POST", "/api/crons/{job}/to-chat", "crons.to_chat"),
}


def _app_rows(sel_calls: MagicMock, op: str) -> list[str]:
    """Outcomes of the app-attributed SEL rows recorded for ``op``."""
    return [
        c.kwargs["outcome"]
        for c in sel_calls.log_api_access.call_args_list
        if c.kwargs.get("operation") == op and c.kwargs.get("caller") == f"app:{APP_A}"
    ]


@pytest.mark.parametrize("target", ["owner", "b", "missing"])
@pytest.mark.parametrize("route", list(ROUTES))
async def test_app_is_refused_a_job_it_does_not_own(
    svc, sel_calls, script_reads, injected, route, target
) -> None:
    ids = await _seed(svc)
    method, path, op = ROUTES[route]
    job, run = ids[target]
    status, text = await _call(svc, APP_A, method, path.format(job=job, run=run))
    assert status == 403, (status, text)
    assert MARK not in text
    assert script_reads == [] and injected == []
    assert _app_rows(sel_calls, op) == ["denied"]


@pytest.mark.parametrize("route", list(ROUTES))
async def test_app_reads_its_own_job(svc, sel_calls, script_reads, injected, route) -> None:
    ids = await _seed(svc)
    method, path, op = ROUTES[route]
    job, run = ids["a"]
    status, text = await _call(svc, APP_A, method, path.format(job=job, run=run))
    assert status == 200, (status, text)
    if route == "to_chat":
        assert injected == [job]
    else:
        assert MARK in text
    assert _app_rows(sel_calls, op) == ["allowed"]


@pytest.mark.parametrize("target", ["owner", "a", "b"])
@pytest.mark.parametrize("route", list(ROUTES))
async def test_owner_reads_every_job(svc, script_reads, injected, route, target) -> None:
    ids = await _seed(svc)
    method, path, _op = ROUTES[route]
    job, run = ids[target]
    status, text = await _call(svc, "", method, path.format(job=job, run=run))
    assert status == 200, (status, text)
    if route == "to_chat":
        assert injected == [job]
    else:
        assert MARK in text


async def test_list_route_shows_app_only_its_own_jobs(svc, sel_calls) -> None:
    # GET /api/crons returns one dict per job. The run data this PR protects --
    # message, last_result, last_error, command -- rides on that dict, so an app
    # must see only its own job here, not just on the per-job routes.
    ids = await _seed(svc)
    status, text = await _call(svc, APP_A, "GET", "/api/crons")
    assert status == 200, (status, text)
    body = json.loads(text)
    assert [j["id"] for j in body["jobs"]] == [ids["a"][0]]
    # Each job's message is on the dict; the foreign ones must not leak.
    assert "task a" in text
    assert "task owner" not in text and "task b" not in text
    # The app-only branch records one ownership-scoping SEL row under app:<name>.
    assert _app_rows(sel_calls, "crons.list") == ["allowed"]


async def test_list_route_owner_sees_every_job(svc, sel_calls) -> None:
    ids = await _seed(svc)
    status, text = await _call(svc, "", "GET", "/api/crons")
    assert status == 200, (status, text)
    body = json.loads(text)
    assert {j["id"] for j in body["jobs"]} == {ids[k][0] for k in ("owner", "a", "b")}
    # The owner path skips the app filter and its audit -- no crons.list row.
    assert _app_rows(sel_calls, "crons.list") == []


async def test_all_history_lists_only_the_apps_own_runs(svc, sel_calls) -> None:
    ids = await _seed(svc)
    status, text = await _call(svc, APP_A, "GET", "/api/crons/history")
    assert status == 200, text
    body = json.loads(text)
    assert [r["job_id"] for r in body["runs"]] == [ids["a"][0]]
    assert body["total"] == 1
    assert f"{MARK}-trace-owner" not in text and f"{MARK}-trace-b" not in text
    # The ownership scoping decision is recorded once under app:<name>.
    assert _app_rows(sel_calls, "crons.history_all") == ["allowed"]


async def test_all_history_total_and_pages_count_only_own_runs(svc) -> None:
    ids = await _seed(svc)
    own_job = ids["a"][0]
    # More own runs, interleaved with foreign ones, so a page boundary matters.
    for i in range(3):
        for key in ("owner", "a", "b"):
            await svc.get_history().append(
                CronRunRecord(
                    job_id=ids[key][0],
                    started_at=time.time(),
                    finished_at=time.time(),
                    summary=f"extra-{key}-{i}",
                )
            )
    status, text = await _call(svc, APP_A, "GET", "/api/crons/history?limit=2&offset=1")
    body = json.loads(text)
    assert status == 200
    assert body["total"] == 4
    assert len(body["runs"]) == 2
    assert {r["job_id"] for r in body["runs"]} == {own_job}


@pytest.mark.parametrize("target", ["owner", "b", "missing"])
async def test_all_history_foreign_job_filter_is_empty(svc, target) -> None:
    ids = await _seed(svc)
    status, text = await _call(svc, APP_A, "GET", f"/api/crons/history?job_id={ids[target][0]}")
    assert status == 200
    assert json.loads(text) == {"runs": [], "total": 0}


async def test_all_history_owner_sees_every_job(svc) -> None:
    ids = await _seed(svc)
    status, text = await _call(svc, "", "GET", "/api/crons/history")
    body = json.loads(text)
    assert status == 200
    assert body["total"] == 3
    assert {r["job_id"] for r in body["runs"]} == {ids[k][0] for k in ("owner", "a", "b")}

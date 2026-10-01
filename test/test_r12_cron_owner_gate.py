"""Owner gate on the cron job mutation routes beyond ``POST /api/crons``.

``PATCH /api/crons/{id}``, ``POST /api/crons/{id}/run``, ``POST
/api/crons/{id}/enable``, ``POST /api/crons/{id}/ack``, ``POST
/api/crons/{id}/cancel``, ``DELETE /api/crons/{id}`` and ``DELETE /api/crons``
rewrite, fire, stop or remove a job that runs on the owner's host. An ack
summary is appended to the job's next prompt, so it rewrites the job too. Each route gets
four caller rows:

* the owner (``app == ""``, subject == owner) still reaches the store;
* an allow-listed channel user (``app == ""``, subject != owner) is refused
  with ``owner_only`` before the store or scheduler is touched, and the refusal
  is SEL-audited under the route's own operation name;
* an app token acting on a job its app created still reaches the store,
  because App Kit documents these paths as declarable app routes;
* the internal-secret transport (no ``app`` claim at all) still reaches the
  store, because ``kirocrew cron trigger`` posts ``/run`` that way.

Every collaborator is a mock on ``state.crons``: no real cron store, scheduler
or agent is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio

OWNER = "U0OWNER0000"
NON_OWNER = "U0NONOWNER"
APP_NAME = "demo-app"
JOB_ID = "abc123def0"

OWNER_ROW: dict[str, object] = {"user": OWNER, "app": ""}
NON_OWNER_ROW: dict[str, object] = {"user": NON_OWNER, "app": ""}
APP_ROW: dict[str, object] = {"user": OWNER, "app": APP_NAME}
# ``app`` deliberately absent: the internal-secret path never publishes it.
INTERNAL_ROW: dict[str, object] = {"user": "internal", "internal_auth": True}


def _identity(claims: dict[str, object]):
    @web.middleware
    async def middleware(request: web.Request, handler):
        for key, value in claims.items():
            request[key] = value
        return await handler(request)

    return middleware


def _crons() -> MagicMock:
    job = SimpleNamespace(
        id=JOB_ID,
        name="nightly",
        chat_folder_id="",
        created_by=f"app:{APP_NAME}",
    )
    crons = MagicMock()
    crons.update_job_async = AsyncMock(return_value=job)
    crons.remove_job_async = AsyncMock(return_value=True)
    crons.remove_jobs = AsyncMock(return_value=([JOB_ID], []))
    crons.enable_job_async = AsyncMock(return_value=True)
    crons.get_job_async = AsyncMock(return_value=job)
    # The app-ownership check reads the job's stamp from the cache-only lookup.
    crons.get_job = MagicMock(return_value=job)
    crons.is_running = MagicMock(return_value=False)
    crons.discard_finished_run = MagicMock()
    crons.run_job = AsyncMock(return_value=None)
    crons.attach_run_task = MagicMock()
    crons.get_history.return_value.delete_job_history = AsyncMock()
    crons.ack_job_async = AsyncMock(return_value=True)
    crons.list_jobs = MagicMock(return_value=[job])
    crons.cancel = AsyncMock(return_value=True)
    return crons


# (method, path, json body, store call that proves the route acted, SEL op name)
ROUTES = [
    (
        "PATCH",
        f"/api/crons/{JOB_ID}",
        {"message": "new prompt"},
        "update_job_async",
        "crons.update",
    ),
    ("POST", f"/api/crons/{JOB_ID}/run", None, "run_job", "crons.run"),
    ("POST", f"/api/crons/{JOB_ID}/enable", {"enabled": True}, "enable_job_async", "crons.enable"),
    ("POST", f"/api/crons/{JOB_ID}/ack", {"summary": "seen"}, "ack_job_async", "crons.ack"),
    ("POST", f"/api/crons/{JOB_ID}/cancel", None, "cancel", "crons.cancel"),
    ("DELETE", f"/api/crons/{JOB_ID}", None, "remove_job_async", "crons.delete"),
    ("DELETE", "/api/crons", {"ids": [JOB_ID]}, "remove_jobs", "crons.batch_delete"),
]
ROUTE_IDS = ["patch", "run", "enable", "ack", "cancel", "delete", "batch_delete"]


@pytest.fixture
def sel_calls(monkeypatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


async def _call(claims: dict[str, object], method: str, path: str, body):
    from kiro_crew.dashboard.handlers import cron as h

    crons = _crons()
    app = web.Application(middlewares=[_identity(claims)])
    app["state"] = MagicMock(crons=crons, owner_id=OWNER)
    app.router.add_patch("/api/crons/{job_id}", h.api_cron_update)
    app.router.add_post("/api/crons/{job_id}/run", h.api_cron_run)
    app.router.add_post("/api/crons/{job_id}/enable", h.api_cron_enable)
    app.router.add_post("/api/crons/{job_id}/ack", h.api_cron_ack)
    app.router.add_post("/api/crons/{job_id}/cancel", h.api_cron_cancel)
    app.router.add_delete("/api/crons/{job_id}", h.api_cron_delete)
    app.router.add_delete("/api/crons", h.api_cron_batch_delete)
    async with TestClient(TestServer(app)) as client:
        kwargs = {} if body is None else {"json": body}
        resp = await client.request(method, path, **kwargs)
        try:
            payload = await resp.json()
        except Exception:
            payload = {}
    return resp.status, payload if isinstance(payload, dict) else {}, crons


@pytest.mark.parametrize(("method", "path", "body", "store_call", "op"), ROUTES, ids=ROUTE_IDS)
async def test_non_owner_is_refused_before_the_store(
    sel_calls: MagicMock, method, path, body, store_call, op
) -> None:
    status, payload, crons = await _call(NON_OWNER_ROW, method, path, body)
    assert status == 403, (status, payload)
    assert payload.get("code") == "owner_only", payload
    getattr(crons, store_call).assert_not_called()
    crons.get_job_async.assert_not_called()
    denied = [
        c.kwargs
        for c in sel_calls.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied"
    ]
    assert [d.get("operation") for d in denied] == [op], denied
    assert denied[0].get("caller") == NON_OWNER


@pytest.mark.parametrize(("method", "path", "body", "store_call", "op"), ROUTES, ids=ROUTE_IDS)
async def test_owner_still_reaches_the_store(
    sel_calls: MagicMock, method, path, body, store_call, op
) -> None:
    status, payload, crons = await _call(OWNER_ROW, method, path, body)
    assert status == 200, (status, payload)
    getattr(crons, store_call).assert_called_once()


@pytest.mark.parametrize(("method", "path", "body", "store_call", "op"), ROUTES, ids=ROUTE_IDS)
async def test_app_token_on_its_own_job_still_reaches_the_store(
    sel_calls: MagicMock, method, path, body, store_call, op
) -> None:
    status, payload, crons = await _call(APP_ROW, method, path, body)
    assert status == 200, (status, payload)
    getattr(crons, store_call).assert_called_once()


async def test_internal_secret_trigger_still_runs(sel_calls: MagicMock) -> None:
    status, payload, crons = await _call(INTERNAL_ROW, "POST", f"/api/crons/{JOB_ID}/run", None)
    assert status == 200, (status, payload)
    crons.run_job.assert_called_once()

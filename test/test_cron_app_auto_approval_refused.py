"""An app token may not set ``approval_mode="auto"`` on a cron job.

``auto`` approves every tool call the job's turns make with no person asked, so
only the owner may set it. ``POST /api/crons`` and ``PATCH /api/crons/{id}``
refuse it from an app token with 403; the app's job keeps waiting for a human.
The owner keeps ``auto``, and an app's other writes still work.

The cron store is a real ``CronService`` in ``tmp_path`` and the app-token
scope check is the real ``_enforce_app_scope``, as in
``test_r12_cron_app_ownership_gate``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers import cron as h

pytestmark = pytest.mark.asyncio

APP = "app-a"
OWNER_SUBJECT = "local-app"
CODE = "app_auto_approval_refused"


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


@pytest.fixture
def grant_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        token_auth, "_app_api_allowlist", lambda name: ("/api/crons",) if name == APP else ()
    )


@pytest.fixture
def svc(tmp_path) -> CronService:
    return CronService(base_dir=tmp_path)


def _server(svc: CronService, app_claim: str) -> web.Application:
    @web.middleware
    async def identity(request: web.Request, handler):
        request["user"] = OWNER_SUBJECT
        request["app"] = app_claim
        if app_claim:
            denied = token_auth._enforce_app_scope(request, app_claim, request.path)
            if denied is not None:
                return denied
        return await handler(request)

    app = web.Application(middlewares=[identity])
    app["state"] = SimpleNamespace(
        crons=svc, owner_id="", push_refresh=MagicMock(), has_slot=MagicMock(return_value=False)
    )
    app.router.add_post("/api/crons", h.api_crons_create)
    app.router.add_patch("/api/crons/{job_id}", h.api_cron_update)
    return app


async def _request(svc: CronService, app_claim: str, method: str, path: str, body: dict):
    async with TestClient(TestServer(_server(svc, app_claim))) as client:
        resp = await client.request(method, path, json=body)
        try:
            payload = await resp.json()
        except Exception:
            payload = {}
    return resp.status, payload if isinstance(payload, dict) else {}


def _create_body(**extra) -> dict:
    return {"name": "job", "message": "task", "every": 3600, **extra}


async def _app_job(svc: CronService) -> str:
    job = await svc.add_job_async("job-a", "task a", every_secs=3600, created_by=f"app:{APP}")
    return job.id


@pytest.mark.parametrize("mode", ["auto", " auto ", "auto\u200b"])
async def test_app_create_with_auto_is_refused(svc, grant_crons, sel_calls, mode) -> None:
    status, payload = await _request(
        svc, APP, "POST", "/api/crons", _create_body(approval_mode=mode)
    )
    assert status == 403, (status, payload)
    assert payload.get("code") == CODE
    assert svc.list_jobs() == []
    denied = [
        c.kwargs
        for c in sel_calls.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied"
    ]
    assert [(d["caller"], d["operation"]) for d in denied] == [(f"app:{APP}", "crons.create")]


@pytest.mark.parametrize("mode", ["AUTO", "Auto", "yes"])
async def test_app_create_with_other_spelling_is_still_invalid(
    svc, grant_crons, sel_calls, mode
) -> None:
    status, _ = await _request(svc, APP, "POST", "/api/crons", _create_body(approval_mode=mode))
    assert status == 400
    assert svc.list_jobs() == []


async def test_app_update_to_auto_is_refused(svc, grant_crons, sel_calls) -> None:
    job_id = await _app_job(svc)
    status, payload = await _request(
        svc, APP, "PATCH", f"/api/crons/{job_id}", {"approval_mode": "auto", "message": "x"}
    )
    assert status == 403, (status, payload)
    assert payload.get("code") == CODE
    job = svc.get_job(job_id)
    assert job.approval_mode == ""
    assert job.message == "task a"  # nothing in the refused write landed


@pytest.mark.parametrize("mode", [" auto", "AUTO", ["auto"]])
async def test_app_update_with_other_spelling_never_stores_auto(
    svc, grant_crons, sel_calls, mode
) -> None:
    job_id = await _app_job(svc)
    status, _ = await _request(svc, APP, "PATCH", f"/api/crons/{job_id}", {"approval_mode": mode})
    assert status == 400
    assert svc.get_job(job_id).approval_mode == ""


@pytest.mark.parametrize("extra", [{}, {"approval_mode": ""}])
async def test_app_create_without_auto_still_works(svc, grant_crons, sel_calls, extra) -> None:
    status, payload = await _request(svc, APP, "POST", "/api/crons", _create_body(**extra))
    assert status == 200, (status, payload)
    job = svc.get_job(payload["id"])
    assert job.approval_mode == ""
    assert job.created_by == f"app:{APP}"


async def test_app_update_without_auto_still_works(svc, grant_crons, sel_calls) -> None:
    job_id = await _app_job(svc)
    status, payload = await _request(
        svc, APP, "PATCH", f"/api/crons/{job_id}", {"approval_mode": "", "message": "rewritten"}
    )
    assert status == 200, (status, payload)
    assert svc.get_job(job_id).message == "rewritten"


async def test_owner_create_with_auto_still_works(svc, sel_calls) -> None:
    status, payload = await _request(
        svc, "", "POST", "/api/crons", _create_body(approval_mode="auto")
    )
    assert status == 200, (status, payload)
    assert svc.get_job(payload["id"]).approval_mode == "auto"


async def test_owner_update_to_auto_still_works(svc, sel_calls) -> None:
    job_id = await _app_job(svc)
    status, payload = await _request(
        svc, "", "PATCH", f"/api/crons/{job_id}", {"approval_mode": "auto"}
    )
    assert status == 200, (status, payload)
    assert svc.get_job(job_id).approval_mode == "auto"


async def _owner_auto_app_job(svc: CronService) -> str:
    job_id = await _app_job(svc)
    await svc.update_job_async(job_id, approval_mode="auto")
    return job_id


@pytest.mark.parametrize(
    "change", [{"message": "rewritten"}, {"channel": "C0AP77JJSN6"}, {"every": 600}]
)
async def test_app_changing_what_an_auto_job_does_turns_auto_off(
    svc, grant_crons, sel_calls, change
) -> None:
    job_id = await _owner_auto_app_job(svc)
    status, payload = await _request(svc, APP, "PATCH", f"/api/crons/{job_id}", change)
    assert status == 200, (status, payload)
    assert svc.get_job(job_id).approval_mode == ""


async def test_app_flag_change_keeps_the_owners_auto(svc, grant_crons, sel_calls) -> None:
    job_id = await _owner_auto_app_job(svc)
    status, payload = await _request(svc, APP, "PATCH", f"/api/crons/{job_id}", {"silent": True})
    assert status == 200, (status, payload)
    assert svc.get_job(job_id).approval_mode == "auto"


async def test_owner_rewrite_keeps_auto(svc, sel_calls) -> None:
    job_id = await _owner_auto_app_job(svc)
    status, payload = await _request(svc, "", "PATCH", f"/api/crons/{job_id}", {"message": "new"})
    assert status == 200, (status, payload)
    assert svc.get_job(job_id).approval_mode == "auto"


def test_rest_and_tool_reset_the_same_fields() -> None:
    from kiro_crew import mcp_cron
    from kiro_crew.dashboard.handlers import cron_jobs

    assert cron_jobs._APP_AUTO_RESETTING_FIELDS == mcp_cron._AUTO_RESETTING_FIELDS

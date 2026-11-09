"""``approval_mode="auto"`` is never available to an app, on any path.

An auto job approves every tool call of every later fire with nobody watching. An
app token is minted from a secret the app's own (sandboxed) code can write, so the
app's request cannot be what turns auto on. Three layers hold the line, and each
section below pins one:

* the REST routes answer an app token that sends ``"auto"`` with 403,
* the job store refuses ``"auto"`` on any job whose ``created_by`` is an app stamp,
* the fire path runs a stored ``auto`` on an app-owned job as hook-based.

The owner's Schedule page keeps setting ``auto`` on the owner's own jobs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.cron_service.fields import effective_job_approval_mode

OWNER = "U0OWNER0000"
APP = "digest-app"
APP_STAMP = f"app:{APP}"


def _identity(**claims: object):
    """Publish what ``token_auth`` sets for one caller class."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        for key, value in claims.items():
            request[key] = value
        return await handler(request)

    return middleware


#: An app token: a non-empty ``app`` claim.
_APP = {"user": OWNER, "app": APP}
#: The dashboard owner's cookie session.
_OWNER = {"user": OWNER, "app": ""}


def _job(created_by: str = APP_STAMP, approval_mode: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        id="j1", job_id="j1", name="j", approval_mode=approval_mode, created_by=created_by
    )


def _state(existing: SimpleNamespace) -> MagicMock:
    crons = MagicMock()
    crons.add_job_async = AsyncMock(return_value=_job())
    crons.update_job_async = AsyncMock(return_value=_job())
    crons.get_job = MagicMock(return_value=existing)
    return MagicMock(crons=crons, owner_id=OWNER)


async def _call(claims: dict, method: str, path: str, route: str, handler, body, existing=None):
    app = web.Application(middlewares=[_identity(**claims)])
    state = _state(existing or _job())
    app["state"] = state
    app.router.add_route(method, route, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, json=body)
        try:
            payload = await resp.json()
        except Exception:
            payload = {}
    return resp.status, payload, state


def _create(claims: dict, approval_mode: str):
    from kiro_crew.dashboard.handlers import cron as handlers

    body = {"name": "x", "message": "m", "every": 3600, "approval_mode": approval_mode}
    return _call(claims, "POST", "/api/crons", "/api/crons", handlers.api_crons_create, body)


def _update(claims: dict, body: dict, existing: SimpleNamespace):
    from kiro_crew.dashboard.handlers import cron as handlers

    return _call(
        claims,
        "PATCH",
        "/api/crons/j1",
        "/api/crons/{job_id}",
        handlers.api_cron_update,
        body,
        existing,
    )


# ── The REST routes ──


@pytest.mark.asyncio
async def test_app_token_create_with_auto_is_refused() -> None:
    status, payload, state = await _create(_APP, "auto")
    assert status == 403, payload
    assert payload.get("code") == "app_auto_approval_refused"
    state.crons.add_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_token_create_with_the_default_mode_still_works() -> None:
    _status, _payload, state = await _create(_APP, "")
    state.crons.add_job_async.assert_awaited_once()
    kwargs = state.crons.add_job_async.await_args.kwargs
    assert kwargs.get("created_by") == APP_STAMP
    assert "approval_mode" not in kwargs


@pytest.mark.asyncio
async def test_owner_create_with_auto_still_works() -> None:
    _status, _payload, state = await _create(_OWNER, "auto")
    state.crons.add_job_async.assert_awaited_once()
    assert state.crons.add_job_async.await_args.kwargs.get("approval_mode") == "auto"


@pytest.mark.asyncio
async def test_app_token_update_to_auto_is_refused_on_its_own_job() -> None:
    status, payload, state = await _update(_APP, {"approval_mode": "auto"}, _job(APP_STAMP))
    assert status == 403, payload
    assert payload.get("code") == "app_auto_approval_refused"
    state.crons.update_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_token_update_may_still_turn_auto_off_on_its_own_job() -> None:
    _status, _payload, state = await _update(_APP, {"approval_mode": ""}, _job(APP_STAMP))
    state.crons.update_job_async.assert_awaited_once()
    assert state.crons.update_job_async.await_args.kwargs.get("approval_mode") == ""


@pytest.mark.asyncio
async def test_app_token_cannot_touch_an_owner_job_at_all() -> None:
    owner_job = _job(created_by=OWNER)
    status, payload, state = await _update(_APP, {"message": "new"}, owner_job)
    assert status == 403, payload
    assert payload.get("code") == "owner_only"
    state.crons.update_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_token_cannot_flip_an_owner_job_to_auto() -> None:
    owner_job = _job(created_by=OWNER)
    status, payload, state = await _update(_APP, {"approval_mode": "auto"}, owner_job)
    assert status == 403, payload
    state.crons.update_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_update_to_auto_still_reaches_the_store() -> None:
    owner_job = _job(created_by=OWNER)
    _status, _payload, state = await _update(_OWNER, {"approval_mode": "auto"}, owner_job)
    state.crons.update_job_async.assert_awaited_once()
    assert state.crons.update_job_async.await_args.kwargs.get("approval_mode") == "auto"


# ── The job store, which every other writer (SDK, CLI, import) reaches ──


@pytest.fixture
def svc(tmp_path: Path) -> CronService:
    service = CronService(base_dir=tmp_path)
    service._dir.mkdir(parents=True, exist_ok=True)
    return service


def test_store_refuses_auto_at_create_for_an_app_stamp(svc: CronService) -> None:
    with pytest.raises(ValueError, match="not available to a job owned by an app"):
        svc.add_job("a", "m", every_secs=3600, created_by=APP_STAMP, approval_mode="auto")
    assert svc.list_jobs(include_disabled=True) == []


def test_store_accepts_auto_for_an_owner_job(svc: CronService) -> None:
    job = svc.add_job("a", "m", every_secs=3600, created_by=OWNER, approval_mode="auto")
    assert job.approval_mode == "auto"


def test_store_refuses_auto_on_update_of_an_app_job_and_leaves_it_alone(svc: CronService) -> None:
    job = svc.add_job("a", "m", every_secs=3600, created_by=APP_STAMP)
    with pytest.raises(ValueError, match="not available to a job owned by an app"):
        svc.update_job(job.id, approval_mode="auto", message="new")
    after = svc.get_job(job.id)
    assert after.approval_mode == ""
    assert after.message == "m"


def test_store_accepts_auto_on_update_of_an_owner_job(svc: CronService) -> None:
    job = svc.add_job("a", "m", every_secs=3600, created_by=OWNER)
    svc.update_job(job.id, approval_mode="auto")
    assert svc.get_job(job.id).approval_mode == "auto"


# ── The fire path ──


def test_an_app_owned_auto_job_fires_as_hook_based_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron_service.fields"):
        assert effective_job_approval_mode("auto", APP_STAMP) == ""
    assert any(APP_STAMP in record.getMessage() for record in caplog.records)


def test_an_owner_auto_job_still_fires_as_auto() -> None:
    assert effective_job_approval_mode("auto", OWNER) == "auto"
    assert effective_job_approval_mode("auto") == "auto"
    assert effective_job_approval_mode("", APP_STAMP) == ""

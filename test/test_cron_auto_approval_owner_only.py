"""``approval_mode="auto"`` on a cron job is the owner's switch.

An auto job approves every tool call of every later fire with nobody watching. The
owner turns it on from the Schedule page (cookie-authenticated, owner-gated routes).
Two other ways to write a job must not reach it:

* the dashboard cron routes called with only the internal secret (no ``app`` claim),
* the cron tools (``cron_add`` / ``cron_update``), whose gateway route is reached the
  same way by the cron MCP server and by any other code in its session.

Each section below pins one of those, plus the owner path that must keep working.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

OWNER = "U0OWNER0000"
SESSION = "dashboard:slot9"
REFUSAL = "Error: approval_mode 'auto' can only be turned on by the owner"


# ── Internal-secret callers meet the owner gate on every cron write route ──


def _identity(**claims: object):
    """Publish what ``token_auth`` sets for one caller class; absent keys stay absent."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        for key, value in claims.items():
            request[key] = value
        return await handler(request)

    return middleware


#: An ``X-Internal-Secret`` caller: ``internal_auth`` set, ``app`` ABSENT.
_INTERNAL = {"user": "internal", "internal_auth": True}
#: The dashboard owner's cookie session.
_OWNER = {"user": OWNER, "app": ""}


def _job() -> SimpleNamespace:
    return SimpleNamespace(id="j1", job_id="j1", name="j", approval_mode="auto")


def _state() -> MagicMock:
    crons = MagicMock()
    crons.add_job_async = AsyncMock(return_value=_job())
    crons.update_job_async = AsyncMock(return_value=_job())
    crons.remove_job_async = AsyncMock(return_value=True)
    crons.enable_job_async = AsyncMock(return_value=_job())
    crons.get_job = MagicMock(return_value=_job())
    return MagicMock(crons=crons, owner_id=OWNER)


async def _call(claims: dict, method: str, path: str, route: str, handler, json_body=None):
    app = web.Application(middlewares=[_identity(**claims)])
    state = _state()
    app["state"] = state
    app.router.add_route(method, route, handler)
    async with TestClient(TestServer(app)) as client:
        resp = await client.request(method, path, json=json_body)
        try:
            body = await resp.json()
        except Exception:
            body = {}
    return resp.status, body, state


def _routes():
    from kiro_crew.dashboard.handlers import cron as handlers

    return (
        (
            "POST",
            "/api/crons",
            "/api/crons",
            handlers.api_crons_create,
            {"name": "x", "message": "m", "every": 3600, "approval_mode": "auto"},
        ),
        (
            "PATCH",
            "/api/crons/j1",
            "/api/crons/{job_id}",
            handlers.api_cron_update,
            {"approval_mode": "auto"},
        ),
        ("DELETE", "/api/crons/j1", "/api/crons/{job_id}", handlers.api_cron_delete, None),
        ("DELETE", "/api/crons", "/api/crons", handlers.api_cron_batch_delete, {"ids": ["j1"]}),
        (
            "POST",
            "/api/crons/j1/enable",
            "/api/crons/{job_id}/enable",
            handlers.api_cron_enable,
            {"enabled": True},
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5), ids=("create", "update", "delete", "batch", "enable"))
async def test_internal_secret_caller_is_refused_by_the_owner_gate(index: int) -> None:
    method, path, route, handler, body = _routes()[index]
    status, payload, state = await _call(_INTERNAL, method, path, route, handler, body)
    assert status == 403, payload
    assert payload.get("code") == "owner_only", payload
    crons = state.crons
    crons.add_job_async.assert_not_awaited()
    crons.update_job_async.assert_not_awaited()
    crons.remove_job_async.assert_not_awaited()
    crons.enable_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_owner_still_creates_an_auto_job_from_the_schedule_page() -> None:
    method, path, route, handler, body = _routes()[0]
    status, payload, state = await _call(_OWNER, method, path, route, handler, body)
    assert payload.get("code") != "owner_only", payload
    state.crons.add_job_async.assert_awaited_once()
    assert state.crons.add_job_async.await_args.kwargs.get("approval_mode") == "auto"


# ── The cron tools refuse to turn auto on ──


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", SESSION)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    return tmp_path


def _svc(home):
    from kiro_crew.cron import CronService

    return CronService(base_dir=home)


def _tool(name: str, args: dict) -> str:
    # The dispatch the gateway's ``POST /api/crons/tools`` route runs.
    from kiro_crew import mcp_cron

    return mcp_cron._call_tool_locally(name, args)


def test_cron_add_refuses_auto_and_writes_nothing(home) -> None:
    result = _tool("cron_add", {"name": "a", "message": "m", "every": 120, "approval_mode": "auto"})
    assert result.startswith(REFUSAL), result
    assert _svc(home).list_jobs(include_disabled=True) == []


def test_cron_add_with_default_approval_still_works(home) -> None:
    result = _tool("cron_add", {"name": "a", "message": "m", "every": 120, "approval_mode": ""})
    assert "Added job" in result, result
    (job,) = _svc(home).list_jobs(include_disabled=True)
    assert job.approval_mode == ""


def test_cron_update_refuses_auto_and_leaves_the_job_alone(home) -> None:
    job = _svc(home).add_job(name="a", message="m", every_secs=120, session_key=SESSION)
    result = _tool("cron_update", {"job_id": job.id, "approval_mode": "auto", "message": "new"})
    assert result.startswith(REFUSAL), result
    after = _svc(home).get_job(job.id)
    assert after.approval_mode == ""
    assert after.message == "m"


def test_cron_update_can_still_turn_auto_off(home) -> None:
    job = _svc(home).add_job(
        name="a", message="m", every_secs=120, session_key=SESSION, approval_mode="auto"
    )
    result = _tool("cron_update", {"job_id": job.id, "approval_mode": ""})
    assert not result.startswith("Error"), result
    assert _svc(home).get_job(job.id).approval_mode == ""


# ── A tool edit that changes what an auto job does turns auto off ──


def _auto_job(home, **over):
    fields = {
        "name": "a",
        "message": "m",
        "every_secs": 120,
        "session_key": SESSION,
        "approval_mode": "auto",
    }
    fields.update(over)
    return _svc(home).add_job(**fields)


@pytest.mark.parametrize(
    "change",
    (
        {"message": "do something else"},
        {"agent": "kirocrew"},
        {"model": "sonnet"},
        {"cron_expr": "0 9 * * *"},
        {"every": 300},
        {"channel": "C0123456789"},
        {"thread_ts": "1700000000.000100"},
    ),
    ids=("message", "agent", "model", "cron_expr", "every", "channel", "thread_ts"),
)
def test_a_behaviour_edit_turns_auto_off(home, change) -> None:
    job = _auto_job(home)
    result = _tool("cron_update", {"job_id": job.id, **change})
    assert not result.startswith("Error"), result
    assert "Auto-approve was turned off" in result
    assert _svc(home).get_job(job.id).approval_mode == ""


def test_a_name_only_edit_keeps_auto(home) -> None:
    job = _auto_job(home)
    result = _tool("cron_update", {"job_id": job.id, "name": "renamed"})
    assert not result.startswith("Error"), result
    assert "Auto-approve was turned off" not in result
    after = _svc(home).get_job(job.id)
    assert after.name == "renamed"
    assert after.approval_mode == "auto"


def test_a_behaviour_edit_on_a_normal_job_says_nothing_about_auto(home) -> None:
    job = _auto_job(home, approval_mode="")
    result = _tool("cron_update", {"job_id": job.id, "message": "new"})
    assert "Auto-approve" not in result
    assert _svc(home).get_job(job.id).approval_mode == ""


@pytest.mark.asyncio
async def test_the_owner_can_edit_an_auto_job_and_keep_auto() -> None:
    # The Schedule page's PATCH writes what the owner sends; auto is not reset.
    from kiro_crew.dashboard.handlers import cron as handlers

    status, payload, state = await _call(
        _OWNER,
        "PATCH",
        "/api/crons/j1",
        "/api/crons/{job_id}",
        handlers.api_cron_update,
        {"message": "new", "approval_mode": "auto"},
    )
    assert payload.get("code") != "owner_only", payload
    state.crons.update_job_async.assert_awaited_once()
    kwargs = state.crons.update_job_async.await_args.kwargs
    assert kwargs.get("approval_mode") == "auto"
    assert kwargs.get("message") == "new"

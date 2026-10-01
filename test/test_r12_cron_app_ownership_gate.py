"""An app token may manage only the cron jobs its own app created.

App Kit lets an app token declare ``/api/crons`` and manage its jobs over REST
(``docs/app-kit/api-reference.md``). The mutation routes -- ``PATCH``,
``DELETE`` (single and batch), ``POST .../run`` and ``POST .../enable`` -- must
still refuse that token on a job it does not own: the person's, another app's,
or one that does not exist. Ownership is the host-written ``created_by``
``app:<name>`` stamp, which ``POST /api/crons`` now writes for an app caller the
same way ``CronSDK`` does.

The stamp is what every later reader keys on, so these tests also drive those
readers for real: ``token_auth.derive_caller_app`` (the cron session's scope)
and ``mcp_cron._vet_app_owner_enabled`` (the disabled-app fire gate).

The cron store is a real ``CronService`` in ``tmp_path``; the app-token scope
check is the real ``_enforce_app_scope``. Only ``run_job`` is replaced, so no
agent is started.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.cron import CronService
from kiro_crew.dashboard import token_auth
from kiro_crew.dashboard.handlers import cron as h

pytestmark = pytest.mark.asyncio

APP_A = "app-a"
APP_B = "app-b"
# A standalone-local owner: ``owner_id`` unset, subject in the local set.
OWNER_SUBJECT = "local-app"


@pytest.fixture
def sel_calls(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    import kiro_crew.sel as sel_mod

    recorder = MagicMock()
    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


@pytest.fixture
def grant_crons(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both apps' manifests declare /api/crons, as App Kit allows.
    monkeypatch.setattr(
        token_auth,
        "_app_api_allowlist",
        lambda name: ("/api/crons",) if name in (APP_A, APP_B) else (),
    )


async def _seed(svc: CronService) -> dict[str, str]:
    ids = {}
    for key, created_by in (("owner", ""), ("a", f"app:{APP_A}"), ("b", f"app:{APP_B}")):
        job = await svc.add_job_async(
            f"job-{key}", f"task {key}", every_secs=3600, created_by=created_by
        )
        ids[key] = job.id
    return ids


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
        push_refresh=MagicMock(),
        has_slot=MagicMock(return_value=False),
    )
    app.router.add_post("/api/crons", h.api_crons_create)
    app.router.add_patch("/api/crons/{job_id}", h.api_cron_update)
    app.router.add_post("/api/crons/{job_id}/run", h.api_cron_run)
    app.router.add_post("/api/crons/{job_id}/enable", h.api_cron_enable)
    app.router.add_delete("/api/crons/{job_id}", h.api_cron_delete)
    app.router.add_delete("/api/crons", h.api_cron_batch_delete)
    return app


async def _request(svc: CronService, app_claim: str, method: str, path: str, body=None):
    async with TestClient(TestServer(_server(svc, app_claim))) as client:
        kwargs = {} if body is None else {"json": body}
        resp = await client.request(method, path, **kwargs)
        try:
            payload = await resp.json()
        except Exception:
            payload = {}
    return resp.status, payload if isinstance(payload, dict) else {}


@pytest.fixture
def svc(tmp_path, monkeypatch: pytest.MonkeyPatch) -> CronService:
    service = CronService(base_dir=tmp_path)
    # Never start an agent: a run only has to be RECORDED as requested.
    monkeypatch.setattr(service, "run_job", AsyncMock(return_value=None))
    return service


def _effect(svc: CronService, route: str, job_id: str) -> bool:
    """Whether ``route`` took effect on ``job_id``."""
    job = svc.get_job(job_id)
    if route == "delete":
        return job is None
    if route == "patch":
        return job is not None and job.message == "rewritten"
    if route == "enable":
        return job is not None and job.enabled is False
    if route == "run":
        return any(c.args == (job_id,) for c in svc.run_job.call_args_list)
    raise AssertionError(route)


ROUTES = {
    "patch": ("PATCH", "/api/crons/{id}", {"message": "rewritten"}, "crons.update"),
    "delete": ("DELETE", "/api/crons/{id}", None, "crons.delete"),
    "run": ("POST", "/api/crons/{id}/run", None, "crons.run"),
    "enable": ("POST", "/api/crons/{id}/enable", {"enabled": False}, "crons.enable"),
}


@pytest.mark.parametrize("route", list(ROUTES))
async def test_app_on_its_own_job_is_allowed(svc, grant_crons, sel_calls, route) -> None:
    ids = await _seed(svc)
    method, path, body, _op = ROUTES[route]
    status, payload = await _request(svc, APP_A, method, path.format(id=ids["a"]), body)
    assert status == 200, (status, payload)
    assert _effect(svc, route, ids["a"])


@pytest.mark.parametrize("target", ["owner", "b", "missing"])
@pytest.mark.parametrize("route", list(ROUTES))
async def test_app_on_a_job_it_does_not_own_is_refused(
    svc, grant_crons, sel_calls, route, target
) -> None:
    ids = await _seed(svc)
    ids["missing"] = "doesnotexist0"
    method, path, body, op = ROUTES[route]
    status, payload = await _request(svc, APP_A, method, path.format(id=ids[target]), body)
    assert status == 403, (status, payload)
    assert payload.get("code") == "owner_only", payload
    if target != "missing":
        assert not _effect(svc, route, ids[target])
    svc.run_job.assert_not_called()
    denied = [
        c.kwargs
        for c in sel_calls.log_api_access.call_args_list
        if c.kwargs.get("outcome") == "denied"
    ]
    assert [d.get("operation") for d in denied] == [op], denied


async def test_batch_delete_with_one_foreign_id_deletes_nothing(
    svc, grant_crons, sel_calls
) -> None:
    ids = await _seed(svc)
    status, payload = await _request(
        svc, APP_A, "DELETE", "/api/crons", {"ids": [ids["a"], ids["owner"]]}
    )
    assert status == 403, (status, payload)
    assert payload.get("code") == "owner_only", payload
    # Checked before any removal: the app's OWN id in the batch survives too.
    assert svc.get_job(ids["a"]) is not None
    assert svc.get_job(ids["owner"]) is not None


async def test_batch_delete_of_own_ids_is_allowed(svc, grant_crons, sel_calls) -> None:
    ids = await _seed(svc)
    status, payload = await _request(svc, APP_A, "DELETE", "/api/crons", {"ids": [ids["a"]]})
    assert status == 200, (status, payload)
    assert payload.get("deleted") == [ids["a"]]
    assert svc.get_job(ids["b"]) is not None


@pytest.mark.parametrize("route", list(ROUTES))
async def test_owner_still_manages_an_app_job(svc, sel_calls, route) -> None:
    ids = await _seed(svc)
    method, path, body, _op = ROUTES[route]
    status, payload = await _request(svc, "", method, path.format(id=ids["a"]), body)
    assert status == 200, (status, payload)
    assert _effect(svc, route, ids["a"])


async def _rest_create(svc: CronService, app_claim: str) -> str:
    status, payload = await _request(
        svc, app_claim, "POST", "/api/crons", {"name": "poller", "message": "go", "every": 3600}
    )
    assert status == 200, (status, payload)
    return payload["id"]


async def test_app_rest_create_is_stamped_and_stays_manageable(svc, grant_crons, sel_calls) -> None:
    job_id = await _rest_create(svc, APP_A)
    assert svc.get_job(job_id).created_by == f"app:{APP_A}"
    status, payload = await _request(
        svc, APP_A, "PATCH", f"/api/crons/{job_id}", {"message": "rewritten"}
    )
    assert status == 200, (status, payload)
    # Another app still cannot touch it.
    status, _ = await _request(svc, APP_B, "DELETE", f"/api/crons/{job_id}")
    assert status == 403
    assert svc.get_job(job_id) is not None


async def test_owner_rest_create_is_not_stamped(svc, sel_calls) -> None:
    job_id = await _rest_create(svc, "")
    assert svc.get_job(job_id).created_by == ""


async def test_app_rest_made_cron_session_resolves_to_the_app(svc, grant_crons, sel_calls) -> None:
    app_job = await _rest_create(svc, APP_A)
    owner_job = await _rest_create(svc, "")
    jobs = svc._jobs
    assert token_auth._cron_job_owner(jobs, app_job) == APP_A
    assert token_auth.derive_caller_app({}, f"cron:{app_job}", jobs, None) == APP_A
    # The person's own cron keeps the person's reach.
    assert token_auth.derive_caller_app({}, f"cron:{owner_job}", jobs, None) == ""


@pytest.mark.parametrize(("enabled", "refused"), [(False, True), (True, False)])
async def test_fire_gate_follows_the_app_toggle_for_a_rest_made_job(
    svc, grant_crons, sel_calls, monkeypatch, enabled, refused
) -> None:
    import kiro_crew.apps.manager as manager
    from kiro_crew import mcp_cron

    job_id = await _rest_create(svc, APP_A)
    monkeypatch.setattr(manager, "app_enabled_state", lambda name: enabled)
    reason = mcp_cron._vet_app_owner_enabled(svc.get_job(job_id))
    assert (reason is not None) is refused, reason


@pytest.mark.parametrize("route", list(ROUTES))
@pytest.mark.parametrize(("target", "outcome"), [("a", "allowed"), ("owner", "denied")])
async def test_each_decision_writes_one_audit_row_naming_the_app(
    svc, grant_crons, sel_calls, route, target, outcome
) -> None:
    ids = await _seed(svc)
    method, path, body, op = ROUTES[route]
    await _request(svc, APP_A, method, path.format(id=ids[target]), body)
    rows = [
        c.kwargs for c in sel_calls.log_api_access.call_args_list if c.kwargs.get("operation") == op
    ]
    assert rows == [
        {
            "caller": f"app:{APP_A}",
            "operation": op,
            "outcome": outcome,
            "source": "dashboard",
            "resources": ids[target],
        }
    ], rows


@pytest.mark.parametrize(("foreign", "outcome"), [(False, "allowed"), (True, "denied")])
async def test_batch_decision_writes_one_audit_row_naming_the_app(
    svc, grant_crons, sel_calls, foreign, outcome
) -> None:
    ids = await _seed(svc)
    batch = [ids["a"], ids["owner"]] if foreign else [ids["a"]]
    await _request(svc, APP_A, "DELETE", "/api/crons", {"ids": batch})
    rows = [
        c.kwargs
        for c in sel_calls.log_api_access.call_args_list
        if c.kwargs.get("operation") == "crons.batch_delete"
    ]
    assert rows == [
        {
            "caller": f"app:{APP_A}",
            "operation": "crons.batch_delete",
            "outcome": outcome,
            "source": "dashboard",
            # A refusal names the id that decided it; an allow names the batch.
            "resources": ids["owner"] if foreign else ids["a"],
        }
    ], rows

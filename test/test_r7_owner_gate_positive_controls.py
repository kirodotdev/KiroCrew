"""Positive controls for the round-7 owner gates: the callers that must NOT be refused.

The owner gate this module guards is
``handlers/_shared.require_owner_dashboard_request``, newly called by twelve
mutating dashboard routes. The refusal half is pinned elsewhere
(``test_s32_dashboard_mutating_routes_owner_gate.py`` and
``test_r7_s31_mcp_discover_owner_gate.py``). What is pinned HERE is the opposite
risk, and it is the one a later edit actually walks into: a gate written one
notch too wide answers 403 to a caller the route exists for, and no refusal test
can see that.

Three caller classes reach these routes, and they are told apart by what the
authentication middleware publishes on the request:

===========================  ==================  ==========================
class                        ``request["app"]``  ``request["internal_auth"]``
===========================  ==================  ==========================
dashboard user (cookie)      ``""``              absent
App Kit app token            the app's name      absent
``X-Internal-Secret``        ABSENT              ``True``
===========================  ==================  ==========================

Owner identity is a property of the FIRST class only, so that is the only class
the gate rules on. The other two keep the control that already governs them --
the constant-time secret match, and ``_enforce_app_scope`` against the app
manifest's declared paths.

Every control asserts TWO things: the response is not the owner refusal, AND the
dangerous collaborator was actually reached. Status alone would pass against a
gate moved somewhere it never runs, which is exactly the regression these rows
exist to catch.

``member_request_scope`` is replaced for the internal-secret rows. A real
loopback caller carries an attested execution record that a synthetic request
cannot have, and without standing in for it every internal row would stop on the
409 ``member_identity_unavailable`` that identity check owns -- a refusal that
has nothing to do with the owner gate and would hide whether the gate fired.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

pytestmark = pytest.mark.asyncio

OWNER = "U0OWNER0000"
NON_OWNER = "U0NONOWNER"
APP_NAME = "demo-app"

#: The code ``require_owner_dashboard_request`` returns. Asserting on the CODE and
#: not merely on 403 is what separates "the owner gate refused me" from a route's
#: own later refusal, which several of these handlers also have.
OWNER_ONLY = "owner_only"


def _identity(**claims: object):
    """Middleware standing in for what ``token_auth`` publishes for one caller class.

    A claim passed as ``None`` is left ABSENT rather than set, because absence is
    itself one of the three signals: the internal-secret path deliberately does
    not publish ``app``, and a key set to ``None`` would read as a fourth class
    that does not exist.
    """

    @web.middleware
    async def middleware(request: web.Request, handler):
        for key, value in claims.items():
            if value is not None:
                request[key] = value
        return await handler(request)

    return middleware


def _owner_claims() -> dict[str, object]:
    return {"user": OWNER, "app": ""}


def _app_token_claims() -> dict[str, object]:
    return {"user": OWNER, "app": APP_NAME}


def _internal_claims() -> dict[str, object]:
    # ``app`` deliberately omitted -- see the module docstring's table.
    return {"user": "internal", "internal_auth": True}


def _client(app: web.Application, claims: dict[str, object]) -> TestClient:
    app.middlewares.append(_identity(**claims))
    return TestClient(TestServer(app))


async def _body(response) -> dict:
    try:
        payload = await response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _assert_not_owner_refused(status: int, body: dict, route: str) -> None:
    """The owner gate did not fire. Deliberately not ``status == 200``.

    Some of these handlers refuse the same request further down for a reason that
    predates this change (a stale project precondition, a member identity this
    fixture cannot mint). Pinning 200 would make those rows assert the whole
    handler rather than the gate, and they would then go red on a change that has
    nothing to do with authorization. ``owner_only`` is the gate's own code, so
    its absence is the precise statement.
    """
    assert (
        body.get("code") != OWNER_ONLY
    ), f"{route}: owner gate refused a legitimate caller: {body}"
    assert status != 403 or body.get("code") != OWNER_ONLY, f"{route}: {status} {body}"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


#: The session an internal-secret caller names in ``X-Session-Key``. A real one is
#: an attested execution record on disk; these rows send the header and let the
#: fixture below vouch for it, because the record is what no synthetic request can
#: have and it is not what these rows are about.
INTERNAL_SESSION = "subagent:r7-owner-gate-control"


@pytest.fixture
def attested_internal_identity(monkeypatch):
    """Stand in for the execution record a real internal-secret caller carries.

    Two seams, both in ``_shared`` and both resolved at call time, so one patch
    reaches every handler: the member scope the identity check reads, and the
    session memory mode ``api_spawn`` resolves for its parent. Neither is the
    subject of these rows -- they are the plumbing an ``X-Internal-Secret`` caller
    arrives with, and without them every internal row would stop on a 409 that
    says nothing about whether the owner gate fired.
    """
    from kiro_crew.dashboard.handlers import _shared

    async def _scope(request: web.Request):
        if request.get("internal_auth") is not True:
            return _shared.MemberScope(None, False, None)
        return _shared.MemberScope(request.headers.get("X-Session-Key", "") or None, True, None)

    async def _memory_mode(_state, _session):
        return "persistent"

    monkeypatch.setattr(_shared, "member_request_scope", _scope)
    monkeypatch.setattr(_shared, "resolve_session_memory_mode", _memory_mode)
    return _scope


#: Sent by every internal-secret row. See :data:`INTERNAL_SESSION`.
INTERNAL_HEADERS = {"X-Session-Key": INTERNAL_SESSION}


# ── steering: POST, PUT, DELETE /api/steering[/{key}] ──


def _steering_app() -> web.Application:
    from kiro_crew.dashboard.handlers.steering import api_steering_create, api_steering_detail

    app = web.Application()
    app["state"] = MagicMock(
        _slots={"default": MagicMock(project="", is_restricted=False)},
        _restricted_keys=set(),
        owner_id=OWNER,
    )
    app.router.add_post("/api/steering", api_steering_create)
    app.router.add_route("*", "/api/steering/{key:.+}", api_steering_detail)
    return app


async def test_owner_can_create_a_steering_file(fake_home) -> None:
    async with _client(_steering_app(), _owner_claims()) as client:
        response = await client.post(
            "/api/steering",
            json={"name": "r7-owner-control", "content": "# owner wrote this\n", "source": "user"},
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/steering")
    written = sorted(p.name for p in (fake_home / ".kiro" / "steering").glob("*.md"))
    assert written == ["r7-owner-control.md"], f"owner's steering write did not land: {written}"


async def test_owner_can_update_a_steering_file(fake_home) -> None:
    target = fake_home / ".kiro" / "steering" / "r7-owner-control.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# before\n", encoding="utf-8")

    async with _client(_steering_app(), _owner_claims()) as client:
        response = await client.put(
            "/api/steering/user/r7-owner-control.md", json={"content": "# after\n"}
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "PUT /api/steering/{key}")
    assert target.read_text(encoding="utf-8") == "# after\n", "owner's steering update did not land"


async def test_owner_can_delete_a_steering_file(fake_home) -> None:
    target = fake_home / ".kiro" / "steering" / "r7-owner-control.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# doomed\n", encoding="utf-8")

    async with _client(_steering_app(), _owner_claims()) as client:
        response = await client.delete("/api/steering/user/r7-owner-control.md")
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "DELETE /api/steering/{key}")
    assert not target.exists(), "owner's steering delete did not land"


# ── spawn: POST /api/spawn ──


def _spawn_app() -> tuple[web.Application, MagicMock]:
    from kiro_crew.dashboard.handlers.messaging import api_spawn

    info = SimpleNamespace(id="a1", agent_id="a1", done=False, error=None, error_code=None)
    spawn = MagicMock(return_value=info)
    app = web.Application()
    app["state"] = MagicMock(subagents=MagicMock(spawn=spawn, max_concurrent=3), owner_id=OWNER)
    app.router.add_post("/api/spawn", api_spawn)
    return app, spawn


async def test_owner_can_spawn(attested_internal_identity) -> None:
    app, spawn = _spawn_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/spawn", json={"task": "do the owner's work"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/spawn (owner)")
    spawn.assert_called()


async def test_internal_secret_caller_can_still_spawn(attested_internal_identity) -> None:
    """The ``spawn_run`` MCP tool -- ``mcp_tools/spawn.py`` over the internal secret.

    Every agent that delegates work goes through this. A gate without the
    ``internal_auth`` clause answers 403 here, because that path leaves ``app``
    absent and the owner predicate reads an absent claim as not-the-owner.
    """
    app, spawn = _spawn_app()
    async with _client(app, _internal_claims()) as client:
        response = await client.post(
            "/api/spawn",
            json={"task": "delegated work", "parent_session": INTERNAL_SESSION},
            headers=INTERNAL_HEADERS,
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/spawn (internal secret)")
    spawn.assert_called()


async def test_app_token_caller_can_still_spawn(attested_internal_identity) -> None:
    """``GET/POST /api/spawn`` is a declarable App Kit path (docs/app-kit/api-reference.md)."""
    app, spawn = _spawn_app()
    async with _client(app, _app_token_claims()) as client:
        response = await client.post("/api/spawn", json={"task": "app work"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/spawn (app token)")
    spawn.assert_called()


# ── crons: POST /api/crons ──


def _cron_app() -> tuple[web.Application, AsyncMock]:
    from kiro_crew.dashboard.handlers.cron import api_crons_create

    add_job = AsyncMock(return_value=SimpleNamespace(id="j1", job_id="j1", name="j"))
    app = web.Application()
    app["state"] = MagicMock(crons=MagicMock(add_job_async=add_job), owner_id=OWNER)
    app.router.add_post("/api/crons", api_crons_create)
    return app, add_job


_CRON_BODY = {"name": "r7-control", "message": "say hello", "every": 3600}


async def test_owner_can_create_a_cron() -> None:
    app, add_job = _cron_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/crons", json=dict(_CRON_BODY))
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/crons (owner)")
    add_job.assert_awaited()


async def test_app_token_caller_can_still_create_a_cron() -> None:
    """``GET/POST /api/crons`` is the App Kit manifest's own ``permissions.api`` example."""
    app, add_job = _cron_app()
    async with _client(app, _app_token_claims()) as client:
        response = await client.post("/api/crons", json=dict(_CRON_BODY))
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/crons (app token)")
    add_job.assert_awaited()


# ── workflows: the run routes and the library-save routes ──


class _FakeWorkflowService:
    """Records what each workflow route would have set running or saved."""

    def __init__(self) -> None:
        self.started: tuple | None = None
        self.from_intent: tuple | None = None
        self.saved: tuple | None = None
        self.updated: tuple | None = None
        self.promoted: tuple | None = None
        self.started_definition: tuple | None = None

    def list_definitions(self, search: str = ""):
        return [{"id": "wfd_1", "slug": "debug"}]

    def get_definition(self, workflow_ref: str):
        return {"id": "wfd_1", "slug": workflow_ref, "source": "source"}

    def save_definition(self, source: str, **kwargs):
        self.saved = (source, kwargs)
        return {"ok": True, "definition": {"id": "wfd_1", "slug": "debug"}}

    def update_definition(self, workflow_id: str, **kwargs):
        self.updated = (workflow_id, kwargs)
        return {"ok": True, "definition": {"id": workflow_id, "revision": 3}}

    async def start(self, source: str, **kwargs):
        self.started = (source, kwargs)
        return {"run_id": "wf_1"}

    async def start_from_intent(self, intent: str, **kwargs):
        self.from_intent = (intent, kwargs)
        return {"run_id": "wf_2"}

    async def start_definition(self, workflow_ref: str, **kwargs):
        self.started_definition = (workflow_ref, kwargs)
        return {"run_id": "wf_3", "workflow_id": "wfd_1", "revision": 2}

    async def promote_run_definition(self, run_id: str, **kwargs):
        self.promoted = (run_id, kwargs)
        return {"ok": True, "definition": {"id": "wfd_2", "slug": "promoted"}}


def _workflow_app() -> tuple[web.Application, _FakeWorkflowService]:
    from kiro_crew.dashboard.handlers.workflows import (
        api_workflow_definition_run,
        api_workflow_definition_update,
        api_workflow_definitions,
        api_workflow_definitions_create,
        api_workflow_run,
        api_workflow_run_intent,
        api_workflow_run_promote,
    )

    service = _FakeWorkflowService()
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=service, owner_id=OWNER)
    app.router.add_post("/api/workflows/run", api_workflow_run)
    app.router.add_post("/api/workflows/run_intent", api_workflow_run_intent)
    app.router.add_get("/api/workflows/definitions", api_workflow_definitions)
    app.router.add_post("/api/workflows/definitions", api_workflow_definitions_create)
    app.router.add_patch(
        "/api/workflows/definitions/{workflow_ref}", api_workflow_definition_update
    )
    app.router.add_post(
        "/api/workflows/definitions/{workflow_ref}/run", api_workflow_definition_run
    )
    app.router.add_post("/api/workflows/runs/{run_id}/promote", api_workflow_run_promote)
    return app, service


async def test_owner_can_run_a_workflow(attested_internal_identity) -> None:
    app, service = _workflow_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/workflows/run", json={"source": "ctx.agent('hi')"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/run (owner)")
    assert service.started is not None, "owner's workflow never started"


async def test_internal_secret_caller_can_still_run_a_workflow(
    attested_internal_identity,
) -> None:
    """The ``workflow_run`` MCP tool -- ``mcp_tools/workflows.py`` over the internal secret."""
    app, service = _workflow_app()
    async with _client(app, _internal_claims()) as client:
        response = await client.post(
            "/api/workflows/run",
            json={"source": "ctx.agent('hi')"},
            headers=INTERNAL_HEADERS,
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/run (internal secret)")
    assert service.started is not None, "the MCP workflow_run path never started a run"


async def test_owner_can_run_an_intent(attested_internal_identity) -> None:
    app, service = _workflow_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/workflows/run_intent", json={"intent": "research pizza"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/run_intent (owner)")
    assert service.from_intent is not None, "owner's intent run never started"


async def test_internal_secret_caller_can_still_run_an_intent(
    attested_internal_identity,
) -> None:
    """The intent leg of the ``workflow_run`` MCP tool -- ``mcp_tools/workflows.py``."""
    app, service = _workflow_app()
    async with _client(app, _internal_claims()) as client:
        response = await client.post(
            "/api/workflows/run_intent",
            json={"intent": "research pizza"},
            headers=INTERNAL_HEADERS,
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/run_intent (internal secret)")
    assert service.from_intent is not None, "the MCP intent path never started a run"


async def test_owner_can_run_a_saved_definition(attested_internal_identity) -> None:
    app, service = _workflow_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/workflows/definitions/debug/run", json={"input": "x"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/definitions/{ref}/run (owner)")
    assert service.started_definition is not None, "owner's saved definition never ran"


async def test_internal_secret_caller_can_still_run_a_saved_definition(
    attested_internal_identity,
) -> None:
    """A saved definition run by the ``workflow_run`` MCP tool -- ``mcp_tools/workflows.py``."""
    app, service = _workflow_app()
    async with _client(app, _internal_claims()) as client:
        response = await client.post(
            "/api/workflows/definitions/debug/run",
            json={"input": "x"},
            headers=INTERNAL_HEADERS,
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(
        status, body, "POST /api/workflows/definitions/{ref}/run (internal secret)"
    )
    assert service.started_definition is not None, "the MCP saved-definition path never ran"


async def test_owner_can_save_a_workflow_definition() -> None:
    app, service = _workflow_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/workflows/definitions", json={"source": "source"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/definitions")
    assert service.saved is not None, "owner's definition was never saved"


async def test_owner_can_update_a_workflow_definition() -> None:
    app, service = _workflow_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.patch(
            "/api/workflows/definitions/wfd_1",
            json={"source": "changed", "expected_revision": 2},
        )
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "PATCH /api/workflows/definitions/{ref}")
    assert service.updated is not None, "owner's definition update never landed"


async def test_owner_can_promote_a_run() -> None:
    app, service = _workflow_app()
    async with _client(app, _owner_claims()) as client:
        response = await client.post("/api/workflows/runs/wf_1/promote", json={"name": "Promoted"})
        status, body = response.status, await _body(response)

    _assert_not_owner_refused(status, body, "POST /api/workflows/runs/{id}/promote")
    assert service.promoted is not None, "owner's promote never landed"


# ── reads stay open ──


async def test_reads_stay_open_for_a_non_owner() -> None:
    """The gate covers verbs, not modules: a non-owner keeps every read it had.

    Read routes are how a non-owner allow-listed channel user sees the state the
    dashboard link was sent for. Gating a module rather than its mutating verbs
    would take that away, and nothing in the refusal tests would notice.
    """
    from kiro_crew.dashboard.handlers.workflows import api_workflow_definitions

    app, _service = _workflow_app()
    claims = {"user": NON_OWNER, "app": ""}
    async with _client(app, claims) as client:
        response = await client.get("/api/workflows/definitions")
        status, body = response.status, await _body(response)

    assert api_workflow_definitions is not None
    _assert_not_owner_refused(status, body, "GET /api/workflows/definitions (non-owner)")
    assert status == 200, f"a non-owner lost a read route: {status} {body}"

"""Ratchet: every cron-mutating route is accounted for by the project-bound owner gate.

A project-bound cron job fires with ``job.project_path`` as its execution cwd,
so any route that can create such a binding, or hand a bound job to the
scheduler / a manual trigger, must require owner authorization -- otherwise an
allow-listed non-owner dashboard token (``app == ""``, which sails through
every app-token check) could bind a job to another project's directory and read
its files back through the job's output. The individual gates are pinned in
``test_cron_project_path_owner_gate.py``, ``test_cron_project_bound_job_owner_gate.py``
and ``test_cron_project_bound_job_toctou.py``.

What none of those pin is COMPLETENESS: a cron-mutating route added later can
silently ship with no gate at all and every existing test still passes. This
module closes that by ENUMERATING the mutating cron routes from the real router
(``server._register_mcp_routes``) and requiring each handler to be classified
either as GATED -- with a live non-owner-refusal assertion below -- or as
EXEMPT with a stated reason. A new ``add_post``/``add_patch``/``add_put``/
``add_delete`` under ``/api/crons`` that no one classifies fails
``test_every_mutating_cron_route_is_classified`` until a reviewer decides which
bucket it belongs in. It is therefore a ratchet, not a snapshot of today's four.

These refusal assertions drive only the NON-owner 403 path, which every gate
returns before the job ever fires -- so the fire-path fields a MagicMock job
would leak into (``execution_context`` / ``chat_folder_id`` /
``bindings.memory_store_name``) are never read here, and a real ``CronService``
job is used besides.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard import server
from kiro_crew.dashboard.handlers import (
    api_cron_enable,
    api_cron_run,
    api_cron_update,
    api_crons_create,
)

_OWNER_GATE = "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request"

# Handlers that carry the project-bound owner gate. Each has a live
# non-owner-refusal assertion in this file (keyed by handler name), so this set
# is not a bare list -- adding a name here without a matching assertion trips
# ``test_gated_routes_each_have_a_live_refusal_assertion``.
_GATED: dict[str, str] = {
    # POST /api/crons -- inline gate on a non-owner create carrying project_path.
    "api_crons_create": "project_path_owner_required",
    # PATCH /api/crons/{id} -- store-side refuse_project_bound under the lock.
    "api_cron_update": "project_bound_job_owner_required",
    # POST /api/crons/{id}/run -- gate before the trigger, then expect_project_path.
    "api_cron_run": "project_bound_job_owner_required",
    # POST /api/crons/{id}/enable -- snapshot gate on re-enable, then CAS.
    "api_cron_enable": "project_bound_job_owner_required",
}

# Mutating cron routes deliberately OUTSIDE the project-bound owner gate, each
# with the reason it does not fire or rebind a project-bound job. This bucket is
# what makes the ratchet honest: a new route is not silently assumed safe -- it
# has to be named here with a justification (or gated above). It does NOT
# certify these routes need no other authorization; it records that THIS gate's
# fire-time-exfiltration boundary is not the one they sit on.
_EXEMPT: dict[str, str] = {
    "api_cron_batch_delete": (
        "DELETE /api/crons -- deletes jobs; deletion stops execution rather than "
        "firing a project-bound job against its cwd, so it is outside this gate's "
        "fire-time boundary."
    ),
    "api_cron_delete": (
        "DELETE /api/crons/{id} -- single-job delete; same reasoning as batch delete."
    ),
    "api_cron_tools": (
        "POST /api/crons/tools -- not job-scoped (lists available cron tools); has no "
        "job to bind or fire."
    ),
    "api_cron_cancel": (
        "POST /api/crons/{id}/cancel -- cancels an in-flight run; stops execution "
        "rather than starting it."
    ),
    "api_cron_ack": (
        "POST /api/crons/{id}/ack -- acknowledges a notification; neither fires the "
        "job nor changes its project binding."
    ),
    "api_cron_to_chat": (
        "POST /api/crons/{id}/to-chat -- re-surfaces an already-stored result into a "
        "chat slot; it neither fires the job nor rebinds it (and stored bodies are "
        "exfil/credential-redacted before injection)."
    ),
    "api_cron_secret_grant": (
        "PUT /api/crons/{id}/secrets -- vault-secret grant is operator-only via its "
        "own internal-auth refusal, a distinct authorization model from this gate."
    ),
}


def _mutating_cron_routes() -> dict[str, tuple[str, str]]:
    """Enumerate mutating ``/api/crons`` routes from the real router.

    Returns ``{handler_name: (method, path)}``. Built by registering routes onto
    a bare app exactly as the gateway does, so a new registration in
    ``server._register_mcp_routes`` is picked up here with no edit to this test.
    """
    app = web.Application()
    server._register_mcp_routes(app)
    found: dict[str, tuple[str, str]] = {}
    for route in app.router.routes():
        if route.method not in {"POST", "PATCH", "PUT", "DELETE"}:
            continue
        info = route.resource.get_info()
        path = info.get("path") or info.get("formatter") or ""
        if not path.startswith("/api/crons"):
            continue
        found[route.handler.__name__] = (route.method, path)
    return found


def test_every_mutating_cron_route_is_classified() -> None:
    routes = _mutating_cron_routes()
    classified = set(_GATED) | set(_EXEMPT)
    unclassified = {name: routes[name] for name in routes if name not in classified}
    assert not unclassified, (
        "a cron-mutating route is not accounted for by the project-bound owner "
        "gate. A project-bound job fires with job.project_path as its cwd, so a "
        "route that can create that binding or hand a bound job to the scheduler "
        "MUST require owner auth. Classify each of these: add it to _GATED with a "
        "live non-owner-refusal assertion, or to _EXEMPT with the reason it does "
        f"not fire/rebind a project-bound job: {unclassified}"
    )
    # No stale classifications: a handler that was renamed or removed must not
    # linger in either bucket pretending to be covered.
    stale = classified - set(routes)
    assert not stale, (
        "these classified handlers no longer appear as mutating cron routes -- "
        f"remove them from _GATED/_EXEMPT: {sorted(stale)}"
    )


def test_gated_routes_each_have_a_live_refusal_assertion() -> None:
    # The GATED bucket is not a free-text allowlist: every name in it must be
    # exercised by one of the per-route refusal tests below, so "classified as
    # gated" cannot drift away from "actually asserted to refuse".
    asserted = {
        "api_crons_create",
        "api_cron_update",
        "api_cron_run",
        "api_cron_enable",
    }
    assert set(_GATED) == asserted, (
        "_GATED and the per-route refusal tests disagree. Every gated handler "
        "needs a live non-owner-refusal test in this file, and every such test "
        "needs its handler in _GATED."
    )


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _request(crons: CronService, *, body: dict | None = None, job_id: str | None = None):
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    if job_id is not None:
        request.match_info = {"job_id": job_id}
    if body is not None:
        attach_body(request, body)
    return request


class TestGatedRoutesRefuseNonOwnerOnProjectBoundJob:
    """One assertion per gated route, in that route's own shape.

    The four differ materially -- create gates the incoming binding inline, run
    gates before triggering, enable gates the re-enable direction on a snapshot,
    and update refuses through the store under its own lock -- so each is driven
    on the shape it actually rejects rather than forced into a single template.
    """

    @pytest.mark.asyncio
    async def test_create_refuses_non_owner_binding_a_project(self, tmp_path):
        # No job exists yet: create's shape is a non-owner supplying project_path
        # in the body, refused before any job is persisted.
        crons = CronService(base_dir=tmp_path)
        request = _request(
            crons,
            body={"name": "n", "message": "m", "every": 3600, "project_path": str(tmp_path)},
        )
        with patch(_OWNER_GATE, lambda request: False):
            resp = await api_crons_create(request)
        assert resp.status == 403
        assert _body_code(resp) == "project_path_owner_required"
        assert crons.list_jobs() == [], "a denied create must persist no job at all"

    @pytest.mark.asyncio
    async def test_update_refuses_non_owner_on_a_bound_job(self, tmp_path):
        # An unrelated field (message) on an already-bound job: the refusal comes
        # from the store's refuse_project_bound precondition under the lock.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(_OWNER_GATE, lambda request: False):
            resp = await api_cron_update(_request(crons, body={"message": "x"}, job_id=job.id))
        assert resp.status == 403
        assert _body_code(resp) == "project_bound_job_owner_required"
        assert crons.list_jobs()[0].message == "m", "a denied edit must not mutate the bound job"

    @pytest.mark.asyncio
    async def test_run_refuses_non_owner_triggering_a_bound_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(_OWNER_GATE, lambda request: False):
            resp = await api_cron_run(_request(crons, job_id=job.id))
        assert resp.status == 403
        assert _body_code(resp) == "project_bound_job_owner_required"
        assert job.id not in crons._claims, "a denied trigger must start no run task"

    @pytest.mark.asyncio
    async def test_enable_refuses_non_owner_reenabling_a_bound_job(self, tmp_path):
        # Only the RE-ENABLE direction is gated: it hands a bound job back to the
        # scheduler, which fires it against its cwd.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(_OWNER_GATE, lambda request: False):
            resp = await api_cron_enable(_request(crons, body={"enabled": True}, job_id=job.id))
        assert resp.status == 403
        assert _body_code(resp) == "project_bound_job_owner_required"


def _body_code(resp: web.Response) -> str:
    import json

    return json.loads(resp.body.decode()).get("code", "")

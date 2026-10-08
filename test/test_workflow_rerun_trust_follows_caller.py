"""A rerun's spawns borrow the Trust of the session that asked, not the prior run's.

``slack.gateway._spawn_parent_slot`` maps a workflow step's session back to the
run's recorded ``session_key`` and auto-approves its spawns when that slot is
trusted. A rerun is a new run, so if it inherits the prior run's origin, any
other session reaching the rerun route over the internal secret could launch an
edited script whose spawns then auto-approve under the trusted chat. Only the
dashboard owner keeps the prior origin; every other caller's rerun is bound to
its own ``X-Session-Key``, as a fresh run from that session would be.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from test_r7_owner_gate_positive_controls import (  # noqa: F401
    OWNER,
    _body,
    _client,
    _internal_claims,
    attested_internal_identity,
)
from test_workflows_service import GOOD_SCRIPT, FakeSessions, _wait_terminal

from kiro_crew.slack import gateway as gw
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio

ORIGIN = "dashboard:trusted-tab"
OTHER = "dashboard:other-tab"
EDITED = GOOD_SCRIPT.replace("ctx.log('hi')", "ctx.log('edited')")


async def _origin_run() -> tuple[WorkflowService, str]:
    svc = WorkflowService(sessions=FakeSessions([]), persist=False)
    first = await svc.start(GOOD_SCRIPT, session_key=ORIGIN)
    assert "run_id" in first, first
    await _wait_terminal(svc, first["run_id"])
    return svc, first["run_id"]


def _slot(svc: WorkflowService, run_id: str) -> str:
    return gw._spawn_parent_slot(f"wf-pool:{run_id}:0", svc)


@pytest.mark.parametrize("source", [EDITED, None], ids=["edited", "unedited"])
async def test_other_session_rerun_does_not_resolve_to_origin_slot(source) -> None:
    svc, prior = await _origin_run()
    assert _slot(svc, prior) == "trusted-tab"

    out = await svc.rerun_subtree(prior, 0, source=source, caller_session=OTHER)
    assert "run_id" in out, out
    handle = svc.registry.get(out["run_id"])
    assert handle.session_key == OTHER
    assert handle.author == OTHER
    assert _slot(svc, out["run_id"]) == "other-tab"


async def test_blank_caller_rerun_resolves_to_no_slot() -> None:
    svc, prior = await _origin_run()
    out = await svc.rerun_subtree(prior, 0, source=EDITED)
    assert "run_id" in out, out
    assert _slot(svc, out["run_id"]) == ""


@pytest.mark.parametrize(
    "caller, owner",
    [(ORIGIN, False), (OTHER, True), ("", True)],
    ids=["same", "owner", "owner-blank"],
)
async def test_same_session_or_owner_rerun_keeps_origin_slot(caller, owner) -> None:
    svc, prior = await _origin_run()
    out = await svc.rerun_subtree(prior, 0, source=EDITED, caller_session=caller, owner=owner)
    assert "run_id" in out, out
    assert svc.registry.get(out["run_id"]).session_key == ORIGIN
    assert _slot(svc, out["run_id"]) == "trusted-tab"


async def test_internal_secret_rerun_from_other_session_is_rebound(
    attested_internal_identity,  # noqa: F811
) -> None:
    """The reviewer's handler path: internal secret + another X-Session-Key + source."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.workflows import api_workflow_run_rerun

    svc, prior = await _origin_run()
    app = web.Application()
    app["state"] = SimpleNamespace(workflow_service=svc, owner_id=OWNER)
    app.router.add_post("/api/workflows/runs/{run_id}/rerun", api_workflow_run_rerun)
    async with _client(app, _internal_claims()) as client:
        response = await client.post(
            f"/api/workflows/runs/{prior}/rerun",
            json={"source": EDITED},
            headers={"X-Session-Key": OTHER},
        )
        status, body = response.status, await _body(response)

    assert status == 200, body
    new_id = body["run_id"]
    await _wait_terminal(svc, new_id)
    assert svc.registry.get(new_id).session_key == OTHER
    assert _slot(svc, new_id) == "other-tab"

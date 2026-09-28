"""Non-owner refusals for the round-7 routes the audit PoCs do not each cover.

The two PoC modules pin five of the twelve gates -- ``POST /api/mcp/discover/install``
(finding 56), ``POST /api/steering`` (62), ``POST /api/spawn`` (63),
``POST /api/workflows/run`` (64) and ``POST /api/crons`` (65). The other seven
routes are named in those findings' own text rather than driven by their proof of
concept, so each one gets its refusal row here. Without them a gate could be
removed and nothing would go red, which is the same hole the findings describe.

Every row drives the registrar's own handler object over a ``TestClient``, so a
check added anywhere other than the request path does not satisfy it, and every
row asserts the shared ``owner_only`` code rather than a bare 403 -- several of
these handlers refuse for other reasons too, and a row that cannot tell those
apart would pass against a deleted gate.

The caller modelled throughout is the one the findings name: a Slack-allowlisted
channel user. ``slack/allowlist.py::send_dashboard_link`` mints
``generate_token(user_id)`` with the default ``app=""``, so the request arrives
fully authenticated with a subject that is not ``state.owner_id``.
"""

from __future__ import annotations

import pytest

# Imported on its own line so the suppression lands on the statement flake8 reports.
# A pytest fixture must be in the requesting module's own namespace to resolve by
# name, which is what makes this "unused" import the steering rows' fixture.
from test_r7_owner_gate_positive_controls import fake_home  # noqa: F401
from test_r7_owner_gate_positive_controls import (
    NON_OWNER,
    OWNER_ONLY,
    _body,
    _client,
    _cron_app,
    _steering_app,
    _workflow_app,
)

pytestmark = pytest.mark.asyncio


def _non_owner_claims() -> dict[str, object]:
    """What ``token_auth`` publishes for an allowlisted channel user."""
    return {"user": NON_OWNER, "app": ""}


# ── steering: the PUT finding 62 names, and the DELETE added with this group ──


async def test_steering_update_refuses_non_owner(fake_home) -> None:  # noqa: F811
    """PUT /api/steering/{key} -- finding 62 names this route beside the POST.

    ``steering.py::_blocked`` refuses a RESTRICTED (incognito/guest) session and
    nothing else, so before this gate an ordinary non-owner session rewrote
    instructions every later agent turn obeys.
    """
    target = fake_home / ".kiro" / "steering" / "victim.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# the owner's own words\n", encoding="utf-8")

    async with _client(_steering_app(), _non_owner_claims()) as client:
        response = await client.put("/api/steering/user/victim.md", json={"content": "# planted\n"})
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner rewrote a steering file: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert (
        target.read_text(encoding="utf-8") == "# the owner's own words\n"
    ), "the owner's steering content was overwritten by a non-owner"


async def test_steering_delete_refuses_non_owner(fake_home) -> None:  # noqa: F811
    """DELETE /api/steering/{key} -- added with this group, not a finding of its own.

    Gating create and update while leaving delete open would swap one write for
    another: the same subject that could plant a steering file could instead
    remove the owner's.
    """
    target = fake_home / ".kiro" / "steering" / "victim.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# the owner's own words\n", encoding="utf-8")

    async with _client(_steering_app(), _non_owner_claims()) as client:
        response = await client.delete("/api/steering/user/victim.md")
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner deleted a steering file: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert target.exists(), "the owner's steering file was deleted by a non-owner"


# ── workflows: the library-save routes finding 64 names beside the run route ──


async def test_workflow_definitions_create_refuses_non_owner() -> None:
    """POST /api/workflows/definitions -- saves source the owner later runs.

    Its only previous gate was ``_require_dashboard_user``, which tests
    ``request.get("app") == ""`` -- precisely the claim the allowlist mints.
    """
    app, service = _workflow_app()
    async with _client(app, _non_owner_claims()) as client:
        response = await client.post("/api/workflows/definitions", json={"source": "source"})
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner saved a workflow definition: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert service.saved is None, "a non-owner's definition was saved"


async def test_workflow_definition_update_refuses_non_owner() -> None:
    """PATCH /api/workflows/definitions/{ref} -- appends a revision to saved source."""
    app, service = _workflow_app()
    async with _client(app, _non_owner_claims()) as client:
        response = await client.patch(
            "/api/workflows/definitions/wfd_1",
            json={"source": "planted", "expected_revision": 2},
        )
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner revised a workflow definition: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert service.updated is None, "a non-owner's revision landed"


async def test_workflow_run_promote_refuses_non_owner() -> None:
    """POST /api/workflows/runs/{id}/promote -- writes a run's source into the library."""
    app, service = _workflow_app()
    async with _client(app, _non_owner_claims()) as client:
        response = await client.post("/api/workflows/runs/wf_1/promote", json={"name": "Planted"})
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner promoted a run: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert service.promoted is None, "a non-owner's promote landed"


async def test_workflow_definition_run_refuses_non_owner() -> None:
    """POST /api/workflows/definitions/{ref}/run -- executes the exact saved source."""
    app, service = _workflow_app()
    async with _client(app, _non_owner_claims()) as client:
        response = await client.post("/api/workflows/definitions/debug/run", json={"input": "x"})
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner ran a saved workflow: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert service.started_definition is None, "a non-owner started a saved workflow"


async def test_workflow_run_intent_refuses_non_owner() -> None:
    """POST /api/workflows/run_intent -- authors a script and runs it in one call."""
    app, service = _workflow_app()
    async with _client(app, _non_owner_claims()) as client:
        response = await client.post("/api/workflows/run_intent", json={"intent": "do anything"})
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner launched an intent run: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    assert service.from_intent is None, "a non-owner's intent run started"


# ── crons: the sibling of finding 65's own PoC, kept here so the app-token
#    carve-out cannot be widened into an every-caller exemption unnoticed ──


async def test_crons_create_still_refuses_a_non_owner_cookie_caller() -> None:
    """The app-token carve-out must not reach the cookie class.

    ``api_crons_create`` gates on ``request.get("app") == ""``. Widening that to
    ``request.get("app") is not None`` or dropping it entirely would readmit the
    subject finding 65 describes, and finding 65's own PoC would still pass if the
    gate were moved behind the body validation -- this row pins the class.
    """
    app, add_job = _cron_app()
    async with _client(app, _non_owner_claims()) as client:
        response = await client.post(
            "/api/crons",
            json={"name": "planted", "message": "exfiltrate", "every": 60, "approval_mode": "auto"},
        )
        status, body = response.status, await _body(response)

    assert status == 403, f"non-owner created a cron: {status} {body}"
    assert body.get("code") == OWNER_ONLY, body
    add_job.assert_not_awaited()

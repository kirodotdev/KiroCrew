"""Admission matrix for the Phase-4 Plane C routes.

Same mechanism as ``test_supervised_phase3_admissions.py``: the supervising Kiro
CLI, holding the ``X-Internal-Secret``, must reach the loop/workflow routes the
Phase-4 bridge RPCs into, and ONLY when the sidecar is supervised. Unsupervised
gateways and wrong-secret callers stay denied, and the base set is untouched.

The workflow-run and workflow-definition prefixes are the load-bearing cases:
their bare prefixes also cover POST mutation children (promote/cancel/rerun,
create/update), but those handlers gate on ``_require_dashboard_user`` — a claim
an internal-secret human caller deliberately leaves absent — so they stay refused
at the handler even though the prefix admits the path. This file asserts the
ADMISSION only; the handler-level refusal is exercised where those handlers are
tested.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    supervised_mixed_internal_method_paths,
    supervised_mixed_internal_paths,
)
from kiro_crew.dashboard.token_auth import token_auth_middleware
from test.test_token_auth import _make_request, _ok_handler

SECRET = "supervised-phase4-secret"

# Routes Phase 4 adds to the SUPERVISED prefix set (any method). The workflow
# routes the bridge uses are NOT here: ``/api/workflows`` is already base-mixed,
# so they are reachable in every mode and adding a supervised duplicate would be
# dead weight (asserted separately below).
_PHASE4_PREFIX_ROUTES = [
    "/api/crew/wakes",
    "/api/autonudge",
]

# Workflow routes the bridge calls, already reachable via the base ``/api/
# workflows`` prefix (the DW engine's MCP tools use them in every mode).
_WORKFLOW_BASE_ROUTES = [
    "/api/workflows/run_intent",
    "/api/workflows/runs",
    "/api/workflows/definitions",
]

# Representative child paths the new prefixes must admit (the routes the bridge
# actually calls), each a read or a write the CLI is the legitimate author of.
_PHASE4_ADMITTED_CHILDREN = [
    ("/api/crew/wakes/abc123def456/ack", "POST"),  # wake/ack
    ("/api/autonudge/loop123", "GET"),  # monitor/inspect by handle
    ("/api/autonudge/loop123", "PATCH"),  # monitor/update
    ("/api/autonudge/loop123", "DELETE"),  # monitor/stop
    ("/api/autonudge/loop123/fire", "POST"),  # manual fire (arms the CLI's own wake)
    ("/api/workflows/runs/run123", "GET"),  # workflow/status|result
]


def _mw(supervised: bool):
    return token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(supervised),
        mixed_internal_methods=supervised_mixed_internal_method_paths(supervised),
        internal_secret=SECRET,
    )


# -- set composition -----------------------------------------------------------


def test_supervised_set_contains_every_phase4_route() -> None:
    added = supervised_mixed_internal_paths(True) - _MIXED_INTERNAL_API_PATHS
    for r in _PHASE4_PREFIX_ROUTES:
        assert r in added


def test_base_set_never_carried_the_phase4_supervised_routes() -> None:
    for r in _PHASE4_PREFIX_ROUTES:
        assert r not in _MIXED_INTERNAL_API_PATHS


def test_workflows_prefix_is_base_mixed() -> None:
    # The bridge's workflow routes come in via the pre-existing base prefix, so
    # N adds no supervised duplicate for them.
    assert "/api/workflows" in _MIXED_INTERNAL_API_PATHS


def test_unsupervised_set_is_still_exactly_the_base_set() -> None:
    assert supervised_mixed_internal_paths(False) is _MIXED_INTERNAL_API_PATHS


# -- admission matrix: supervised + right secret -> 200 ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PHASE4_PREFIX_ROUTES)
async def test_supervised_secret_reaches_phase4_routes(path: str) -> None:
    req = _make_request(path=path, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method", _PHASE4_ADMITTED_CHILDREN)
async def test_supervised_secret_reaches_phase4_children(path: str, method: str) -> None:
    req = _make_request(path=path, method=method, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _WORKFLOW_BASE_ROUTES)
@pytest.mark.parametrize("supervised", [True, False])
async def test_workflow_routes_reachable_in_both_modes(path: str, supervised: bool) -> None:
    # Reached via the base ``/api/workflows`` prefix regardless of supervision.
    req = _make_request(path=path, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(supervised)(req, _ok_handler)
    assert resp.status == 200


# -- admission matrix: unsupervised -> 403 -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PHASE4_PREFIX_ROUTES)
async def test_unsupervised_secret_holder_denied_on_phase4_routes(path: str) -> None:
    req = _make_request(path=path, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(False)(req, _ok_handler)
    assert resp.status == 403


# -- admission matrix: wrong secret -> 403 -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PHASE4_PREFIX_ROUTES)
async def test_supervised_wrong_secret_denied_on_phase4_routes(path: str) -> None:
    req = _make_request(path=path, headers={"X-Internal-Secret": "nope"})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 403


# -- the base set stays reachable when supervised ------------------------------


@pytest.mark.asyncio
async def test_base_mixed_route_still_reachable_when_supervised() -> None:
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200

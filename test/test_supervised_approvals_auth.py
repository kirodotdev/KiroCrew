"""Supervised sidecar: the parent (Kiro CLI) polls/resolves approvals with the
internal secret. Regression for the Phase-2 D2 finding where every
``GET /api/approvals`` from the supervising CLI was denied "Token required"
while ``/api/spawn`` was granted, so a spawn parked in ``running`` forever.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.server import (
    _MIXED_INTERNAL_API_PATHS,
    supervised_mixed_internal_paths,
)
from kiro_crew.dashboard.token_auth import token_auth_middleware
from test.test_token_auth import _make_request, _ok_handler

SECRET = "supervised-test-secret"


def test_unsupervised_set_is_the_base_set_and_excludes_approvals() -> None:
    paths = supervised_mixed_internal_paths(False)
    assert paths is _MIXED_INTERNAL_API_PATHS
    assert "/api/approvals" not in paths


def test_supervised_set_adds_only_approvals() -> None:
    paths = supervised_mixed_internal_paths(True)
    assert paths - _MIXED_INTERNAL_API_PATHS == {"/api/approvals"}
    assert "/api/spawn" in paths  # base set preserved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/approvals", "GET"),
        ("/api/approvals/abc123/approve", "POST"),
        ("/api/approvals/abc123/reject_once", "POST"),
    ],
)
async def test_supervised_parent_with_secret_reaches_approvals(path: str, method: str) -> None:
    mw = token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(True), internal_secret=SECRET
    )
    req = _make_request(path=path, method=method, headers={"X-Internal-Secret": SECRET})
    resp = await mw(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_unsupervised_secret_holder_still_denied_on_approvals() -> None:
    mw = token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(False), internal_secret=SECRET
    )
    req = _make_request(path="/api/approvals", headers={"X-Internal-Secret": SECRET})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403


@pytest.mark.asyncio
async def test_supervised_wrong_secret_denied_on_approvals() -> None:
    mw = token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(True), internal_secret=SECRET
    )
    req = _make_request(path="/api/approvals", headers={"X-Internal-Secret": "nope"})
    resp = await mw(req, _ok_handler)
    assert resp.status == 403

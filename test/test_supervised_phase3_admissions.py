"""Phase-3 supervised-sidecar admissions.

Extends the Phase-2 E3 mechanism (``test_supervised_approvals_auth.py``): the
supervising Kiro CLI, holding the ``X-Internal-Secret`` it read from
``run/gateway-<port>.secret``, must reach the data-plane routes the ``/crew``
subcommands RPC into (memory, knowledge, sidecar status), and ONLY when the
sidecar is supervised. Unsupervised gateways and wrong-secret callers stay
denied, and the base set is untouched.

The method-scoped admission for ``/api/knowledge/sources`` is the load-bearing
case: its bare prefix would also admit the ``POST`` add-source and the whole
``/{id}/...`` ingest/mutate family, which the bridge never calls and the CLI is
not the author of, so only ``GET`` is admitted.
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

SECRET = "supervised-phase3-secret"

# Routes Phase 3 adds to the supervised prefix set (any method), each a read the
# parent is entitled to or a write it authors on its own memory.
_PHASE3_PREFIX_ROUTES = [
    "/api/memory/semantic",
    "/api/memory/records",
    "/api/knowledge/search-for-context",
    "/api/sidecar/status",
    "/api/status",
]


def _mw(supervised: bool):
    return token_auth_middleware(
        mixed_internal_paths=supervised_mixed_internal_paths(supervised),
        mixed_internal_methods=supervised_mixed_internal_method_paths(supervised),
        internal_secret=SECRET,
    )


# -- set composition -----------------------------------------------------------


def test_unsupervised_set_is_exactly_the_base_set() -> None:
    assert supervised_mixed_internal_paths(False) is _MIXED_INTERNAL_API_PATHS
    assert supervised_mixed_internal_method_paths(False) == {}


def test_supervised_prefix_additions_are_exactly_the_phase3_reads_plus_approvals() -> None:
    added = supervised_mixed_internal_paths(True) - _MIXED_INTERNAL_API_PATHS
    assert added == {"/api/approvals", *_PHASE3_PREFIX_ROUTES}


def test_supervised_method_scoped_map_is_only_knowledge_sources_get() -> None:
    assert supervised_mixed_internal_method_paths(True) == {
        "/api/knowledge/sources": frozenset({"GET"})
    }


def test_base_set_never_carried_the_phase3_routes() -> None:
    # The additions must be additions, not routes that were already mixed.
    for r in _PHASE3_PREFIX_ROUTES + ["/api/knowledge/sources"]:
        assert r not in _MIXED_INTERNAL_API_PATHS


# -- admission matrix: supervised + right secret -> 200 ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PHASE3_PREFIX_ROUTES)
async def test_supervised_secret_reaches_phase3_reads(path: str) -> None:
    req = _make_request(path=path, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_supervised_secret_reaches_memory_semantic_put() -> None:
    # ``memory/add`` is a PUT; the prefix admits it (and a delete of the
    # caller's own key) as a write the supervising CLI authors.
    req = _make_request(
        path="/api/memory/semantic", method="PUT", headers={"X-Internal-Secret": SECRET}
    )
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200


@pytest.mark.asyncio
async def test_supervised_secret_reaches_knowledge_sources_get_only() -> None:
    ok = _make_request(path="/api/knowledge/sources", headers={"X-Internal-Secret": SECRET})
    assert (await _mw(True)(ok, _ok_handler)).status == 200


# -- admission matrix: the method-scoped route rejects writes ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/knowledge/sources", "POST"),  # add_source
        ("/api/knowledge/sources/abc123/sync", "POST"),  # ingest a source
        ("/api/knowledge/sources/abc123/ingest-text", "POST"),  # write text into the store
        ("/api/knowledge/sources/abc123", "DELETE"),  # delete a source
    ],
)
async def test_supervised_secret_denied_on_knowledge_sources_writes(path: str, method: str) -> None:
    # These carry no dashboard cookie and are not method-admitted, so the mixed
    # path falls through to cookie auth and denies (deny-by-default).
    req = _make_request(path=path, method=method, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 403


# -- admission matrix: unsupervised -> 403 -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PHASE3_PREFIX_ROUTES + ["/api/knowledge/sources"])
async def test_unsupervised_secret_holder_denied_on_phase3_routes(path: str) -> None:
    req = _make_request(path=path, headers={"X-Internal-Secret": SECRET})
    resp = await _mw(False)(req, _ok_handler)
    assert resp.status == 403


# -- admission matrix: wrong secret -> 403 -------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PHASE3_PREFIX_ROUTES + ["/api/knowledge/sources"])
async def test_supervised_wrong_secret_denied_on_phase3_routes(path: str) -> None:
    req = _make_request(path=path, headers={"X-Internal-Secret": "nope"})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 403


# -- the base set stays reachable when supervised ------------------------------


@pytest.mark.asyncio
async def test_base_mixed_route_still_reachable_when_supervised() -> None:
    # /api/spawn is base-mixed; widening for Phase 3 must not drop it.
    req = _make_request(path="/api/spawn", headers={"X-Internal-Secret": SECRET})
    resp = await _mw(True)(req, _ok_handler)
    assert resp.status == 200

"""``GET /api/shared-crew/people`` and ``/mode``: a burst of reads by one viewer shares builds.

Every open dashboard polls both routes, and each read lists the whole session catalog.
Concurrent reads by one viewer share one build that started after each of them arrived
(the crew-board rule, see ``crew_board_flight``); two viewers never share an answer.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from shared_crew_host_isolation import host_isolation  # noqa: F401

from kiro_crew.shared_crew import people, preview, routes_identity
from kiro_crew.shared_crew.contracts import ACTOR_REQUEST_KEY, ActorContext, Provenance, Transport
from kiro_crew.shared_crew.identity import tailnet_principal

pytestmark = pytest.mark.usefixtures("host_isolation")

#: Longest any wait in these tests may take before the test fails instead of hanging.
_BOUND_S = 30.0
ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)
BOB = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_SERVE)
_VIEWERS = {"alice": ALICE, "bob": BOB}
_PATHS = [routes_identity.PEOPLE_PATH, routes_identity.MODE_PATH]


@pytest.fixture(autouse=True)
def _fresh_registry(_floor_monkeypatch):
    # People and mode read the session catalog only with the Shared Crew preview on.
    _floor_monkeypatch.setenv(preview.PREVIEW_ENV, "1")
    people.reset_for_tests()
    yield
    people.reset_for_tests()


class _Catalog:
    """A session catalog whose listings are counted and held until the test releases them."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.listings = 0
        self._lock = threading.Lock()

    def list_sessions_indexed(self) -> list[dict[str, Any]]:
        with self._lock:
            self.listings += 1
        assert self.release.wait(_BOUND_S), "the test never released the catalog"
        return [{"key": "dashboard_a0", "title": "alpha", "attribution": None}]


@web.middleware
async def _fake_auth(request: web.Request, handler: Any) -> web.StreamResponse:
    # Stands in for token_auth_middleware's published claims: a person the direct
    # listener admitted, named by the test's header.
    request["is_dashboard_user"] = True
    request["app"] = ""
    request["auth_tailnet_direct"] = True
    request[ACTOR_REQUEST_KEY] = ActorContext(
        principal=_VIEWERS[request.headers["X-Test-Viewer"]],
        request_id="r",
        transport=Transport.HTTP,
    )
    return await handler(request)


def _app(catalog: _Catalog) -> web.Application:
    app = web.Application(middlewares=[_fake_auth])
    app["state"] = SimpleNamespace(conversation_log=catalog)
    routes_identity.register(app)
    return app


def _count_admitted(monkeypatch, expected: int) -> asyncio.Event:
    """An event set once *expected* reads have passed the access check."""
    real = routes_identity._refusal
    admitted = asyncio.Event()
    seen: list[int] = []

    async def counted(request: web.Request) -> web.Response | None:
        refused = await real(request)
        seen.append(1)
        if len(seen) >= expected:
            admitted.set()
        return refused

    monkeypatch.setattr(routes_identity, "_refusal", counted)
    return admitted


async def _read_all(path: str, viewers: list[str], catalog: _Catalog, admitted: asyncio.Event):
    """Every read in flight at once; the catalog is released once all were admitted."""
    async with TestClient(TestServer(_app(catalog))) as client:
        try:
            reads = [
                asyncio.ensure_future(client.get(path, headers={"X-Test-Viewer": name}))
                for name in viewers
            ]
            await asyncio.wait_for(admitted.wait(), _BOUND_S)
        finally:
            catalog.release.set()
        responses = await asyncio.wait_for(asyncio.gather(*reads), _BOUND_S)
        return [r.status for r in responses], [await r.json() for r in responses]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_a_burst_of_reads_by_one_viewer_shares_builds(monkeypatch, path):
    catalog = _Catalog()
    admitted = _count_admitted(monkeypatch, 8)
    statuses, bodies = await _read_all(path, ["alice"] * 8, catalog, admitted)
    assert statuses == [200] * 8
    # All eight reads were admitted before the first listing could finish: at most one
    # build was running and one waiting, whatever the scheduling.
    assert catalog.listings <= 2
    assert all(body == bodies[0] for body in bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _PATHS)
async def test_concurrent_reads_by_two_viewers_each_get_their_own_answer(monkeypatch, path):
    catalog = _Catalog()
    admitted = _count_admitted(monkeypatch, 2)
    statuses, bodies = await _read_all(path, ["alice", "bob"], catalog, admitted)
    assert statuses == [200, 200]
    assert [body["viewer"]["id"] for body in bodies] == [ALICE.id, BOB.id]
    assert catalog.listings == 2

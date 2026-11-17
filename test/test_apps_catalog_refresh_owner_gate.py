"""Owner gate on the two app-catalog refresh routes.

``POST /api/apps/registries/refresh`` busts every configured registry's cache
and re-fetches it; ``POST /api/app-store/refresh`` drops the shared catalog
caches every user reads. A dashboard subject that is not the owner gets 403
before either side effect runs, as on the sibling registry writes.

``/api/app-store/refresh`` keeps its app-token path: an app reaches it only
through an explicit ``permissions.api`` grant that token auth checks, so the
handler gates dashboard subjects only.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.apps.registry as registry_mod
from kiro_crew.apps import routes

pytestmark = pytest.mark.asyncio

OWNER = "owner-subject"


class _RecordingSel:
    def log_api_access(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hits: list[str] = []
    monkeypatch.setattr(routes, "sel", lambda: _RecordingSel())

    async def _refresh(*a: Any, **k: Any) -> dict:
        hits.append("registries.refresh")
        return {"refreshed": []}

    monkeypatch.setattr(registry_mod, "refresh_registries", _refresh)
    monkeypatch.setattr(
        routes.official_catalog, "forget_cache", lambda: hits.append("catalog.forget")
    )
    monkeypatch.setattr(routes, "forget_category_order_cache", lambda: None)
    monkeypatch.setattr(routes, "forget_editorial_cache", lambda: None)
    return hits


def _server(user: str, app_claim: str) -> TestServer:
    @web.middleware
    async def _claims(request: web.Request, handler: Any) -> web.StreamResponse:
        request["user"] = user
        request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_claims])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_app_routes(app)
    return TestServer(app)


@pytest.mark.parametrize(
    "path,effect",
    [
        ("/api/apps/registries/refresh", "registries.refresh"),
        ("/api/app-store/refresh", "catalog.forget"),
    ],
)
async def test_the_owner_reaches_the_refresh(reached: list[str], path: str, effect: str) -> None:
    async with TestClient(_server(OWNER, "")) as client:
        resp = await client.post(path)
    assert resp.status == 200
    assert effect in reached


@pytest.mark.parametrize("path", ["/api/apps/registries/refresh", "/api/app-store/refresh"])
async def test_a_non_owner_dashboard_subject_is_refused(reached: list[str], path: str) -> None:
    async with TestClient(_server("someone-else", "")) as client:
        resp = await client.post(path)
        body = await resp.json()
    assert resp.status == 403
    assert body["code"] == "owner_only"
    assert reached == []


async def test_an_app_token_keeps_its_granted_store_refresh(reached: list[str]) -> None:
    async with TestClient(_server("app:notes", "notes")) as client:
        resp = await client.post("/api/app-store/refresh")
    assert resp.status == 200
    assert "catalog.forget" in reached

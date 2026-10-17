"""Owner gate on Design Critique's host-touching POST routes.

``POST /discover`` and ``POST /render`` start host work: a git clone, a route
scan and a headless Chromium run over a host directory, and PNGs written under
the owner's data home. So a dashboard caller that is not the owner, and a request
that carries no app claim at all, get the shared 403 ``owner_only`` and never
reach the job start. The owner still starts the job.

An app token is judged by the token middleware's scope check first: its own
namespace, or a manifest ``permissions.api`` grant. The stand-in middleware here
calls the real ``token_auth.app_token_path_allowed``, so a foreign token with no
grant is refused there, and one with a grant reaches the handler and is admitted.

The reads stay open: ``GET /method`` and the ``GET ?job=`` polls answer every
caller as before.

``_start_job`` is replaced by a recorder, so no git / node / Chromium child is
spawned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.design_critique.backend import routes
from kiro_crew.dashboard import token_auth

OWNER = "owner-user"
BASE = "/api/apps/design-critique"
GRANTED = "granted-app"
UNGRANTED = "ungranted-app"


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Enable the app, isolate the data home, grant one foreign app, record job starts."""
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(routes, "config_dir", lambda: tmp_path / "home")
    monkeypatch.setattr(routes, "_node", lambda: "/usr/bin/node-not-run")
    grants = {GRANTED: (f"{BASE}/*",)}
    monkeypatch.setattr(token_auth, "_app_api_allowlist", lambda name: grants.get(name, ()))
    calls: list[str] = []

    def _record(work: Callable[[], Any]) -> str:
        coro = work()
        coro.close()  # never run the host work
        calls.append("job")
        return "job-id"

    monkeypatch.setattr(routes, "_start_job", _record)
    return calls


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "owner-project"
    proj.mkdir()
    (proj / "package.json").write_text("{}", encoding="utf-8")
    return proj


@asynccontextmanager
async def _client() -> AsyncIterator[TestClient]:
    @web.middleware
    async def _identity(request: web.Request, handler: Any) -> web.StreamResponse:
        # Stand-in for token_auth: stamp the claims, then apply the real
        # app-token scope decision the middleware makes before any handler.
        request["user"] = request.headers.get("X-Test-User", OWNER)
        if request.headers.get("X-Test-No-App") != "1":
            app_name = request.headers.get("X-Test-App", "")
            request["app"] = app_name
            if app_name and not token_auth.app_token_path_allowed(app_name, request.path):
                return web.json_response({"error": "out of scope", "code": "app_scope"}, status=403)
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = SimpleNamespace(owner_id=OWNER)
    routes.register_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


def _bodies(project: Path) -> dict[str, dict[str, Any]]:
    return {
        "discover": {"kind": "local", "value": str(project)},
        "render": {"kind": "local", "value": str(project), "picks": [{"ref": "/"}]},
    }


REFUSED = [
    pytest.param({"X-Test-User": "someone-else"}, "owner_only", id="non-owner-dashboard-subject"),
    pytest.param({"X-Test-No-App": "1"}, "owner_only", id="missing-app-claim"),
    pytest.param({"X-Test-App": UNGRANTED}, "app_scope", id="foreign-app-token-no-grant"),
]

ADMITTED = [
    pytest.param({}, id="owner"),
    pytest.param({"X-Test-App": "design-critique"}, id="own-app-token"),
    pytest.param({"X-Test-App": GRANTED}, id="foreign-app-token-with-grant"),
]

READERS = [
    pytest.param({}, id="owner"),
    pytest.param({"X-Test-User": "someone-else"}, id="non-owner-dashboard-subject"),
    pytest.param({"X-Test-No-App": "1"}, id="missing-app-claim"),
    pytest.param({"X-Test-App": "design-critique"}, id="own-app-token"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["discover", "render"])
@pytest.mark.parametrize(("headers", "code"), REFUSED)
async def test_refused_caller_gets_403_and_no_job(
    started: list[str],
    project: Path,
    route: str,
    headers: dict[str, str],
    code: str,
) -> None:
    async with _client() as client:
        r = await client.post(f"{BASE}/{route}", json=_bodies(project)[route], headers=headers)
        status, payload = r.status, await r.json()
    assert status == 403
    assert payload.get("code") == code
    assert started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["discover", "render"])
@pytest.mark.parametrize("headers", ADMITTED)
async def test_admitted_caller_starts_the_job(
    started: list[str],
    project: Path,
    route: str,
    headers: dict[str, str],
) -> None:
    async with _client() as client:
        r = await client.post(f"{BASE}/{route}", json=_bodies(project)[route], headers=headers)
        status, payload = r.status, await r.json()
    assert status == 200
    assert payload.get("job") == "job-id"
    assert started == ["job"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["discover", "render"])
@pytest.mark.parametrize("headers", READERS)
async def test_job_poll_still_answers_every_caller(
    started: list[str],
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    headers: dict[str, str],
) -> None:
    monkeypatch.setitem(
        routes._JOBS,
        "job-1",
        {"status": "done", "result": {"ok": True}, "error": None, "created_at": 0.0},
    )
    monkeypatch.setattr(routes, "_sweep_jobs", lambda: None)
    async with _client() as client:
        r = await client.get(f"{BASE}/{route}", params={"job": "job-1"}, headers=headers)
        status, payload = r.status, await r.json()
    assert status == 200
    assert payload == {"status": "done", "result": {"ok": True}}


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", READERS)
async def test_method_still_answers_every_caller(
    started: list[str], headers: dict[str, str]
) -> None:
    async with _client() as client:
        r = await client.get(f"{BASE}/method", headers=headers)
        status, payload = r.status, await r.json()
    assert status == 200
    assert "checklist" in payload

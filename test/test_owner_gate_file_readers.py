"""Owner gate on the host-file reader routes in ``dashboard/handlers/files.py``.

Each reader serves any path the host user can read, so the caller must be the
dashboard owner. An allow-listed channel user holds a dashboard token whose
claims are ``user=<their id>`` and ``app=""``; these rows pin that such a
caller gets the standard ``owner_only`` 403 with none of the file's bytes, and
that the owner still reaches the file.

``/api/file-raw`` is declared in a shipped App Kit manifest (design_critique's
``permissions.api``), so a named app token keeps its path there; the gate binds
the dashboard-user class, including a request with no app claim at all.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import files as files_mod

SECRET = "OWNERPRIVATEBYTES-7f3a"
NON_OWNER = "U_ALLOWED_TEAMMATE"
NO_APP = "<absent>"

GET_ROUTES = [
    ("/api/file-read", files_mod.api_file_read, "path"),
    ("/api/file-watch", files_mod.api_file_watch, "path"),
    ("/api/file-diff", files_mod.api_file_diff, "path"),
    ("/api/file-download", files_mod.api_file_download, "path"),
    ("/api/file-raw", files_mod.api_file_raw, "path"),
    ("/api/file-stream", files_mod.api_file_stream, "path"),
    ("/api/file-sheet", files_mod.api_file_sheet, "path"),
    ("/api/file-office-preview", files_mod.api_file_office_preview, "path"),
    ("/api/browse-files", files_mod.api_browse_files, "dir"),
    ("/api/browse-dirs", files_mod.api_browse_dirs, "dir"),
]


class _State:
    owner_id = ""
    file_indexes: dict = {}


@web.middleware
async def _claims(request: web.Request, handler):
    request["user"] = request.headers.get("X-Test-User", "local-app")
    app_claim = request.headers.get("X-Test-App", "")
    if app_claim != NO_APP:
        request["app"] = app_claim
    return await handler(request)


def _app() -> web.Application:
    app = web.Application(middlewares=[_claims])
    app["state"] = _State()
    for route, handler, _ in GET_ROUTES:
        app.router.add_get(route, handler)
    app.router.add_post("/api/file-grep", files_mod.api_file_grep)
    return app


@pytest.fixture
def planted(tmp_path):
    (tmp_path / "notes.txt").write_text(SECRET + "\n", encoding="utf-8")
    # /api/file-raw serves only sniffed media, so its rows read an SVG.
    (tmp_path / "pic.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg"><text>{SECRET}</text></svg>', encoding="utf-8"
    )
    (tmp_path / "sub").mkdir()
    return tmp_path


async def _call(client: TestClient, route: str, kind: str, planted, headers: dict):
    if route == "/api/file-grep":
        return await client.post(route, json={"root": str(planted), "q": SECRET}, headers=headers)
    name = "pic.svg" if route == "/api/file-raw" else "notes.txt"
    target = planted if kind == "dir" else planted / name
    return await client.get(route, params={"path": str(target)}, headers=headers)


async def _assert_owner_only(resp) -> None:
    assert resp.status == 403
    body = await resp.text()
    assert "owner_only" in body
    assert SECRET not in body
    assert "notes.txt" not in body


ALL_ROUTES = [r for r, _, _ in GET_ROUTES] + ["/api/file-grep"]
KIND = {r: k for r, _, k in GET_ROUTES} | {"/api/file-grep": "dir"}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ALL_ROUTES)
async def test_non_owner_dashboard_user_is_refused(route, planted):
    async with TestClient(TestServer(_app())) as client:
        resp = await _call(client, route, KIND[route], planted, {"X-Test-User": NON_OWNER})
        await _assert_owner_only(resp)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ALL_ROUTES)
async def test_owner_passes_the_gate(route, planted):
    async with TestClient(TestServer(_app())) as client:
        resp = await _call(client, route, KIND[route], planted, {})
        try:
            assert resp.status != 403, route
            if route == "/api/file-watch":
                return
            body = await resp.text()
            assert "owner_only" not in body
            if route in ("/api/file-read", "/api/file-download", "/api/file-raw", "/api/file-grep"):
                assert resp.status == 200, route
                assert SECRET in body, route
            if route == "/api/browse-files":
                assert resp.status == 200
                assert "notes.txt" in body
            if route == "/api/browse-dirs":
                assert resp.status == 200
                assert "sub" in body
        finally:
            resp.close()


@pytest.mark.asyncio
async def test_file_raw_named_app_token_keeps_its_path(planted):
    async with TestClient(TestServer(_app())) as client:
        resp = await _call(
            client, "/api/file-raw", "path", planted, {"X-Test-App": "design-critique"}
        )
        assert resp.status == 200
        assert SECRET in await resp.text()


@pytest.mark.asyncio
async def test_file_raw_request_without_app_claim_is_refused(planted):
    async with TestClient(TestServer(_app())) as client:
        resp = await _call(client, "/api/file-raw", "path", planted, {"X-Test-App": NO_APP})
        await _assert_owner_only(resp)


@pytest.mark.asyncio
async def test_file_download_named_app_token_is_refused(planted):
    async with TestClient(TestServer(_app())) as client:
        resp = await _call(
            client, "/api/file-download", "path", planted, {"X-Test-App": "design-critique"}
        )
        await _assert_owner_only(resp)

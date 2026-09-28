"""Owner gate on the host-file reader routes in ``dashboard/handlers/files.py``.

Each reader serves any path the host user can read, so the caller must be the
dashboard owner. An allow-listed channel user holds a dashboard token whose
claims are ``user=<their id>`` and ``app=""``; these rows pin that such a
caller gets the standard ``owner_only`` 403 with none of the file's bytes, and
that the owner still reaches the file.

``/api/file-search`` is the widest row: every other reader here names one path,
while that one takes a search ROOT and answers with every matching name under
it, so a refusal has to withhold the listing and not merely the bytes.

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
    # Takes an ARBITRARY root in ``?project=`` and answers with real names,
    # sizes and mtimes, so it is the row where an ungated reader reaches
    # furthest: the whole host minus the sensitive set.
    ("/api/file-search", files_mod.api_file_search, "search"),
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
    if kind == "search":
        # Fuzzy FILENAME search, so the query is the planted name, not its bytes.
        return await client.get(
            route, params={"project": str(planted), "q": "notes"}, headers=headers
        )
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
            if route == "/api/file-search":
                assert resp.status == 200
                assert "notes.txt" in body
        finally:
            resp.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "status", "marker"),
    [
        # The two exits that answered nothing at all before: a query under the
        # minimum length, and a ``?project=`` that is not a directory. The gate
        # records only denials, so a grant leaving no event here would be an
        # authorization nobody can see afterwards.
        ({"project": "<planted>", "q": "x"}, 200, "short_query"),
        ({"project": "<planted>/notes.txt", "q": "notes"}, 404, "project="),
    ],
)
async def test_file_search_audits_the_exits_that_audited_nothing(
    params, status, marker, planted, monkeypatch
):
    seen: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(files_mod, "_sel", lambda: _Sel())
    resolved = {
        k: v.replace("<planted>", str(planted)) if isinstance(v, str) else v
        for k, v in params.items()
    }
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/api/file-search", params=resolved)
        assert resp.status == status
    allowed = [
        e for e in seen if e.get("outcome") == "allowed" and marker in str(e.get("resources", ""))
    ]
    assert len(allowed) == 1, seen
    assert allowed[0]["operation"] == "file_search"


@pytest.mark.asyncio
async def test_file_search_does_not_double_audit_a_served_query(planted, monkeypatch):
    # The ordinary success path already carried one ``allowed`` event, and the
    # per-exit audits must not add a second to it: this endpoint answers a
    # typeahead, so one served query is one row.
    seen: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(files_mod, "_sel", lambda: _Sel())
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/api/file-search", params={"project": str(planted), "q": "notes"})
        assert resp.status == 200
    assert len([e for e in seen if e.get("outcome") == "allowed"]) == 1, seen


@pytest.mark.asyncio
async def test_file_search_offloads_a_cold_sel_write(planted, monkeypatch):
    # A failed startup warm leaves construction to the first caller, which is key
    # load plus a tail read of the log. On the event loop that is a stall, so the
    # cold path has to take a worker thread.
    import kiro_crew.sel as sel_mod

    hops: list[str] = []

    class _Sel:
        def log_api_access(self, **kw):
            hops.append("write")

    monkeypatch.setattr(files_mod, "_sel", lambda: _Sel())
    monkeypatch.setattr(sel_mod, "sel_is_warm", lambda: False)

    real_to_thread = files_mod.asyncio.to_thread

    async def _tracking(fn, *a, **kw):
        hops.append("to_thread")
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(files_mod.asyncio, "to_thread", _tracking)

    async with TestClient(TestServer(_app())) as client:
        resp = await client.get("/api/file-search", params={"project": str(planted), "q": "x"})
        assert resp.status == 200
    assert "to_thread" in hops, hops
    assert hops.index("to_thread") < hops.index("write")


@pytest.mark.asyncio
async def test_file_search_answers_even_when_the_audit_raises(planted, monkeypatch):
    # These audits sit on exits that answered cleanly before, so a raising writer
    # must not turn a 404 into a 500. The record degrades, never the answer.
    class _Sel:
        def log_api_access(self, **kw):
            raise RuntimeError("sel is down")

    monkeypatch.setattr(files_mod, "_sel", lambda: _Sel())
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(
            "/api/file-search",
            params={"project": str(planted / "notes.txt"), "q": "notes"},
        )
        assert resp.status == 404
        assert "Project directory not found" in await resp.text()


@pytest.mark.asyncio
async def test_file_search_records_nothing_allowed_for_a_refused_caller(planted, monkeypatch):
    # The mirror: the gate owns the denial audit, so a refused caller must leave
    # no ``allowed`` row behind for a reader to mistake for a grant.
    seen: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(files_mod, "_sel", lambda: _Sel())
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(
            "/api/file-search",
            params={"project": str(planted), "q": "notes"},
            headers={"X-Test-User": NON_OWNER},
        )
        assert resp.status == 403
    assert [e for e in seen if e.get("outcome") == "allowed"] == []


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

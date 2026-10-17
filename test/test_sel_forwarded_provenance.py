"""Forwarded-actor provenance has its own SEL field across the auth and audit layers.

``audit_actor`` marks a forwarded request by suffixing ``caller`` with
``_via_proxy``, but only on the rows it reaches. ``token_auth`` writes its own
rows -- including the returned 401/403 refusals -- with its own ``caller``.

Every row those layers write while holding the request records
``metadata["forwarded"]`` from the same predicate, whichever of them wrote it,
and a handler's own row takes the verdict the deny-audit boundary binds for the
request.
``caller`` keeps its value on every row, and the ``_via_proxy`` suffix stays
where it is, so readers keyed on either keep matching.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

import kiro_crew
import kiro_crew.dashboard.token_auth as ta
from kiro_crew.sel import SecurityEvent, SecurityEventLog

_FORWARDED_HEADERS = {"X-Forwarded-For": "203.0.113.7"}


class _Capture:
    """Stand-in ``self`` for :meth:`SecurityEventLog.log_api_access`."""

    def __init__(self) -> None:
        self.events: list[SecurityEvent] = []

    def log(self, event: SecurityEvent, *, critical: bool = False) -> None:
        self.events.append(event)


@pytest.mark.parametrize("forwarded", [True, False])
def test_log_api_access_records_forwarded_in_its_own_field(forwarded: bool) -> None:
    """Both values land as a bool, and ``caller_identity`` is left as passed."""
    cap = _Capture()
    SecurityEventLog.log_api_access(
        cap,  # type: ignore[arg-type]
        caller="dashboard_user",
        operation="POST /api/x",
        outcome="ok",
        forwarded=forwarded,
    )
    (event,) = cap.events
    assert event.metadata == {"forwarded": forwarded}
    assert event.caller_identity == "dashboard_user"


def test_log_api_access_without_forwarded_writes_no_key() -> None:
    """A row not written for a request says nothing, rather than "made directly"."""
    cap = _Capture()
    SecurityEventLog.log_api_access(
        cap,  # type: ignore[arg-type]
        caller="system",
        operation="dashboard_sessions_revoked",
        outcome="ok",
    )
    (event,) = cap.events
    assert "forwarded" not in event.metadata


@pytest.fixture
def _clean_token_state(tmp_path, monkeypatch):
    import kiro_crew.dashboard.revocation_gen as _rg

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr(_rg, "_gen", 0)
    monkeypatch.setattr(ta, "_revoked_store_singleton", None)
    ta._state.clear_all()
    ta._app_perms_cache.clear()
    yield
    ta._state.clear_all()
    ta._app_perms_cache.clear()


def _mock_request(path: str, *, cookies: dict, headers: dict) -> MagicMock:
    req = MagicMock(spec=web.Request)
    req.path = path
    req.query = {}
    req.cookies = cookies
    req.remote = "127.0.0.1"
    req.headers = headers
    req.method = "GET"
    return req


async def _ok_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clean_token_state")
async def test_a_token_auth_refusal_records_forwarded_and_keeps_its_caller(monkeypatch) -> None:
    """The returned 401/403 rows -- where forwarded misuse shows first -- carry the field.

    Driven forwarded and direct in one test: the two rows must differ in
    ``forwarded`` and agree in ``caller``, so an exact-identity query still
    matches both halves.
    """
    rows: dict[str, list[dict]] = {}
    for label, headers in (("forwarded", _FORWARDED_HEADERS), ("direct", {})):
        calls: list[dict] = []

        class _FakeSel:
            def log_api_access(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(ta, "_sel_fn", lambda: _FakeSel())
        mw = ta.token_auth_middleware(
            internal_paths=frozenset({"/api/workflows"}),
            internal_secret="s",
            local_only=False,
        )
        req = _mock_request(
            "/api/workflows/runs", cookies={"mc_token_5476": "garbage"}, headers=headers
        )
        resp = await mw(req, _ok_handler)
        assert resp.status == 403
        rows[label] = [c for c in calls if c.get("outcome") == "denied"]
        assert rows[label], f"{label}: no denial row in {calls}"

    assert {r["forwarded"] for r in rows["forwarded"]} == {True}
    assert {r["forwarded"] for r in rows["direct"]} == {False}
    assert [r["caller"] for r in rows["forwarded"]] == [r["caller"] for r in rows["direct"]]


class _SelSpy:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_api_access(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_a_boundary_refusal_records_forwarded_beside_the_kept_suffix(monkeypatch) -> None:
    """``_audit_denied`` rows gain the field; the ``_via_proxy`` suffix is unchanged."""
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard import server as server_mod

    @web.middleware
    async def refuses(request: web.Request, handler: object) -> web.StreamResponse:
        raise web.HTTPForbidden(text="no")

    seen: dict[str, dict] = {}
    for label, headers in (("forwarded", _FORWARDED_HEADERS), ("direct", {})):
        spy = _SelSpy()
        monkeypatch.setattr(server_mod, "sel", lambda s=spy: s)
        app = web.Application(
            middlewares=[server_mod._make_deny_audit_middleware("dashboard_user"), refuses]
        )
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/api/sessions", headers=headers)).status == 403
        (row,) = [c for c in spy.calls if c.get("outcome") == "denied"]
        seen[label] = row

    assert seen["forwarded"]["forwarded"] is True
    assert seen["forwarded"]["caller"] == "dashboard_user_via_proxy"
    assert seen["direct"]["forwarded"] is False
    assert seen["direct"]["caller"] == "dashboard_user"


# Writers whose SEL rows are written for an HTTP request. A new
# ``log_api_access`` call inside a function that has the request in hand must
# pass ``forwarded=``; one written without a request (start-up, revoke-all)
# is outside the rule and stays keyless.
_REQUEST_BOUND_WRITERS = (
    "dashboard/token_auth.py",
    "dashboard/token_auth_paths.py",
    "dashboard/token_auth_app_scope.py",
    "dashboard/token_auth_callers.py",
    "dashboard/server_runtime/middleware_chain.py",
    "dashboard/server_runtime/security_middleware.py",
)


def _calls_missing_forwarded(path: Path) -> tuple[int, list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def has_request_in_scope(node: ast.AST) -> bool:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = cur.args
                names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
                if "request" in names:
                    return True
            cur = parents.get(cur)
        return False

    checked = 0
    missing: list[int] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "log_api_access"
        ):
            continue
        if not has_request_in_scope(node):
            continue
        checked += 1
        if not any(kw.arg == "forwarded" for kw in node.keywords):
            missing.append(node.lineno)
    return checked, missing


@pytest.mark.parametrize("rel", _REQUEST_BOUND_WRITERS)
def test_every_request_bound_sel_writer_records_forwarded(rel: str) -> None:
    """Exhaustive over each writer module: no request-bound row can skip the field."""
    path = Path(kiro_crew.__file__).parent / rel
    checked, missing = _calls_missing_forwarded(path)
    assert checked, f"{rel}: found no request-bound log_api_access call; the walk is broken"
    assert not missing, f"{rel}: log_api_access without forwarded= at lines {missing}"


class _RealHelperCapture(_Capture):
    """Runs the real ``log_api_access`` body and captures the event it builds."""

    log_api_access = SecurityEventLog.log_api_access


def _boundary_app(monkeypatch, cap: _RealHelperCapture, handler) -> web.Application:
    from kiro_crew.dashboard import server as server_mod

    monkeypatch.setattr(server_mod, "sel", lambda: cap)
    app = web.Application(middlewares=[server_mod._make_deny_audit_middleware("dashboard_user")])
    app.router.add_get("/api/x", handler)
    return app


@pytest.mark.asyncio
async def test_a_handler_row_records_forwarded_without_passing_it(monkeypatch) -> None:
    """A handler's own row takes the boundary's verdict, explicit values still win.

    Red before the boundary bound it: both rows carried no ``forwarded`` key.
    """
    from aiohttp.test_utils import TestClient, TestServer

    cap = _RealHelperCapture()

    async def handler(request: web.Request) -> web.Response:
        cap.log_api_access(caller="dashboard_user", operation="implicit", outcome="ok")
        cap.log_api_access(
            caller="dashboard_user", operation="explicit", outcome="ok", forwarded=False
        )
        return web.Response(text="ok")

    async with TestClient(TestServer(_boundary_app(monkeypatch, cap, handler))) as client:
        assert (await client.get("/api/x", headers=_FORWARDED_HEADERS)).status == 200
        assert (await client.get("/api/x")).status == 200

    rows = [(e.operation, e.metadata) for e in cap.events]
    assert rows == [
        ("implicit", {"forwarded": True}),
        ("explicit", {"forwarded": False}),
        ("implicit", {"forwarded": False}),
        ("explicit", {"forwarded": False}),
    ]


@pytest.mark.asyncio
async def test_provenance_lives_as_long_as_the_request(monkeypatch) -> None:
    """Awaited work inside the request records it; work outliving it stays keyless.

    ``to_thread`` and an awaited task copy the context and run inside the
    request. A task the handler leaves running copies the same context but writes
    after the response, and a plain thread copies no context at all.
    """
    import asyncio
    import threading

    from aiohttp.test_utils import TestClient, TestServer

    cap = _RealHelperCapture()
    release = asyncio.Event()
    background: list[asyncio.Task] = []

    def write(op: str) -> None:
        cap.log_api_access(caller="dashboard_user", operation=op, outcome="ok")

    async def late() -> None:
        await release.wait()
        write("task_after_response")

    async def handler(request: web.Request) -> web.Response:
        await asyncio.to_thread(write, "to_thread")
        await asyncio.create_task(asyncio.to_thread(write, "awaited_task"))
        thread = threading.Thread(target=write, args=("plain_thread",))
        thread.start()
        thread.join()
        background.append(asyncio.create_task(late()))
        return web.Response(text="ok")

    async with TestClient(TestServer(_boundary_app(monkeypatch, cap, handler))) as client:
        assert (await client.get("/api/x", headers=_FORWARDED_HEADERS)).status == 200
        release.set()
        await background[0]

    assert {e.operation: e.metadata for e in cap.events} == {
        "to_thread": {"forwarded": True},
        "awaited_task": {"forwarded": True},
        "plain_thread": {},
        "task_after_response": {},
    }


def test_the_binding_is_closed_and_restored_when_the_request_ends() -> None:
    """Nested binds restore the outer verdict; leaving the last one leaves none."""
    from kiro_crew.sel_request_provenance import (
        bind_request_forwarded,
        current_request_forwarded,
    )

    assert current_request_forwarded() is None
    with bind_request_forwarded(True):
        with bind_request_forwarded(False):
            assert current_request_forwarded() is False
        assert current_request_forwarded() is True
    assert current_request_forwarded() is None

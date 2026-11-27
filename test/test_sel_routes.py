"""``POST /api/apps/{name}/_sel``: the gateway writes an app backend's audit rows.

A spawned app backend must not hold the SEL MAC key, so it asks the gateway to
sign and append. These tests run real app tokens through the real
``token_auth_middleware`` and pin that:

* the row's actor comes from the verified token, never from the body;
* a cookie/owner session, another app's token and an unauthenticated call are
  refused with nothing written;
* a body that names ``caller`` / ``caller_identity`` / ``source`` is a 400;
* an app's own Route Registry cannot shadow the route;
* a per-app limiter and a failed write answer 429 / 500 and never echo input.

Every test goes through the aiohttp test client with real app tokens; none uses a
backend-side client.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.streams import StreamReader
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import sel_routes
from kiro_crew.apps.manager_records import InstalledApp, _write_installed, app_dir
from kiro_crew.apps.route_registry import RouteRegistry, _RegisteredRoute
from kiro_crew.dashboard import revocation_gen, token_auth
from kiro_crew.testing.wait import await_until


class _Clock:
    now = 1_000_000.0


_CLOCK = _Clock()
_A = "sel-app-a"
_B = "sel-app-b"
_OK = {"method": "log_api_access", "kwargs": {"operation": "a.b", "outcome": "ok"}}


class _Rec:
    """Stands in for the real log: records calls, can be told to fail."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def _row(self, name: str, kw: dict[str, Any]) -> None:
        if self.fail:
            raise OSError("disk full: /secret/path")
        self.rows.append((name, kw))

    def log_api_access(self, **kw: Any) -> None:
        self._row("log_api_access", kw)

    def log_tool_invocation(self, **kw: Any) -> None:
        self._row("log_tool_invocation", kw)

    def log_governance_decision(self, **kw: Any) -> None:
        self._row("log_governance_decision", kw)

    def app_rows(self) -> list[tuple[str, dict[str, Any]]]:
        return [r for r in self.rows if r[1].get("source") == "app"]


@pytest.fixture
def rec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rec:
    # One injected clock drives the token mint and the app record's cutoff, so their order is
    # set by the test, never by timer resolution (a coarse Windows clock can make them equal).
    fake = types.ModuleType("fake_time")
    fake.__dict__.update(time.__dict__)
    fake.time = lambda: _CLOCK.now  # type: ignore[attr-defined]
    fake.monotonic = lambda: _CLOCK.now  # type: ignore[attr-defined]
    _CLOCK.now = 1_000_000.0
    monkeypatch.setattr("kiro_crew.dashboard.token_auth.time", fake)
    monkeypatch.setattr("kiro_crew.apps.manager_records.time", fake)
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.apps.manager_records.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"sel-routes-test-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    for name in (_A, _B):
        _write_installed(name, InstalledApp(name=name, version="1.0.0", enabled=True))
    r = _Rec()
    monkeypatch.setattr(sel_routes, "sel", lambda: r)
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: r)
    sel_routes._buckets.clear()
    return r


@contextlib.asynccontextmanager
async def _client(*middlewares: Any, registry: bool = False):
    app = web.Application(middlewares=list(middlewares) or [token_auth.token_auth_middleware()])
    sel_routes.register_sel_routes(app)
    if registry:
        RouteRegistry(app).ensure_catch_all()
    async with TestClient(TestServer(app)) as client:
        yield client


async def _post(client: TestClient, name: str, token: str | None, body: Any = _OK) -> int:
    params = {"token": token} if token else None
    resp = await client.post(f"/api/apps/{name}/_sel", params=params, json=body)
    return resp.status


def _tok(name: str) -> str:
    _CLOCK.now += 1.0  # strictly after the record the fixture wrote
    return token_auth.generate_token(name, app=name)


@pytest.mark.asyncio
async def test_actor_comes_from_the_token_and_the_row_reaches_the_log(rec) -> None:
    body = {"method": "log_api_access", "kwargs": {**_OK["kwargs"], "resources": "r"}}
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), body) == 204
    assert rec.app_rows() == [
        (
            "log_api_access",
            {
                "caller": f"app:{_A}",
                "source": "app",
                "operation": f"{_A}.a.b",
                "outcome": "ok",
                "resources": "r",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["log_tool_invocation", "log_governance_decision"])
async def test_an_app_cannot_write_the_gateways_security_event_kinds(rec, method) -> None:
    body = {"method": method, "kwargs": {"tool_name": "t", "outcome": "denied"}}
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), body) == 400
    assert rec.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["publish", "internal_auth", f"{_A}.publish"])
async def test_the_operation_is_kept_under_the_apps_own_namespace(rec, op) -> None:
    body = {"method": "log_api_access", "kwargs": {"operation": op, "outcome": "ok"}}
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), body) == 204
    assert rec.app_rows()[0][1]["operation"] == f"{_A}.{op.removeprefix(_A + '.')}"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["disable", "uninstall"])
async def test_a_token_minted_before_disable_or_uninstall_stops_working(rec, stop) -> None:
    token = _tok(_A)
    async with _client() as client:
        assert await _post(client, _A, token) == 204
        if stop == "disable":
            _write_installed(_A, InstalledApp(name=_A, version="1.0.0", enabled=False))
        else:
            (app_dir(_A) / "installed.json").unlink()
        assert await _post(client, _A, token) == 403
    assert len(rec.app_rows()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["caller", "caller_identity", "source", "session_key", "agent", "actor", "app", "caller_kind"],
)
async def test_a_body_that_names_an_identity_field_is_a_400(rec, key) -> None:
    async with _client() as client:
        nested = {"method": "log_api_access", "kwargs": {**_OK["kwargs"], key: "owner"}}
        assert await _post(client, _A, _tok(_A), nested) == 400
        assert await _post(client, _A, _tok(_A), {**_OK, key: "owner"}) == 400
    assert rec.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"method": "log_other", "kwargs": {}},
        {"method": "log_api_access", "kwargs": {"operation": "o", "outcome": "ok", "x": "y"}},
        {"method": "log_api_access", "kwargs": {"operation": 5, "outcome": "ok"}},
        {"method": "log_api_access", "kwargs": {"operation": "o"}},
        {"method": "log_api_access", "kwargs": {"operation": "o", "outcome": "ok", "critical": 1}},
        ["not", "an", "object"],
        {"method": "log_api_access", "kwargs": {"operation": "x" * 201, "outcome": "ok"}},
    ],
)
async def test_a_malformed_body_is_a_400_and_writes_nothing(rec, body) -> None:
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), body) == 400
    assert rec.rows == []


@pytest.mark.asyncio
async def test_a_deeply_nested_body_is_a_400_not_a_500(rec) -> None:
    async with _client() as client:
        resp = await client.post(
            f"/api/apps/{_A}/_sel", params={"token": _tok(_A)}, data=b"[" * 15000
        )
        assert resp.status == 400
    assert rec.rows == []


@pytest.mark.asyncio
async def test_an_oversize_body_is_a_413(rec) -> None:
    big = {"method": "log_api_access", "kwargs": {**_OK["kwargs"], "resources": "x" * 20000}}
    async with _client() as client:
        assert await _post(client, _A, _tok(_A), big) == 413
    assert rec.rows == []


class _Body:
    """A real aiohttp StreamReader, plus a signal for "the consumer came back for more"."""

    def __init__(self) -> None:
        self.reader = StreamReader(mock.Mock(_reading_paused=False), 2**16)
        self.wants_more = asyncio.Event()

    async def read(self, n: int) -> bytes:
        return await self.reader.read(n)

    async def iter_chunked(self, n: int):
        async for chunk in self.reader.iter_chunked(n):
            yield chunk
            self.wants_more.set()


@pytest.mark.asyncio
async def test_a_body_that_arrives_in_several_chunks_is_read_whole() -> None:
    body = _Body()
    task = asyncio.ensure_future(sel_routes._read_capped(SimpleNamespace(content=body)))
    try:
        body.reader.feed_data(b'{"method"')
        # A single read() would have returned the first chunk by now; the loop waits for the rest.
        await await_until(body.wants_more, what="the reader asking for the next chunk")
        assert not task.done()
        body.reader.feed_data(b': "x"}')
        body.reader.feed_eof()
        assert await await_until(task, what="the whole body") == b'{"method": "x"}'
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_body_over_the_cap_is_refused_as_soon_as_it_passes_it() -> None:
    body = _Body()
    task = asyncio.ensure_future(sel_routes._read_capped(SimpleNamespace(content=body)))
    try:
        body.reader.feed_data(b"x" * 10000)
        body.reader.feed_data(b"x" * 10000)
        body.reader.feed_eof()  # a regressed cap would return the body, not wait for more
        assert await await_until(task, what="the over-cap refusal") is None
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_an_owner_token_is_refused(rec) -> None:
    async with _client() as client:
        assert await _post(client, _A, token_auth.generate_token("owner")) == 403
    assert rec.app_rows() == []


@pytest.mark.asyncio
async def test_a_cookie_session_with_no_app_claim_is_refused(rec) -> None:
    @web.middleware
    async def _cookie_session(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = ""  # what the middleware stamps on a dashboard cookie
        return await handler(request)

    async with _client(_cookie_session) as client:
        assert await _post(client, _A, None) == 403
    assert rec.app_rows() == []


@pytest.mark.asyncio
async def test_another_apps_token_is_refused(rec) -> None:
    async with _client() as client:
        assert await _post(client, _B, _tok(_A)) == 403
        assert await _post(client, _A, _tok(_B)) == 403
    assert rec.app_rows() == []


@pytest.mark.asyncio
async def test_the_handler_itself_refuses_a_token_for_another_namespace(rec) -> None:
    @web.middleware
    async def _as_a(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = _A  # the middleware passed it; the handler must still compare
        return await handler(request)

    async with _client(_as_a) as client:
        assert await _post(client, _B, None) == 403
    assert rec.app_rows() == []


@pytest.mark.asyncio
async def test_no_credential_is_refused(rec) -> None:
    async with _client() as client:
        assert await _post(client, _A, None) in (401, 403)
    assert rec.app_rows() == []


@pytest.mark.asyncio
async def test_an_apps_own_route_registry_cannot_shadow_the_route(rec) -> None:
    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    sel_routes.register_sel_routes(app)
    registry = RouteRegistry(app)

    async def _hijack(request: web.Request, *_a: Any) -> web.Response:
        return web.Response(status=418)

    hijack = _RegisteredRoute(method="POST", path="/_sel", handler=_hijack, has_params=False)
    registry._routes[_A] = [hijack]
    registry.ensure_catch_all()
    async with TestClient(TestServer(app)) as client:
        assert await _post(client, _A, _tok(_A)) == 204


@pytest.mark.asyncio
async def test_the_limiter_is_per_app_and_refills(rec, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(sel_routes, "_now", lambda: clock[0])
    monkeypatch.setattr(sel_routes, "_BURST", 2)
    async with _client() as client:
        assert [await _post(client, _A, _tok(_A)) for _ in range(3)] == [204, 204, 429]
        assert await _post(client, _B, _tok(_B)) == 204
        clock[0] += 1.0
        assert await _post(client, _A, _tok(_A)) == 204
    assert len(rec.app_rows()) == 4  # the refused call never reached the log, so no rotation


@pytest.mark.asyncio
async def test_a_failed_write_is_a_500_that_echoes_nothing(rec) -> None:
    rec.fail = True
    async with _client() as client:
        for critical in (False, True):
            kwargs = {**_OK["kwargs"], "critical": critical}
            resp = await client.post(
                f"/api/apps/{_A}/_sel",
                params={"token": _tok(_A)},
                json={"method": "log_api_access", "kwargs": kwargs},
            )
            assert resp.status == 500
            assert "secret" not in await resp.text()

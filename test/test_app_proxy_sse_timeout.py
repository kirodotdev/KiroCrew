"""A proxied server-sent-event stream must outlive the ordinary request timeout.

``handle_app_api_proxy`` bounds an ordinary request at ``_PROXY_TIMEOUT``. Handed
to aiohttp as ``ClientTimeout(total=...)`` that clock also covers reading the
response body, and a stream's body does not end, so it cuts the stream mid-body --
which reaches the browser as a truncated chunked response
(``ERR_INVALID_CHUNKED_ENCODING``) rather than as an error. Only the two
subprocess-backend apps are proxied, so no in-process app can show this.

Both tests below drive the REAL handler against a REAL backend server over
loopback, with ``_PROXY_TIMEOUT`` shortened so the assertions do not take half a
minute:

* the stream survives well past the total bound, and every event arrives;
* a slow NON-stream response is still cut at that bound, so lifting the bound for
  streams does not abolish it.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import routes

#: Short enough to keep the tests fast, long enough that a slow loopback hop
#: cannot be mistaken for the bound firing.
_BOUND = 0.4

#: Events the backend emits, spaced so the stream is still open after the bound.
_EVENTS = 6
_EVENT_GAP = _BOUND / 2


@pytest.mark.parametrize(
    "content_type",
    ["text/event-stream", "text/event-stream; charset=utf-8", "TEXT/EVENT-STREAM"],
)
def test_charset_does_not_hide_the_stream(content_type: str) -> None:
    """A parameter or different case must not make the total bound apply again."""
    assert routes._is_event_stream(content_type)


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "text/plain", "", "text/event-stream-ish"],
)
def test_only_event_stream_lifts_the_bound(content_type: str) -> None:
    assert not routes._is_event_stream(content_type)


async def _backend() -> web.Application:
    """An app backend: one event stream, one slow ordinary response."""

    async def stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for index in range(_EVENTS):
            await resp.write(f"data: {index}\n\n".encode())
            await asyncio.sleep(_EVENT_GAP)
        await resp.write_eof()
        return resp

    async def slow_headers(request: web.Request) -> web.Response:
        # Delays the RESPONSE, not the body: nothing is sent, so the proxy has
        # prepared nothing and can still answer with a status of its own. That is
        # the cleanly observable form of the ordinary total bound. A backend that
        # delays mid-BODY is deliberately NOT asserted here: the proxy has already
        # sent headers by then, so the cut can only reach the client as a truncated
        # chunked response, which is a separate shortcoming of the relay and not
        # what the bound's presence is being checked against.
        await asyncio.sleep(_BOUND * 6)
        return web.json_response({"late": True})

    app = web.Application()
    app.router.add_get("/api/stream", stream)
    app.router.add_get("/api/slow-headers", slow_headers)
    return app


async def _gateway(monkeypatch, backend_base: str) -> web.Application:
    """A gateway carrying the real proxy handler, with its lookups stubbed.

    Only the three lookups that reach installed state are replaced -- enablement,
    backend address, and the signing secret. The timeout logic under test, the
    signing, the header filtering and the body relay are all the real code.
    """
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    monkeypatch.setattr(routes, "_resolve_app_backend_url", lambda name: backend_base)
    monkeypatch.setattr(routes, "_get_app_secret", lambda name: "test-secret")
    monkeypatch.setattr(routes, "_PROXY_TIMEOUT", _BOUND)

    app = web.Application()
    app.router.add_route("*", "/apps/{name}/api/{path:.*}", routes.handle_app_api_proxy)
    return app


@pytest.mark.asyncio
async def test_a_proxied_event_stream_outlives_the_total_bound(monkeypatch) -> None:
    backend = TestServer(await _backend())
    await backend.start_server()
    try:
        base = f"http://127.0.0.1:{backend.port}"
        gateway = TestClient(TestServer(await _gateway(monkeypatch, base)))
        await gateway.start_server()
        try:
            resp = await gateway.get("/apps/demo/api/stream")
            assert resp.status == 200
            assert routes._is_event_stream(resp.headers["Content-Type"])
            received = []
            # Bound the test itself. A relay cut mid-body leaves a truncated
            # chunked response that never terminates, so an unbounded read here
            # would hang CI rather than fail it -- and that hang is exactly the
            # browser-visible symptom (ERR_INVALID_CHUNKED_ENCODING).
            deadline = _EVENTS * _EVENT_GAP + _BOUND * 4
            async with asyncio.timeout(deadline):
                async for line in resp.content:
                    text = line.decode().strip()
                    if text.startswith("data:"):
                        received.append(text)
            assert received == [f"data: {index}" for index in range(_EVENTS)]
        finally:
            await gateway.close()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_a_slow_non_stream_response_is_still_cut(monkeypatch) -> None:
    """The negative control: the bound is lifted for streams, not abolished.

    A 504 rather than the backend's payload is the whole assertion. Without the
    total bound this request would wait ``_BOUND * 6``, and the elapsed check
    below fails on that even if the status somehow matched.
    """
    backend = TestServer(await _backend())
    await backend.start_server()
    try:
        base = f"http://127.0.0.1:{backend.port}"
        gateway = TestClient(TestServer(await _gateway(monkeypatch, base)))
        await gateway.start_server()
        try:
            started = asyncio.get_running_loop().time()
            resp = await gateway.get("/apps/demo/api/slow-headers")
            elapsed = asyncio.get_running_loop().time() - started
            assert resp.status == 504, await resp.text()
            assert (await resp.json())["error"] == "backend timeout"
            assert elapsed < _BOUND * 4, f"cut took {elapsed:.2f}s, bound is {_BOUND}s"
        finally:
            await gateway.close()
    finally:
        await backend.close()

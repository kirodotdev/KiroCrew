"""A proxied server-sent-event stream must outlive the ordinary request timeout.

``handle_app_api_proxy`` bounds an ordinary request at ``_PROXY_TIMEOUT``. Handed
to aiohttp as ``ClientTimeout(total=...)`` that clock also covers reading the
response body, and a stream's body does not end, so it cuts the stream mid-body --
which reaches the browser as a truncated chunked response
(``ERR_INVALID_CHUNKED_ENCODING``) rather than as an error. Only the two
subprocess-backend apps are proxied, so no in-process app can show this.

The loopback tests below drive the REAL handler against a REAL backend server,
with ``_PROXY_TIMEOUT`` (and, where a test needs it, ``_PROXY_IDLE_TIMEOUT``)
shortened so the assertions do not take half a minute:

* the stream survives well past the total bound, and every event arrives;
* a slow NON-stream response is still cut at that bound, so lifting the bound for
  streams does not abolish it;
* a stream whose backend goes silent is cut at the idle bound. Once the total is
  lifted, ``sock_read`` is the only thing that ends a stalled stream, so this is
  the guard that keeps a hung backend from holding the gateway, its upstream
  socket and a pool slot for as long as the browser tab stays open;
* a cut that lands after the head has been relayed reaches the client as a
  closed, unterminated body -- never as a second ``HTTP/1.1 5xx`` head written
  into the body of the 200 already sent, and never on a connection left open.
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

#: The idle bound under test. Shortened only in the test that needs it, so the
#: live stream keeps the real 60s and its own pauses can never trip the guard.
_IDLE_BOUND = _BOUND * 2

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


#: Longest the stalled backend waits for the gateway to drop it before giving up.
#: Far past the idle bound, so a gateway that never cuts is seen as such rather
#: than the backend simply finishing on its own.
_STALL = _IDLE_BOUND * 5

#: How often the stalled backend looks at its own connection.
_STALL_TICK = _IDLE_BOUND / 20


async def _backend() -> web.Application:
    """An app backend: an event stream, a slow ordinary response, a stalled stream."""

    async def stream(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for index in range(_EVENTS):
            await resp.write(f"data: {index}\n\n".encode())
            await asyncio.sleep(_EVENT_GAP)
        await resp.write_eof()
        return resp

    async def stall(request: web.Request) -> web.StreamResponse:
        # One event, then silence. The backend never writes again, so it cannot
        # learn of the cut from a failed write. It sees the drop one of two ways:
        # ``TestServer`` runs with ``handler_cancellation`` on, so the lost
        # connection cancels this handler; a server without it leaves the handler
        # running with a closed transport, which the loop watches for. Either
        # way the moment is recorded. The BACKEND side is the observable here:
        # the client has already been sent a 200 by then, so what the cut looks
        # like there is the relay's business, not the idle bound's.
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b"data: 0\n\n")
        loop = asyncio.get_running_loop()
        started = loop.time()
        dropped: asyncio.Future[float | None] = request.app["dropped"]
        try:
            while loop.time() - started < _STALL:
                transport = request.transport
                if transport is None or transport.is_closing():
                    dropped.set_result(loop.time() - started)
                    return resp
                await asyncio.sleep(_STALL_TICK)
        except asyncio.CancelledError:
            dropped.set_result(loop.time() - started)
            raise
        dropped.set_result(None)
        return resp

    async def slow_headers(request: web.Request) -> web.Response:
        # Delays the RESPONSE, not the body: nothing is sent, so the proxy has
        # prepared nothing and can still answer with a status of its own. That is
        # the cleanly observable form of the ordinary total bound. The mid-BODY
        # form is ``slow_body`` below, checked on the wire.
        await asyncio.sleep(_BOUND * 6)
        return web.json_response({"late": True})

    async def slow_body(request: web.Request) -> web.StreamResponse:
        # An ORDINARY response that sends its head and part of its body, then
        # stalls: the total bound fires after ``resp.prepare()`` on the gateway.
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        await resp.write(b'{"partial": ')
        await asyncio.sleep(_STALL)
        await resp.write(b"true}")
        await resp.write_eof()
        return resp

    app = web.Application()
    app["dropped"] = asyncio.get_running_loop().create_future()
    app.router.add_get("/api/stream", stream)
    app.router.add_get("/api/stall", stall)
    app.router.add_get("/api/slow-headers", slow_headers)
    app.router.add_get("/api/slow-body", slow_body)
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


@pytest.mark.asyncio
async def test_a_silent_event_stream_is_cut_at_the_idle_bound(monkeypatch) -> None:
    """The guard that replaces the total bound for streams.

    With the total lifted, ``sock_read=_PROXY_IDLE_TIMEOUT`` is all that ends a
    stream whose backend has gone quiet. Set it to ``None`` and this test is the
    one that goes red: the backend then keeps the connection for the whole
    ``_STALL`` and reports it was never dropped.

    Observed on the BACKEND side on purpose. By the time the cut fires, the
    gateway has already sent the client a 200, so what the client sees is the
    relay's mid-body failure shape and not this guard.
    """
    monkeypatch.setattr(routes, "_PROXY_IDLE_TIMEOUT", _IDLE_BOUND)
    backend_app = await _backend()
    backend = TestServer(backend_app)
    await backend.start_server()
    try:
        base = f"http://127.0.0.1:{backend.port}"
        gateway = TestClient(TestServer(await _gateway(monkeypatch, base)))
        await gateway.start_server()
        try:
            resp = await gateway.get("/apps/demo/api/stall")
            assert resp.status == 200
            assert routes._is_event_stream(resp.headers["Content-Type"])
            # Backstop for the test itself: with the guard gone the backend only
            # gives up after ``_STALL``, and this must fail rather than hang.
            async with asyncio.timeout(_STALL * 2):
                held_for = await backend_app["dropped"]
            assert (
                held_for is not None
            ), f"gateway held the silent upstream for the full {_STALL:.1f}s stall"
            assert (
                held_for < _IDLE_BOUND * 3
            ), f"cut took {held_for:.2f}s, idle bound is {_IDLE_BOUND}s"
            # It also must not have been cut by the total bound reappearing: the
            # first event arrives at once, so the total would have fired at
            # ``_BOUND``, well before the idle bound elapses.
            assert (
                held_for >= _IDLE_BOUND * 0.8
            ), f"cut after {held_for:.2f}s, before the idle bound of {_IDLE_BOUND}s"
        finally:
            await gateway.close()
    finally:
        await backend.close()


async def _raw_get(port: int, path: str, deadline: float) -> tuple[bytes, bool]:
    """Fetch ``path`` over a bare socket; return the bytes and whether EOF came.

    A real HTTP client would parse the second head out of the body as garbage or
    hide it behind a framing error; the wire is the only place the splice is
    visible as what it is. ``deadline`` bounds the read: a connection the
    gateway leaves open returns ``(bytes, False)`` rather than hanging the test.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: */*\r\n\r\n".encode())
        await writer.drain()
        raw = bytearray()
        try:
            async with asyncio.timeout(deadline):
                while chunk := await reader.read(65536):
                    raw += chunk
        except TimeoutError:
            return bytes(raw), False
        return bytes(raw), True
    finally:
        writer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/apps/demo/api/stall", id="stream-idle-cut"),
        pytest.param("/apps/demo/api/slow-body", id="ordinary-total-cut"),
    ],
)
async def test_a_mid_body_cut_closes_the_body_instead_of_writing_a_second_head(
    monkeypatch, path: str
) -> None:
    """Either bound firing after the head is out ends the transfer honestly.

    Before the fix both cases put ``HTTP/1.1 502`` (idle cut) or ``504`` (total
    cut) INTO the chunked body of the 200 already sent, then held the connection
    open on keep-alive. The two things asserted here are the two the browser
    sees: exactly one head on the wire, and the connection closed -- with no
    terminating chunk, so the body reads as cut rather than as complete.
    """
    monkeypatch.setattr(routes, "_PROXY_IDLE_TIMEOUT", _IDLE_BOUND)
    backend = TestServer(await _backend())
    await backend.start_server()
    try:
        base = f"http://127.0.0.1:{backend.port}"
        gateway = TestServer(await _gateway(monkeypatch, base))
        await gateway.start_server()
        try:
            raw, closed = await _raw_get(gateway.port, path, deadline=_STALL)
            assert raw.startswith(b"HTTP/1.1 200 "), raw[:80]
            assert raw.count(b"HTTP/1.1 ") == 1, f"second head spliced into body: {raw!r}"
            assert closed, "gateway left the cut connection open"
            # The body was under way when the cut landed...
            assert b"data: 0" in raw or b'{"partial": ' in raw, raw
            # ...and is left unterminated: no final zero-length chunk.
            assert not raw.endswith(b"\r\n0\r\n\r\n"), f"cut body was terminated: {raw!r}"
        finally:
            await gateway.close()
    finally:
        await backend.close()

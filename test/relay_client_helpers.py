"""A Python fake of the Kiro relay's host leg, driven by the protocol v1 fixtures.

``FakeRelay`` serves ``/v1/host`` on loopback port 0. It answers the upgrade with a
plain 401 unless the ``Authorization: Bearer`` token is one it accepts, and hands
each accepted connection to the test as a ``FakeHostLeg`` the test scripts frame by
frame. ``FakeCrew`` stands in for the Crew's own web server. ``fixture`` loads a
copied fixture from ``test/fixtures/relay_protocol_v1``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from kiro_crew.dashboard.kiro_relay.caller import published_relay_caller
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.relay_client.client import RelayClient
from kiro_crew.relay_client.connection import RelayTimings
from kiro_crew.relay_client.ingress import LoopbackIngress
from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES
from kiro_crew.relay_client.token_source import HostCredential, StaticHostTokenSource
from kiro_crew.testing.wait import await_until

FIXTURES = Path(__file__).parent / "fixtures" / "relay_protocol_v1"
TOKEN = "eyJ0eXAiOiJraXJvLXJlbGF5LWhvc3Qrand0In0.eyJ0ZXN0IjoxfQ.c2ln"
#: The owner the request fixtures name, so a personal Crew honours them.
OWNER = "u-0001.example-owner"
CONTRACT = "Kiro Relay protocol v1"


def fixture(name: str) -> Dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def valid_message(name: str, index: int = 0, **overrides: Any) -> Dict[str, Any]:
    """A copy of one fixture's ``valid`` message, with *overrides* applied."""
    message = copy.deepcopy(fixture(name)["valid"][index]["message"])
    message.update(overrides)
    return message


def ready_frame() -> Dict[str, Any]:
    return valid_message("frames/ready")


class TopRng(random.Random):
    """An RNG whose ``uniform`` always answers the top of the range."""

    def uniform(self, a: float, b: float) -> float:
        return b


class RecordingSleep:
    """A sleep that returns at once and records each delay it was asked for."""

    def __init__(self) -> None:
        self.delays: List[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


class FakeHostLeg:
    """One host connection, as the fake relay sees it."""

    def __init__(self, request: web.Request, ws: web.WebSocketResponse, token: str) -> None:
        self.request = request
        self.ws = ws
        self.token = token
        self.user_agent = request.headers.get("User-Agent", "")
        self.inbox: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self.closed = asyncio.Event()
        self.close_code: Optional[int] = None

    async def serve(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type == WSMsgType.TEXT:
                    await self.inbox.put(json.loads(msg.data))
        finally:
            self.close_code = self.ws.close_code
            self.closed.set()

    async def recv(self, *, skip_keepalive: bool = True) -> Dict[str, Any]:
        while True:
            frame = await await_until(asyncio.ensure_future(self.inbox.get()), what="a relay frame")
            if skip_keepalive and frame.get("type") in ("ping", "pong"):
                continue
            return frame

    async def recv_type(self, kind: str) -> Dict[str, Any]:
        frame = await self.recv(skip_keepalive=kind not in ("ping", "pong"))
        assert frame["type"] == kind, f"expected {kind}, got {frame}"
        return frame

    async def send(self, frame: Dict[str, Any]) -> None:
        await self.ws.send_str(json.dumps(frame))

    async def handshake(self, ready: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        hello = await self.recv_type("hello")
        await self.send(ready or ready_frame())
        return hello

    async def close(self, code: int, reason: str = "") -> None:
        await self.ws.close(code=code, message=reason.encode("utf-8"))

    def abort(self) -> None:
        """Drop the TCP connection with no close frame."""
        transport = self.request.transport
        if transport is not None:
            transport.abort()

    async def wait_closed(self) -> Optional[int]:
        await await_until(self.closed, what="the host leg to close")
        return self.close_code

    async def read_response(self, sid: int) -> tuple[Dict[str, Any], bytes]:
        """The response head and the whole body of stream *sid*."""
        import base64

        head = await self.recv_type("response")
        assert head["id"] == sid
        body = b""
        while True:
            frame = await self.recv()
            assert frame.get("id") == sid, frame
            if frame["type"] == "response-end":
                return head, body
            assert frame["type"] == "response-chunk", frame
            body += base64.b64decode(frame["data"])


class FakeRelay:
    """The relay's host-leg endpoint, on loopback."""

    def __init__(self, *, tokens: Optional[set] = None) -> None:
        self.tokens = set(tokens or {TOKEN})
        self.legs: asyncio.Queue[FakeHostLeg] = asyncio.Queue()
        self.refused: List[str] = []
        self.dials = 0
        self._server: Optional[TestServer] = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"ws://127.0.0.1:{self._server.port}/v1/host"

    async def __aenter__(self) -> "FakeRelay":
        app = web.Application()
        app.router.add_get("/v1/host", self._host)
        self._server = TestServer(app, host="127.0.0.1", port=0)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            await self._server.close()

    async def _host(self, request: web.Request) -> web.StreamResponse:
        self.dials += 1
        auth = request.headers.get("Authorization", "")
        token = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
        if token not in self.tokens:
            self.refused.append(token)
            # The plain 401: no reason header, no WWW-Authenticate.
            return web.Response(status=401, text="Unauthorized")
        ws = web.WebSocketResponse(autoping=True)
        await ws.prepare(request)
        leg = FakeHostLeg(request, ws, token)
        await self.legs.put(leg)
        await leg.serve()
        return ws

    async def next_leg(self) -> FakeHostLeg:
        return await await_until(asyncio.ensure_future(self.legs.get()), what="a host connection")


class FakeCrew:
    """The Crew's own web server, behind the gateway's two real relay-header layers.

    In the gateway's order: the relay ingress middleware outermost, then the
    client-sent strip just inside it, so every relayed request a test sends crosses
    both before a handler sees it.
    """

    def __init__(self, *, canonical_redirect: bool = False) -> None:
        self.canonical_redirect = canonical_redirect
        self.gateway = RelayIngress(enabled=True)
        self.seen: List[Dict[str, Any]] = []
        self.release = asyncio.Event()
        self.stream_bytes = 200 * 1024
        self._server: Optional[TestServer] = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.port or 0

    async def __aenter__(self) -> "FakeCrew":
        app = web.Application(
            middlewares=[
                relay_ingress_middleware(strict_internal_paths=frozenset()),
                relay_header_strip_middleware,
            ]
        )
        if self.canonical_redirect:
            from kiro_crew.dashboard.server import build_host_canonical_redirect

            app.middlewares.append(build_host_canonical_redirect("localhost"))
            app.router.add_get("/", self._sessions)
        app[RELAY_INGRESS_APP_KEY] = self.gateway
        app.router.add_route("*", "/api/sessions", self._sessions)
        app.router.add_post("/upload", self._upload)
        app.router.add_get("/stream", self._stream)
        app.router.add_get("/slow", self._slow)
        app.router.add_get("/drop", self._drop)
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/ws-refused", self._refused)
        self._server = TestServer(app, host="127.0.0.1", port=0)
        await self._server.start_server()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            await self._server.close()

    def _record(self, request: web.Request) -> None:
        self.seen.append(
            {
                "path": request.path,
                "headers": dict(request.headers),
                "caller": published_relay_caller(request),
                "remote": request.remote,
            }
        )

    async def _sessions(self, request: web.Request) -> web.Response:
        self._record(request)
        response = web.json_response({"ok": True, "query": request.query_string})
        response.set_cookie("kc_session", "abc123", httponly=True)
        response.set_cookie("kc_theme", "dark")
        return response

    async def _upload(self, request: web.Request) -> web.Response:
        self._record(request)
        body = await request.read()
        return web.json_response({"bytes": len(body), "head": body[:16].decode("latin-1")})

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        self._record(request)
        response = web.StreamResponse(headers={"content-type": "application/octet-stream"})
        await response.prepare(request)
        sent = 0
        while sent < self.stream_bytes:
            piece = bytes([sent // 1024 % 251]) * min(16 * 1024, self.stream_bytes - sent)
            await response.write(piece)
            sent += len(piece)
        await response.write_eof()
        return response

    async def _slow(self, request: web.Request) -> web.Response:
        self._record(request)
        await self.release.wait()
        return web.Response(text="finished")

    async def _drop(self, request: web.Request) -> web.Response:
        """Drop the connection with no response head at all."""
        self._record(request)
        transport = request.transport
        assert transport is not None
        transport.abort()
        await self.release.wait()
        return web.Response(text="never sent")

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        self._record(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await ws.send_bytes(msg.data)
        return ws

    async def _refused(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.Response(status=403, text="no")


def make_client(
    relay: FakeRelay,
    crew: "FakeCrew | int",
    *,
    source: Optional[StaticHostTokenSource] = None,
    timings: Optional[RelayTimings] = None,
    sleep: Any = None,
    rng: Optional[random.Random] = None,
    monotonic: Any = None,
    crew_url: Optional[str] = None,
) -> tuple[RelayClient, StaticHostTokenSource, LoopbackIngress]:
    source = source or StaticHostTokenSource(
        HostCredential(TOKEN, crew_url=crew_url, crew_kind="personal", owner_user_id=OWNER)
    )
    if isinstance(crew, FakeCrew):
        port, gateway = crew.port, crew.gateway
    else:
        port, gateway = crew, RelayIngress(enabled=True)
    ingress = LoopbackIngress(
        "127.0.0.1", port, gateway=gateway, max_ws_message_bytes=WS_MESSAGE_MAX_BYTES
    )
    kwargs: Dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    client = RelayClient(
        host_url=relay.url,
        crew_url_for=lambda ep: f"https://{ep}.kiro-relay.us-east-1.on.aws",
        token_source=source,
        ingress=ingress,
        client_version="0.0.0-test",
        timings=timings or RelayTimings(),
        rng=rng or TopRng(),
        **kwargs,
    )
    return client, source, ingress


async def stop(client: RelayClient, ingress: LoopbackIngress) -> None:
    await await_until(asyncio.ensure_future(client.stop()), what="the relay client to stop")
    await ingress.aclose()

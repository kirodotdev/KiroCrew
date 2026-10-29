"""The loopback hand-off names a Host the gateway's Host check allows, on any loopback bind.

The relay client dials the address the gateway bound, which can be any loopback
address (``KIROCREW_BIND=127.0.0.2``). The gateway's Host check allows only the
canonical loopback names plus what ``dashboard.url`` names, so the hand-off sends
the canonical loopback host of the bound address's family. That Host grants
nothing: a relayed request keeps its relay identity and stays remote.
"""

from __future__ import annotations

import socket
from typing import Any, Dict, List

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from relay_client_helpers import OWNER

from kiro_crew.dashboard.kiro_relay.caller import RELAY_REMOTE, published_relay_caller
from kiro_crew.dashboard.kiro_relay.ingress import (
    RELAY_INGRESS_APP_KEY,
    RelayIngress,
    relay_ingress_middleware,
)
from kiro_crew.dashboard.origin import check_host, is_direct_local_request
from kiro_crew.dashboard.relay_headers import relay_header_strip_middleware
from kiro_crew.dashboard.urls import build_allowed_origins
from kiro_crew.relay_client.ingress import LoopbackIngress, RelayedRequest, RelayedWebSocket
from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES

pytestmark = pytest.mark.asyncio

#: A path the gateway keeps to local callers; the relay ingress refuses it to a relayed one.
_LOCAL_ONLY_PATH = "/api/internal/local-only"


def _family_available(address: str) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family) as probe:
            probe.bind((address, 0))
    except OSError:
        return False
    return True


class _Gateway:
    """The gateway's Host check and relay layers, in its order, bound to *address*."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.ingress = RelayIngress(enabled=True)
        self.seen: List[Dict[str, Any]] = []
        self._server: TestServer | None = None

    async def __aenter__(self) -> "_Gateway":
        @web.middleware
        async def host_check(request: web.Request, handler: Any) -> web.StreamResponse:
            if not check_host(request):
                return web.json_response({"error": "host not allowed"}, status=403)
            return await handler(request)

        app = web.Application(
            middlewares=[
                relay_ingress_middleware(strict_internal_paths=frozenset({_LOCAL_ONLY_PATH})),
                relay_header_strip_middleware,
                host_check,
            ]
        )
        app[RELAY_INGRESS_APP_KEY] = self.ingress
        app.router.add_get("/api/sessions", self._sessions)
        app.router.add_get(_LOCAL_ONLY_PATH, self._sessions)
        app.router.add_get("/ws", self._ws)
        self._server = TestServer(app, host=self.address, port=0)
        await self._server.start_server()
        # The default dashboard.url names no host, so only the loopback names count.
        app["allowed_origins"] = build_allowed_origins(self.port, local_only=True)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._server is not None:
            await self._server.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.port or 0

    def _record(self, request: web.Request) -> None:
        self.seen.append(
            {
                "host": request.headers.get("Host"),
                "remote": request.remote,
                "caller": published_relay_caller(request),
                "direct_local": is_direct_local_request(request),
            }
        )

    async def _sessions(self, request: web.Request) -> web.Response:
        self._record(request)
        return web.json_response({"ok": True})

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        self._record(request)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str(msg.data)
        return ws


def _handoff(gateway: _Gateway) -> LoopbackIngress:
    ingress = LoopbackIngress(
        gateway.address,
        gateway.port,
        gateway=gateway.ingress,
        max_ws_message_bytes=WS_MESSAGE_MAX_BYTES,
    )
    ingress.bind_host(crew_kind="personal", owner_user_id=OWNER)
    return ingress


def _get(path: str) -> RelayedRequest:
    return RelayedRequest(method="GET", path=path, query="", headers=(), user=OWNER)


@pytest.mark.parametrize(
    ("address", "host"),
    [("127.0.0.2", "127.0.0.1"), ("::1", "[::1]")],
    ids=["v4-not-127.0.0.1", "v6"],
)
async def test_a_relayed_request_passes_the_host_check_on_any_loopback_bind(
    address: str, host: str
) -> None:
    if not _family_available(address):
        pytest.skip(f"{address} cannot be bound on this machine")
    async with _Gateway(address) as gateway:
        ingress = _handoff(gateway)
        try:
            response = await ingress.open_http(_get("/api/sessions"), None)
            try:
                assert response.status == 200
            finally:
                await response.aclose()
        finally:
            await ingress.aclose()
        assert gateway.seen[0]["host"] == f"{host}:{gateway.port}"


async def test_a_relayed_websocket_passes_the_host_check_on_a_non_default_loopback_bind() -> None:
    if not _family_available("127.0.0.2"):
        pytest.skip("127.0.0.2 cannot be bound on this machine")
    async with _Gateway("127.0.0.2") as gateway:
        ingress = _handoff(gateway)
        try:
            ws = await ingress.open_websocket(
                RelayedWebSocket(path="/ws", query="", protocols=(), headers=(), user=OWNER)
            )
            await ws.send("ping")
            assert await ws.receive() == "ping"
            await ws.close(1000)
        finally:
            await ingress.aclose()
        assert gateway.seen[0]["host"] == f"127.0.0.1:{gateway.port}"


async def test_the_canonical_host_grants_nothing_a_relayed_request_stays_remote() -> None:
    if not _family_available("127.0.0.2"):
        pytest.skip("127.0.0.2 cannot be bound on this machine")
    async with _Gateway("127.0.0.2") as gateway:
        ingress = _handoff(gateway)
        try:
            response = await ingress.open_http(_get("/api/sessions"), None)
            await response.aclose()
            refused = await ingress.open_http(_get(_LOCAL_ONLY_PATH), None)
            try:
                assert refused.status == 403
            finally:
                await refused.aclose()
        finally:
            await ingress.aclose()
        (served,) = gateway.seen  # the local-only path never reached its handler
        assert served["remote"] == RELAY_REMOTE
        assert served["caller"] is not None and served["caller"].kiro_user_id == OWNER
        assert served["direct_local"] is False

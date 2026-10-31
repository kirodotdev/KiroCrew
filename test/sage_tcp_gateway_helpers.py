"""Test gateways for the TCP-only peer challenge.

``RealTcpGateway`` serves the gateway's REAL ``/api/health`` handler (the one that
answers the challenge) from an aiohttp app on a loopback TCP port, in a thread of
its own, plus a ``/api/spawn`` route that records the ``X-Internal-Secret`` each
request carried, so a test can assert what reached the gateway.

``RawListener`` is a loopback listener that answers the challenge however a test
tells it to, and records every byte it received, so a test can assert an
impostor never saw the secret.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable

from aiohttp import web

from kiro_crew.dashboard.handlers import core as core_mod


class RealTcpGateway:
    """The real ``api_health`` handler, served over loopback TCP with *secret*."""

    def __init__(self, secret: str, port: int = 0) -> None:
        self.secret = secret
        self.received_secrets: list[str] = []
        self._requested_port = port
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._runner: web.AppRunner | None = None
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    async def _spawn(self, request: web.Request) -> web.Response:
        self.received_secrets.append(request.headers.get("X-Internal-Secret", ""))
        return web.json_response({"spawned": True})

    async def _start(self) -> int:
        app = web.Application()
        app["local_secret"] = self.secret
        app.router.add_get("/api/health", core_mod.api_health)
        app.router.add_get("/api/spawn", self._spawn)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._requested_port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        # The loopback origins a real gateway serves, so check_host passes for the
        # sender's ``Host: 127.0.0.1:<port>`` as it does in production.
        app["allowed_origins"] = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        return port

    def start(self) -> RealTcpGateway:
        self._thread.start()
        self.port = asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(30)
        return self

    def stop(self) -> None:
        if self._runner is not None:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(30)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)
        self._loop.close()


class RawListener:
    """A loopback listener whose answer to each connection is *respond*.

    *respond(conn)* runs once per accepted connection with the connected socket;
    it may read and write as it likes. Every byte read through :meth:`recv_all`
    is kept in :attr:`received`, so a test can assert what this listener saw.
    """

    def __init__(self, respond: Callable[[RawListener, socket.socket], None], port: int = 0):
        self._respond = respond
        self.received = b""
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def recv_head(self, conn: socket.socket) -> bytes:
        """Read one request head and keep it."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        self.received += data
        return data

    def drain(self, conn: socket.socket, seconds: float = 1.0) -> None:
        """Keep everything the client sends for up to *seconds*."""
        conn.settimeout(seconds)
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                self.received += chunk
        except OSError:
            return

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            try:
                conn.settimeout(10)
                self._respond(self, conn)
            except OSError:
                pass
            finally:
                conn.close()

    def start(self) -> RawListener:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            # shutdown() wakes a thread blocked in accept(); close() alone does not
            # on Linux.
            self._srv.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(5)


def http_response(headers: dict[str, str], body: bytes = b"{}", status: str = "200 OK") -> bytes:
    """A Content-Length framed HTTP/1.1 response."""
    lines = [f"HTTP/1.1 {status}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def challenge_nonce(head: bytes) -> str:
    """The nonce a challenge request head carries, or ``""``."""
    for line in head.decode("iso-8859-1").split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-gateway-peer-challenge":
            return value.strip()
    return ""

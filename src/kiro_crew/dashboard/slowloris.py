"""Slowloris mitigation for the aiohttp dashboard / API servers.

aiohttp (3.9 .. 3.x) exposes NO first-class server-side "time to read the
request line + headers" timeout. ``web.RequestHandler`` only offers
``keepalive_timeout`` (idle time *between* requests on a persistent
connection) and header *size* caps (``max_line_size`` / ``max_field_size`` /
``max_headers``) -- none of which bound how long a client may take to *send*
the initial request. A client that opens a connection and dribbles the request
line/headers one byte at a time therefore parks a connection indefinitely: the
request-read loop in ``RequestHandler.start()`` blocks on ``await self._waiter``
with no deadline until a complete message is parsed. Enough such connections
exhaust the accept loop / fd budget -- the classic slowloris DoS (CWE-400).

The only robust in-process fix is a connection-level guard, since a
``@web.middleware`` runs *after* the request line + headers are already parsed
and so cannot see (let alone bound) a slow header read. This module adds that
guard as a thin ``RequestHandler`` subclass that arms a one-shot deadline when
the connection is established and disarms it the instant the first complete
request message has been parsed. Because the clock stops once headers are in,
it never touches response duration -- long-lived streaming responses (SSE,
chunked downloads) are unaffected.

Wiring: ``build_hardened_runner(app)`` returns a ``web.AppRunner`` subclass
that (a) builds our hardened ``Server``/``RequestHandler`` and (b) sets a
bounded ``keepalive_timeout`` so idle persistent connections are also reaped
(defence-in-depth for the between-requests vector that the per-request deadline
does not cover). Neither knob weakens ``client_max_size`` or the bind-address
logic. Deployments exposed beyond loopback should still front the gateway with
a hardened reverse proxy; this guard is the in-process backstop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

# Max seconds a client may take to send the complete request line + headers
# before the connection is force-closed. Generous enough for any legitimate
# client on a slow link, tight enough to reap a stalled slowloris connection.
HEADER_READ_TIMEOUT = 30.0

# Idle time a persistent (keep-alive) connection may sit between requests
# before aiohttp closes it. aiohttp's default is 3630s (~1h), itself a mild
# resource-exhaustion vector; 75s matches common reverse-proxy defaults and is
# transparent to HTTP clients (they simply reconnect).
KEEPALIVE_TIMEOUT = 75.0


class SlowlorisRequestHandler(web.RequestHandler):
    """``RequestHandler`` that bounds the request-line + header read time.

    The deadline is armed in :meth:`connection_made` and disarmed as soon as
    the first complete request has been parsed (``_request_count`` goes > 0).
    If it expires first, the connection is force-closed. The guard covers only
    the pre-handler read, so response streaming is never interrupted.
    """

    # No ``__slots__`` here: the base defines slots, but omitting them in this
    # subclass gives instances a ``__dict__`` so the guard bookkeeping below
    # can be attached without extending the base slot list.

    def __init__(
        self, *args: Any, header_read_timeout: float = HEADER_READ_TIMEOUT, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._srh_timeout = header_read_timeout
        self._srh_handle: asyncio.TimerHandle | None = None
        self._srh_disarmed = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        if self._srh_timeout and self._srh_timeout > 0:
            self._srh_handle = self._loop.call_later(
                self._srh_timeout, self._srh_expire
            )

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        # A complete request (line + headers) was parsed -> stop the clock.
        # Body bytes arrive later via the payload parser and do not bump
        # _request_count, so the guard fires exactly once, for the first read.
        #
        # INVARIANT (aiohttp >=3.9,<4): RequestHandler.data_received increments
        # self._request_count SYNCHRONOUSLY as each complete message is parsed,
        # before the request-handling task runs. The disarm therefore lands the
        # instant headers are parsed and never mid-response, so it cannot
        # force-close a live keep-alive/streaming connection. The keep-alive
        # regression test (test_dashboard_slowloris.py) pins this behaviour: if
        # a future aiohttp moves the increment into the handler task, that test
        # goes red rather than shipping a spurious force-close. This guard bounds
        # the FIRST request's header read (the open-and-never-complete slowloris
        # vector, re-armed per new connection); idle keep-alive reuse is bounded
        # separately by keepalive_timeout.
        if not self._srh_disarmed and self._request_count > 0:
            self._srh_disarm()

    def connection_lost(self, exc: BaseException | None) -> None:
        self._srh_cancel()
        super().connection_lost(exc)

    def _srh_disarm(self) -> None:
        self._srh_disarmed = True
        self._srh_cancel()

    def _srh_cancel(self) -> None:
        if self._srh_handle is not None:
            self._srh_handle.cancel()
            self._srh_handle = None

    def _srh_expire(self) -> None:
        self._srh_handle = None
        if self._srh_disarmed:
            return
        # No complete request received within the deadline: drop the stalled
        # connection. force_close() cancels the read waiter and closes the
        # transport, unblocking RequestHandler.start().
        self.force_close()


class SlowlorisServer(web.Server):
    """``web.Server`` that hands out :class:`SlowlorisRequestHandler`.

    ``web.Server.__call__`` hardcodes ``RequestHandler``, so overriding it is
    the injection point for a custom handler while reusing all of the base
    server's configuration (request factory, handler kwargs, loop).
    """

    _header_read_timeout: float = HEADER_READ_TIMEOUT

    @classmethod
    def from_server(
        cls, base: web.Server, header_read_timeout: float
    ) -> "SlowlorisServer":
        """Rebuild ``base`` as a hardened server, preserving its config."""
        server = cls(
            base.request_handler,
            request_factory=base.request_factory,
            handler_cancellation=base.handler_cancellation,
            loop=base._loop,
            **base._kwargs,
        )
        server._header_read_timeout = header_read_timeout
        return server

    def __call__(self) -> web.RequestHandler:
        try:
            return SlowlorisRequestHandler(
                self,
                loop=self._loop,
                header_read_timeout=self._header_read_timeout,
                **self._kwargs,
            )
        except TypeError:
            # Failsafe mirrors web.Server.__call__: strip custom handler_args.
            kwargs = {
                k: v
                for k, v in self._kwargs.items()
                if k in ("debug", "access_log_class")
            }
            return SlowlorisRequestHandler(
                self,
                loop=self._loop,
                header_read_timeout=self._header_read_timeout,
                **kwargs,
            )


class SlowlorisAppRunner(web.AppRunner):
    """``AppRunner`` that installs the slowloris-hardened server + timeouts."""

    __slots__ = ("_header_read_timeout",)

    def __init__(
        self,
        app: web.Application,
        *,
        header_read_timeout: float = HEADER_READ_TIMEOUT,
        keepalive_timeout: float = KEEPALIVE_TIMEOUT,
        **kwargs: Any,
    ) -> None:
        # keepalive_timeout is forwarded to RequestHandler via the runner's
        # **kwargs; only set it if the caller has not overridden it.
        kwargs.setdefault("keepalive_timeout", keepalive_timeout)
        super().__init__(app, **kwargs)
        self._header_read_timeout = header_read_timeout

    async def _make_server(self) -> web.Server:
        base = await super()._make_server()
        return SlowlorisServer.from_server(base, self._header_read_timeout)


def build_hardened_runner(
    app: web.Application,
    *,
    header_read_timeout: float = HEADER_READ_TIMEOUT,
    keepalive_timeout: float = KEEPALIVE_TIMEOUT,
) -> web.AppRunner:
    """Return an ``AppRunner`` with slowloris mitigation wired in.

    Drop-in replacement for ``web.AppRunner(app)`` at the dashboard / API
    server start sites.
    """
    return SlowlorisAppRunner(
        app,
        header_read_timeout=header_read_timeout,
        keepalive_timeout=keepalive_timeout,
    )

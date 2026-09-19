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

The runner also disables aiohttp's server-side request-body decompression
(``auto_decompress=False``). aiohttp's parser decompresses a
``Content-Encoding``-encoded request body SYNCHRONOUSLY on the event loop
(``DeflateBuffer.feed_data`` -> ``decompress_sync``), so a single
highly-compressible body can wedge the loop long enough to trip the loop-stall
watchdog and take the whole gateway down (event-loop starvation, CWE-400).
No Kiro Crew client sends compressed request bodies, so the code path is pure
attack surface. With decompression off, such a body would reach handlers as
raw bytes and fail JSON parsing with a confusing 400 — the
:func:`reject_compressed_body_middleware` below turns that into an explicit
415 instead.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import hdrs, web

logger = logging.getLogger(__name__)

# Max seconds a client may take to send the complete request line + headers
# before the connection is force-closed. Generous enough for any legitimate
# client on a slow link, tight enough to reap a stalled slowloris connection.
HEADER_READ_TIMEOUT = 30.0

# Idle time a persistent (keep-alive) connection may sit between requests
# before aiohttp closes it. aiohttp's default is 3630s (~1h), itself a mild
# resource-exhaustion vector; 75s matches common reverse-proxy defaults and is
# transparent to HTTP clients (they simply reconnect).
KEEPALIVE_TIMEOUT = 75.0


@web.middleware  # type: ignore[misc]
async def reject_compressed_body_middleware(
    request: web.Request,
    handler: object,
) -> web.StreamResponse:
    """Refuse requests that carry a compressed (``Content-Encoding``) body.

    The hardened runner runs with ``auto_decompress=False`` (see module
    docstring), so a compressed body is never inflated server-side — it would
    reach the handler as raw bytes and fail there with an opaque 400. This
    middleware gives the sender the honest contract instead: an explicit 415
    before any handler touches the body. Header-only inspection, loop-safe.

    ``identity`` is a no-op encoding and passes through. Requests without a
    body (e.g. a stray header on a GET) also pass — there is nothing to
    decompress, so rejecting them would only break odd-but-harmless clients.
    """
    encoding = request.headers.get(hdrs.CONTENT_ENCODING, "").strip().lower()
    if encoding and encoding != "identity" and request.can_read_body:
        # Identify the sender: the one production stall this guards against
        # came from an unrecorded client (no access log on the request path),
        # so the rejection itself is the detector for who sends compressed
        # bodies. User-Agent is attacker-controlled prose — logged for triage,
        # never trusted.
        logger.warning(
            "rejected compressed request body: peer=%s method=%s path=%s "
            "content_encoding=%s user_agent=%r",
            request.remote,
            request.method,
            request.rel_url.path,
            encoding,
            request.headers.get(hdrs.USER_AGENT, ""),
        )
        return web.json_response(
            {
                "code": "unsupported_content_encoding",
                "error": "unsupported_content_encoding",
                "message": (
                    f"Compressed request bodies are not supported "
                    f"(Content-Encoding: {encoding}). Send the body uncompressed."
                ),
            },
            status=415,
        )
    return await handler(request)  # type: ignore[operator]


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
        try:
            super().connection_made(transport)
        except OSError:
            # The socket can race closed between accept() and this callback
            # (observed on macOS, where aiohttp's tcp_keepalive() setsockopt
            # then raises EINVAL). The connection is already dead: close it
            # quietly and leave the deadline unarmed instead of letting the
            # error escape the transport callback as an unhandled asyncio
            # exception dump. force_close() is safe even when the base call
            # failed partway (it tolerates a missing transport/waiter).
            logger.debug(
                "dropping connection that closed during setup", exc_info=True
            )
            self.force_close()
            return
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
            # ``auto_decompress`` is deliberately KEPT: it is the security knob
            # that prevents on-loop request-body decompression (see module
            # docstring). If a future aiohttp drops the kwarg, this re-raises
            # loudly instead of silently re-enabling the wedge.
            kwargs = {
                k: v
                for k, v in self._kwargs.items()
                if k in ("debug", "access_log_class", "auto_decompress")
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
        # Never inflate request bodies on the event loop: aiohttp's parser
        # decompresses Content-Encoding bodies synchronously in-loop
        # (DeflateBuffer.feed_data -> decompress_sync), which a single
        # compression-bomb request can turn into a multi-second loop stall.
        # With this off the DeflateBuffer is never constructed; the paired
        # reject_compressed_body_middleware returns an explicit 415 for such
        # requests. Not overridable: a caller passing True would re-enable the
        # exact in-loop decompression this runner exists to prevent.
        kwargs["auto_decompress"] = False
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
    **kwargs: Any,
) -> web.AppRunner:
    """Return an ``AppRunner`` with slowloris mitigation wired in.

    Drop-in replacement for ``web.AppRunner(app)`` at the dashboard / API
    server start sites. Extra ``**kwargs`` (e.g. ``max_field_size``) are
    forwarded through to the underlying aiohttp request handler.
    """
    return SlowlorisAppRunner(
        app,
        header_read_timeout=header_read_timeout,
        keepalive_timeout=keepalive_timeout,
        **kwargs,
    )

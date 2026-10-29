"""How the relay client hands a relayed request to the Crew's own web server.

:class:`CrewIngress` is the client's side of the hand-off: it calls it once per
relayed request or WebSocket and never builds a request to the web server itself.
The verified caller always travels beside the request as :attr:`RelayedRequest.user`,
never as a header the client copied.

:class:`LoopbackIngress` is the one implementation. It sends each request to the
gateway's own loopback TCP listener, with headers stamped by the dashboard's relay
ingress (``kiro_crew.dashboard.kiro_relay.ingress.RelayIngress``): that seam owns the
relay-path header rule, so the stamp drops any header the person's client sent under
the shared relay prefix (``kiro_crew.dashboard.relay_headers.RELAY_HEADER_PREFIX``),
adds the caller header (``kiro_relay.caller.RELAY_USER_HEADER``) from the relay's own
message and the per-process handover credential, and the ingress middleware honours
the stamp only on a request that arrives that way. This module never names a relay
header itself. The client binds the host connection there once it is ready
(:meth:`CrewIngress.bind_host`) and unbinds it when the connection ends.

Three more properties of the hand-off: the loopback session keeps no cookies and
follows no redirects (a WebSocket handshake answered with one is a refused upgrade),
so one viewer's ``Set-Cookie`` can never ride along on another viewer's request and
the handover credential never leaves the loopback listener; each hand-off gets its
own connection, never reused; and the path is sent exactly as the relay named it.
A body is held to the length its head declared (``HttpStream``), so the web server
reads exactly that request and nothing after it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Optional, Protocol, Sequence, Tuple, Union

import aiohttp
import yarl
from aiohttp.abc import AbstractStreamWriter
from aiohttp.payload import AsyncIterablePayload
from multidict import CIMultiDict

if TYPE_CHECKING:
    from kiro_crew.dashboard.kiro_relay.ingress import RelayIngress as GatewayRelayIngress

logger = logging.getLogger(__name__)

Headers = Tuple[Tuple[str, str], ...]

#: Request headers that describe one hop and never cross to the next.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        # The relay already settled 100-continue with the viewer and sends the body
        # regardless, so the loopback hop never renegotiates it.
        "expect",
    }
)
#: Response headers that frame the body on one hop; the relay frames it again.
_RESPONSE_FRAMING = frozenset({"connection", "keep-alive", "transfer-encoding", "content-length"})
#: Every way the loopback hand-off fails before a response head: no listener,
#: a dropped or reset connection, a connect timeout. Each reads as the Crew's web
#: server being unreachable, so the viewer hears it at once rather than waiting
#: out the relay's own timeout. A refused WebSocket upgrade is caught before this.
_HANDOFF_FAILURES = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


@dataclass(frozen=True)
class RelayedRequest:
    """One relayed HTTP request, as the relay's ``request`` head described it."""

    method: str
    path: str
    query: str
    headers: Headers
    user: str
    #: True when the body is under request-body credit: the ingress then asks for
    #: each next piece only once every earlier byte has left this process for the
    #: web server, because asking is what gives credit back (``HttpStream``).
    credited_body: bool = False


@dataclass(frozen=True)
class RelayedWebSocket:
    """One relayed WebSocket, as the relay's ``ws-upgrade`` described it."""

    path: str
    query: str
    protocols: Tuple[str, ...]
    headers: Headers
    user: str


class IngressUnreachable(Exception):
    """The Crew's web server could not be reached at all."""


class IngressUnavailable(Exception):
    """The dashboard's relay ingress will not take a hand-off: it is off or unbound."""


class IngressRefused(Exception):
    """The Crew's web server answered a WebSocket upgrade with something other than 101."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the Crew refused the WebSocket upgrade ({status})")
        self.status = status


class IngressResponse(Protocol):
    """The web server's answer: a head, then a body read in pieces."""

    status: int
    headers: Headers

    def iter_body(self, chunk_size: int) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class IngressWebSocket(Protocol):
    """A WebSocket opened to the web server on a viewer's behalf."""

    async def send(self, data: Union[str, bytes]) -> None: ...

    async def receive(self) -> Optional[Union[str, bytes]]:
        """The next message, or ``None`` once the socket has closed."""
        ...

    @property
    def close_code(self) -> Optional[int]: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class CrewIngress(Protocol):
    """How the relay client reaches the Crew's web server."""

    def bind_host(self, *, crew_kind: str, owner_user_id: str) -> None:
        """Start honouring hand-offs for the host connection the lease describes.

        Raises :class:`IngressUnavailable` when the gateway will not honour them.
        """
        ...

    def unbind_host(self) -> None:
        """Stop honouring hand-offs; the next binding gets a new credential."""
        ...

    async def open_http(
        self, request: RelayedRequest, body: Optional[AsyncIterator[bytes]]
    ) -> IngressResponse:
        """Send *request* with *body* (``None`` for no body) and return the head.

        With :attr:`RelayedRequest.credited_body` set, the ingress asks *body* for
        each next piece only once the pieces before it have left this process for
        the web server, never while they wait in a local write buffer.
        """
        ...

    async def open_websocket(self, request: RelayedWebSocket) -> IngressWebSocket: ...

    async def aclose(self) -> None: ...


def forwardable_request_headers(headers: Sequence[Tuple[str, str]]) -> list[Tuple[str, str]]:
    """The relayed headers that cross to the web server: every one but the per-hop ones."""
    out: list[Tuple[str, str]] = []
    for name, value in headers:
        lower = name.lower()
        if lower in _HOP_BY_HOP or lower.startswith("sec-websocket-"):
            continue
        out.append((lower, value))
    return out


def relayable_response_headers(headers: Sequence[Tuple[str, str]]) -> Tuple[dict, list]:
    """Split the web server's response headers into the ``response`` head's two fields.

    Returns ``(headers, set_cookies)``: names lower-cased, repeated headers joined
    with ``", "`` (except ``Set-Cookie``, whose values stay separate), and the
    framing headers left out because the relay frames the body again.
    """
    merged: dict = {}
    cookies: list = []
    for name, value in headers:
        lower = name.lower()
        if lower == "set-cookie":
            cookies.append(value)
            continue
        if lower in _RESPONSE_FRAMING:
            continue
        merged[lower] = f"{merged[lower]}, {value}" if lower in merged else value
    return merged, cookies


class _LoopbackResponse:
    def __init__(self, response: aiohttp.ClientResponse) -> None:
        self._response = response
        self.status = response.status
        self.headers: Headers = tuple((k, v) for k, v in response.headers.items())

    async def iter_body(self, chunk_size: int) -> AsyncIterator[bytes]:
        async for piece in self._response.content.iter_chunked(chunk_size):
            yield piece

    async def aclose(self) -> None:
        self._response.release()
        self._response.close()


class _LoopbackWebSocket:
    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def send(self, data: Union[str, bytes]) -> None:
        if isinstance(data, str):
            await self._ws.send_str(data)
        else:
            await self._ws.send_bytes(data)

    async def receive(self) -> Optional[Union[str, bytes]]:
        while True:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                return msg.data
            if msg.type == aiohttp.WSMsgType.BINARY:
                return bytes(msg.data)
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return None

    @property
    def close_code(self) -> Optional[int]:
        return self._ws.close_code

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._ws.close(code=code, message=reason.encode("utf-8"))


class _FlushedBody(AsyncIterablePayload):
    """A request body asked for one piece at a time, each fully written out first.

    aiohttp's own iterable payload asks for the next piece while up to 64 KiB of the
    earlier ones still wait in the connection's write buffer, and asking is what
    gives request-body credit back. So this payload sets the connection's write
    buffer limit to zero and, before asking for each next piece, waits until that
    buffer is empty: every earlier byte has then left this process for the web
    server's socket. Each hand-off has its own connection, so the limit touches no
    other request.
    """

    def __init__(self, body: AsyncIterator[bytes]) -> None:
        self._writer: Optional[AbstractStreamWriter] = None
        super().__init__(self._paced(body))

    async def _paced(self, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for piece in body:
            yield piece
            # aiohttp is asking for the next piece: let the last ones leave first.
            await self._flushed()

    async def _flushed(self) -> None:
        """Return only once every byte written so far has left the write buffer.

        A transport that is gone, closing, or still holding bytes after the drain
        never counts as flushed: the write fails instead, so the next piece is never
        asked for and no credit goes back for bytes that did not leave.
        """
        writer = self._writer
        transport: Optional[asyncio.Transport] = getattr(writer, "transport", None)
        if writer is None or transport is None or transport.is_closing():
            raise ConnectionResetError("the loopback connection closed before the body left")
        transport.set_write_buffer_limits(high=0)
        if transport.get_write_buffer_size() > 0:
            await writer.drain()
        if transport.is_closing() or transport.get_write_buffer_size() > 0:
            raise ConnectionResetError("the loopback connection did not take the request body")

    async def write(self, writer: AbstractStreamWriter) -> None:
        self._writer = writer
        await super().write(writer)

    async def write_with_length(  # type: ignore[override]
        self, writer: AbstractStreamWriter, content_length: Optional[int]
    ) -> None:
        # aiohttp 3.12 and later write through here; earlier releases call write().
        self._writer = writer
        await super().write_with_length(writer, content_length)  # type: ignore[misc]


class LoopbackIngress:
    """Hand relayed requests to the gateway's own loopback listener, stamped by *gateway*.

    *host* and *port* name the gateway's loopback TCP listener; the dashboard's relay
    ingress honours a hand-off on no other transport. The session is created on
    first use, inside the running loop, and closed by :meth:`aclose`.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        gateway: "GatewayRelayIngress",
        max_ws_message_bytes: int,
    ) -> None:
        if not 0 < port < 65536:
            raise ValueError(f"not a TCP port: {port!r}")
        bracketed = f"[{host}]" if ":" in host else host
        self._base = f"http://{bracketed}:{port}"
        self._gateway = gateway
        self._max_ws_message_bytes = max_ws_message_bytes
        self._session: Optional[aiohttp.ClientSession] = None

    def bind_host(self, *, crew_kind: str, owner_user_id: str) -> None:
        from kiro_crew.dashboard.kiro_relay.caller import CrewKind, HostBinding
        from kiro_crew.dashboard.kiro_relay.ingress import RelayIngressError

        try:
            binding = HostBinding(crew_kind=CrewKind(crew_kind), owner_user_id=owner_user_id)
            self._gateway.bind_host(binding)
        except (RelayIngressError, ValueError) as exc:
            raise IngressUnavailable(str(exc)) from exc

    def unbind_host(self) -> None:
        self._gateway.unbind_host()

    def _stamped(self, headers: Sequence[Tuple[str, str]], user: str) -> CIMultiDict[str]:
        from kiro_crew.dashboard.kiro_relay.ingress import RelayIngressError

        try:
            return self._gateway.stamp_headers(
                forwardable_request_headers(headers), relay_user=user
            )
        except (RelayIngressError, ValueError) as exc:
            raise IngressUnavailable(str(exc)) from exc

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:

            async def refuse_redirect(
                _session: aiohttp.ClientSession,
                _context: object,
                params: aiohttp.TraceRequestRedirectParams,
            ) -> None:
                # ws_connect has no allow_redirects option in aiohttp 3.x.
                # Stop before it can send the handover credential to Location.
                params.response.close()
                raise IngressRefused(params.response.status)

            trace = aiohttp.TraceConfig()
            trace.on_request_redirect.append(refuse_redirect)
            self._session = aiohttp.ClientSession(
                # One loopback connection per hand-off, never reused: whatever one
                # relayed request leaves on a connection cannot reach another's.
                # No pool cap: after each move hands off, at most two host connections
                # remain live (current and one draining), each admitting at most
                # MAX_STREAMS_PER_CONNECTION streams. A make-before-break dial can
                # temporarily overlap both until ready ends the earlier drain.
                connector=aiohttp.TCPConnector(force_close=True, limit=0),
                cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False,
                trust_env=False,
                skip_auto_headers=("User-Agent", "Accept", "Accept-Encoding", "Content-Type"),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
                trace_configs=[trace],
            )
        return self._session

    def _url(self, path: str, query: str) -> yarl.URL:
        # ``encoded=True`` sends the path and query exactly as the relay named
        # them: no re-quoting, no dot-segment folding, a leading ``//`` kept. The
        # protocol layer already refused anything that is not printable ASCII.
        raw = f"{self._base}{path}?{query}" if query else f"{self._base}{path}"
        return yarl.URL(raw, encoded=True)

    async def open_http(
        self, request: RelayedRequest, body: Optional[AsyncIterator[bytes]]
    ) -> IngressResponse:
        headers = self._stamped(request.headers, request.user)
        data: Union[None, AsyncIterator[bytes], _FlushedBody] = body
        if body is not None and request.credited_body:
            data = _FlushedBody(body)
        try:
            response = await self._client().request(
                request.method,
                self._url(request.path, request.query),
                headers=headers,
                data=data,
                allow_redirects=False,
            )
        except _HANDOFF_FAILURES as exc:
            raise IngressUnreachable(str(exc)) from exc
        return _LoopbackResponse(response)

    async def open_websocket(self, request: RelayedWebSocket) -> IngressWebSocket:
        headers = self._stamped(request.headers, request.user)
        try:
            ws = await self._client().ws_connect(
                self._url(request.path, request.query),
                protocols=request.protocols,
                headers=headers,
                autoping=True,
                max_msg_size=self._max_ws_message_bytes,
            )
        except aiohttp.WSServerHandshakeError as exc:
            raise IngressRefused(exc.status) from exc
        except _HANDOFF_FAILURES as exc:
            raise IngressUnreachable(str(exc)) from exc
        return _LoopbackWebSocket(ws)

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

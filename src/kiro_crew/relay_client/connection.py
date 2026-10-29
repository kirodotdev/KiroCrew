"""One host connection: the WebSocket the Crew holds open to the relay.

:meth:`HostConnection.run` dials the relay with the host token, sends ``hello``,
reads frames until the socket ends and returns how it ended as a
:class:`~kiro_crew.relay_client.backoff.ConnectionEnd`. While it runs it answers
``ping``, sends its own ``ping`` every 25 seconds, gives up on a socket that has
been silent for 60, and dispatches each stream's frames to its
:class:`~kiro_crew.relay_client.streams.HttpStream` or
:class:`~kiro_crew.relay_client.streams.WsStream`. Deciding what to do after a
connection ends is the client's job, not this class's.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set, Tuple, Union
from urllib.parse import urlsplit

import aiohttp

from kiro_crew.relay_client.backoff import ConnectionEnd
from kiro_crew.relay_client.ingress import CrewIngress, IngressUnavailable
from kiro_crew.relay_client.protocol import (
    CLOSE_GOING_AWAY,
    CLOSE_HANDSHAKE_SECONDS,
    CREW_FIRST_BYTE_SECONDS,
    DRAIN_BACKSTOP_SECONDS,
    ERR_CREW_UNREACHABLE,
    ERR_INVALID_MESSAGE,
    ERR_MESSAGE_TOO_LARGE,
    ERR_UNSUPPORTED_FRAME,
    FEATURE_FLOW_CREDIT,
    FEATURE_REQUEST_CREDIT,
    MAX_STREAMS_PER_CONNECTION,
    MAX_TEXT_FRAME_BYTES,
    MOVE_QUIET_GRACE_SECONDS,
    MOVE_WINDOW_MAX_SECONDS,
    OFFERED_FEATURES,
    PING_INTERVAL_SECONDS,
    PROTOCOL_VERSION,
    SILENCE_REPLACE_SECONDS,
    WS_PAUSE_WATCHDOG_SECONDS,
    FrameError,
    build_error,
    build_hello,
    encode,
    is_loopback_host,
    parse_inbound,
)
from kiro_crew.relay_client.streams import (
    BufferBudget,
    CreditGate,
    HttpStream,
    WsReassembler,
    WsStream,
    decode_body_data,
)
from kiro_crew.relay_client.token_source import HostCredential

logger = logging.getLogger(__name__)

#: How long one dial may take, the TLS and WebSocket handshakes included.
DIAL_TIMEOUT_SECONDS = 30.0

_PROXY_SCHEME = {"wss": "https", "ws": "http", "https": "https", "http": "http"}


def env_proxy_for(url: str) -> Optional[str]:
    """The proxy the environment names for *url*, or ``None`` to dial directly.

    Reads only the proxy variables (``HTTPS_PROXY``, ``HTTP_PROXY``, ``ALL_PROXY``
    and ``NO_PROXY``), never netrc, so no stored password can reach the relay. A
    loopback relay, which only a local test runs, is never proxied.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if not host or is_loopback_host(host):
        return None
    if urllib.request.proxy_bypass(host):
        return None
    proxies = urllib.request.getproxies()
    proxy = proxies.get(_PROXY_SCHEME.get(parts.scheme, "")) or proxies.get("all")
    return proxy or None


@dataclass(frozen=True)
class RelayTimings:
    """Every duration the client keeps, defaulting to the contract's numbers."""

    ping_interval: float = PING_INTERVAL_SECONDS
    silence: float = SILENCE_REPLACE_SECONDS
    first_byte: float = CREW_FIRST_BYTE_SECONDS
    ws_pause_watchdog: float = WS_PAUSE_WATCHDOG_SECONDS
    dial: float = DIAL_TIMEOUT_SECONDS
    close_handshake: float = CLOSE_HANDSHAKE_SECONDS
    move_grace: float = MOVE_QUIET_GRACE_SECONDS
    #: The longest an old connection drains before the client closes it itself.
    drain_backstop: float = DRAIN_BACKSTOP_SECONDS


Stream = Union[HttpStream, WsStream]


class HostConnection:
    """One dial of the relay, from upgrade to close."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        url: str,
        credential: HostCredential,
        ingress: CrewIngress,
        binding: Tuple[str, str],
        client_version: str,
        timings: RelayTimings,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._binding = binding
        self._url = url
        self._credential = credential
        self._ingress = ingress
        self._client_version = client_version
        self._timings = timings
        self._monotonic = monotonic
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._send_lock = asyncio.Lock()
        self._streams: Dict[int, Stream] = {}
        self._reassembler = WsReassembler(monotonic)
        self._budget = BufferBudget()
        #: New streams refused because the connection held its cap, since it opened.
        self.refused_streams = 0
        self._ping_count = 0
        self._closing_code: Optional[int] = None
        self.ready = asyncio.Event()
        self.ready_frame: Optional[Dict[str, Any]] = None
        self.features: Set[str] = set()
        #: Set when the relay asked this connection to move; the client opens the next one.
        self.move_requested = asyncio.Event()
        self.move_close_in: float = 0.0
        self.http_streams_idle = asyncio.Event()
        self.http_streams_idle.set()
        #: Set whenever a request head arrives; a draining connection clears it to
        #: tell a quiet grace from one a late request broke.
        self._request_arrived = asyncio.Event()
        #: Set once this connection is closing or closed, by either side.
        self._ended = asyncio.Event()

    @property
    def credential(self) -> HostCredential:
        return self._credential

    @property
    def endpoint_id(self) -> str:
        return (self.ready_frame or {}).get("endpointId", "")

    @property
    def closed_by_client(self) -> bool:
        """True once this side started the close, so its end is not the relay's verdict."""
        return self._closing_code is not None

    @property
    def serving(self) -> bool:
        """True from ``ready`` until this connection starts to close, by either side.

        This is when a host counts as connected (contract section 4.2).
        """
        return self.ready.is_set() and not self._ended.is_set()

    @property
    def stream_count(self) -> int:
        return len(self._streams)

    async def send(
        self, frame: Dict[str, Any], unless: Optional[Callable[[], bool]] = None
    ) -> None:
        """Send *frame*; with *unless*, skip it when that is true once the lock is held."""
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionResetError("the relay connection is closed")
        text = encode(frame)
        async with self._send_lock:
            if unless is not None and unless():
                return
            await ws.send_str(text)

    async def send_token(self, credential: HostCredential) -> None:
        self._credential = credential
        await self.send({"type": "token", "token": credential.token})

    async def run(self) -> ConnectionEnd:
        try:
            ws = await asyncio.wait_for(
                self._session.ws_connect(
                    self._url,
                    headers={
                        "Authorization": f"Bearer {self._credential.token}",
                        "User-Agent": f"kiro-crew/{self._client_version}",
                    },
                    autoping=True,
                    autoclose=True,
                    # Frames past the contract's limit are refused by this client
                    # with an error frame rather than by the library with a close.
                    max_msg_size=MAX_TEXT_FRAME_BYTES * 4,
                    # The session does not trust the environment; the proxy, if
                    # one is set, is passed here so netrc is never consulted.
                    proxy=env_proxy_for(self._url),
                ),
                timeout=self._timings.dial,
            )
        except aiohttp.WSServerHandshakeError as exc:
            return ConnectionEnd(http_status=exc.status, detail=f"upgrade refused ({exc.status})")
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, ValueError) as exc:
            return ConnectionEnd(detail=f"dial failed: {type(exc).__name__}")
        self._ws = ws
        if self._closing_code is not None:
            # The client stopped while the dial was in flight.
            await self._close_ws(ws, self._closing_code)
            return ConnectionEnd(code=self._closing_code, detail="closed during the dial")
        keepalive = asyncio.create_task(self._keepalive(), name="relay-keepalive")
        try:
            await self.send(build_hello(client_version=self._client_version))
            return await self._read_loop(ws)
        except ConnectionError:
            return self._end(ws, detail="the socket closed while sending")
        finally:
            self._ended.set()
            keepalive.cancel()
            await self._cancel_streams()
            if not ws.closed:
                await self._close_ws(ws, 1000 if self._closing_code is None else self._closing_code)

    async def close(self, code: int, reason: str = "") -> None:
        """Close this connection from the client's side."""
        self._closing_code = code
        self._ended.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            await self._close_ws(ws, code, reason)

    async def drain_and_close(self, *, quiet: float = 0.0) -> None:
        """Let open HTTP requests finish, then close with 1000.

        Once none is in flight, the connection stays open until no new request has
        arrived for *quiet* seconds, and serves any that does: the relay may have
        written one here just before it stopped routing to this connection, and it
        travels apart from the new connection's ``ready``.

        A ``reconnect``'s ``closeInSeconds`` is only the time to get the new
        connection ready; it does not bound this drain. A request that outlives it
        keeps going here until the relay closes the connection with 1012, which
        ends the drain (contract section 4.5). The drain's only deadline of its own
        is ``drain_backstop``, which the relay always beats, and the grace never
        runs past it.

        WebSockets do not finish on their own, so the close ends them; the relay
        then closes each viewer's socket with 1012 and the viewer reconnects to
        the new connection.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(self._timings.drain_backstop, 0.0)
        quiet = quiet if math.isfinite(quiet) and quiet > 0 else 0.0
        while True:
            if self._ended.is_set():
                return  # closed under the drain: a newer move, the relay or a stop
            # Cleared before the idle check, so a request that lands after it
            # wakes the grace below rather than slipping past it.
            self._request_arrived.clear()
            if not self.http_streams_idle.is_set():
                remaining = max(deadline - loop.time(), 0.0)
                if not await _first_set(self.http_streams_idle, self._ended, timeout=remaining):
                    if loop.time() < deadline:
                        continue
                    logger.info(
                        "relay move: open requests outlived the drain backstop; closing anyway"
                    )
                    break
                continue
            grace = min(quiet, deadline - loop.time())
            if grace <= 0:
                break
            if not await _first_set(self._request_arrived, self._ended, timeout=grace):
                if self._request_arrived.is_set() or not self.http_streams_idle.is_set():
                    continue  # a request was admitted as the grace ran out: serve it
                break  # quiet for the whole grace
        await self.close(1000)

    async def _close_ws(
        self, ws: aiohttp.ClientWebSocketResponse, code: int, reason: str = ""
    ) -> None:
        try:
            await asyncio.wait_for(
                ws.close(code=code, message=reason.encode("utf-8")),
                timeout=self._timings.close_handshake,
            )
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
            logger.debug("relay close handshake did not finish", exc_info=True)

    def _end(
        self, ws: aiohttp.ClientWebSocketResponse, *, detail: str = "", reason: str = ""
    ) -> ConnectionEnd:
        return ConnectionEnd(
            code=ws.close_code,
            reason=reason,
            reached_ready=self.ready.is_set(),
            detail=detail,
        )

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> ConnectionEnd:
        while True:
            try:
                msg = await ws.receive(timeout=self._timings.silence)
            except asyncio.TimeoutError:
                # Nothing at all for the silence window: the path is dead even if
                # the socket looks open, so give it up and let the client redial.
                logger.info(
                    "relay connection silent for %.0f s; replacing it", self._timings.silence
                )
                await self._close_ws(ws, 1000)
                return ConnectionEnd(reached_ready=self.ready.is_set(), detail="silence")
            if msg.type == aiohttp.WSMsgType.TEXT:
                ended = await self._dispatch(msg.data)
                if ended is not None:
                    return ended
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await self.send(
                    build_error(ERR_UNSUPPORTED_FRAME, "binary frames are not in version 1")
                )
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                reason = msg.extra if isinstance(msg.extra, str) else ""
                return self._end(ws, reason=reason)
            else:
                return self._end(ws, detail=f"socket ended: {msg.type.name}")

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self._timings.ping_interval)
            self._ping_count += 1
            try:
                await self.send({"type": "ping", "nonce": f"p-{self._ping_count:06d}"})
            except ConnectionError:
                return

    async def _dispatch(self, text: str) -> Optional[ConnectionEnd]:
        try:
            frame = parse_inbound(text)
        except FrameError as exc:
            return await self._refuse(exc)
        kind = frame["type"]
        try:
            handler = getattr(self, "_on_" + kind.replace("-", "_"))
            return await handler(frame)
        except FrameError as exc:
            exc.frame_type = exc.frame_type or kind
            return await self._refuse(exc)
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception:
            logger.exception("relay client failed to handle a %s frame", kind)
            sid = frame.get("id")
            if not isinstance(sid, int) or isinstance(sid, bool) or sid < 0:
                sid = None
            refusal = FrameError(
                ERR_INVALID_MESSAGE,
                "the relay client could not handle this frame",
                stream_id=sid,
            )
            refusal.frame_type = kind
            return await self._refuse(refusal)

    async def _refuse(self, exc: FrameError) -> Optional[ConnectionEnd]:
        if exc.frame_type == "ready" and not self.ready.is_set():
            # A malformed ready leaves the client nothing to serve: drop the socket
            # and let the client redial with backoff.
            logger.warning("relay sent a malformed ready: %s", exc.message)
            ws = self._ws
            if ws is not None:
                await self._close_ws(ws, 1000)
            return ConnectionEnd(detail=f"malformed ready: {exc.message}")
        if not exc.answer:
            logger.warning("relay sent an unanswerable frame: %s", exc.message)
            return None
        if exc.stream_id is not None:
            # Before the error is queued, so no credit the stream sends can follow it.
            stream = self._streams.get(exc.stream_id)
            if isinstance(stream, HttpStream):
                stream.stop_credit()
        await self.send(build_error(exc.code, exc.message, stream_id=exc.stream_id))
        if exc.stream_id is not None:
            self._end_stream(exc.stream_id)
        return None

    def _end_stream(self, sid: int) -> None:
        stream = self._streams.get(sid)
        if stream is not None:
            stream.cancel()
        self._reassembler.drop(sid)

    def _stream_done(self, sid: int) -> None:
        self._streams.pop(sid, None)
        self._reassembler.drop(sid)
        if not any(isinstance(s, HttpStream) for s in self._streams.values()):
            self.http_streams_idle.set()

    async def _cancel_streams(self) -> None:
        streams = list(self._streams.values())
        for stream in streams:
            stream.cancel()
        for stream in streams:
            await stream.wait()

    def _new_stream_id(self, frame: Dict[str, Any]) -> int:
        sid = frame["id"]
        if not self.ready.is_set():
            raise FrameError(ERR_INVALID_MESSAGE, "a stream opened before ready", stream_id=sid)
        if sid in self._streams:
            raise FrameError(ERR_INVALID_MESSAGE, "this stream id is already open", stream_id=sid)
        if len(self._streams) >= MAX_STREAMS_PER_CONNECTION:
            # Refused before any row exists for it, so a refused stream holds nothing.
            self.refused_streams += 1
            if self.refused_streams == 1:
                logger.warning(
                    "relay connection holds %d streams; refusing new ones until some end",
                    MAX_STREAMS_PER_CONNECTION,
                )
            raise FrameError(
                ERR_CREW_UNREACHABLE,
                f"the Crew holds {MAX_STREAMS_PER_CONNECTION} relayed streams already",
                stream_id=sid,
            )
        return sid

    # ── frame handlers: each returns a ConnectionEnd only when the socket must end ──

    async def _on_ready(self, frame: Dict[str, Any]) -> Optional[ConnectionEnd]:
        if self.ready.is_set():
            logger.debug("relay sent a second ready; ignoring it")
            return None
        if frame["protocolVersion"] != PROTOCOL_VERSION:
            raise FrameError(ERR_INVALID_MESSAGE, "ready names a protocol version not offered")
        self.ready_frame = frame
        self.features = set(frame["features"]) & set(OFFERED_FEATURES)
        crew_kind, owner_user_id = self._binding
        try:
            # Bound before ready is marked, so no request this connection carries
            # can reach the ingress ahead of its binding.
            self._ingress.bind_host(crew_kind=crew_kind, owner_user_id=owner_user_id)
        except IngressUnavailable as exc:
            logger.error("the dashboard's relay ingress refused the host binding: %s", exc)
            ws = self._ws
            if ws is not None:
                await self._close_ws(ws, CLOSE_GOING_AWAY)
            return ConnectionEnd(stop_reason=f"the dashboard's relay ingress is unavailable: {exc}")
        deprecation = frame.get("deprecation")
        if deprecation:
            logger.warning(
                "the relay will require protocol version %s from %s; update Kiro Crew before then",
                deprecation.get("minProtocolVersion"),
                deprecation.get("effectiveAt"),
            )
        self.ready.set()
        return None

    async def _on_ping(self, frame: Dict[str, Any]) -> None:
        pong: Dict[str, Any] = {"type": "pong"}
        if "nonce" in frame:
            pong["nonce"] = frame["nonce"]
        await self.send(pong)

    async def _on_pong(self, frame: Dict[str, Any]) -> None:
        # Receiving it is the point: any frame restarts the silence window.
        return None

    async def _on_reconnect(self, frame: Dict[str, Any]) -> None:
        logger.info(
            "relay asked this connection to move (%s, closing in %s s)",
            frame["reason"],
            frame["closeInSeconds"],
        )
        self.move_close_in = float(min(frame["closeInSeconds"], MOVE_WINDOW_MAX_SECONDS))
        self.move_requested.set()

    async def _on_error(self, frame: Dict[str, Any]) -> None:
        sid = frame.get("id")
        if sid is not None:
            # The relay ended that stream; stop the local side and send nothing more.
            self._end_stream(sid)
            return
        logger.warning("relay reported %s: %s", frame["code"], frame.get("message", ""))

    async def _on_request(self, frame: Dict[str, Any]) -> None:
        sid = self._new_stream_id(frame)
        credit = None
        if FEATURE_FLOW_CREDIT in self.features and "initialCredit" in frame:
            credit = CreditGate(frame["initialCredit"])
        stream = HttpStream(
            frame,
            send=self.send,
            ingress=self._ingress,
            on_done=self._stream_done,
            credit=credit,
            first_byte_seconds=self._timings.first_byte,
            budget=self._budget,
            monotonic=self._monotonic,
            request_credit=FEATURE_REQUEST_CREDIT in self.features,
            send_unless=self.send,
        )
        self._streams[sid] = stream
        self.http_streams_idle.clear()
        self._request_arrived.set()
        stream.start()

    def _http(self, frame: Dict[str, Any]) -> Optional[HttpStream]:
        stream = self._streams.get(frame["id"])
        return stream if isinstance(stream, HttpStream) else None

    def _ws_stream(self, frame: Dict[str, Any]) -> Optional[WsStream]:
        stream = self._streams.get(frame["id"])
        return stream if isinstance(stream, WsStream) else None

    async def _on_request_chunk(self, frame: Dict[str, Any]) -> None:
        stream = self._http(frame)
        if stream is not None:
            stream.feed(decode_body_data(frame))

    async def _on_request_end(self, frame: Dict[str, Any]) -> None:
        stream = self._http(frame)
        if stream is not None:
            stream.end_body()

    async def _on_request_cancel(self, frame: Dict[str, Any]) -> None:
        stream = self._http(frame)
        if stream is not None:
            stream.cancel()

    async def _on_flow_credit(self, frame: Dict[str, Any]) -> None:
        stream = self._http(frame)
        if stream is not None and stream.credit is not None:
            stream.credit.add(frame["credit"])

    async def _on_ws_upgrade(self, frame: Dict[str, Any]) -> None:
        sid = self._new_stream_id(frame)
        stream = WsStream(
            frame,
            send=self.send,
            ingress=self._ingress,
            on_done=self._stream_done,
            pause_watchdog_seconds=self._timings.ws_pause_watchdog,
            budget=self._budget,
        )
        self._streams[sid] = stream
        stream.start()

    async def _on_ws_frame(self, frame: Dict[str, Any]) -> None:
        for sid in self._reassembler.stale():
            self._reassembler.drop(sid)
            await self.send(
                build_error(
                    ERR_MESSAGE_TOO_LARGE, "a WebSocket message stalled mid-way", stream_id=sid
                )
            )
            self._end_stream(sid)
        stream = self._ws_stream(frame)
        if stream is None:
            return
        message = self._reassembler.add(frame)
        if message is not None:
            stream.from_relay(message)

    async def _on_ws_close(self, frame: Dict[str, Any]) -> None:
        stream = self._ws_stream(frame)
        if stream is not None:
            stream.relay_closed(frame.get("code"), frame.get("reason", ""))

    async def _on_ws_flow(self, frame: Dict[str, Any]) -> None:
        stream = self._ws_stream(frame)
        if stream is not None:
            stream.set_paused(frame["paused"])


async def _first_set(*events: asyncio.Event, timeout: float) -> bool:
    """Wait until any of *events* is set; ``False`` when *timeout* passes first."""
    if any(event.is_set() for event in events):
        return True
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
    # The events, not the waiters: one set in the same tick the timeout fired has
    # not woken its waiter yet, and must still count.
    return any(event.is_set() for event in events)

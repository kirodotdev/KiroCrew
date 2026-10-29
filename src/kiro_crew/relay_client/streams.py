"""One relayed HTTP request or WebSocket, from the relay's frames to the Crew and back.

A host connection carries many streams at once, each with its own id. An
:class:`HttpStream` turns a ``request`` head, its ``request-chunk`` frames and its
``request-end`` into one request to the Crew's web server, and streams the answer
back as a ``response`` head, ``response-chunk`` frames and a ``response-end``,
spending the relay's ``flow-credit`` so a slow viewer never makes the client send
more than the relay agreed to hold. When ``request-credit`` is agreed, it also sends
``flow-credit`` of its own for the request body, giving back only what has left this
process for the Crew's web server, so one large upload waits for the Crew rather than filling the
host connection ahead of every other stream. A :class:`WsStream` opens the same WebSocket to
the web server and carries messages both ways, pausing its read side while the
relay says the viewer is slow.

Failures end one stream with an ``error`` frame that names it, never the connection:
the web server could not be reached (``crew_unreachable``), sent no response head in
time (``crew_not_answering``), or answered with a head the relay would refuse.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Coroutine, Dict, List, Optional, Union

from kiro_crew.relay_client.ingress import (
    CrewIngress,
    IngressRefused,
    IngressResponse,
    IngressUnavailable,
    IngressUnreachable,
    IngressWebSocket,
    RelayedRequest,
    RelayedWebSocket,
    relayable_response_headers,
)
from kiro_crew.relay_client.protocol import (
    BODY_CHUNK_RAW_BYTES,
    CONNECTION_BUFFER_MAX_BYTES,
    ERR_CREW_NOT_ANSWERING,
    ERR_CREW_UNREACHABLE,
    ERR_INVALID_MESSAGE,
    ERR_MESSAGE_TOO_LARGE,
    REQUEST_BUFFER_MAX_BYTES,
    REQUEST_CREDIT_GRANT_BYTES,
    REQUEST_CREDIT_INITIAL_BYTES,
    WS_CLOSE_CREW_REFUSED,
    WS_MESSAGE_MAX_BYTES,
    WS_QUEUE_MAX_MESSAGES,
    WS_REASSEMBLY_CONCURRENT,
    WS_REASSEMBLY_MAX_PIECES,
    WS_REASSEMBLY_PER_CONNECTION_BYTES,
    WS_REASSEMBLY_PIECE_TIMEOUT_SECONDS,
    FrameError,
    build_error,
    build_flow_credit,
    build_ws_close,
    declared_content_length,
    response_head_problem,
    split_ws_data,
)

logger = logging.getLogger(__name__)

SendFrame = Callable[[Dict[str, Any]], Awaitable[None]]
#: Send a frame unless the predicate is true once the connection's send lock is held.
SendUnless = Callable[[Dict[str, Any], Callable[[], bool]], Awaitable[None]]
StreamDone = Callable[[int], None]

#: Methods whose request carries no body unless a framing header says otherwise.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def decode_body_data(frame: Dict[str, Any]) -> bytes:
    """The bytes a ``request-chunk`` carries, base64 or not."""
    data = frame["data"]
    if frame.get("base64"):
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise FrameError(
                ERR_INVALID_MESSAGE, "request-chunk data is not base64", stream_id=frame["id"]
            ) from None
    return data.encode("utf-8")


class BufferBudget:
    """Bytes every stream on one connection holds together, against one ceiling.

    A stream asks before it queues bytes the relay sent and gives them back once
    the Crew's web server has taken them or the stream ends, so the streams of a
    connection can never together hold more than :data:`CONNECTION_BUFFER_MAX_BYTES`.
    """

    def __init__(self, ceiling: int = CONNECTION_BUFFER_MAX_BYTES) -> None:
        self._ceiling = ceiling
        self.held = 0

    def take(self, size: int, sid: int) -> None:
        """Hold *size* more bytes, or raise :class:`FrameError` naming stream *sid*."""
        if self.held + size > self._ceiling:
            raise FrameError(
                ERR_MESSAGE_TOO_LARGE,
                "the Crew is holding too much relayed data on this connection",
                stream_id=sid,
            )
        self.held += size

    def give(self, size: int) -> None:
        if size > self.held:
            # Every give answers one take, so this is an accounting bug: say so
            # rather than let a clamp hide it.
            logger.warning(
                "relay buffer budget gave back %d bytes while holding %d", size, self.held
            )
        self.held = max(0, self.held - size)


class CreditGate:
    """Response bytes one stream may still send before the relay grants more."""

    def __init__(self, initial: int) -> None:
        self._credit = initial
        self._granted = asyncio.Event()
        if initial > 0:
            self._granted.set()

    @property
    def available(self) -> int:
        return self._credit

    @property
    def stalled(self) -> bool:
        """True while the stream waits for a grant."""
        return self._credit <= 0

    def add(self, credit: int) -> None:
        self._credit += credit
        if self._credit > 0:
            self._granted.set()

    async def take(self, want: int) -> int:
        """Wait for credit, then spend up to *want* bytes of it and return how many."""
        while self._credit <= 0:
            self._granted.clear()
            await self._granted.wait()
        taken = min(want, self._credit)
        self._credit -= taken
        return taken


class HttpStream:
    """One relayed HTTP request and its streamed response."""

    def __init__(
        self,
        head: Dict[str, Any],
        *,
        send: SendFrame,
        ingress: CrewIngress,
        on_done: StreamDone,
        credit: Optional[CreditGate],
        first_byte_seconds: float,
        budget: Optional[BufferBudget] = None,
        monotonic: Callable[[], float] = time.monotonic,
        request_credit: bool = False,
        send_unless: Optional[SendUnless] = None,
    ) -> None:
        self.id: int = head["id"]
        self._head = head
        self._send = send
        self._send_unless = send_unless
        self._ingress = ingress
        self._on_done = on_done
        self.credit = credit
        self._first_byte_seconds = first_byte_seconds
        self._budget = budget or BufferBudget()
        self._monotonic = monotonic
        self._body: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._buffered = 0
        #: The body length the head declared; the body handed over is held to it.
        self._declared_length = declared_content_length(head["headers"])
        self._received = 0
        self._body_ended = False
        self._error_sent = False
        #: Set once the stream sent its last frame or is going away, so no
        #: request-body credit follows its end.
        self._ended = False
        #: With ``request-credit`` agreed: all the request-body credit this stream
        #: has extended, the starting credit included. ``None`` when not agreed, and
        #: then the stream sends no credit and holds the relay to none.
        self._request_granted: Optional[int] = (
            REQUEST_CREDIT_INITIAL_BYTES if request_credit else None
        )
        #: Body bytes that have left this process for the web server, not yet given back.
        self._taken_not_granted = 0
        self._last_progress = monotonic()
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        self._task = _start_stream_task(self._run(), f"relay-http-{self.id}", self._finish)

    def _finish(self) -> None:
        """Release the stream: runs once, when its task ends, however it ended."""
        self._ended = True
        while not self._body.empty():
            self._body.get_nowait()
        self._budget.give(self._buffered)
        self._buffered = 0
        self._on_done(self.id)

    def feed(self, data: bytes) -> None:
        """Queue one body chunk. Raises :class:`FrameError` past either buffer bound."""
        if self._body_ended:
            raise FrameError(
                ERR_INVALID_MESSAGE, "request-chunk after request-end", stream_id=self.id
            )
        if not data:
            return
        if self._request_granted is not None and self._received + len(data) > self._request_granted:
            # The relay agreed to send no more body than this stream granted.
            raise FrameError(
                ERR_INVALID_MESSAGE,
                "the request body ran past the request-body credit the Crew granted",
                stream_id=self.id,
            )
        if self._declared_length is not None and self._received + len(data) > self._declared_length:
            # Refused before any byte past the declared length is queued, so the
            # Crew's server can never read the excess as a request of its own.
            raise FrameError(
                ERR_INVALID_MESSAGE,
                "the request body is longer than its content-length",
                stream_id=self.id,
            )
        if self._buffered + len(data) > REQUEST_BUFFER_MAX_BYTES:
            raise FrameError(
                ERR_MESSAGE_TOO_LARGE,
                "the Crew is reading this request body slower than it arrives",
                stream_id=self.id,
            )
        self._budget.take(len(data), self.id)
        self._buffered += len(data)
        self._received += len(data)
        self._body.put_nowait(data)

    def end_body(self) -> None:
        if (
            not self._body_ended
            and self._declared_length is not None
            and self._received != self._declared_length
        ):
            raise FrameError(
                ERR_INVALID_MESSAGE,
                "the request body is shorter than its content-length",
                stream_id=self.id,
            )
        if not self._body_ended:
            self._body_ended = True
            self._body.put_nowait(None)

    def cancel(self) -> None:
        """End the stream with nothing more sent: the viewer or the connection went away."""
        self._ended = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _iter_body(self) -> AsyncIterator[bytes]:
        credited = self._request_granted is not None
        while True:
            piece = await self._body.get()
            if piece is None:
                return
            if not credited:
                self._release(len(piece))
            self._last_progress = self._monotonic()
            yield piece
            if credited:
                # The ingress asks for the next piece only once this one has left
                # the process for the Crew's web server (credited_body), so this
                # is the earliest it counts as taken: until now it stays charged,
                # and credit for it goes back no sooner.
                self._release(len(piece))
                await self._give_back_credit(len(piece))

    def _release(self, size: int) -> None:
        self._buffered -= size
        self._budget.give(size)

    def stop_credit(self) -> None:
        """Send no more request-body credit: the stream is ending."""
        self._ended = True

    def _credit_closed(self) -> bool:
        return self._body_ended or self._ended

    async def _give_back_credit(self, taken: int) -> None:
        """Grant the relay the body bytes that have just left this process for the web server.

        That is as far as the client can see: bytes in the operating system's
        socket buffers count as taken, and once those fill the stream stalls.
        Grants gather to :data:`REQUEST_CREDIT_GRANT_BYTES`, at most the starting
        credit, so a relay that spent all its credit is always answered once the
        Crew's server takes what is queued. Nothing is sent once the relay has
        sent the whole body or the stream has ended, checked again under the send
        lock so a grant can never follow the stream's last frame.
        """
        if self._request_granted is None:
            return
        self._taken_not_granted += taken
        if self._taken_not_granted < REQUEST_CREDIT_GRANT_BYTES or self._credit_closed():
            return
        grant = self._taken_not_granted
        self._taken_not_granted = 0
        self._request_granted += grant
        frame = build_flow_credit(self.id, grant)
        if self._send_unless is not None:
            await _quietly(self._send_unless(frame, self._credit_closed))
        else:
            await _quietly(self._send(frame))

    def _has_body(self) -> bool:
        names = {k.lower() for k in self._head["headers"]}
        if "content-length" in names or "transfer-encoding" in names:
            return True
        return self._head["method"] not in _BODYLESS_METHODS

    async def _run(self) -> None:
        response: Optional[IngressResponse] = None
        try:
            response = await self._open()
            if response is not None:
                await self._relay_response(response)
        except asyncio.CancelledError:
            raise
        except ConnectionError:
            # The host socket closed under us; the relay has already ended the stream.
            pass
        except Exception:
            logger.warning("relayed request %d failed", self.id, exc_info=True)
            # End the stream with a typed error, so the viewer hears at once
            # rather than waiting out the relay's own timeout; once per stream.
            if not self._error_sent:
                await _quietly(
                    self._send_error(
                        ERR_CREW_UNREACHABLE, "the relay client could not complete this request"
                    )
                )
        finally:
            if response is not None:
                await _quietly(response.aclose())

    async def _send_error(self, code: str, message: str) -> None:
        """End this stream with an ``error`` frame; marked first, so it goes out once."""
        self._error_sent = True
        self._ended = True
        await self._send(build_error(code, message, stream_id=self.id))

    async def _open(self) -> Optional[IngressResponse]:
        head = self._head
        request = RelayedRequest(
            method=head["method"],
            path=head["path"],
            query=head["query"],
            headers=tuple(head["headers"].items()),
            user=head["user"],
            credited_body=self._request_granted is not None,
        )
        body = self._iter_body() if self._has_body() else None
        opening = asyncio.ensure_future(self._ingress.open_http(request, body))
        try:
            while True:
                remaining = self._last_progress + self._first_byte_seconds - self._monotonic()
                if remaining <= 0:
                    opening.cancel()
                    await self._send_error(
                        ERR_CREW_NOT_ANSWERING,
                        "the Crew's web server sent no response head in time",
                    )
                    return None
                done, _ = await asyncio.wait({opening}, timeout=remaining)
                if done:
                    break
            return opening.result()
        except (IngressUnreachable, IngressUnavailable) as exc:
            await self._send_error(ERR_CREW_UNREACHABLE, str(exc))
            return None
        finally:
            if not opening.done():
                opening.cancel()

    async def _relay_response(self, response: IngressResponse) -> None:
        headers, cookies = relayable_response_headers(response.headers)
        head: Dict[str, Any] = {
            "type": "response",
            "id": self.id,
            "status": response.status,
            "headers": headers,
            "streaming": True,
        }
        if cookies:
            head["setCookies"] = cookies
        problem = response_head_problem(head)
        if problem is not None:
            await self._send_error(ERR_INVALID_MESSAGE, problem)
            return
        await self._send(head)
        try:
            async for piece in response.iter_body(BODY_CHUNK_RAW_BYTES):
                await self._send_body(piece)
        except (asyncio.CancelledError, ConnectionError):
            raise
        except Exception as exc:
            # The head is out, so the viewer has a status already; an error frame
            # makes the relay cut the viewer's body rather than end it cleanly.
            await self._send_error(
                ERR_CREW_UNREACHABLE,
                f"the Crew's web server failed mid-body: {type(exc).__name__}",
            )
            return
        self._ended = True
        await self._send({"type": "response-end", "id": self.id})

    async def _send_body(self, piece: bytes) -> None:
        view = memoryview(piece)
        while view:
            size = len(view) if self.credit is None else await self.credit.take(len(view))
            data = base64.b64encode(view[:size]).decode("ascii")
            await self._send(
                {"type": "response-chunk", "id": self.id, "data": data, "base64": True}
            )
            view = view[size:]


class WsReassembler:
    """Rebuild WebSocket messages the relay sent in ``partial`` pieces.

    Bounds, from the contract: one message at most 10 MiB, at most 50 MiB and 10
    messages in reassembly on one connection, and at most 10 seconds between
    nonempty pieces. Empty pieces cost one byte of connection budget, and every
    message is also bounded by WS_REASSEMBLY_MAX_PIECES retained fragments.
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._parts: Dict[int, List[str]] = {}
        self._sizes: Dict[int, int] = {}
        self._binary_chars: Dict[int, int] = {}
        self._binary_tails: Dict[int, str] = {}
        self._charges: Dict[int, int] = {}
        self._last: Dict[int, float] = {}

    def _total(self) -> int:
        return sum(self._charges.values())

    def stale(self) -> List[int]:
        """Ids whose next piece is overdue; the caller ends those streams."""
        now = self._monotonic()
        return [
            sid for sid, at in self._last.items() if now - at > WS_REASSEMBLY_PIECE_TIMEOUT_SECONDS
        ]

    def drop(self, sid: int) -> None:
        self._parts.pop(sid, None)
        self._sizes.pop(sid, None)
        self._binary_chars.pop(sid, None)
        self._binary_tails.pop(sid, None)
        self._charges.pop(sid, None)
        self._last.pop(sid, None)

    def add(self, frame: Dict[str, Any]) -> Optional[Union[str, bytes]]:
        """Take one ``ws-frame``; return the whole message once its last piece lands."""
        sid = frame["id"]
        data: str = frame["data"]
        binary = bool(frame.get("binary"))
        size = (len(data) * 3) // 4 if binary else len(data.encode("utf-8"))
        if frame.get("partial") or sid in self._parts:
            if sid not in self._parts and len(self._parts) >= WS_REASSEMBLY_CONCURRENT:
                raise FrameError(
                    ERR_MESSAGE_TOO_LARGE, "too many messages in reassembly", stream_id=sid
                )
            if len(self._parts.get(sid, ())) >= WS_REASSEMBLY_MAX_PIECES:
                self.drop(sid)
                raise FrameError(
                    ERR_MESSAGE_TOO_LARGE, "too many WebSocket message pieces", stream_id=sid
                )
            new_size = self._sizes.get(sid, 0) + size
            # Count every fragment, including one whose binary flag differs.
            # Keep the original per-piece total for text-mode limit checks.
            chars = self._binary_chars.get(sid, 0) + len(data)
            tail = (self._binary_tails.get(sid, "") + data[-2:])[-2:]
            self._binary_chars[sid] = chars
            self._binary_tails[sid] = tail
            message_size = new_size
            if binary:
                # Pieces split ONE base64 string, even between its two padding
                # characters. Floor once for the accumulated text, not per piece.
                message_size = (chars * 3) // 4 - (len(tail) - len(tail.rstrip("=")))
            if message_size > WS_MESSAGE_MAX_BYTES:
                self.drop(sid)
                raise FrameError(
                    ERR_MESSAGE_TOO_LARGE, "WebSocket message is over 10 MiB", stream_id=sid
                )
            charge = max(1, size)
            if self._total() + charge > WS_REASSEMBLY_PER_CONNECTION_BYTES:
                self.drop(sid)
                raise FrameError(
                    ERR_MESSAGE_TOO_LARGE, "too much WebSocket data in reassembly", stream_id=sid
                )
            self._parts.setdefault(sid, []).append(data)
            self._sizes[sid] = new_size
            self._charges[sid] = self._charges.get(sid, 0) + charge
            if data or sid not in self._last:
                self._last[sid] = self._monotonic()
            if frame.get("partial"):
                return None
            data = "".join(self._parts[sid])
            self.drop(sid)
        if not binary:
            return data
        try:
            return base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise FrameError(
                ERR_INVALID_MESSAGE, "ws-frame data is not base64", stream_id=sid
            ) from None


class WsStream:
    """One relayed WebSocket between a viewer and the Crew's web server."""

    def __init__(
        self,
        head: Dict[str, Any],
        *,
        send: SendFrame,
        ingress: CrewIngress,
        on_done: StreamDone,
        pause_watchdog_seconds: float,
        budget: Optional[BufferBudget] = None,
    ) -> None:
        self.id: int = head["id"]
        self._head = head
        self._send = send
        self._ingress = ingress
        self._on_done = on_done
        self._pause_watchdog_seconds = pause_watchdog_seconds
        self._budget = budget or BufferBudget()
        self._to_crew: asyncio.Queue[Union[str, bytes]] = asyncio.Queue()
        self._to_crew_bytes = 0
        self._to_crew_count = 0
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._watchdog: Optional[asyncio.TimerHandle] = None
        self._closed_by_relay = False
        self._relay_close_code: Optional[int] = None
        self._relay_close_reason = ""
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def paused(self) -> bool:
        return not self._resumed.is_set()

    def start(self) -> None:
        self._task = _start_stream_task(self._run(), f"relay-ws-{self.id}", self._finish)

    def _finish(self) -> None:
        """Release the stream: runs once, when its task ends, however it ended."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        while not self._to_crew.empty():
            self._to_crew.get_nowait()
        self._budget.give(self._to_crew_bytes)
        self._to_crew_bytes = 0
        self._to_crew_count = 0
        self._on_done(self.id)

    def from_relay(self, message: Union[str, bytes]) -> None:
        """Queue one message for the Crew's side. Raises past either buffer bound.

        A message always fits when nothing is queued ahead of it, so every message
        the contract allows (up to 10 MiB) can be delivered; the per-stream bound
        caps only the backlog behind a slow Crew.
        """
        if self._to_crew_count >= WS_QUEUE_MAX_MESSAGES:
            raise FrameError(
                ERR_MESSAGE_TOO_LARGE,
                "too many WebSocket messages waiting for the Crew",
                stream_id=self.id,
            )
        size = len(message) if isinstance(message, bytes) else len(message.encode("utf-8"))
        if self._to_crew_bytes and self._to_crew_bytes + size > REQUEST_BUFFER_MAX_BYTES:
            raise FrameError(
                ERR_MESSAGE_TOO_LARGE,
                "the Crew is reading this WebSocket slower than it arrives",
                stream_id=self.id,
            )
        self._budget.take(size, self.id)
        self._to_crew_bytes += size
        self._to_crew_count += 1
        self._to_crew.put_nowait(message)

    def set_paused(self, paused: bool) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if paused:
            self._resumed.clear()
            # The relay may never send the resume; the read side picks up again
            # on its own, as the contract asks.
            self._watchdog = asyncio.get_running_loop().call_later(
                self._pause_watchdog_seconds, self._resumed.set
            )
        else:
            self._resumed.set()

    def relay_closed(self, code: Optional[int], reason: str) -> None:
        """The viewer closed: close the Crew's side with the same code, send nothing back."""
        self._closed_by_relay = True
        self._relay_close_code = code
        self._relay_close_reason = reason
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def wait(self) -> None:
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        writer: Optional[asyncio.Task[None]] = None
        local: Optional[IngressWebSocket] = None
        try:
            local = await self._open()
            if local is None:
                return
            writer = asyncio.create_task(self._write_to_crew(local))
            await self._read_from_crew(local)
            writer.cancel()
            await self._send(build_ws_close(self.id, local.close_code))
        except asyncio.CancelledError:
            if local is not None:
                if self._closed_by_relay:
                    code, reason = self._relay_close_code or 1000, self._relay_close_reason
                else:
                    # The host connection went away under this socket.
                    code, reason = 1001, "relay connection closed"
                await _quietly(local.close(code, reason))
                local = None
            raise
        except ConnectionError:
            pass
        except Exception:
            logger.warning("relayed WebSocket %d failed", self.id, exc_info=True)
            await _quietly(self._send(build_ws_close(self.id, None, "relay client error")))
        finally:
            if writer is not None and not writer.done():
                writer.cancel()
            if writer is not None:
                # Settled before the stream is released, so its in-flight
                # message is given back exactly once, by the writer itself.
                await asyncio.gather(writer, return_exceptions=True)
            if local is not None:
                await _quietly(local.close())

    async def _open(self) -> Optional[IngressWebSocket]:
        head = self._head
        protocols = tuple(p.strip() for p in head.get("protocols", "").split(",") if p.strip())
        request = RelayedWebSocket(
            path=head["path"],
            query=head["query"],
            protocols=protocols,
            headers=tuple((k, v) for k, v in head.get("headers", [])),
            user=head["user"],
        )
        try:
            return await self._ingress.open_websocket(request)
        except IngressRefused as exc:
            await self._send(
                build_ws_close(
                    self.id,
                    WS_CLOSE_CREW_REFUSED,
                    f"Crew refused the WebSocket upgrade ({exc.status})",
                )
            )
        except IngressUnreachable:
            await self._send(
                build_ws_close(self.id, WS_CLOSE_CREW_REFUSED, "Crew web server unreachable")
            )
        except IngressUnavailable:
            await self._send(
                build_ws_close(self.id, WS_CLOSE_CREW_REFUSED, "Crew relay hand-off unavailable")
            )
        return None

    async def _write_to_crew(self, local: IngressWebSocket) -> None:
        while True:
            message = await self._to_crew.get()
            size = len(message) if isinstance(message, bytes) else len(message.encode("utf-8"))
            try:
                await local.send(message)
            finally:
                # Held until the Crew's side has taken it, so a slow Crew keeps
                # the message it is reading inside the budget too.
                self._to_crew_bytes -= size
                self._to_crew_count -= 1
                self._budget.give(size)

    async def _read_from_crew(self, local: IngressWebSocket) -> None:
        while True:
            await self._resumed.wait()
            message = await local.receive()
            if message is None:
                return
            if isinstance(message, bytes):
                data, binary = base64.b64encode(message).decode("ascii"), True
            else:
                data, binary = message, False
            pieces = split_ws_data(data)
            for index, piece in enumerate(pieces):
                frame: Dict[str, Any] = {"type": "ws-frame", "id": self.id, "data": piece}
                if binary:
                    frame["binary"] = True
                if index < len(pieces) - 1:
                    frame["partial"] = True
                await self._send(frame)


async def _quietly(awaitable: Awaitable[Any]) -> None:
    try:
        await awaitable
    except Exception:
        logger.debug("relay stream cleanup step failed", exc_info=True)


def _start_stream_task(
    coro: Coroutine[Any, Any, None], name: str, finish: Callable[[], None]
) -> "asyncio.Task[None]":
    """Run one stream's coroutine, calling *finish* exactly once when the task ends.

    A done callback rather than the coroutine's ``finally``: a task cancelled before
    its first step never enters the coroutine, so its ``finally`` never runs, and a
    stream released only there would stay registered on the connection, holding
    its queued body, for as long as the connection lives.
    """
    task = asyncio.create_task(coro, name=name)

    def _done(_task: "asyncio.Task[None]") -> None:
        try:
            finish()
        except Exception:
            logger.warning("releasing relay stream %s failed", name, exc_info=True)

    task.add_done_callback(_done)
    return task

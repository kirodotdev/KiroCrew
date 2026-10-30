"""Gateway side of a remote session: a resilient stdio pair over ``wire``.

:class:`RemoteLink` pings every ``keepalive_seconds``, drops a connection silent
for ``stall_seconds``, redials with jittered backoff, and reports LOST after
``lease_seconds`` unauthenticated or RETIRING when the worker retires. An
authentication failure is never retried.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Awaitable, Callable, Optional

from kiro_crew.remote_session import wire

logger = logging.getLogger("kiro_crew.remote_session.link")

Dial = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

CONNECTING = "connecting"
CONNECTED = "connected"
RECONNECTING = "reconnecting"
RETIRING = "retiring"
FINISHED = "finished"
LOST = "lost"
REFUSED = "refused"
CLOSED = "closed"
_TERMINAL = frozenset({FINISHED, LOST, REFUSED, CLOSED})
TICK_SECONDS = 0.25
_TERMINATOR = re.compile(rb"[\r\n]")


class LinkUnavailable(ConnectionError):
    """The remote session cannot carry input (lost, refused or closed)."""


class _ConsumerGate:
    """The transport ``RemoteLink.stdout`` pauses when its reader falls behind;
    ``resumed_at`` lets the stall check skip time spent waiting on the reader."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.open = asyncio.Event()
        self.open.set()
        self.resumed_at = 0.0

    def pause_reading(self) -> None:
        self.open.clear()

    def resume_reading(self) -> None:
        self.resumed_at = self._clock()
        self.open.set()


class RemoteLink:
    def __init__(
        self,
        session_id: str,
        secret: bytes,
        dial: Dial,
        *,
        keepalive_seconds: float = 60.0,
        stall_seconds: float = 180.0,
        lease_seconds: float = 300.0,
        handshake_seconds: float = 30.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        max_unacked_bytes: int = wire.DEFAULT_MAX_UNACKED_BYTES,
        clock: Callable[[], float] = time.monotonic,
        on_state: Optional[Callable[[str], None]] = None,
    ) -> None:
        wire.validate_session_id(session_id)
        if len(secret) < wire.SECRET_MIN_BYTES:
            raise ValueError("session secret is too short")
        if not 0 < keepalive_seconds < stall_seconds <= lease_seconds:
            raise ValueError("need 0 < keepalive < stall <= lease")
        self.session_id = session_id
        self._secret = secret
        self._dial = dial
        self.keepalive_seconds = keepalive_seconds
        self.stall_seconds = stall_seconds
        self.lease_seconds = lease_seconds
        self.handshake_seconds = handshake_seconds
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._clock = clock
        self._on_state = on_state
        self.ep = wire.Endpoint(wire.GATEWAY, max_unacked_bytes=max_unacked_bytes)
        self.stdout = asyncio.StreamReader(limit=wire.MAX_LINE_BYTES * 64)
        # While stdout's flow control holds this gate shut nothing is read or
        # acked, so the worker stops at its unacked window.
        self._consumer = _ConsumerGate(clock)
        self.stdout.set_transport(self._consumer)  # type: ignore[arg-type]
        self.state = CONNECTING
        self.retire_reason = ""
        self.error = ""
        self.connections = 0
        self.lease_started = clock()
        self.last_authenticated = 0.0
        self._writer: Optional[asyncio.StreamWriter] = None
        self._attached = asyncio.Event()
        self._room = asyncio.Event()
        self._ping_n = 0
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def wait_connected(self, timeout: float) -> None:
        await asyncio.wait_for(self._attached.wait(), timeout)

    async def write(self, data: bytes) -> None:
        """Send stdin bytes, waiting (not dropping) when the buffer is full."""
        if self.state in _TERMINAL:
            raise LinkUnavailable(f"remote session is {self.state}")
        while not self.ep.can_accept(len(data)):
            if self.state in _TERMINAL:
                raise LinkUnavailable(f"remote session is {self.state}")
            self._room.clear()
            await self._room.wait()
        self._send(self.ep.write(data, self._clock()))

    def close_input(self) -> None:
        """Signal end of stdin; the worker closes the child's stdin after delivery."""
        self._send(self.ep.close("stdin-closed"))

    async def aclose(self) -> None:
        self._set_state(CLOSED)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._drop()
        if not self.stdout.at_eof():
            self.stdout.feed_eof()

    def health(self) -> dict:
        now = self._clock()
        return {
            "state": self.state,
            "attached": self.ep.attached,
            "connections": self.connections,
            "lease_age_seconds": round(now - (self.last_authenticated or self.lease_started), 1),
            "last_inbound_seconds": (
                round(now - self.ep.last_inbound, 1) if self.ep.last_inbound else None
            ),
            "unacked_bytes": self.ep.unacked_bytes,
            "retire_reason": self.retire_reason,
            "error": self.error,
            "stats": dict(self.ep.stats),
        }

    def _set_state(self, state: str) -> None:
        if self.state == state or self.state in _TERMINAL:
            return
        self.state = state
        logger.info("remote session %s: %s", self.session_id, state)
        if state in _TERMINAL:
            self._room.set()
            self._consumer.open.set()  # nothing waits on a reader that is finished
            if state != FINISHED and not self.stdout.at_eof():
                self.stdout.set_exception(LinkUnavailable(f"remote session is {state}"))
        if self._on_state is not None:
            self._on_state(state)

    def _send(self, data: bytes) -> None:
        if data and self._writer is not None and not self._writer.is_closing():
            self._writer.write(data)

    def _drop(self) -> None:
        self.ep.detach()
        self._attached.clear()
        if self._writer is not None:
            self._writer.close()
        self._writer = None

    def _lease_expired(self) -> bool:
        anchor = self.last_authenticated or self.lease_started
        return self._clock() - anchor > self.lease_seconds

    async def _run(self) -> None:
        backoff = self.backoff_initial
        while self.state not in _TERMINAL:
            if self._lease_expired():
                self.error = self.error or "no authenticated connection within the lease"
                self._set_state(LOST)
                return
            try:
                await self._connect_once()
                backoff = self.backoff_initial
            except wire.AuthError as exc:
                self.error = f"authentication failed: {exc}"
                self._set_state(REFUSED)
                return
            except wire.WireError as exc:
                # The peer lost or invented stream state: resending cannot repair it.
                self.error = f"protocol error: {exc}"
                self._set_state(LOST)
                return
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self._drop()
            if self.ep.peer_finished:
                self.stdout.feed_eof()
                self._set_state(FINISHED)
                return
            if self.state in _TERMINAL:
                return
            if self.state != RETIRING:
                self._set_state(RECONNECTING)
            await asyncio.sleep(backoff * random.uniform(0.5, 1.0))
            backoff = min(backoff * 2, self.backoff_max)

    async def _connect_once(self) -> None:
        reader, writer = await asyncio.wait_for(self._dial(), self.handshake_seconds)
        self._writer = writer
        shake = wire.GatewayHandshake(self.session_id, self._secret)
        buf = b""

        async def line() -> bytes:
            nonlocal buf
            while True:
                buf = buf.lstrip(b"\r\n")
                match = _TERMINATOR.search(buf)
                if match:
                    first, buf = buf[: match.start()], buf[match.end() :]
                    return first
                if len(buf) > wire.MAX_HANDSHAKE_LINE_BYTES:
                    raise wire.AuthError("handshake line too long")
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionError("closed during handshake")
                buf += chunk

        hello = await asyncio.wait_for(line(), self.handshake_seconds)
        writer.write(shake.on_hello(hello, self.ep.recv_next))
        welcome = await asyncio.wait_for(line(), self.handshake_seconds)
        attachment = shake.on_welcome(welcome)
        now = self._clock()
        self._send(self.ep.attach(attachment, now, leftover=buf))
        self.connections += 1
        self.last_authenticated = now
        self._attached.set()
        if self.state != RETIRING:
            self._set_state(CONNECTED)
        ticker = asyncio.ensure_future(self._tick())
        try:
            # Anything the worker sent right after its welcome is already in buf.
            await self._consume(b"")
            while chunk := await reader.read(65536):
                await self._consume(chunk)
                if self.ep.peer_finished:
                    return
                # Read on only once the session's reader has caught up.
                await self._consumer.open.wait()
            raise ConnectionError("worker closed the connection")
        finally:
            ticker.cancel()

    async def _consume(self, chunk: bytes) -> None:
        now = self._clock()
        data, out, events = self.ep.feed(chunk, now)
        if data:
            self.stdout.feed_data(data)
        self._send(out)
        self._room.set()
        for kind, arg in events:
            if kind == "retire":
                self.retire_reason = arg
                self._set_state(RETIRING)

    async def _tick(self) -> None:
        last_ping = self._clock()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            now = self._clock()
            # Only authenticated inbound traffic renews the lease.
            self.last_authenticated = max(self.last_authenticated, self.ep.last_inbound)
            self._send(self.ep.tick(now))
            self._room.set()
            if now - last_ping >= self.keepalive_seconds:
                self._ping_n += 1
                self._send(self.ep.ping(self._ping_n))
                last_ping = now
            if not self._consumer.open.is_set():
                continue  # this side stopped reading; the connection is not silent
            quiet_since = max(self.ep.last_inbound, self._consumer.resumed_at)
            if now - quiet_since > self.stall_seconds:
                logger.warning("remote session %s: connection stalled", self.session_id)
                if self._writer is not None:
                    self._writer.close()
                return

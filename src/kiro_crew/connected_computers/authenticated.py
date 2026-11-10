"""Connection-bound integrity for the connector wire; SSH supplies confidentiality.

Every application frame after auth, including ready and stop, is an envelope
with a directional sequence and HMAC. The peer key never crosses the socket.
A relay can observe, delay or drop traffic, but cannot change or replay it.
Callers serialize encode AND send together; any error ends this connection.

Dropping is caught by :class:`Liveness`: each side sends an authenticated
``ping`` every :data:`.protocol.HEARTBEAT_SECS` and drops the connection when no
authenticated frame arrived for :data:`.protocol.LIVENESS_DEADLINE_SECS`, so a
relay that drops the tail of the traffic while keeping the WebSocket's own pings
and pongs alive cannot hold a connection (or its processes) open. The deadline
is a watchdog of its own: a relay that stops reading, so that sends and the send
lock stall, does not hold it either.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Awaitable, Callable, Final, Optional, TypeVar

from kiro_crew.connected_computers import protocol

_SESSION_LABEL: Final[bytes] = b"kirocrew-connected-computer-session-v1"
_MAX_SEQUENCE: Final[int] = (1 << 64) - 1


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


class SessionFrames:
    """One socket's directional keys and strictly consecutive send/receive counters."""

    def __init__(self, peer_key: str, bound: tuple[str, str, str, str], *, gateway: bool) -> None:
        if not protocol.valid_nonce(peer_key) or not all(
            protocol.valid_nonce(nonce) for nonce in bound[2:]
        ):
            raise protocol.ProtocolError("invalid session key or nonce")
        key = hmac.new(bytes.fromhex(peer_key), _SESSION_LABEL + _canonical(bound), hashlib.sha256)
        client_key = hmac.digest(key.digest(), b"client-to-gateway", "sha256")
        gateway_key = hmac.digest(key.digest(), b"gateway-to-client", "sha256")
        self._send_key, self._recv_key = (
            (gateway_key, client_key) if gateway else (client_key, gateway_key)
        )
        self._send_sequence = 0
        self._recv_sequence = 0
        self._failed = False

    def encode(self, frame: dict[str, Any]) -> str:
        if self._failed or self._send_sequence > _MAX_SEQUENCE:
            raise protocol.ProtocolError("session ended")
        envelope = {"seq": self._send_sequence, "frame": frame}
        mac = hmac.digest(self._send_key, _canonical(envelope), "sha256").hex()
        text = protocol.encode({**envelope, "mac": mac})
        self._send_sequence += 1
        return text

    def decode(self, text: Any, *, allowed: frozenset[str]) -> dict[str, Any]:
        try:
            return self._decode(text, allowed=allowed)
        except (ValueError, TypeError, RecursionError) as exc:
            self._failed = True
            raise protocol.ProtocolError("invalid authenticated frame") from exc

    def _decode(self, text: Any, *, allowed: frozenset[str]) -> dict[str, Any]:
        if (
            self._failed
            or not isinstance(text, str)
            or len(text.encode("utf-8")) > protocol.MAX_FRAME_BYTES
        ):
            raise protocol.ProtocolError("session ended or frame too large")
        envelope = json.loads(text)
        if not isinstance(envelope, dict) or set(envelope) != {"seq", "frame", "mac"}:
            raise protocol.ProtocolError("invalid envelope")
        sequence, mac = envelope["seq"], envelope["mac"]
        if (
            type(sequence) is not int
            or sequence != self._recv_sequence
            or sequence > _MAX_SEQUENCE
            or not protocol.valid_nonce(mac)
        ):
            raise protocol.ProtocolError("invalid sequence or MAC")
        expected = hmac.digest(
            self._recv_key,
            _canonical({"seq": sequence, "frame": envelope["frame"]}),
            "sha256",
        ).hex()
        if not hmac.compare_digest(expected, mac):
            raise protocol.ProtocolError("invalid MAC")
        frame = protocol.decode(protocol.encode(envelope["frame"]), allowed=allowed)
        self._recv_sequence += 1
        return frame


_T = TypeVar("_T")


def _consume(task: "asyncio.Future[Any]") -> None:
    """Retrieve a finished send's outcome so an abandoned one never warns."""
    if not task.cancelled():
        task.exception()


class Liveness:
    """Authenticated progress on one connection: heartbeats out, a deadline in.

    :meth:`progress` is called for every frame that passed its MAC and sequence
    check, and only for those: a transport ping or pong is not progress.
    :meth:`run` runs two independent tasks until :attr:`lost` is set:

    * a watchdog that sleeps until ``deadline`` after the last progress and sets
      :attr:`lost` once it passed. It never sends and never takes a send lock,
      so a stalled send cannot hold it;
    * a pinger that sends a ``ping`` every ``interval``. Each send is bounded by
      ``send_timeout`` (the deadline unless given); one that fails or does not
      finish in time sets :attr:`lost`.

    :meth:`receive` answers None once liveness is lost. Losing liveness
    interrupts the connection. It is never a refusal. ``clock`` and ``sleep``
    are injected so tests move time without waiting for it.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = protocol.HEARTBEAT_SECS,
        deadline: float = protocol.LIVENESS_DEADLINE_SECS,
        send_timeout: Optional[float] = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self.interval = interval
        self.deadline = deadline
        self.send_timeout = deadline if send_timeout is None else send_timeout
        self._last = clock()
        self.lost = asyncio.Event()

    def progress(self) -> None:
        """An authenticated frame arrived."""
        self._last = self._clock()

    def stale(self) -> bool:
        return self._clock() - self._last >= self.deadline

    async def run(self, send_ping: Callable[[], Awaitable[None]]) -> None:
        """Watch the deadline and ping until liveness is lost (or this is cancelled)."""
        tasks = [
            asyncio.ensure_future(self._watchdog()),
            asyncio.ensure_future(self._pinger(send_ping)),
        ]
        try:
            await self.lost.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watchdog(self) -> None:
        while not self.lost.is_set():
            remaining = self._last + self.deadline - self._clock()
            if remaining <= 0:
                self.lost.set()
                return
            await self._sleep(remaining)

    async def _pinger(self, send_ping: Callable[[], Awaitable[None]]) -> None:
        while not self.lost.is_set():
            await self._sleep(self.interval)
            if self.lost.is_set():
                return
            sending = asyncio.ensure_future(send_ping())
            sending.add_done_callback(_consume)
            bound = asyncio.ensure_future(self._sleep(self.send_timeout))
            try:
                await asyncio.wait({sending, bound}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                bound.cancel()
                if not sending.done():
                    # Never awaited: a send stuck in its lock or the transport
                    # must not hold this task either.
                    sending.cancel()
            if not sending.done() or sending.cancelled() or sending.exception() is not None:
                self.lost.set()
                return

    async def receive(self, receive: Callable[[], Awaitable[_T]]) -> Optional[_T]:
        """``receive()``'s message, or None once liveness is lost (it is cancelled)."""
        if self.lost.is_set():
            return None
        incoming = asyncio.ensure_future(receive())
        lost = asyncio.ensure_future(self.lost.wait())
        try:
            await asyncio.wait({incoming, lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            lost.cancel()
            if not incoming.done():
                incoming.cancel()
            await asyncio.gather(incoming, lost, return_exceptions=True)
        if incoming.cancelled():
            return None
        return incoming.result()

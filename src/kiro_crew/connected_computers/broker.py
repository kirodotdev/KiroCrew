"""Remote gateway: the live connector sockets, one per computer, and their channels.

A computer has at most one live connection here. A second connection for the
same device id is accepted only with a credential of the same or a newer
generation (the record's ``cgen``, already verified by :mod:`.connector_token`), and
then it REPLACES the first, whose channels end as interrupted; a credential for
another device can never claim this one, since the device id comes from the
verified credential, not from the connection (B11).

A channel belongs to exactly one connection. When that connection ends -- the
socket dropped, the computer was revoked, the feature was turned off -- every
channel on it ends as interrupted and is never re-opened elsewhere: nothing is
resent, and nothing moves to another connection or computer (B08). New work
after a reconnect opens new channels.

Bounded throughout: :data:`~.protocol.MAX_CHANNELS` channels per chat (session
key) on a connection, :data:`~.protocol.MAX_CONNECTION_CHANNELS` per connection
across its chats, a credit window per channel and direction, and
:data:`MAX_LIVE_DEVICES` live connections per gateway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from typing import Any, AsyncIterator, Awaitable, Callable, Final, Optional

from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.connector_token import CredentialRejected
from kiro_crew.connected_computers.devices import DeviceRecord
from kiro_crew.remote_session.link import LinkUnavailable
from kiro_crew.session_environment import SessionEnvironmentUnavailable

logger = logging.getLogger(__name__)

#: Live connections one gateway holds.
MAX_LIVE_DEVICES: Final[int] = 32
#: How long the computer has to answer an ``open``.
OPEN_TIMEOUT_SECS: Final[float] = 30.0
#: Each stop I/O phase, including waiting for the send lock, is bounded.
_STOP_IO_TIMEOUT_SECS: Final[float] = 5.0
#: Sessions whose interruption notes are kept for the next turn's context.
_MAX_NOTED_SESSIONS: Final[int] = 256

STATE_OPENING: Final[str] = "opening"
STATE_OPEN: Final[str] = "open"
STATE_FINISHED: Final[str] = "finished"
STATE_INTERRUPTED: Final[str] = "interrupted"
STATE_CLOSED: Final[str] = "closed"


def interrupted_message(label: str) -> str:
    return (
        f"The connection to {label} was lost while this ran, so whether it finished "
        "is unknown. It was not retried and did not run anywhere else. Check the "
        "computer's files before running it again."
    )


class DeviceOffline(SessionEnvironmentUnavailable):
    """The chat's computer has no live connection; the chat stays bound to it."""


class ChannelInterrupted(LinkUnavailable):
    """The channel's connection ended while it was open: the outcome is unknown."""


class _Reader:
    """``link.stdout``: reads the channel's buffer and grants the computer credit."""

    def __init__(self, channel: "Channel") -> None:
        self._channel = channel

    async def read(self, n: int) -> bytes:
        data = await self._channel._recv.read(n)
        if data:
            self._channel._grant(len(data))
        return data


class Channel:
    """One tool server's byte stream to the computer, on one connection."""

    def __init__(
        self, conn: "DeviceConnection", ch: int, kind: str, session_key: str, generation: int = 0
    ) -> None:
        self.conn = conn
        #: The connection generation this channel lives on; it never moves.
        self.generation = generation
        self.id = ch
        self.kind = kind
        self.session_key = session_key
        self.state = STATE_OPENING
        self.error = ""
        self._recv = protocol.RecvBuffer()
        self._send = protocol.SendWindow()
        self._opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.stdout = _Reader(self)

    async def wait_opened(self, timeout: float = OPEN_TIMEOUT_SECS) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(self._opened), timeout)
        except asyncio.TimeoutError as exc:
            await self.aclose()
            raise ChannelInterrupted("the computer did not answer in time") from exc

    async def write(self, data: bytes) -> None:
        if self.state != STATE_OPEN:
            raise ChannelInterrupted(self.error or "channel is not open")
        for start in range(0, len(data), protocol.MAX_DATA_BYTES):
            piece = data[start : start + protocol.MAX_DATA_BYTES]
            try:
                await self._send.acquire(len(piece))
            except protocol.WindowClosed as exc:
                raise ChannelInterrupted(self.error or "channel ended") from exc
            await self.conn.send(protocol.data_frames(self.id, piece)[0])

    def close_input(self) -> None:
        if self.state == STATE_OPEN:
            self.conn.send_later({"op": "eof", "ch": self.id})

    async def aclose(self) -> None:
        sent = self._close()
        if sent is not None:
            await asyncio.shield(sent)

    def abandon(self) -> None:
        """Close without waiting: an open that was cancelled or failed half way."""
        self._close()

    def _close(self) -> Optional[asyncio.Future[None]]:
        # Out of the channel table before anything is awaited, so a frame the
        # computer sent before it reads the close is dropped by ``handle`` instead
        # of failing the whole connection; ``send_close`` keeps the number unused
        # until the close is on the wire.
        closing = self.state in (STATE_OPENING, STATE_OPEN)
        if closing:
            self.state = STATE_CLOSED
        self._end(ChannelInterrupted("channel closed"), eof=True)
        if closing and self.conn.alive:
            return self.conn.send_close(self.id)
        return None

    def _grant(self, n: int) -> None:
        if self.state == STATE_OPEN:
            self.conn.send_later({"op": "credit", "ch": self.id, "bytes": n})

    def _end(self, error: Optional[BaseException], *, eof: bool) -> None:
        # Only this channel: its number may already belong to a newer one.
        if self.conn.channels.get(self.id) is self:
            del self.conn.channels[self.id]
        self._send.close()
        if eof:
            self._recv.feed_eof()
        elif error is not None:
            self._recv.fail(error)
        if not self._opened.done():
            self._opened.set_exception(
                error if error is not None else ChannelInterrupted("channel ended")
            )
            # Retrieved here so an unawaited open never logs "never retrieved".
            self._opened.exception()

    # -- frames from the computer -------------------------------------------------
    # The computer answers an ``open`` with ``opened`` or ``refused`` before any
    # other frame for it, so a data, eof, credit or closed frame reaching a channel
    # that is not open belongs to an earlier channel with the same number, sent
    # before the computer read that channel's ``close``: it is dropped, never
    # applied to this channel and never a protocol error.
    def on_opened(self) -> None:
        if self.state == STATE_OPENING:
            self.state = STATE_OPEN
            if not self._opened.done():
                self._opened.set_result(None)

    def on_refused(self, why: str) -> None:
        self.state = STATE_CLOSED
        self.error = why or "the computer refused"
        self._end(ChannelInterrupted(self.error), eof=False)

    def on_data(self, data: bytes) -> None:
        if self.state == STATE_OPEN:
            self._recv.feed(data)

    def on_eof(self) -> None:
        if self.state == STATE_OPEN:
            self._recv.feed_eof()

    def on_closed(self, why: str) -> None:
        if self.state != STATE_OPEN:
            return
        # The tool server ended on its own: whatever it wrote has arrived.
        self.state = STATE_FINISHED
        self.error = why
        self._end(None, eof=True)

    def on_credit(self, n: int) -> None:
        if self.state == STATE_OPEN:
            self._send.grant(n)

    def on_lost(self, why: str) -> None:
        if self.state in (STATE_FINISHED, STATE_CLOSED):
            return
        self.state = STATE_INTERRUPTED
        self.error = why
        self._end(ChannelInterrupted(why), eof=False)


Sender = Callable[[str], Awaitable[None]]


class DeviceConnection:
    """The connector socket of one computer, after its credential was verified."""

    def __init__(
        self,
        record: DeviceRecord,
        sender: Sender,
        *,
        root: str,
        mcp: tuple[str, ...] = (),
        platform: str = "",
        confinement: str = "",
        closer: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.record = record
        self.root = root
        self.mcp = tuple(mcp)
        self.platform = platform
        self.confinement = confinement
        self.generation = 0
        self.alive = True
        self.channels: dict[int, Channel] = {}
        self._sender = sender
        self._closer = closer
        self._next_ch = 1
        #: Numbers of channels closed here whose ``close`` is not on the wire yet.
        self._closing: set[int] = set()
        self._send_lock = asyncio.Lock()
        self._pending: set[asyncio.Task[None]] = set()

    @property
    def device_id(self) -> str:
        return self.record.device_id

    async def send(self, frame: dict[str, Any]) -> None:
        if not self.alive:
            raise ChannelInterrupted("the computer is not connected")
        text = protocol.encode(frame)
        async with self._send_lock:
            await self._sender(text)

    def send_later(self, frame: dict[str, Any]) -> None:
        """Send without waiting (credit, eof); a failure ends the connection."""

        async def run() -> None:
            try:
                await self.send(frame)
            except Exception as exc:  # noqa: BLE001 -- the socket is gone
                self.lost(f"send failed: {type(exc).__name__}")

        task = asyncio.ensure_future(run())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def send_close(self, ch: int) -> asyncio.Future[None]:
        """Send ``close`` for ``ch``; the number is not reused until it is on the wire.

        A new channel given ``ch`` sooner could put its ``open`` ahead of this
        ``close``. The send runs on its own, so a cancelled caller still sends it;
        a failure ends the connection, as in :meth:`send_later`.
        """
        self._closing.add(ch)

        async def run() -> None:
            try:
                await self.send({"op": "close", "ch": ch})
            except Exception as exc:  # noqa: BLE001 -- the socket is gone
                self.lost(f"send failed: {type(exc).__name__}")
            finally:
                self._closing.discard(ch)

        task = asyncio.ensure_future(run())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    def _channel_id(self) -> int:
        for _ in range(protocol.MAX_CHANNEL_ID):
            ch = self._next_ch
            self._next_ch = ch % protocol.MAX_CHANNEL_ID + 1
            if ch not in self.channels and ch not in self._closing:
                return ch
        raise ChannelInterrupted("no free channel number")

    async def open(self, kind: str, session_key: str) -> Channel:
        protocol.kind_of(kind)
        if not self.alive:
            raise ChannelInterrupted("the computer is not connected")
        # The product limit is per chat, so one chat's tool servers never use up
        # another chat's room; the connection-wide bound only caps memory.
        in_chat = sum(1 for c in self.channels.values() if c.session_key == session_key)
        if in_chat >= protocol.MAX_CHANNELS:
            raise ChannelInterrupted(
                "this chat is already running its most tool servers on this computer "
                f"({protocol.MAX_CHANNELS} per chat)"
            )
        if len(self.channels) >= protocol.MAX_CONNECTION_CHANNELS:
            raise ChannelInterrupted(
                "this computer is already running its most tool servers across all chats "
                f"({protocol.MAX_CONNECTION_CHANNELS})"
            )
        _family, name = protocol.kind_of(kind)
        if name and name not in self.mcp:
            raise ChannelInterrupted(f"the computer does not expose a local MCP server {name!r}")
        channel = Channel(self, self._channel_id(), kind, session_key, self.generation)
        self.channels[channel.id] = channel
        try:
            await self.send({"op": "open", "ch": channel.id, "kind": kind})
            await channel.wait_opened()
        except BaseException:
            # Cancelled or failed before it was handed out: nobody owns it, so the
            # computer is told to close it whether or not it already started it.
            channel.abandon()
            raise
        return channel

    def handle(self, frame: dict[str, Any]) -> None:
        """Dispatch one validated frame from the computer."""
        op = frame["op"]
        if op == "ready":
            raise protocol.ProtocolError("ready sent twice")
        channel = self.channels.get(frame["ch"])
        if channel is None:
            # A late frame for a channel that already ended here is dropped.
            return
        if op == "opened":
            channel.on_opened()
        elif op == "refused":
            channel.on_refused(protocol.reason(frame["reason"]))
        elif op == "data":
            channel.on_data(protocol.data_of(frame))
        elif op == "eof":
            channel.on_eof()
        elif op == "closed":
            channel.on_closed(protocol.reason(frame["reason"]))
        elif op == "credit":
            channel.on_credit(frame["bytes"])

    def lost(self, why: str) -> None:
        """The connection ended: every open channel ends as interrupted."""
        self.alive = False
        for channel in list(self.channels.values()):
            channel.on_lost(why)
        self.channels.clear()

    def close_later(self) -> None:
        """Close the socket without waiting (a replaced connection)."""
        closer = self._closer
        if closer is None:
            return

        async def run() -> None:
            with contextlib.suppress(Exception):
                await closer()

        task = asyncio.ensure_future(run())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def stop(self, why: str, *, refusal: str = "") -> None:
        """Tell the computer to stop everything for this gateway, then close.

        ``refusal`` is a credential refusal code: sent as the authenticated
        ``refusal`` frame instead of ``stop``, so the computer ends its grant
        (``revoked``, ``removed``) or renews (``expired``) rather than redialling.
        """
        # Sent even after the channels were marked lost: the computer must hear it
        # to stop the process trees it still runs for this gateway.
        if refusal:
            text = protocol.encode({"op": "refusal", "code": refusal})
        else:
            text = protocol.encode({"op": "stop", "reason": protocol.reason(why)})

        async def send_stop() -> None:
            async with self._send_lock:
                await self._sender(text)

        try:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(send_stop(), _STOP_IO_TIMEOUT_SECS)
        finally:
            self.lost(why)
            # Closing must still run when the send lock or the send stalls, or
            # the caller is cancelled; the socket close has its own bound.
            if self._closer is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._closer(), _STOP_IO_TIMEOUT_SECS)


class _Fence:
    """One device's admission lock, kept only while someone holds or waits on it."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class ConnectionBroker:
    """Every live computer connection on this gateway."""

    def __init__(self) -> None:
        self._live: dict[str, DeviceConnection] = {}
        self._generation = 0
        self._notes: OrderedDict[str, list[str]] = OrderedDict()
        self._fences: dict[str, _Fence] = {}

    @contextlib.asynccontextmanager
    async def admission(self, device_id: str) -> AsyncIterator[None]:
        """Serialize ``device_id``'s final admission with its Disconnect and Remove.

        The connect route re-reads the record, attaches and sends ``admitted``
        inside it; Disconnect and Remove write the record and end the live
        connection inside it. So a Disconnect lands either before the re-read
        (which then refuses) or after the attach (and then ends that connection).
        """
        fence = self._fences.get(device_id)
        if fence is None:
            fence = self._fences[device_id] = _Fence()
        fence.users += 1
        try:
            async with fence.lock:
                yield
        finally:
            fence.users -= 1
            if fence.users == 0 and self._fences.get(device_id) is fence:
                del self._fences[device_id]

    def admission_waiters(self, device_id: str) -> int:
        """How many callers hold or wait on ``device_id``'s admission fence."""
        fence = self._fences.get(device_id)
        return fence.users if fence is not None else 0

    def live(self, device_id: str) -> Optional[DeviceConnection]:
        conn = self._live.get(device_id)
        return conn if conn is not None and conn.alive else None

    def attach(self, conn: DeviceConnection) -> int:
        """Make ``conn`` the computer's live connection; its generation number."""
        existing = self._live.get(conn.device_id)
        if existing is not None and existing.alive:
            if existing.record.cgen > conn.record.cgen:
                raise CredentialRejected("superseded", "a newer credential is connected")
            why = "replaced by a new connection from the same computer"
            self._note_interrupted(existing, why)
            existing.lost(why)
            existing.close_later()
        elif existing is None and len(self._live) >= MAX_LIVE_DEVICES:
            raise CredentialRejected("capacity", "this gateway has too many computers connected")
        self._generation += 1
        conn.generation = self._generation
        self._live[conn.device_id] = conn
        return conn.generation

    def detach(self, conn: DeviceConnection, why: str) -> None:
        if self._live.get(conn.device_id) is conn:
            del self._live[conn.device_id]
        self._note_interrupted(conn, why)
        conn.lost(why)

    def _note_interrupted(self, conn: DeviceConnection, why: str) -> None:
        for channel in conn.channels.values():
            if channel.state in (STATE_OPENING, STATE_OPEN) and channel.session_key:
                notes = self._notes.setdefault(channel.session_key, [])
                if len(notes) < 8:
                    notes.append(interrupted_message(conn.record.label))
                self._notes.move_to_end(channel.session_key)
        while len(self._notes) > _MAX_NOTED_SESSIONS:
            self._notes.popitem(last=False)

    def take_interruptions(self, session_key: str) -> list[str]:
        """Interruption notes for ``session_key``'s next turn (read once)."""
        return self._notes.pop(session_key, [])

    async def open_channel(self, device_id: str, kind: str, *, session_key: str) -> Channel:
        conn = self.live(device_id)
        if conn is None:
            raise DeviceOffline(
                "this chat's computer is offline; connect it from that computer to continue"
            )
        return await conn.open(kind, session_key)

    async def disconnect(self, device_id: str, why: str, *, refusal: str = "") -> bool:
        """End ``device_id``'s live connection; ``refusal`` as in :meth:`DeviceConnection.stop`."""
        conn = self._live.get(device_id)
        if conn is None:
            return False
        self.detach(conn, why)
        await conn.stop(why, refusal=refusal)
        return True

    async def shutdown_all(self, why: str) -> int:
        conns = list(self._live.values())
        for conn in conns:
            self.detach(conn, why)
        await asyncio.gather(*(c.stop(why) for c in conns), return_exceptions=True)
        return len(conns)


_BROKER: Optional[ConnectionBroker] = None


def get_broker() -> ConnectionBroker:
    """This gateway process's broker (created on first use)."""
    global _BROKER
    if _BROKER is None:
        _BROKER = ConnectionBroker()
    return _BROKER

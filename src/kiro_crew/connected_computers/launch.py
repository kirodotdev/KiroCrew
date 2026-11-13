"""Remote gateway: a connected computer's tool servers, behind the existing relay.

:class:`ConnectedComputerLaunch` is what a prepared session's ``launch`` is for a
connected computer. It fits the same seams a Coder or VM launch does
(``workspace_server``, ``relay``, ``close_relay``), so the session start, the
bridge, the KAS fence that unmounts built-ins and the tool gate are all unchanged.
Its relay is the ordinary :class:`~kiro_crew.remote_session.relay.WorkspaceRelay`
(a token-authenticated loopback port per runtime on THIS machine); only the link
behind it differs: a :class:`ChannelLink` over the computer's own connector
socket instead of a ``coder ssh`` dial.

A :class:`ChannelLink` has no resume. When its channel is interrupted, or its
stream ends while calls are still waiting, it answers every one of them with an
error saying the outcome is unknown and the call was not retried (B08), then
ends; it never redials, so a call can neither be replayed nor move to another
connection or computer. At most :data:`_MAX_PENDING_CALLS` calls wait at once:
the next is answered "not sent" and never reaches the computer, so no call is
ever sent without being tracked.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import os
from collections import deque
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

from kiro_crew import platform_compat
from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.broker import (
    ChannelInterrupted,
    ConnectionBroker,
    get_broker,
    interrupted_message,
)
from kiro_crew.connected_computers.identity import LABEL_MAX
from kiro_crew.remote_session.link import LinkUnavailable
from kiro_crew.session_environment import CONNECTED_MCP_SERVER_PREFIX, WORKSPACE_SERVER_NAME

logger = logging.getLogger(__name__)

#: Tool calls one link may have waiting; a call past it is refused, never sent.
_MAX_PENDING_CALLS: Final[int] = 64
#: Longest JSON-RPC line a link holds: a longer request is refused, a longer
#: reply is passed through and matched using only its bounded prefix.
_MAX_TRACKED_LINE: Final[int] = 8 * 1024 * 1024
#: A borrowed write argument may contain one maximum-sized line and its newline.
_MAX_WRITE_BYTES: Final[int] = _MAX_TRACKED_LINE + 1
_MAX_RESPONSE_PREFIX: Final[int] = 64 * 1024
#: JSON escaping can grow an id by at most six bytes per input byte.
_MAX_ID_BYTES: Final[int] = 6 * _MAX_TRACKED_LINE
_MAX_ERROR_TEXT_CHARS: Final[int] = 1024
_MAX_ERROR_BYTES: Final[int] = _MAX_ID_BYTES + 6 * _MAX_ERROR_TEXT_CHARS + 256
#: Both the active synthetic answer and queued refusals share these ceilings.
#: Room for a maximally escaped id preserves the existing request-line limit.
_MAX_QUEUED_REPLY_BYTES: Final[int] = 64 * 1024 * 1024
_MAX_QUEUED_REPLIES: Final[int] = 64
_LINK_IO_BYTES: Final[int] = protocol.MAX_DATA_BYTES
_RELAY_CLOSE_WAIT_SECONDS: Final[float] = 15.0
_RUNTIME_END_POLL_SECONDS: Final[float] = 2.0


def refused_message(label: str) -> str:
    return (
        f"Too many tool calls are already waiting on {label}, so this one was not "
        "sent and did not run. Try it again once the others finish."
    )


def ended_message(label: str) -> str:
    return (
        f"The tool server on {label} stopped before answering, so whether this "
        "finished is unknown. It was not retried and did not run anywhere else. "
        "Check the computer's files before running it again."
    )


def _error_result(rid: Any, text: str) -> bytes:
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }
    return (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")


def _response_key_from_prefix(prefix: bytes) -> Optional[str]:
    """Find a complete top-level id without mistaking nested or quoted ids for it.

    Decode each complete field once using the stdlib JSON parser. An unfinished
    value ends the scan; a prefix cannot validate the rest of an oversized line.
    """
    key = None
    try:
        # A byte-bound prefix can end in the middle of a UTF-8 code point.
        text = codecs.getincrementaldecoder("utf-8")().decode(prefix, final=False)
        decoder = json.JSONDecoder()

        def skip_space(pos: int) -> int:
            while pos < len(text) and text[pos] in " \t\r\n":
                pos += 1
            return pos

        pos = skip_space(0)
        if pos == len(text) or text[pos] != "{":
            return None
        pos += 1
        while pos < len(text):
            name, pos = decoder.raw_decode(text, skip_space(pos))
            pos = skip_space(pos)
            if not isinstance(name, str) or pos == len(text) or text[pos] != ":":
                return None
            if name == "method":
                return None  # A server request is not a reply to a pending call.
            value, pos = decoder.raw_decode(text, skip_space(pos + 1))
            pos = skip_space(pos)
            if pos == len(text) or text[pos] not in ",}":
                break  # A number at the prefix boundary may still have more digits.
            if name == "id":
                key = json.dumps(value, sort_keys=True)
            if text[pos] == "}":
                break
            pos += 1
    except (ValueError, UnicodeDecodeError, RecursionError):
        pass
    return key


class RequestTooLarge(LinkUnavailable):
    """A request line past :data:`_MAX_TRACKED_LINE`; none of it was sent."""


class PendingCalls:
    """The JSON-RPC ``tools/call`` requests a link has sent and not seen answered.

    Reads newline-delimited JSON-RPC (MCP's stdio framing) in both directions.
    Requests are admitted a whole line at a time (:meth:`admit`), so a call is
    either counted and sent, or refused with an answer of its own and never
    sent: with :data:`_MAX_PENDING_CALLS` waiting, the next call is refused.
    Nothing tracked is ever dropped, so on loss or end every call still waiting
    gets its unknown-outcome answer (:meth:`close_out`).
    """

    def __init__(self, refusal: str = "") -> None:
        self._ids: dict[str, None] = {}
        self._refusal = (refusal or refused_message("the computer"))[:_MAX_ERROR_TEXT_CHARS]
        self._up = bytearray()
        self._down_head = bytearray()
        self._down_skip = False
        self._down_mid = False
        #: Set once a request line overflowed; the link refuses it explicitly.
        self.too_large = False

    def __len__(self) -> int:
        return len(self._ids)

    @property
    def mid_response(self) -> bool:
        """Whether the bridge has received part of a reply line."""
        return self._down_mid

    @staticmethod
    def _parse(line: bytes) -> Optional[dict[str, Any]]:
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return None
        return msg if isinstance(msg, dict) else None

    def admit(self, data: bytes) -> tuple[bytes, bytes]:
        """``(bytes to send, refusal answers)`` for ``data`` from the bridge.

        Whole lines only; a partial last line is held until its newline (or
        :meth:`finish`). A held line past the bound sets :attr:`too_large`.
        """
        send = bytearray()
        refused = bytearray()
        start = 0
        while start < len(data) and not self.too_large:
            end = data.find(b"\n", start)
            stop = len(data) if end < 0 else end
            if len(self._up) + stop - start > _MAX_TRACKED_LINE:
                self._up.clear()
                self.too_large = True
                break
            self._up.extend(memoryview(data)[start:stop])
            if end < 0:
                break
            line = bytes(self._up)
            self._up.clear()
            admitted, answer = self._admit_lines([line], terminated=True)
            send.extend(admitted)
            refused.extend(answer)
            start = end + 1
        return bytes(send), bytes(refused)

    def finish(self) -> tuple[bytes, bytes]:
        """The held partial line, admitted as is: the bridge's input ended."""
        rest = bytes(self._up)
        self._up.clear()
        return self._admit_lines([rest], terminated=False) if rest else (b"", b"")

    def _admit_lines(self, lines: list[bytes], *, terminated: bool) -> tuple[bytes, bytes]:
        send = bytearray()
        refused = bytearray()
        for line in lines:
            msg = self._parse(line) if line.strip() else None
            if msg is not None and msg.get("method") == "tools/call" and "id" in msg:
                key = json.dumps(msg["id"], sort_keys=True)
                if key not in self._ids and (
                    len(self._ids) >= _MAX_PENDING_CALLS
                    or sum(map(len, self._ids)) + len(key) > _MAX_ID_BYTES
                ):
                    refused += _error_result(msg["id"], self._refusal)
                    continue
                self._ids[key] = None
            send += line
            if terminated:
                send += b"\n"
        return bytes(send), bytes(refused)

    def observe_response(self, data: bytes) -> None:
        if not data:
            return
        pieces = data.split(b"\n")
        for index, piece in enumerate(pieces):
            if not self._down_skip:
                if len(self._down_head) + len(piece) > _MAX_TRACKED_LINE:
                    del self._down_head[_MAX_RESPONSE_PREFIX:]
                    self._down_head += piece[: max(0, _MAX_RESPONSE_PREFIX - len(self._down_head))]
                    self._down_skip = True
                else:
                    self._down_head += piece
            if index == len(pieces) - 1:
                break
            if self._down_skip:
                key = _response_key_from_prefix(bytes(self._down_head))
                if key is not None:
                    self._ids.pop(key, None)
                # An unidentifiable reply cannot settle some other waiting call.
                # Keep those calls for close_out's unknown-outcome answers.
            else:
                msg = self._parse(bytes(self._down_head)) if self._down_head.strip() else None
                if msg is not None and "id" in msg and "method" not in msg:
                    self._ids.pop(json.dumps(msg["id"], sort_keys=True), None)
            self._down_head.clear()
            self._down_skip = False
        self._down_mid = not data.endswith(b"\n")

    def pop_outcome(self, text: str) -> bytes:
        """One oldest pending call, serialized only when the bridge can read it."""
        if not self._ids:
            return b""
        key = next(iter(self._ids))
        del self._ids[key]
        return _error_result(json.loads(key), text[:_MAX_ERROR_TEXT_CHARS])

    def end_response(self) -> bool:
        """Release response accounting and report whether a newline is owed."""
        mid = self._down_mid
        self._down_head.clear()
        self._down_skip = False
        self._down_mid = False
        return mid

    def close_out(self, text: str) -> bytes:
        """Collect at most the pending-call budget; live links drain one at a time."""
        chunks = [b"\n"] if self.end_response() else []
        while self._ids:
            chunks.append(self.pop_outcome(text))
        return b"".join(chunks)


Dial = Callable[[], Awaitable[Any]]


class ChannelLink:
    """A relay link over one channel of the computer's connector socket."""

    def __init__(self, worker_id: str, secret: bytes, dial: Dial, *, label: str = "") -> None:
        del secret  # The connector socket is already authenticated end to end.
        self.worker_id = worker_id
        self.label = (label or "the computer")[:LABEL_MAX]
        self.state = "connecting"
        self.error = ""
        self._dial = dial
        self._channel: Any = None
        self._ready: Optional[asyncio.Future[Any]] = None
        self._task: Optional[asyncio.Task[None]] = None
        #: The active answer shares its allocation with a queued refusal, never a copy.
        self._synthetic = memoryview(b"")
        self._refusals: deque[bytes] = deque()
        self._queued_bytes = 0
        self._drained = asyncio.Event()
        self._wake = asyncio.Event()
        self._writing = False
        self._input_closed = False
        self._outcome = ""
        self._end_prefix = False
        self._tail: Optional[asyncio.Task[None]] = None
        self.pending = PendingCalls(refused_message(self.label))
        self.stdout = _LinkReader(self)

    def start(self) -> None:
        if self._ready is not None:
            return
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._task = asyncio.ensure_future(self._connect())

    async def _connect(self) -> None:
        assert self._ready is not None
        try:
            channel = await self._dial()
        except BaseException as exc:
            self.state = "lost"
            self.error = (str(exc) or type(exc).__name__)[:_MAX_ERROR_TEXT_CHARS]
            if not self._ready.done():
                self._ready.set_exception(LinkUnavailable(self.error))
                self._ready.exception()
            if not isinstance(exc, Exception):
                raise
            return
        self._channel = channel
        self.state = "connected"
        if not self._ready.done():
            self._ready.set_result(channel)

    async def wait_connected(self, timeout: float) -> None:
        assert self._ready is not None
        await asyncio.wait_for(asyncio.shield(self._ready), timeout)

    async def _wait(self) -> Any:
        if self._ready is None:
            raise LinkUnavailable("link was not started")
        return await asyncio.shield(self._ready)

    async def _reserve_answer(self) -> None:
        """Reserve one worst-case answer BEFORE consuming another request line."""
        while (
            len(self._refusals) + bool(self._synthetic) >= _MAX_QUEUED_REPLIES
            or self._queued_bytes + _MAX_ERROR_BYTES > _MAX_QUEUED_REPLY_BYTES
        ):
            if self.state not in ("connecting", "connected"):
                raise LinkUnavailable(self.error or "link ended")
            self._drained.clear()
            await self._drained.wait()
        if self.state not in ("connecting", "connected"):
            raise LinkUnavailable(self.error or "link ended")

    def _refuse(self, refused: bytes) -> None:
        if refused:
            self._refusals.append(refused)
            self._queued_bytes += len(refused)
            self._wake.set()

    async def _send(self, channel: Any, send: bytes, refused: bytes) -> None:
        self._refuse(refused)
        if send:
            try:
                await channel.write(send)
            except ChannelInterrupted as exc:
                self._lost(str(exc))
                raise
        if self.pending.too_large:
            self.error = f"a tool request too large to send to {self.label} was refused"
            channel.close_input()
            raise RequestTooLarge(self.error)

    async def write(self, data: bytes) -> None:
        # WorkspaceRelay has one upstream pump. Refuse concurrent producers rather
        # than retaining an unbounded lock wait-list, each holding its input bytes.
        if self._writing or self._input_closed:
            raise LinkUnavailable("link input is already writing or closed")
        if len(data) > _MAX_WRITE_BYTES:
            self.error = "a bridge input batch too large to send was refused"
            self.pending.too_large = True
            if self._channel is not None:
                self._channel.close_input()
            raise RequestTooLarge(self.error)
        self._writing = True
        try:
            channel = await self._wait()
            if self.state not in ("connecting", "connected"):
                raise LinkUnavailable(self.error or "link ended")
            if self.pending.too_large:
                raise RequestTooLarge(self.error)
            start = 0
            while start < len(data):
                await self._reserve_answer()
                # Feed at most one line and one wire-sized piece. A large caller
                # batch is never split into a list or admitted ahead of capacity.
                stop = min(start + _LINK_IO_BYTES, len(data))
                newline = data.find(b"\n", start, stop)
                if newline >= 0:
                    stop = newline + 1
                send, refused = self.pending.admit(data[start:stop])
                await self._send(channel, send, refused)
                del send, refused
                start = stop
        finally:
            self._writing = False

    def close_input(self) -> None:
        if self._input_closed:
            return
        if self._writing:
            raise LinkUnavailable("link input is already writing")
        self._input_closed = True
        channel = self._channel
        if channel is None:
            return
        if not self.pending._up:
            channel.close_input()
            return

        async def tail() -> None:
            try:
                # EOF uses the same reservation as write, including a refused
                # unterminated tools/call. It cannot append past a full queue.
                await self._reserve_answer()
                send, refused = self.pending.finish()
                await self._send(channel, send, refused)
            except LinkUnavailable:
                return
            channel.close_input()

        self._tail = asyncio.ensure_future(tail())

    async def aclose(self) -> None:
        if self.state not in ("lost", "finished"):
            self.state = "closed"
        self._drained.set()
        self._wake.set()
        for task in (self._task, self._tail):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await self._channel.aclose()

    def health(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error, "waiting_calls": len(self.pending)}

    def _close_out(self, text: str) -> None:
        self._outcome = text[:_MAX_ERROR_TEXT_CHARS]
        self._end_prefix = self.pending.end_response()
        self._drained.set()
        self._wake.set()

    def _lost(self, why: str) -> None:
        if self.state in ("connecting", "connected"):
            self.state = "lost"
            self.error = why[:_MAX_ERROR_TEXT_CHARS]
            self._close_out(interrupted_message(self.label))

    def _ended(self) -> None:
        """The channel's stream ended: every call still waiting is unknown (R11)."""
        if self.state in ("connecting", "connected"):
            self.state = "finished"
            self._close_out(ended_message(self.label))


class _LinkReader:
    """``link.stdout``: the channel's bytes, plus the link's own answers.

    Answers (refusals, unknown outcomes) only ever enter the stream between
    reply lines, so the bridge never sees a garbled line. The stream ends
    (``b""`` or :class:`LinkUnavailable`) only after every call that was
    waiting has been answered.
    """

    def __init__(self, link: ChannelLink) -> None:
        self._link = link
        self._reading = False

    async def read(self, n: int) -> bytes:
        if self._reading:
            raise LinkUnavailable("link output is already reading")
        self._reading = True
        try:
            return await self._read(n)
        finally:
            self._reading = False

    async def _read(self, n: int) -> bytes:
        link = self._link
        if n == 0:
            return b""
        n = min(n, _LINK_IO_BYTES) if n > 0 else _LINK_IO_BYTES
        while True:
            if link._end_prefix:
                link._end_prefix = False
                return b"\n"
            if not link._synthetic and not link.pending.mid_response:
                if link._refusals:
                    link._synthetic = memoryview(link._refusals.popleft())
                elif link._outcome and len(link.pending):
                    answer = link.pending.pop_outcome(link._outcome)
                    link._synthetic = memoryview(answer)
                    link._queued_bytes += len(answer)
            if link._synthetic:
                out = bytes(link._synthetic[:n])
                allocation = memoryview(link._synthetic.obj).nbytes
                link._synthetic = link._synthetic[n:]
                if not link._synthetic:
                    # Release the entire backing bytes, including an already-read
                    # prefix: slicing a memoryview keeps that allocation alive.
                    link._synthetic = memoryview(b"")
                    link._queued_bytes -= allocation
                    link._drained.set()
                return out
            if link.state == "finished":
                return b""
            if link.state in ("lost", "closed"):
                raise LinkUnavailable(link.error or "link ended")
            try:
                data = await self._next(n)
            except LinkUnavailable as exc:
                link._lost(str(exc))
                continue
            if data is None:
                continue  # woken by a refusal
            if not data:
                link._ended()
                continue
            link.pending.observe_response(data)
            return data

    async def _next(self, n: int) -> Optional[bytes]:
        """The channel's next bytes, or ``None`` when a refusal is ready first."""
        link = self._link
        channel = await link._wait()
        if link.pending.mid_response:
            # A refusal cannot go before this reply line ends anyway.
            return bytes(await channel.stdout.read(n))
        link._wake.clear()
        reader = asyncio.ensure_future(channel.stdout.read(n))
        waker = asyncio.ensure_future(link._wake.wait())
        try:
            await asyncio.wait({reader, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waker.cancel()
            if not reader.done():
                # Safe: the channel's buffer gives up nothing until a read returns.
                reader.cancel()
            await asyncio.gather(reader, waker, return_exceptions=True)
        if reader.cancelled():
            return None
        return bytes(reader.result())


class ConnectedComputerLaunch:
    """One session's tool servers on one connected computer."""

    def __init__(
        self,
        device_id: str,
        label: str,
        *,
        session_key: str,
        exposed_mcp: Sequence[str] = (),
        broker: Optional[ConnectionBroker] = None,
        python: Optional[str] = None,
    ) -> None:
        self.device_id = device_id
        self.label = label or device_id
        self.session_key = session_key
        self.exposed_mcp = tuple(exposed_mcp)
        self.binding_id = f"cc-{device_id}"
        self.session_id = self.binding_id
        self._broker = broker
        self._python = python
        self.relay: Any = None
        self.mcp_relays: list[Any] = []
        self._next_port = 0
        self._workers: dict[int, tuple[str, str]] = {}
        self._links: dict[str, ChannelLink] = {}

    @property
    def broker(self) -> ConnectionBroker:
        return self._broker if self._broker is not None else get_broker()

    # -- the seams WorkspaceRelay uses ------------------------------------------
    def new_worker_id(self) -> str:
        return f"{self.binding_id}-{os.urandom(4).hex()}"

    async def start_worker(self, worker_id: str, remote_argv: Sequence[str], secret: bytes) -> int:
        """Record which tool server ``worker_id`` is; the computer starts it on open.

        Returns a handle in place of a port: :meth:`dial` turns it back into the
        channel kind. Nothing starts here, so nothing can leak if the relay never
        uses the worker.
        """
        del secret
        if len(remote_argv) != 1:
            raise ValueError("a connected computer starts tool servers by kind only")
        protocol.kind_of(remote_argv[0])
        self._next_port += 1
        self._workers[self._next_port] = (worker_id, remote_argv[0])
        return self._next_port

    def dial(self, port: int) -> Dial:
        worker_id, kind = self._workers.pop(port)

        async def open_channel() -> Any:
            return await self.broker.open_channel(
                self.device_id, kind, session_key=self.session_key
            )

        del worker_id
        return open_channel

    def link_kwargs(self) -> dict[str, Any]:
        return {"label": self.label}

    def make_link(self, worker_id: str, secret: bytes, dial: Dial, **kwargs: Any) -> ChannelLink:
        link = ChannelLink(worker_id, secret, dial, **kwargs)
        self._links[worker_id] = link
        return link

    def mark_stopping(self, worker_ids: Sequence[str]) -> None:
        """Nothing to record: a channel's process tree stops when the channel closes."""

    async def stop_worker(self, worker_id: str) -> None:
        link = self._links.pop(worker_id, None)
        if link is not None:
            await link.aclose()

    # -- the seams the session start uses ----------------------------------------
    def _bridge_entry(self, name: str, port: int) -> dict[str, object]:
        import sys

        argv = platform_compat.isolated_python_argv(
            "-P",
            "-m",
            "kiro_crew.remote_session.client",
            "--pipe",
            f"127.0.0.1:{port}",
            "--token-file",
            "",
            executable=self._python or sys.executable,
        )
        return {"name": name, "command": argv[0], "args": argv[1:], "env": []}

    async def _relay(self, kind: str) -> tuple[Any, int]:
        from kiro_crew.remote_session.relay import WorkspaceRelay, new_token

        relay = WorkspaceRelay(
            self,
            [kind],
            new_token(),
            link_factory=self.make_link,
            name=f"{self.binding_id}-{os.urandom(3).hex()}",
            prestart=False,
        )
        port = await relay.start()
        return relay, port

    async def workspace_server(self, workdir: str) -> dict[str, object]:
        """The bridge's ``mcpServers`` element for the computer's workspace tools.

        ``workdir`` is the root the computer reported, which its own grant fixed;
        it is only shown to the model and matched by the tool gate. The computer
        never takes a path from this side.
        """
        if not isinstance(workdir, str) or not workdir.startswith("/"):
            raise ValueError("a connected computer's root must be an absolute POSIX path")
        if self.broker.live(self.device_id) is None:
            from kiro_crew.connected_computers.broker import DeviceOffline

            raise DeviceOffline(
                "this chat's computer is offline; connect it from that computer to continue"
            )
        await self.close_relay()
        relay, port = await self._relay(protocol.KIND_WORKSPACE)
        self.relay = relay
        return self._bridge_entry(WORKSPACE_SERVER_NAME, port)

    async def local_mcp_servers(self) -> list[dict[str, Any]]:
        """One bridge per local MCP server the computer exposed, by its own name.

        Each is mounted as ``connected-computer-<name>``, so the remote tool gate,
        policy and approvals apply to it like any server, and governance never
        auto-approves it (``platform.governance.may_skip_gate``).
        """
        out: list[dict[str, Any]] = []
        for name in self.exposed_mcp:
            relay, port = await self._relay(f"{protocol.KIND_MCP_PREFIX}{name}")
            self.mcp_relays.append(relay)
            out.append(
                {
                    "server": self._bridge_entry(f"{CONNECTED_MCP_SERVER_PREFIX}{name}", port),
                    "write_token": relay.write_token,
                }
            )
        return out

    def own_relays(self) -> tuple[Any, ...]:
        """The relays attached now: the workspace one and each local MCP one.

        A runtime captures them when it attaches and closes only those
        (:meth:`close_relays`), so a runtime that ends after a respawn attached
        new relays to this launch never closes the respawn's.
        """
        return tuple(r for r in (self.relay, *self.mcp_relays) if r is not None)

    async def close_relay(self, relay: Any = None) -> None:
        """Close ``relay``, or every relay of this launch; bounded wait."""
        await self.close_relays(self.own_relays() if relay is None else (relay,))

    async def close_relays(self, relays: Sequence[Any]) -> None:
        """Close exactly ``relays`` and stop tracking them; bounded wait."""
        targets = [r for r in relays if r is not None]
        if self.relay is not None and any(self.relay is r for r in targets):
            self.relay = None
        self.mcp_relays = [m for m in self.mcp_relays if not any(m is r for r in targets)]
        for target in targets:
            try:
                await asyncio.wait_for(asyncio.shield(target.close()), _RELAY_CLOSE_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("connected computer relay %s is still closing", target.name)
            except Exception:  # noqa: BLE001 -- the idle reaper remains
                logger.info("Could not close connected computer relay", exc_info=True)

    def close_relay_when_ended(
        self, is_alive: Callable[[], bool], on_end: Optional[Callable[[], None]] = None
    ) -> None:
        """Close the relays attached now once ``is_alive()`` is False for its runtime."""
        owned = self.own_relays()

        async def watch() -> None:
            while is_alive():
                await asyncio.sleep(_RUNTIME_END_POLL_SECONDS)
            if on_end is not None:
                with contextlib.suppress(Exception):
                    on_end()
            await self.close_relays(owned)

        task = asyncio.ensure_future(watch())
        _WATCHERS.add(task)
        task.add_done_callback(_WATCHERS.discard)


_WATCHERS: "set[asyncio.Task[None]]" = set()

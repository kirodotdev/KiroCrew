"""Remote gateway: a connected computer's tool servers, behind the existing relay.

:class:`ConnectedComputerLaunch` is what a prepared session's ``launch`` is for a
connected computer. It fits the same seams a Coder or VM launch does
(``workspace_server``, ``relay``, ``close_relay``), so the session start, the
bridge, the KAS fence that unmounts built-ins and the tool gate are all unchanged.
Its relay is the ordinary :class:`~kiro_crew.remote_session.relay.WorkspaceRelay`
(a token-authenticated loopback port per runtime on THIS machine); only the link
behind it differs: a :class:`ChannelLink` over the computer's own connector
socket instead of a ``coder ssh`` dial.

A :class:`ChannelLink` relays bytes unchanged, without interpreting JSON-RPC.
When its channel is interrupted, the bridge closes and the tool server stops.
Nothing is retried, resent or fabricated; a link never redials or moves to
another connection or computer. Channel credit and bounded I/O batches apply
backpressure without retaining requests or replies in the link.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Awaitable, Callable, Final, Optional, Sequence

from kiro_crew import platform_compat
from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.broker import ConnectionBroker, get_broker
from kiro_crew.connected_computers.identity import LABEL_MAX
from kiro_crew.remote_session.link import LinkUnavailable
from kiro_crew.session_environment import CONNECTED_MCP_SERVER_PREFIX, WORKSPACE_SERVER_NAME

logger = logging.getLogger(__name__)

#: Cap a borrowed write argument before any await; no whole-message buffering.
_MAX_WRITE_BYTES: Final[int] = 8 * 1024 * 1024 + 1
_MAX_ERROR_TEXT_CHARS: Final[int] = 1024
_LINK_IO_BYTES: Final[int] = protocol.MAX_DATA_BYTES
_RELAY_CLOSE_WAIT_SECONDS: Final[float] = 15.0
_RUNTIME_END_POLL_SECONDS: Final[float] = 2.0

Dial = Callable[[], Awaitable[Any]]


class ChannelLink:
    """A byte-only relay over one channel of the computer's connector socket."""

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
        self._writing = False
        self._input_closed = False
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
        if self.state != "connecting":
            channel.abandon()
            return
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

    async def write(self, data: bytes) -> None:
        # WorkspaceRelay has one upstream pump. Refuse concurrent producers rather
        # than retaining an unbounded lock wait-list, each holding its input bytes.
        if self._writing or self._input_closed:
            raise LinkUnavailable("link input is already writing or closed")
        if len(data) > _MAX_WRITE_BYTES:
            self._lost("a bridge input batch too large to send was refused")
            self._input_closed = True
            if self._channel is not None:
                self._channel.abandon()
            raise LinkUnavailable(self.error)
        self._writing = True
        try:
            channel = await self._wait()
            if self.state != "connected":
                raise LinkUnavailable(self.error or "link ended")
            for start in range(0, len(data), _LINK_IO_BYTES):
                await channel.write(data[start : start + _LINK_IO_BYTES])
        except LinkUnavailable as exc:
            self._lost(str(exc))
            raise
        finally:
            self._writing = False

    def close_input(self) -> None:
        if self._input_closed:
            return
        if self._writing:
            raise LinkUnavailable("link input is already writing")
        self._input_closed = True
        if self._channel is not None:
            self._channel.close_input()

    async def aclose(self) -> None:
        if self.state not in ("lost", "finished"):
            self.state = "closed"
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await self._channel.aclose()

    def health(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error}

    def _lost(self, why: str) -> None:
        if self.state in ("connecting", "connected"):
            self.state = "lost"
            self.error = why[:_MAX_ERROR_TEXT_CHARS]
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(LinkUnavailable(self.error))
                self._ready.exception()


class _LinkReader:
    """Read channel bytes directly; loss ends the bridge without adding any bytes."""

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
        if link.state == "finished":
            return b""
        if link.state in ("lost", "closed"):
            raise LinkUnavailable(link.error or "link ended")
        n = min(n, _LINK_IO_BYTES) if n > 0 else _LINK_IO_BYTES
        try:
            channel = await link._wait()
            data = bytes(await channel.stdout.read(n))
        except LinkUnavailable as exc:
            link._lost(str(exc))
            raise
        if not data and link.state == "connected":
            link.state = "finished"
        return data


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

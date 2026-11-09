"""Local machine: one connector socket to one remote gateway, for one grant.

The connector dials OUT through the Remote Crew SSH local forward (the
instance's ``local_port`` on this machine's loopback) with only its device id,
a fresh nonce and its credential's generation. The gateway's ``hello`` must
name the gateway and device the grant was made for and prove the grant's peer
key over both nonces; only then does the connector send a nonce-bound proof of
credential possession (``auth``) and ``ready``. The bearer never crosses the socket, and every frame after
``hello``, ``auth`` included, has a directional sequence and session MAC. A socket
that answers as anything else is closed with nothing sent. It then
starts tool servers only when asked by kind:

* ``workspace`` -- ``workspace_tools.py --root <granted root>``;
* ``mcp:<name>`` -- that exposed local MCP server, as snapshotted in the grant;
  its locally turned-off tools stay off through :mod:`.mcp_filter` (B14).

Every one runs under :mod:`.confinement` on the granted root, with an
environment built from nothing. The gateway names no path, argv or environment.

Before each ``open`` the connector re-reads its grant and the local switch, so
a local Off or a revocation refuses new work at once even while the gateway is
unreachable (B01, B07). When the socket ends, every process tree it started is
stopped; nothing is resumed or retried (B08).

WebSocket close codes and reasons are not authenticated (a port relay can
forge them), so nothing here reads them. The gateway states a refusal as an
authenticated ``refusal`` frame, which raises :class:`AuthenticatedRefusal`;
its first authenticated frame otherwise is ``admitted``, which sets
:attr:`Connector.admitted`. A run that ended without admission, or on a frame
that failed its MAC, sequence or schema, is only a failed attempt.

From ``ready`` on, the run also needs authenticated progress
(:class:`.authenticated.Liveness`): it pings once admitted, and a gateway that
sent no authenticated frame for :data:`.protocol.LIVENESS_DEADLINE_SECS` ends
the run as an interrupted connection (never a refusal), stopping every process
it started. The WebSocket's own pings and pongs do not count. Each frame's
work (an ``open`` preparing a confined process, a ``close`` stopping one) runs
raced against that deadline: losing liveness cancels it and stops the running
channels without waiting for it, and an ``open`` re-checks before it spawns.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Final, Optional, TypeVar

from kiro_crew.connected_computers import protocol
from kiro_crew.connected_computers.authenticated import Liveness, SessionFrames
from kiro_crew.connected_computers.confinement import run_owned, wrap_off_loop
from kiro_crew.connected_computers.local_store import Grant
from kiro_crew.connected_computers.mcp_filter import ToolFilter

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECS: Final[float] = 20.0
#: Grace a stopped tool server's process group gets after SIGTERM, before SIGKILL.
STOP_GRACE_SECS: Final[float] = 3.0
_READ_CHUNK: Final[int] = protocol.MAX_DATA_BYTES


class ConnectorRefused(RuntimeError):
    """The gateway or this machine refused the connection; the message says why."""


class AuthenticatedRefusal(ConnectorRefused):
    """The gateway refused this computer in a frame that passed its session MAC.

    ``code`` is the gateway's refusal code (``credentials.CredentialRejected.code``).
    The session key is bound to the grant's peer key and both handshake nonces,
    so only the gateway the user approved can send it, on this connection.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"the remote gateway refused this computer ({code})")
        self.code = code

    @property
    def terminal(self) -> bool:
        """An explicit end: disconnected, removed, replaced, or another computer."""
        return self.code in protocol.TERMINAL_REFUSALS

    @property
    def expired(self) -> bool:
        """The credential ran out: renew it over SSH, then redial."""
        return self.code == protocol.EXPIRED_REFUSAL


_W = TypeVar("_W")
#: Stops of processes whose spawn finished after it was cancelled, kept referenced.
_LATE_STOPS: set["asyncio.Future[Any]"] = set()


def _consume(task: "asyncio.Future[Any]") -> None:
    """Retrieve an abandoned task's outcome so it never warns."""
    if not task.cancelled():
        task.exception()


async def _spawn(confined: Any) -> asyncio.subprocess.Process:
    """Start ``confined``; a spawn that completes after a cancellation is stopped."""
    from kiro_crew import platform_compat

    spawning = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *confined.argv,
            cwd=confined.cwd,
            env=confined.env,
            pass_fds=confined.pass_fds,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    )
    try:
        return await asyncio.shield(spawning)
    except asyncio.CancelledError:

        def stop_late(done: "asyncio.Future[asyncio.subprocess.Process]") -> None:
            if not done.cancelled() and done.exception() is None:
                reap = asyncio.ensure_future(
                    platform_compat.terminate_and_reap(done.result(), grace=STOP_GRACE_SECS)
                )
                _LATE_STOPS.add(reap)
                reap.add_done_callback(_LATE_STOPS.discard)

        spawning.add_done_callback(stop_late)
        raise


def _remove_scratch(path: str) -> None:
    """Remove a channel's scratch folder (always called off the event loop)."""
    shutil.rmtree(path, ignore_errors=True)


def tools_dir() -> str:
    """Where this install's ``workspace_tools.py`` lives (read-only in the sandbox)."""
    import kiro_crew.remote_session as pkg

    return str(Path(os.path.realpath(pkg.__file__)).parent)


class _LocalChannel:
    """One confined child process and its two credit-bounded byte streams."""

    def __init__(
        self,
        ch: int,
        proc: asyncio.subprocess.Process,
        scratch: str,
        *,
        tool_filter: Optional[ToolFilter] = None,
    ) -> None:
        self.ch = ch
        self.proc = proc
        self.scratch = scratch
        self.window = protocol.SendWindow()
        self.inbound = protocol.RecvBuffer()
        self.tasks: list[asyncio.Task[None]] = []
        #: Keeps the server's locally turned-off tools off (B14); None relays as is.
        self.filter = tool_filter
        #: Serialises output to the gateway so a reply lands only between lines.
        self.out = asyncio.Condition()
        self.ended = False
        self._stop_task: "Optional[asyncio.Future[None]]" = None

    async def stop(self) -> None:
        """Stop the child's whole process group, bounded (R9).

        Its input ends first, then the group gets SIGTERM and up to
        :data:`STOP_GRACE_SECS` to exit, then SIGKILL; the leader is reaped under
        the helper's own bound. The group is signalled only while the leader is
        unreaped, so a recycled pid is never signalled. Windows, and a child that
        already exited, get the platform's tree kill instead. The commands the
        tool server started run in sessions of their own; its SIGTERM retirement
        stops their groups (``workspace_tools._retire_live_commands``).

        Every caller shares one stop, which runs to its end even when a caller
        is cancelled (lost liveness cancels a ``close`` midway): the kill never
        depends on the caller surviving.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._stop(asyncio.current_task()))
        await asyncio.shield(self._stop_task)

    async def _stop(self, caller: "Optional[asyncio.Task[Any]]") -> None:
        from kiro_crew import platform_compat

        # A pump that saw the child exit calls this itself: never cancel or await
        # the calling task, or the stop would end before the kill and cleanup.
        pumps = [task for task in self.tasks if task is not caller]
        self.ended = True
        for task in pumps:
            task.cancel()
        self.window.close()
        await asyncio.gather(*pumps, return_exceptions=True)
        if self.proc.returncode is None:
            with contextlib.suppress(Exception):
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            await platform_compat.terminate_and_reap(self.proc, grace=STOP_GRACE_SECS)
        await asyncio.to_thread(shutil.rmtree, self.scratch, ignore_errors=True)


class Connector:
    """Run one grant's connector socket until it ends; :meth:`run` returns why."""

    def __init__(
        self,
        grant: Grant,
        *,
        url: str,
        plan: Any,
        grant_still_active: Callable[[], bool],
        session_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.grant = grant
        #: The liveness clock and heartbeat wait (injected by tests).
        self._clock = clock
        self._sleep = sleep
        self.url = url
        self.plan = plan
        self._still_active = grant_still_active
        self._session_factory = session_factory
        self._channels: dict[int, _LocalChannel] = {}
        #: Channels taken out of ``_channels`` whose stop has not been awaited to
        #: its end: :meth:`stop_all` awaits it when the frame's work was cancelled.
        self._stopping: set[_LocalChannel] = set()
        #: Frame work cancelled when liveness was lost, awaited (bounded) at the end.
        self._orphans: set[asyncio.Future[Any]] = set()
        #: Set once the run is ending: no new channel is started after it.
        self._closing = False
        self._liveness: Optional[Liveness] = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._frames: SessionFrames | None = None
        self.ended = asyncio.Event()
        #: The gateway sent its authenticated ``admitted``: only this resets the
        #: controller's back-off.
        self.admitted = False
        #: The run ended on a frame that failed its MAC, sequence or schema: the
        #: controller keeps its back-off growing rather than redialling at once.
        self.protocol_failed = False

    # -- the socket ---------------------------------------------------------------
    async def _send(self, frame: dict[str, Any]) -> None:
        if self._frames is None:
            raise protocol.ProtocolError("session is not authenticated")
        async with self._send_lock:
            await self._ws.send_str(self._frames.encode(frame))

    async def run(self) -> str:
        import aiohttp

        if not self.grant.credential or not self.grant.peer_key:
            raise ConnectorRefused(
                "this computer has no credential for that gateway yet; share it again"
            )
        from kiro_crew.connected_computers.credentials import credential_generation

        factory = self._session_factory or (lambda: aiohttp.ClientSession())
        nonce = secrets.token_hex(32)
        headers = {protocol.DEVICE_HEADER: self.grant.device_id, protocol.NONCE_HEADER: nonce}
        generation = credential_generation(self.grant.credential)
        if generation:
            # Which enrollment's peer key the gateway must prove: so a computer
            # that was disconnected still hears why. Not a credential.
            headers[protocol.GENERATION_HEADER] = generation
        try:
            async with factory() as session:
                # No credential here: whatever answers this port has not proved
                # it is the gateway yet (R6).
                ws = await session.ws_connect(
                    self.url,
                    headers=headers,
                    max_msg_size=protocol.MAX_FRAME_BYTES,
                    heartbeat=30.0,
                )
                self._ws = ws
                try:
                    return await self._serve(ws, nonce)
                finally:
                    await self.stop_all()
                    await self._reap_orphans()
                    with contextlib.suppress(Exception):
                        await ws.close()
        finally:
            self.ended.set()

    async def _serve(self, ws: Any, nonce: str) -> str:
        import aiohttp

        from kiro_crew.connected_computers.credentials import connector_auth, proof_matches

        first = await ws.receive(timeout=CONNECT_TIMEOUT_SECS)
        if first.type != aiohttp.WSMsgType.TEXT:
            raise ConnectorRefused("the gateway refused this computer")
        try:
            hello = protocol.decode(first.data, allowed=frozenset({"hello"}))
        except protocol.ProtocolError as exc:
            raise ConnectorRefused("the gateway did not prove who it is") from exc
        grant = self.grant
        bound = (grant.gateway_id, grant.device_id, nonce, hello["nonce"])
        if (
            hello["gateway"] != grant.gateway_id
            or hello["device"] != grant.device_id
            or not proof_matches(grant.peer_key, bound, hello["proof"])
        ):
            # Not the gateway the user approved: say nothing to it, least of all
            # the credential.
            raise ConnectorRefused("the gateway is not the one this computer was shared with")
        self._frames = SessionFrames(grant.peer_key, bound, gateway=False)
        try:
            await self._send(connector_auth(grant.credential, bound))
            await self._send(
                {
                    "op": "ready",
                    "v": protocol.PROTOCOL_VERSION,
                    "root": self.grant.root,
                    "mcp": [m.name for m in self.grant.mcp],
                    "platform": sys.platform,
                    "confinement": self.plan.name,
                }
            )
        except ConnectionError:
            # The gateway may already have refused ``auth`` and closed: only its
            # authenticated refusal, if it arrived, says why; never the close.
            msg = await ws.receive(timeout=CONNECT_TIMEOUT_SECS)
            if msg.type == aiohttp.WSMsgType.TEXT:
                self._refusal_in(msg.data)
            raise
        liveness = Liveness(clock=self._clock, sleep=self._sleep)
        self._liveness = liveness
        heartbeat = asyncio.ensure_future(liveness.run(self._ping))
        try:
            return await self._dispatch(ws, liveness)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _ping(self) -> None:
        """One authenticated heartbeat; none before the gateway admitted this computer."""
        if self.admitted:
            await self._send({"op": "ping"})

    async def _dispatch(self, ws: Any, liveness: Liveness) -> str:
        import aiohttp

        assert self._frames is not None
        closing = frozenset(
            {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED}
        )
        while True:
            msg = await liveness.receive(ws.receive)
            if msg is None:
                # Interrupted, not refused: the grant stays and the controller redials.
                return (
                    "the gateway sent nothing authenticated for "
                    f"{int(liveness.deadline)} seconds; the connection was dropped"
                )
            if msg.type in closing:
                # The close code and reason are unauthenticated: never read.
                if not self.admitted:
                    return "the connection ended before the gateway admitted this computer"
                return "the connection ended"
            if msg.type != aiohttp.WSMsgType.TEXT:
                return "the gateway closed the connection"
            allowed = protocol.AFTER_ADMISSION if self.admitted else protocol.BEFORE_ADMISSION
            try:
                frame = self._frames.decode(msg.data, allowed=allowed)
            except protocol.ProtocolError as exc:
                self.protocol_failed = True
                return f"protocol error: {exc}"
            liveness.progress()
            if frame["op"] == "ping":
                continue
            if frame["op"] == "admitted":
                self.admitted = True
                continue
            if frame["op"] == "refusal":
                raise AuthenticatedRefusal(frame["code"])
            if frame["op"] == "stop":
                return f"the gateway ended access: {protocol.reason(frame['reason'])}"
            try:
                finished, why = await self._guarded(liveness, self._work(frame))
            except protocol.ProtocolError as exc:
                self.protocol_failed = True
                return f"protocol error: {exc}"
            if not finished:
                return (
                    "the gateway sent nothing authenticated for "
                    f"{int(liveness.deadline)} seconds; the connection was dropped"
                )
            if why:
                return why

    async def _guarded(self, liveness: Liveness, work: Awaitable[_W]) -> tuple[bool, Any]:
        """``(True, result)`` of ``work``, or ``(False, None)`` once liveness is lost first.

        Lost first: the work is cancelled and NOT awaited here (it may be stuck
        in a worker thread or a stop), so the run ends and :meth:`stop_all`
        stops every channel at once; :meth:`_reap_orphans` collects it later.
        """
        task = asyncio.ensure_future(work)
        lost = asyncio.ensure_future(liveness.lost.wait())
        try:
            await asyncio.wait({task, lost}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            self._abandon(task)
            raise
        finally:
            lost.cancel()
        if task.done():
            return True, task.result()
        self._abandon(task)
        return False, None

    def _abandon(self, task: "asyncio.Future[Any]") -> None:
        if not task.done():
            task.cancel()
            self._orphans.add(task)

    async def _reap_orphans(self) -> None:
        """Give cancelled frame work a bounded chance to finish its own cleanup."""
        orphans, self._orphans = self._orphans, set()
        if orphans:
            await asyncio.wait(orphans, timeout=STOP_GRACE_SECS)
            for task in orphans:
                task.add_done_callback(_consume)

    async def _work(self, frame: dict[str, Any]) -> str:
        """One work frame; returns why the run must end, or ""."""
        if frame["op"] == "data" and not await self._may_work():
            # A revoke or Off written by another process on this computer
            # (the CLI, the other host): new input never reaches a tool
            # server, and the run ends, stopping every process it started.
            return "access to this computer was turned off on it"
        await self._handle(frame)
        return ""

    def _usable(self) -> bool:
        """The run is not ending and its gateway is still live: a channel may start."""
        return not self._closing and not (
            self._liveness is not None and self._liveness.lost.is_set()
        )

    def _refusal_in(self, text: Any) -> None:
        """Raise the authenticated refusal ``text`` carries; anything else is ignored."""
        assert self._frames is not None
        try:
            frame = self._frames.decode(text, allowed=frozenset({"refusal"}))
        except protocol.ProtocolError:
            self.protocol_failed = True
            return
        raise AuthenticatedRefusal(frame["code"])

    # -- frames from the gateway --------------------------------------------------
    async def _may_work(self) -> bool:
        """The local switch and this grant, read now from their stores."""
        from kiro_crew.connected_computers.flag import local_enabled

        if not await asyncio.to_thread(local_enabled):
            return False
        return bool(await asyncio.to_thread(self._still_active))

    async def _handle(self, frame: dict[str, Any]) -> None:
        op = frame["op"]
        ch = frame["ch"]
        if op == "open":
            await self._open(ch, frame["kind"])
            return
        channel = self._channels.get(ch)
        if channel is None:
            return
        if op == "data":
            channel.inbound.feed(protocol.data_of(frame))
        elif op == "eof":
            channel.inbound.feed_eof()
        elif op == "credit":
            channel.window.grant(frame["bytes"])
        elif op == "close":
            await self._retire(channel)

    async def _retire(self, channel: _LocalChannel) -> None:
        """Stop ``channel``; if cancelled midway, :meth:`stop_all` awaits the stop."""
        if self._channels.get(channel.ch) is channel:
            del self._channels[channel.ch]
        self._stopping.add(channel)
        await channel.stop()
        self._stopping.discard(channel)

    async def _refuse(self, ch: int, why: str) -> None:
        await self._send({"op": "refused", "ch": ch, "reason": protocol.reason(why)})

    def _argv_for(self, kind: str) -> tuple[list[str], dict[str, str], list[str]]:
        family, name = protocol.kind_of(kind)
        if family == protocol.KIND_WORKSPACE:
            tools = tools_dir()
            argv = [sys.executable, "-I", "-u", os.path.join(tools, "workspace_tools.py")]
            return [*argv, "--root", self.grant.root], {}, [tools]
        for exposed in self.grant.mcp:
            if exposed.name == name:
                return [exposed.command, *exposed.args], dict(exposed.env), []
        raise ConnectorRefused(f"{name} is not shared from this computer")

    def _filter_for(self, kind: str, off: Optional[tuple[str, ...]] = None) -> Optional[ToolFilter]:
        """A filter for an exposed server with tools turned off here, else None (B14).

        ``off`` is the list :func:`.local_mcp.live_limits` read at this start;
        without it the consent's snapshot is used.
        """
        family, name = protocol.kind_of(kind)
        if family == protocol.KIND_WORKSPACE:
            return None
        for exposed in self.grant.mcp:
            if exposed.name == name:
                names = exposed.disabled_tools if off is None else off
                return ToolFilter(names) if names else None
        return None

    async def _live_off(self, kind: str) -> Optional[tuple[str, ...]]:
        """This start's tools-off list for an exposed server; None when it must not start."""
        from kiro_crew.connected_computers import local_mcp

        _family, name = protocol.kind_of(kind)
        exposed = next((m for m in self.grant.mcp if m.name == name), None)
        if exposed is None:
            return None
        try:
            servers = await asyncio.to_thread(local_mcp.configured_servers)
        except Exception:  # noqa: BLE001 -- unread local config: do not start it
            logger.warning("connected computer: local MCP config unreadable", exc_info=True)
            return None
        return local_mcp.live_limits(exposed, servers)

    async def _open(self, ch: int, kind: str) -> None:
        from kiro_crew.connected_computers.flag import local_enabled

        if ch in self._channels:
            raise protocol.ProtocolError("channel already open")
        # Off the loop: without a live snapshot it reads the config files.
        if not await asyncio.to_thread(local_enabled):
            await self._refuse(ch, "connected computers is turned off on this computer")
            return
        if not await asyncio.to_thread(self._still_active):
            await self._refuse(ch, "access to this computer was turned off on it")
            return
        if len(self._channels) >= protocol.MAX_CHANNELS:
            await self._refuse(ch, "this computer is already running its most tool servers")
            return
        try:
            # Off the loop: finding the tool server resolves the install's path.
            argv, env, reads = await asyncio.to_thread(self._argv_for, kind)
            off: Optional[tuple[str, ...]] = None
            if protocol.kind_of(kind)[0] != protocol.KIND_WORKSPACE:
                off = await self._live_off(kind)
                if off is None:
                    raise ConnectorRefused(
                        f"{protocol.kind_of(kind)[1]} was removed or turned off on this computer"
                    )
            tool_filter = self._filter_for(kind, off)
        except (ConnectorRefused, protocol.ProtocolError, ValueError) as exc:
            await self._refuse(ch, str(exc))
            return
        # Off the loop: the temp folder, and every probe, realpath walk and
        # seccomp pipe write a plan's wrap does, can stall on slow storage.
        scratch = await run_owned(_remove_scratch, tempfile.mkdtemp, prefix="kirocrew-cc-")
        try:
            confined = await wrap_off_loop(
                self.plan, argv, root=self.grant.root, scratch=scratch, reads=reads, env=env
            )
            try:
                # Liveness may have been lost while this was being prepared.
                proc = await _spawn(confined) if self._usable() else None
            finally:
                confined.close()
        except asyncio.CancelledError:
            # The socket is ending: no refusal to send, but the folder is removed.
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True))
            raise
        except Exception as exc:  # noqa: BLE001 -- refuse, never run unconfined
            await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
            await self._refuse(ch, f"could not start it confined: {type(exc).__name__}")
            return
        if proc is None:
            await asyncio.to_thread(shutil.rmtree, scratch, ignore_errors=True)
            return
        channel = _LocalChannel(ch, proc, scratch, tool_filter=tool_filter)
        if not self._usable():
            # Spawned as the run ended: stopped here, since stop_all may have run.
            await channel.stop()
            return
        self._channels[ch] = channel
        await self._send({"op": "opened", "ch": ch})
        channel.tasks = [
            asyncio.ensure_future(self._pump_out(channel)),
            asyncio.ensure_future(self._pump_in(channel)),
        ]

    async def _pump_out(self, channel: _LocalChannel) -> None:
        """The child's stdout to the gateway, within the gateway's credit."""
        assert channel.proc.stdout is not None
        why = "finished"
        try:
            while True:
                data = await channel.proc.stdout.read(_READ_CHUNK)
                if not data:
                    break
                if channel.filter is not None:
                    data = channel.filter.outbound(data)
                await self._emit(channel, data)
            if channel.filter is not None:
                await self._emit(channel, channel.filter.outbound_eof())
            async with channel.out:
                channel.ended = True
                channel.out.notify_all()
            await self._send({"op": "eof", "ch": channel.ch})
            code = await channel.proc.wait()
            why = f"exited with status {code}"
        except (protocol.WindowClosed, asyncio.CancelledError):
            return
        except Exception as exc:  # noqa: BLE001 -- the socket is gone
            why = f"failed: {type(exc).__name__}"
        if self._channels.get(channel.ch) is channel:
            del self._channels[channel.ch]
            self._stopping.add(channel)
            with contextlib.suppress(Exception):
                await self._send({"op": "closed", "ch": channel.ch, "reason": why})
            await channel.stop()
            self._stopping.discard(channel)

    async def _pump_in(self, channel: _LocalChannel) -> None:
        """Gateway bytes to the child's stdin, granting credit as each is written."""
        stdin = channel.proc.stdin
        assert stdin is not None
        try:
            while True:
                data = await channel.inbound.read(_READ_CHUNK)
                if channel.filter is None:
                    to_child, replies = data, b""
                elif data:
                    to_child, replies = channel.filter.inbound(data)
                else:
                    to_child, replies = channel.filter.inbound_eof()
                if to_child:
                    stdin.write(to_child)
                    await stdin.drain()
                if replies:
                    await self._reply(channel, replies)
                if not data:
                    stdin.close()
                    return
                # Credit what the gateway sent, forwarded to the server or not.
                await self._send({"op": "credit", "ch": channel.ch, "bytes": len(data)})
        except (asyncio.CancelledError, ConnectionError, BrokenPipeError):
            return
        except Exception:  # noqa: BLE001 -- the child or the socket ended
            logger.debug("connected computer: stdin pump ended", exc_info=True)

    async def _emit(self, channel: _LocalChannel, data: bytes) -> None:
        """Send server output within the gateway's credit, one writer at a time."""
        if not data:
            return
        async with channel.out:
            await self._send_data(channel, data)
            channel.out.notify_all()

    async def _reply(self, channel: _LocalChannel, data: bytes) -> None:
        """Answer the gateway for the server, only between the server's lines (B14).

        Dropped once the server's output has ended: the gateway then answers
        every call still waiting as outcome unknown.
        """
        assert channel.filter is not None
        tool_filter = channel.filter
        async with channel.out:
            await channel.out.wait_for(lambda: channel.ended or tool_filter.at_boundary)
            if channel.ended:
                return
            await self._send_data(channel, data)

    async def _send_data(self, channel: _LocalChannel, data: bytes) -> None:
        """Send ``data`` one frame at a time, each within the credit it needs.

        A single line can be larger than the whole window (a filtered server
        buffers each line up to its 8 MiB limit), so credit is taken per frame,
        never for the whole buffer. Callers hold ``channel.out``, so the frames of
        one message are never interleaved with another's.
        """
        step = protocol.MAX_DATA_BYTES
        for start in range(0, len(data), step):
            piece = data[start : start + step]
            await channel.window.acquire(len(piece))
            await self._send(protocol.data_frames(channel.ch, piece)[0])

    async def stop_all(self) -> None:
        """Stop every process tree this socket started (bounded per child).

        Also those whose own stop was cancelled midway, and no channel starts
        after this (:meth:`_usable`).
        """
        self._closing = True
        channels = [*self._channels.values(), *self._stopping]
        self._channels.clear()
        self._stopping.clear()
        await asyncio.gather(*(c.stop() for c in channels), return_exceptions=True)

    async def close(self) -> None:
        """End the socket now (local Off, revocation, switch off)."""
        await self.stop_all()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

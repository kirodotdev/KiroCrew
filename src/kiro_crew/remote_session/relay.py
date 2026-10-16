"""Gateway-side relays: the engine's bridge reaches the guest through here.

The bridge (``client.py``) is an MCP child of the agent engine, so it runs inside
the engine's sandbox. That sandbox cannot use the gateway machine's ``coder login``
(on macOS the CLI keeps it in the login keychain), so a dial from there fails
before any byte reaches the guest. The relays run that dial in the GATEWAY.

Two shapes:

* :class:`WorkspaceRelay` (what remote sessions use). The engine may launch its
  stdio MCP server more than once per runtime (a tools probe, a session reload),
  and each launch needs its own working stream. So the relay does the remote side
  per CONNECTION: it starts a fresh guest worker with a fresh one-use secret, runs
  the authenticated, integrity-checked :class:`RemoteLink` to it here in the
  gateway, and pipes plain bytes to the bridge. The bridge proves itself with a
  per-runtime token (a 0600 file in a 0700 directory, removed when the relay
  closes), so a local process that merely finds the port gets nothing.
* :class:`DialRelay` pipes one connection through a dial command; kept for the
  older bridge mode in which the bridge holds the link itself.

Both bind 127.0.0.1 only, serve a bounded number of connections, and close
themselves after ``idle_timeout`` with nothing connected.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from kiro_crew.remote_session import wire

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_STDERR_TAIL = 400
_MAX_LIVE_RELAYS = 64
_TOKEN_LINE_MAX = 256
_TOKEN_READ_TIMEOUT = 10.0
#: Strong references: asyncio keeps only weak ones to server tasks.
_LIVE: "set[Any]" = set()


class _Relay:
    def __init__(self, *, name: str, idle_timeout: float) -> None:
        self.name = name
        self.idle_timeout = idle_timeout
        self.port = 0
        self._server: Optional[asyncio.base_events.Server] = None
        self._active = 0
        self._last_activity = time.monotonic()
        self._idle_task: Optional[asyncio.Task] = None

    async def start(self) -> int:
        if len(_LIVE) >= _MAX_LIVE_RELAYS:
            raise RuntimeError("too many remote workspace relays are open")
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        _LIVE.add(self)
        self._idle_task = asyncio.ensure_future(self._reap_when_idle())
        return self.port

    async def close(self) -> None:
        _LIVE.discard(self)
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._closed()

    def _closed(self) -> None:
        """Subclass hook: release what the relay owns."""

    async def _reap_when_idle(self) -> None:
        while True:
            await asyncio.sleep(min(60.0, self.idle_timeout))
            if not self._active and time.monotonic() - self._last_activity >= self.idle_timeout:
                await self.close()
                return

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raise NotImplementedError


class WorkspaceRelay(_Relay):
    """Per-connection fresh guest worker + gateway-side link, behind a token."""

    def __init__(
        self,
        launch: Any,
        remote_argv: Sequence[str],
        token_path: Path,
        token: bytes,
        *,
        link_factory: Optional[Callable[..., Any]] = None,
        name: str = "",
        idle_timeout: float = 900.0,
        max_connections: int = 4,
        prestart: bool = True,
        spare_max_age: float = wire.SPARE_MAX_AGE_SECONDS,
    ) -> None:
        super().__init__(name=name, idle_timeout=idle_timeout)
        self.launch = launch
        self.remote_argv = list(remote_argv)
        self.token_path = Path(token_path)
        self._token = token
        self._link_factory = link_factory
        self.max_connections = max_connections
        self.prestart = prestart
        # One guest worker started ahead of the next connection, so a launch
        # pays only the tunnel handshake (~5 s of worker start moves off the
        # critical path). Each spare still has its own fresh one-use secret and
        # is handed to exactly one connection, never shared.
        self._spare: Optional[asyncio.Task] = None
        self._spare_started = 0.0
        self.spare_max_age = spare_max_age

    async def start(self) -> int:
        port = await super().start()
        self._refill()
        return port

    def _closed(self) -> None:
        with contextlib.suppress(OSError):
            self.token_path.unlink()

    async def close(self) -> None:
        # Stop the unused spare BEFORE returning, not in a detached task: a stop
        # still running when its loop shuts down is cancelled mid-command and can
        # leave the guest worker running.
        spare, self._spare = self._spare, None
        await super().close()
        if spare is not None:
            await self._discard(spare)

    def _refill(self) -> None:
        if not self.prestart or self._server is None or self._spare is not None:
            return
        self._spare_started = time.monotonic()
        self._spare = asyncio.ensure_future(self._start_worker())

    async def _start_worker(self) -> tuple[str, bytes, int]:
        from kiro_crew.remote_session import wire

        worker_id = self.launch.new_worker_id()
        secret = wire.new_secret()
        port = await self.launch.start_worker(worker_id, self.remote_argv, secret)
        return worker_id, secret, port

    async def _discard(self, spare: asyncio.Task) -> None:
        """Stop a spare nobody will use (relay closing, or it went stale)."""
        # Never cancel a start in flight: killing the local `coder ssh` mid-start
        # does not stop the guest worker it already launched (it is setsid'd),
        # which would leak it with no id to stop it by. Let it finish, then stop it.
        try:
            worker_id, _secret, _port = await spare
        except BaseException:
            return
        with contextlib.suppress(Exception):
            await self.launch.stop_worker(worker_id)

    async def _take_worker(self) -> tuple[str, bytes, int]:
        """The spare if it is usable, else a worker started now; then refill."""
        spare, self._spare = self._spare, None
        worker = None
        if spare is not None:
            if time.monotonic() - self._spare_started > self.spare_max_age:
                # Older than the guest's orphan timeout allows: it may have
                # exited on its own. Start fresh instead of dialling a corpse.
                await self._discard(spare)
            else:
                try:
                    worker = await spare
                except Exception as exc:
                    logger.warning("remote workspace %s: spare worker failed: %s", self.name, exc)
        if worker is None:
            worker = await self._start_worker()
        self._refill()
        return worker

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= self.max_connections:
            writer.close()
            return
        self._active += 1
        worker_id = ""
        link = None
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), _TOKEN_READ_TIMEOUT)
            except (asyncio.TimeoutError, ValueError):
                return
            presented = line.strip()[:_TOKEN_LINE_MAX]
            if not hmac.compare_digest(presented, self._token):
                logger.warning("remote workspace %s: refused a relay connection", self.name)
                return
            link, worker_id = await self._open_link()
            await self._pump(reader, writer, link)
        except Exception as exc:  # the bridge sees the stream end; record why
            logger.warning("remote workspace %s: connection failed: %s", self.name, exc)
        finally:
            writer.close()
            if link is not None:
                with contextlib.suppress(Exception):
                    await link.aclose()
            if worker_id:
                with contextlib.suppress(Exception):
                    await self.launch.stop_worker(worker_id)
            self._active -= 1
            self._last_activity = time.monotonic()

    async def _open_link(self) -> tuple[Any, str]:
        from kiro_crew.remote_session.client import command_dial
        from kiro_crew.remote_session.link import RemoteLink

        worker_id, secret, port = await self._take_worker()
        own_dial = getattr(self.launch, "dial", None)
        dial = (
            own_dial(port)
            if callable(own_dial)
            else command_dial(self.launch.dial_argv(port), self.launch.dial_env())
        )
        factory = self._link_factory or RemoteLink
        link = factory(worker_id, secret, dial, **self.launch.link_kwargs())
        link.start()
        return link, worker_id

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, link: Any
    ) -> None:
        from kiro_crew.remote_session.link import LinkUnavailable

        async def up() -> None:
            while chunk := await reader.read(_CHUNK):
                await link.write(chunk)
                self._last_activity = time.monotonic()
            link.close_input()

        async def down() -> None:
            while True:
                try:
                    chunk = await link.stdout.read(_CHUNK)
                except LinkUnavailable:
                    return
                if not chunk:
                    return
                writer.write(chunk)
                await writer.drain()
                self._last_activity = time.monotonic()

        tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        try:
            # Down ends the connection (the guest finished or the link was lost);
            # up ending only closes the guest's stdin, whose output still drains.
            await tasks[1]
        finally:
            tasks[0].cancel()
        if getattr(link, "state", "") not in ("finished", "closed"):
            logger.warning(
                "remote workspace %s: link ended %s: %s",
                self.name,
                getattr(link, "state", "?"),
                getattr(link, "error", ""),
            )


class DialRelay(_Relay):
    """Accept one loopback connection at a time and pipe it through ``argv``."""

    def __init__(
        self,
        argv: Sequence[str],
        env: Optional[dict[str, str]] = None,
        *,
        name: str = "",
        idle_timeout: float = 900.0,
        min_dial_gap: float = 1.0,
    ) -> None:
        if not argv:
            raise ValueError("a dial command is required")
        super().__init__(name=name, idle_timeout=idle_timeout)
        self.argv = list(argv)
        self.env = env
        self.min_dial_gap = min_dial_gap
        self._last_dial = 0.0

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        now = time.monotonic()
        if self._active or now - self._last_dial < self.min_dial_gap:
            writer.close()
            return
        self._active = 1
        self._last_dial = now
        process: Optional[asyncio.subprocess.Process] = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.env,
                start_new_session=True,
            )
            await self._pump(reader, writer, process)
        except OSError as exc:
            logger.warning("remote workspace %s: dial could not start: %s", self.name, exc)
        finally:
            writer.close()
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), 5)
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
            self._active = 0
            self._last_activity = time.monotonic()

    async def _pump(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        process: asyncio.subprocess.Process,
    ) -> None:
        assert process.stdin is not None and process.stdout is not None
        assert process.stderr is not None
        stdin, stdout, stderr = process.stdin, process.stdout, process.stderr
        tail = bytearray()

        async def up() -> None:
            while chunk := await reader.read(_CHUNK):
                stdin.write(chunk)
                await stdin.drain()
                self._last_activity = time.monotonic()
            stdin.close()

        async def down() -> None:
            while chunk := await stdout.read(_CHUNK):
                writer.write(chunk)
                await writer.drain()
                self._last_activity = time.monotonic()

        async def err() -> None:
            while chunk := await stderr.read(_CHUNK):
                tail.extend(chunk)
                del tail[:-_STDERR_TAIL]

        tasks = [asyncio.ensure_future(t()) for t in (up, down, err)]
        try:
            await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks[:2]:
                task.cancel()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(process.wait(), 2)
            tasks[2].cancel()
        if process.returncode:
            reason = tail.decode("utf-8", "replace").strip().splitlines()[-1:] or [""]
            logger.warning(
                "remote workspace %s: dial exited %s: %s",
                self.name,
                process.returncode,
                reason[0][:300],
            )


def write_token_file(directory: Path, name: str) -> tuple[Path, bytes]:
    """A fresh relay token in a private file the sandboxed bridge can read."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = os.urandom(24).hex().encode("ascii")
    path = directory / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, token)
    finally:
        os.close(fd)
    return path, token

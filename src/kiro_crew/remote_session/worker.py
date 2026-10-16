"""Guest-side worker: runs the agent process and serves it over ``wire``.

Copied into a session environment beside ``wire.py`` and started once per
session, for example::

    python3 worker.py --session-id S --secret-file /run/crew/S.key \
        --listen 127.0.0.1:7001 -- kiro-cli acp --agent-engine v3

Stdlib only. Security and lifetime properties:

* The secret file must be owned by this user and readable by nobody else; it
  is read once and unlinked, so code the agent later runs cannot read it from
  disk. Run the worker as a different OS user from agent tools so they cannot
  read it from this process either.
* Only an authenticated gateway is attached. Any other connection, including
  another process inside the task reaching the loopback port, gets the hello
  line and is dropped after a failed or slow handshake. A newly authenticated
  connection replaces the old one, so a half-dead tunnel never blocks recovery.
* The child keeps running across connections; its output is buffered (bounded)
  until the gateway resumes.
* Credentials are never passed to the child: ``KIRO_API_KEY`` and the other
  credential variables are removed, so the engine asks the gateway for a token.
* With no authenticated gateway for ``--orphan-timeout`` seconds the child is
  stopped and the worker exits, so abandoned compute stops costing money.
* SIGTERM retires cleanly: the gateway is told, the child gets ``--retire-grace``
  seconds to finish, then the final output is flushed and the worker exits.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import stat
import sys
import time
from typing import Optional

try:
    from . import wire  # type: ignore[no-redef]
except ImportError:  # copied beside wire.py in a guest
    import wire  # type: ignore[no-redef]

#: Environment variables a child must never inherit. The engine prefers an
#: environment API key over the gateway's token callback, so one leaking in
#: from an image or template would silently bypass sign-out.
WITHHELD_ENV = frozenset(
    {
        "KIRO_API_KEY",
        "KIRO_IDENTITY",
        "SMC_CONTROL_SECRET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "CREW_SESSION_SECRET",
    }
)
HANDSHAKE_TIMEOUT = 10.0
TICK_SECONDS = 0.25
READ_CHUNK = 64 * 1024


def log(text: str) -> None:
    print(f"crew-worker: {text}", file=sys.stderr, flush=True)


def _user_id() -> "int | None":
    """This user's uid, or None on an OS without one."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _group_ids() -> str:
    """``pgid N sid N`` for the log, where the OS has process groups."""
    getpgid = getattr(os, "getpgid", None)
    getsid = getattr(os, "getsid", None)
    if getpgid is None or getsid is None:
        return ""
    return f"pgid {getpgid(0)} sid {getsid(0)}"


def read_secret(path: str) -> bytes:
    """Read and unlink a private secret file, refusing a shared or foreign one."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("secret file is not a regular file")
        uid = _user_id()
        if uid is not None and info.st_uid != uid:
            raise PermissionError("secret file is not owned by this user")
        if info.st_mode & 0o077:
            raise PermissionError("secret file must not be readable by group or others")
        data = os.read(fd, 4096).strip()
    finally:
        os.close(fd)
    os.unlink(path)
    secret = bytes.fromhex(data.decode("ascii"))
    if len(secret) < wire.SECRET_MIN_BYTES:
        raise ValueError("secret is too short")
    return secret


def child_env(base: Optional[dict] = None) -> dict:
    env = dict(os.environ if base is None else base)
    for name in WITHHELD_ENV:
        env.pop(name, None)
    return env


class Worker:
    def __init__(
        self,
        session_id: str,
        secret: bytes,
        command: list[str],
        *,
        orphan_timeout: float = wire.ORPHAN_TIMEOUT_SECONDS,
        retire_grace: float = 120.0,
        drain_timeout: float = 30.0,
        max_unacked_bytes: int = wire.DEFAULT_MAX_UNACKED_BYTES,
        port_file: str = "",
    ) -> None:
        wire.validate_session_id(session_id)
        self.port_file = port_file
        if not command:
            raise ValueError("a child command is required")
        self.session_id = session_id
        self._secret = secret
        self.command = command
        self.orphan_timeout = orphan_timeout
        self.retire_grace = retire_grace
        self.drain_timeout = drain_timeout
        self.ep = wire.Endpoint(wire.WORKER, max_unacked_bytes=max_unacked_bytes)
        self.child: Optional[asyncio.subprocess.Process] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._conn_task: Optional[asyncio.Task] = None
        # Created in run(), inside the loop: on Python < 3.10 an Event made
        # before the loop starts binds to a different loop and fails on wait.
        self._room_ev: Optional[asyncio.Event] = None
        self._last_gateway = time.monotonic()
        self._stopping_ev: Optional[asyncio.Event] = None
        self.exit_code = 0

    # Created on first use, which is always inside the running loop: on Python
    # < 3.10 an Event made before the loop starts binds to a different loop and
    # raises on wait(). Guest images ship 3.9.
    @property
    def _room(self) -> asyncio.Event:
        if self._room_ev is None:
            self._room_ev = asyncio.Event()
        return self._room_ev

    @property
    def _stopping(self) -> asyncio.Event:
        if self._stopping_ev is None:
            self._stopping_ev = asyncio.Event()
        return self._stopping_ev

    # -- output to the gateway -------------------------------------------------
    def _send(self, data: bytes) -> None:
        if data and self._writer is not None and not self._writer.is_closing():
            self._writer.write(data)

    async def _pump_child_stdout(self) -> None:
        assert self.child and self.child.stdout
        while chunk := await self.child.stdout.read(READ_CHUNK):
            while not self.ep.can_accept(len(chunk)):
                self._room.clear()
                await self._room.wait()  # backpressure: stop reading the child
            self._send(self.ep.write(chunk, time.monotonic()))

    # -- connections -----------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        shake = wire.WorkerHandshake(self.session_id, self._secret)
        try:
            writer.write(shake.hello())
            line = await asyncio.wait_for(_read_line(reader), HANDSHAKE_TIMEOUT)
            attachment, welcome = shake.on_join(line, self.ep.recv_next)
        except (asyncio.TimeoutError, wire.WireError, ConnectionError, ValueError) as exc:
            log(f"rejected a connection: {type(exc).__name__}")
            writer.close()
            return
        # Authenticated: this connection replaces any previous one.
        old_writer, old_task = self._writer, self._conn_task
        self._writer, self._conn_task = writer, asyncio.current_task()
        if old_task is not None and old_task is not asyncio.current_task():
            old_task.cancel()
        if old_writer is not None:
            old_writer.close()
        now = time.monotonic()
        self._last_gateway = now
        try:
            writer.write(welcome)
            writer.write(self.ep.attach(attachment, now))
            log("gateway attached")
            while chunk := await reader.read(READ_CHUNK):
                data, out, events = self.ep.feed(chunk, time.monotonic())
                self._last_gateway = time.monotonic()
                if data and self.child and self.child.stdin:
                    self.child.stdin.write(data)
                    await self.child.stdin.drain()
                self._send(out)
                self._room.set()
                if self.ep.peer_finished and self.child and self.child.stdin:
                    if not self.child.stdin.is_closing():
                        self.child.stdin.close()
                for kind, _ in events:
                    if kind == "close":
                        log("gateway closed its input")
        except (ConnectionError, wire.WireError) as exc:
            log(f"connection ended: {type(exc).__name__}")
        finally:
            if self._writer is writer:
                self.ep.detach()
                self._writer, self._conn_task = None, None
            writer.close()

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            self._send(self.ep.tick(time.monotonic()))
            self._room.set()
            if self.ep.attached:
                self._last_gateway = time.monotonic()
            elif time.monotonic() - self._last_gateway > self.orphan_timeout:
                log("no gateway within the orphan timeout; stopping")
                self.exit_code = 3
                self._stopping.set()

    # -- lifecycle -------------------------------------------------------------
    async def retire(self, reason: str = "retire") -> None:
        """Tell the gateway, give the child time to finish, then stop it."""
        self._send(self.ep.retire(reason))
        if self.child is None:
            return
        try:
            await asyncio.wait_for(self.child.wait(), self.retire_grace)
        except asyncio.TimeoutError:
            self.child.terminate()

    async def run(self, listen_host: str, listen_port: int) -> int:
        self.child = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=child_env(),
            # Its own process group: a signal aimed at the group the worker
            # was launched in (an SSH session reaper) must not reach the engine.
            start_new_session=True,
        )
        server = await asyncio.start_server(
            self.handle, listen_host, listen_port, limit=wire.MAX_LINE_BYTES * 2
        )
        self.port = server.sockets[0].getsockname()[1]
        log(f"listening on {listen_host}:{self.port}, child pid {self.child.pid}")
        if self.port_file:
            # Atomic: a reader polling for the file never sees a partial port.
            tmp = f"{self.port_file}.tmp"
            with open(tmp, "w", encoding="ascii") as handle:
                handle.write(f"{self.port} {os.getpid()}\n")
            os.replace(tmp, self.port_file)
        loop = asyncio.get_running_loop()
        log(f"pid {os.getpid()} {_group_ids()} ppid {os.getppid()}")
        try:
            loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(self._on_term()))
            # A session hangup must not take the worker down: the gateway
            # reaches it again through a new connection.
            hangup = getattr(signal, "SIGHUP", None)
            if hangup is not None:
                loop.add_signal_handler(hangup, lambda: log("ignored SIGHUP"))
        except (NotImplementedError, RuntimeError):
            pass
        ticker = asyncio.ensure_future(self._ticker())
        pump = asyncio.ensure_future(self._pump_child_stdout())
        stop = asyncio.ensure_future(self._stopping.wait())
        await asyncio.wait({pump, stop}, return_when=asyncio.FIRST_COMPLETED)
        if pump.done() and not pump.cancelled() and pump.exception() is not None:
            # A worker bug must never read as the engine dying on its own.
            exc = pump.exception()
            log(f"output pump failed: {type(exc).__name__}: {exc}"[:300])
            self.exit_code = 5
        if self.child.returncode is None:
            self.child.terminate()
            try:
                await asyncio.wait_for(self.child.wait(), 10)
            except asyncio.TimeoutError:
                self.child.kill()
        code = await self.child.wait()
        try:  # the child's stdout reaches EOF once it exits; queue the rest
            await asyncio.wait_for(pump, 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pump.cancel()
        # Final output is queued; promise the end and wait (bounded) for acks.
        self._send(self.ep.close("child-exit"))
        deadline = time.monotonic() + self.drain_timeout
        while self.ep.unacked_bytes and time.monotonic() < deadline:
            await asyncio.sleep(TICK_SECONDS)
        ticker.cancel()
        stop.cancel()
        server.close()
        if self._writer is not None:
            self._writer.close()
        log(f"child exited {code}; worker exiting")
        return self.exit_code or (code if code and code > 0 else 0)

    async def _on_term(self) -> None:
        log("SIGTERM: retiring")
        await self.retire("sigterm")
        self._stopping.set()


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    buf = b""
    while True:
        lines, rest, overflow = wire.split_lines(buf, wire.MAX_HANDSHAKE_LINE_BYTES)
        if overflow:
            raise wire.AuthError("handshake line too long")
        if lines:
            if rest or len(lines) > 1:
                raise wire.AuthError("unexpected data during handshake")
            return lines[0]
        chunk = await reader.read(wire.MAX_HANDSHAKE_LINE_BYTES)
        if not chunk:
            raise ConnectionError("closed during handshake")
        buf = rest + chunk


def attach(port: int, *, stdin=None, stdout=None) -> int:
    """Pipe this process's stdio to the worker's loopback port, unchanged.

    The gateway's dial command (``coder ssh WS -- python3 worker.py attach``)
    runs this inside the guest, so the tunnel carries ``wire`` bytes straight
    to the worker. It adds no trust: the worker still authenticates the peer.
    """
    import socket
    import threading

    out = stdout or sys.stdout.buffer
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.settimeout(None)
    if stdin is not None:
        read = stdin.read1 if hasattr(stdin, "read1") else stdin.read
    else:
        # Raw descriptor: a daemon thread blocked in a buffered read would
        # abort the interpreter at exit.
        in_fd = sys.stdin.fileno()

        def read(n: int) -> bytes:
            return os.read(in_fd, n)

    def upstream() -> None:
        try:
            while chunk := read(READ_CHUNK):
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    threading.Thread(target=upstream, daemon=True).start()
    try:
        while chunk := sock.recv(READ_CHUNK):
            out.write(chunk)
            out.flush()
    except OSError:
        pass
    finally:
        sock.close()
    return 0


def _parse(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    if "--" not in argv:
        raise SystemExit("usage: worker.py [options] -- <command ...>")
    split = argv.index("--")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session-id", required=True)
    p.add_argument("--secret-file", required=True)
    p.add_argument("--listen", default="127.0.0.1:7001")
    p.add_argument("--orphan-timeout", type=float, default=wire.ORPHAN_TIMEOUT_SECONDS)
    p.add_argument("--retire-grace", type=float, default=120.0)
    p.add_argument("--port-file", default="", help="write '<port> <pid>' here once listening")
    return p.parse_args(argv[:split]), argv[split + 1 :]


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["attach"]:
        p = argparse.ArgumentParser(prog="worker.py attach")
        p.add_argument("--port", type=int, required=True)
        return attach(p.parse_args(argv[1:]).port)
    args, command = _parse(argv)
    host, _, port = args.listen.rpartition(":")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("--listen must be a loopback address; reach it through the tunnel")
    worker = Worker(
        args.session_id,
        read_secret(args.secret_file),
        command,
        orphan_timeout=args.orphan_timeout,
        retire_grace=args.retire_grace,
        port_file=args.port_file,
    )
    return asyncio.run(worker.run(host, int(port)))


if __name__ == "__main__":
    sys.exit(main())

"""Local bridge process for a remote session: this process's stdio <-> RemoteLink.

``AcpRuntime`` spawns this exactly like a local engine, so its PID tracking,
reapers, sandbox and credential callback all apply unchanged; the engine's
JSON-RPC (including ``_kiro/auth/getAccessToken``) passes through byte for byte.

    python -m kiro_crew.remote_session.client --session-id S --secret-file F \
        -- coder ssh --disable-autostart WS -- python3 .../worker.py attach --port P

Everything after ``--`` is the dial command: run once per connection attempt,
its stdin/stdout are the tunnel to the guest worker. The secret file is read
once and unlinked, so it never appears in argv or the environment.

Exit codes tell the runtime why the stream ended:
  0  FINISHED  the remote engine exited and all its output was delivered
  75 LOST      no authenticated connection within the lease; restore elsewhere
  76 RETIRING  the worker retired and the stream ended; restore elsewhere
  77 REFUSED   the peer failed authentication; do not retry this binding
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
from typing import Optional

from kiro_crew.remote_session import link as link_mod
from kiro_crew.remote_session import wire
from kiro_crew.remote_session.worker import _user_id, read_secret

EXIT_FINISHED = 0
EXIT_LOST = 75
EXIT_RETIRING = 76
EXIT_REFUSED = 77
READ_CHUNK = 64 * 1024


class _ProcessWriter:
    """StreamWriter-shaped view of a dial process's stdin that also ends the process."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        assert process.stdin is not None
        self._stdin = process.stdin

    def write(self, data: bytes) -> None:
        self._stdin.write(data)

    def is_closing(self) -> bool:
        return self._stdin.is_closing() or self._process.returncode is not None

    def close(self) -> None:
        if not self._stdin.is_closing():
            self._stdin.close()
        if self._process.returncode is None:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass


def command_dial(argv: list[str], env: Optional[dict] = None) -> link_mod.Dial:
    """A Dial that runs ``argv`` per attempt and uses its stdio as the tunnel."""
    if not argv:
        raise ValueError("a dial command is required")

    async def dial():
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=wire.MAX_LINE_BYTES * 2,
        )
        assert process.stdout is not None
        return process.stdout, _ProcessWriter(process)

    return dial


def tcp_dial(host: str, port: int) -> link_mod.Dial:
    """A Dial that opens one loopback connection to the gateway's dial relay.

    Used when the bridge runs inside a sandbox that cannot run the dial command
    itself (``relay.DialRelay`` runs it in the gateway instead).
    """
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the dial relay is loopback-only")

    async def dial():
        return await asyncio.open_connection(host, port, limit=wire.MAX_LINE_BYTES * 2)

    return dial


def exit_code_for(state: str) -> int:
    return {
        link_mod.FINISHED: EXIT_FINISHED,
        link_mod.RETIRING: EXIT_RETIRING,
        link_mod.REFUSED: EXIT_REFUSED,
    }.get(state, EXIT_LOST)


async def bridge(
    link: link_mod.RemoteLink,
    stdin_read,
    stdout_write,
) -> int:
    """Pump local stdin into the link and the link's stdout out, until it ends."""
    loop = asyncio.get_running_loop()
    link.start()
    inbound: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=16)

    def reader_thread() -> None:
        # A daemon thread, not the executor: a blocking stdin read must never
        # hold up interpreter exit once the link has ended.
        while True:
            try:
                chunk = stdin_read(READ_CHUNK)
            except (OSError, ValueError):
                chunk = b""
            fut = asyncio.run_coroutine_threadsafe(inbound.put(chunk), loop)
            try:
                fut.result()
            except Exception:
                return
            if not chunk:
                return

    threading.Thread(target=reader_thread, daemon=True).start()

    async def upstream() -> None:
        while True:
            chunk = await inbound.get()
            if not chunk:
                link.close_input()
                return
            await link.write(chunk)

    async def downstream() -> None:
        while True:
            chunk = await link.stdout.read(READ_CHUNK)
            if not chunk:
                return
            await loop.run_in_executor(None, stdout_write, chunk)

    up = asyncio.ensure_future(upstream())
    try:
        await downstream()
    except link_mod.LinkUnavailable:
        pass
    finally:
        up.cancel()
        state = link.state
        retiring = state == link_mod.RETIRING or link.retire_reason
        await link.aclose()
    if state == link_mod.FINISHED:
        return EXIT_FINISHED
    if retiring:
        return EXIT_RETIRING
    print(f"crew-remote: {state}: {link.error}", file=sys.stderr, flush=True)
    return exit_code_for(state)


def _write_all(chunk: bytes) -> None:
    out = sys.stdout.buffer
    out.write(chunk)
    out.flush()


async def pipe(host: str, port: int, token: bytes, stdin_read, stdout_write) -> int:
    """Plain stdio <-> loopback pipe to a gateway :class:`WorkspaceRelay`.

    The relay holds the authenticated link to the guest; this only proves itself
    with the runtime's token and moves bytes. Exits 0 when the relay ends the
    stream, 75 when it cannot be reached.
    """
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("the workspace relay is loopback-only")
    try:
        reader, writer = await asyncio.open_connection(host, port, limit=wire.MAX_LINE_BYTES * 2)
    except OSError as exc:
        print(f"crew-remote: relay unreachable: {exc}", file=sys.stderr, flush=True)
        return EXIT_LOST
    writer.write(token + b"\n")
    loop = asyncio.get_running_loop()
    inbound: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=16)

    def reader_thread() -> None:
        while True:
            try:
                chunk = stdin_read(READ_CHUNK)
            except (OSError, ValueError):
                chunk = b""
            fut = asyncio.run_coroutine_threadsafe(inbound.put(chunk), loop)
            try:
                fut.result()
            except Exception:
                return
            if not chunk:
                return

    threading.Thread(target=reader_thread, daemon=True).start()

    async def upstream() -> None:
        while True:
            chunk = await inbound.get()
            if not chunk:
                if writer.can_write_eof():
                    writer.write_eof()
                return
            writer.write(chunk)
            await writer.drain()

    up = asyncio.ensure_future(upstream())
    try:
        while chunk := await reader.read(READ_CHUNK):
            await loop.run_in_executor(None, stdout_write, chunk)
    finally:
        up.cancel()
        writer.close()
    return EXIT_FINISHED


def _read_token(path: str) -> bytes:
    """Read the relay token WITHOUT unlinking it: the engine may relaunch this
    server, and every launch must authenticate. The relay deletes it on close."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        uid = _user_id()
        if uid is not None and (info.st_uid != uid or info.st_mode & 0o077):
            raise PermissionError("relay token file is not private")
        return os.read(fd, 256).strip()
    finally:
        os.close(fd)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--pipe" in argv:
        q = argparse.ArgumentParser(prog="kiro_crew.remote_session.client")
        q.add_argument("--pipe", required=True, help="HOST:PORT of a gateway workspace relay")
        q.add_argument("--token-file", required=True)
        pargs = q.parse_args(argv)
        host, _, port_text = pargs.pipe.rpartition(":")
        if not port_text.isdigit():
            raise SystemExit("--pipe must be HOST:PORT")
        token = _read_token(pargs.token_file)
        fd = sys.stdin.fileno()
        return asyncio.run(pipe(host, int(port_text), token, lambda n: os.read(fd, n), _write_all))
    split = argv.index("--") if "--" in argv else len(argv)
    p = argparse.ArgumentParser(prog="kiro_crew.remote_session.client")
    p.add_argument("--session-id", required=True)
    p.add_argument("--secret-file", required=True)
    p.add_argument(
        "--connect", help="HOST:PORT of a loopback dial relay, instead of a dial command"
    )
    p.add_argument("--keepalive", type=float, default=60.0)
    p.add_argument("--stall", type=float, default=180.0)
    p.add_argument("--lease", type=float, default=300.0)
    args = p.parse_args(argv[:split])
    dial_argv = argv[split + 1 :]
    if bool(args.connect) == bool(dial_argv):
        raise SystemExit("usage: client [options] (--connect HOST:PORT | -- <dial command ...>)")
    if args.connect:
        host, _, port_text = args.connect.rpartition(":")
        if not port_text.isdigit():
            raise SystemExit("--connect must be HOST:PORT")
        dial = tcp_dial(host, int(port_text))
    else:
        dial = command_dial(dial_argv)
    secret = read_secret(args.secret_file)
    # os.read on the raw descriptor, not sys.stdin.buffer: the reader is a daemon
    # thread still blocked in read() when the link ends, and a buffered reader's
    # lock held by a daemon thread aborts the interpreter at exit (SIGABRT).
    fd = sys.stdin.fileno()

    def read(n: int) -> bytes:
        return os.read(fd, n)

    async def run() -> int:
        # Built inside the loop: the link's StreamReader binds to the running loop.
        link = link_mod.RemoteLink(
            args.session_id,
            secret,
            dial,
            keepalive_seconds=args.keepalive,
            stall_seconds=args.stall,
            lease_seconds=args.lease,
        )
        return await bridge(link, read, _write_all)

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())

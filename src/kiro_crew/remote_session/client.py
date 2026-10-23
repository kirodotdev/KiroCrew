"""Local bridge process for a remote session: this process's stdio <-> the guest.

``--pipe HOST:PORT --token-file F`` pipes bytes through a gateway
``WorkspaceRelay``; exits 0 when the relay ends the stream, 75 when unreachable.
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
from kiro_crew.remote_session.worker import _user_id

EXIT_FINISHED = 0
EXIT_LOST = 75
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


def _write_all(chunk: bytes) -> None:
    out = sys.stdout.buffer
    out.write(chunk)
    out.flush()


async def pipe(host: str, port: int, token: bytes, stdin_read, stdout_write) -> int:
    """Stdio <-> loopback pipe to the relay: prove the runtime's token, move bytes."""
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
    """Read the relay token without unlinking it: every relaunch must authenticate;
    the relay deletes it on close."""
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
    q = argparse.ArgumentParser(prog="kiro_crew.remote_session.client")
    q.add_argument("--pipe", required=True, help="HOST:PORT of a gateway workspace relay")
    q.add_argument("--token-file", required=True)
    pargs = q.parse_args(argv)
    host, _, port_text = pargs.pipe.rpartition(":")
    if not port_text.isdigit():
        raise SystemExit("--pipe must be HOST:PORT")
    token = _read_token(pargs.token_file)
    # Raw os.read: a daemon thread still blocked on a buffered reader's lock at
    # exit aborts the interpreter (SIGABRT).
    fd = sys.stdin.fileno()
    return asyncio.run(pipe(host, int(port_text), token, lambda n: os.read(fd, n), _write_all))


if __name__ == "__main__":
    sys.exit(main())

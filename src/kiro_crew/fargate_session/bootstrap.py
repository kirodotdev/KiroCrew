"""Gateway side of the guest bootstrap protocol (``remote_session/fargate_bootstrap.py``).

Each call opens one TCP connection to the local end of the SSM port forward,
reads the bootstrap's nonce, and makes one request. Answers are checked with the
session secret, so a reply corrupted in the tunnel (or one that did not come
from the bootstrap we enrolled) is never acted on: it is refused. Only a call
that failed before its request was sent is retried, so no command runs twice.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import zlib
from pathlib import Path
from typing import Awaitable, Callable, Optional

BOOTSTRAP_PORT = 47821
_CONNECT_TIMEOUT = 20.0
_LINE_TIMEOUT = 30.0
_RUN_TIMEOUT = 180.0
_ATTEMPTS = 3
_MAX_LINE = 64 * 1024 * 1024

#: Opens one TCP connection to the bootstrap (through the forward).
Connect = Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
#: Proves a connected stream belongs to the task's own SSM forward child; raises if not.
VerifyPeer = Callable[[asyncio.StreamWriter], Awaitable[None]]


class BootstrapError(RuntimeError):
    """The guest bootstrap could not be reached or refused the request."""


class AlreadyEnrolled(BootstrapError):
    """The bootstrap answered an enrolment with a refusal: someone enrolled it first."""


def container_command() -> list[str]:
    """``python3 -c <bootstrap>`` for the task's container override.

    Compressed, because a RunTask override is capped at 8 KiB in total. The
    program carries no secret: the secret arrives later, over the forward.
    """
    source = Path(__file__).resolve().parents[1] / "remote_session" / "fargate_bootstrap.py"
    packed = base64.b64encode(zlib.compress(source.read_bytes(), 9)).decode("ascii")
    program = (
        "import base64,zlib;g={'__name__':'__main__'};"
        f"exec(zlib.decompress(base64.b64decode('{packed}')),g)"
    )
    return ["python3", "-c", program]


def _mac(secret: bytes, nonce: bytes, *parts: bytes) -> bytes:
    return hmac.new(secret, nonce + b"".join(parts), hashlib.sha256).hexdigest().encode()


class _Lines:
    """Split a stream at LF or CR (the tunnel can turn a final LF into CR)."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.buf = bytearray()

    async def line(self, timeout: float = _LINE_TIMEOUT) -> bytes:
        """The next line. A reset, stall or close is a :class:`BootstrapError`,
        so every caller sees one error type whatever the tunnel did."""

        async def read() -> bytes:
            start = 0
            while True:
                ends = [
                    i for i in (self.buf.find(b"\n", start), self.buf.find(b"\r", start)) if i >= 0
                ]
                if ends:
                    index = min(ends)
                    head = bytes(self.buf[:index])
                    del self.buf[: index + 1]
                    while self.buf[:1] in (b"\n", b"\r"):
                        del self.buf[:1]
                    return head
                if len(self.buf) > _MAX_LINE:
                    raise BootstrapError("the bootstrap sent an oversized line")
                start = len(self.buf)
                chunk = await self.reader.read(256 * 1024)
                if not chunk:
                    raise BootstrapError("the bootstrap closed the connection")
                self.buf += chunk

        try:
            return await asyncio.wait_for(read(), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise BootstrapError(f"the tunnel to the bootstrap failed: {exc!r}") from exc


async def _send(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write one request line; a broken tunnel is a :class:`BootstrapError`."""
    try:
        writer.write(data)
        await writer.drain()
    except OSError as exc:
        raise BootstrapError(f"the tunnel to the bootstrap failed: {exc!r}") from exc


async def _open(connect: Connect) -> tuple[_Lines, asyncio.StreamWriter, bytes]:
    try:
        reader, writer = await asyncio.wait_for(connect(), _CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as exc:
        raise BootstrapError(f"could not reach the task's bootstrap: {exc}") from exc
    lines = _Lines(reader)
    try:
        hello = await lines.line()
    except BaseException:
        writer.close()
        raise
    words = hello.split(b" ")
    if len(words) != 2 or words[0] != b"KCB1":
        writer.close()
        raise BootstrapError("the forwarded port is not a Kiro Crew bootstrap")
    return lines, writer, words[1]


async def enroll(
    connect: Connect,
    secret: bytes,
    *,
    verify_peer: VerifyPeer,
) -> None:
    """Enroll only after attesting the kernel owner of this exact connection.

    The greeting is not authentication. No key bytes leave the gateway until
    the forward proves that this peer belongs to its own pinned AWS child.
    """
    lines, writer, nonce = await _open(connect)
    try:
        await verify_peer(writer)
        await _send(writer, b"ENROLL " + secret.hex().encode() + b"\n")
        answer = (await lines.line()).split(b" ")
    finally:
        writer.close()
    if answer[:2] == [b"ERR", b"enrolled"]:
        raise AlreadyEnrolled("the task's bootstrap was already enrolled by someone else")
    if len(answer) != 2 or answer[0] != b"ENROLLED":
        raise BootstrapError(f"the bootstrap answered {answer[:1]!r} to the enrolment")
    if not hmac.compare_digest(answer[1], _mac(secret, nonce, b"enrolled")):
        raise BootstrapError("the bootstrap's enrolment answer did not verify")


def _request(secret: bytes, nonce: bytes, body: dict) -> bytes:
    payload = base64.b64encode(json.dumps(body).encode())
    return b"REQ " + payload + b" " + _mac(secret, nonce, payload) + b"\n"


async def run(
    connect: Connect,
    secret: bytes,
    command: str,
    stdin: bytes = b"",
    *,
    verify_peer: VerifyPeer,
) -> tuple[int, bytes, str]:
    """Run ``command`` in the task; ``(exit code, stdout, stderr tail)``.

    Only a failure before the request is sent (connect or greeting) is retried:
    once it is written the guest may have run it, so sending it again could run
    it twice. A dropped or unverifiable answer is returned as an error instead.
    No request byte leaves before ``verify_peer`` proves the connection is the
    task's own forward: a process that took the local port first never reads it.
    """
    last: Optional[BaseException] = None
    for _ in range(_ATTEMPTS):
        try:
            lines, writer, nonce = await _open(connect)
        except BootstrapError as exc:
            last = exc
            continue
        try:
            await verify_peer(writer)
            await _send(
                writer,
                _request(
                    secret,
                    nonce,
                    {"op": "run", "command": command, "stdin": base64.b64encode(stdin).decode()},
                ),
            )
            answer = (await lines.line(_RUN_TIMEOUT)).split(b" ")
        finally:
            writer.close()
        if len(answer) == 3 and answer[0] == b"RES":
            if hmac.compare_digest(answer[2], _mac(secret, nonce, b"res", answer[1])):
                result = json.loads(base64.b64decode(answer[1]))
                return (
                    int(result.get("code") if result.get("code") is not None else -1),
                    base64.b64decode(result.get("stdout") or ""),
                    str(result.get("stderr") or ""),
                )
        raise BootstrapError(f"the bootstrap answered {answer[:1]!r}")
    raise BootstrapError(f"the task's bootstrap could not be reached: {last}")


async def pipe(
    connect: Connect, secret: bytes, port: int, *, verify_peer: VerifyPeer
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A byte stream to the guest worker on ``port``, through the bootstrap.

    Like :func:`run`, nothing is sent until ``verify_peer`` attests the connection.
    """
    lines, writer, nonce = await _open(connect)
    try:
        await verify_peer(writer)
        await _send(writer, _request(secret, nonce, {"op": "pipe", "port": int(port)}))
        answer = (await lines.line()).split(b" ")
    except BaseException:
        writer.close()
        raise
    if (
        len(answer) != 2
        or answer[0] != b"OK"
        or not hmac.compare_digest(answer[1], _mac(secret, nonce, b"ok"))
    ):
        writer.close()
        raise BootstrapError(f"the bootstrap refused the stream ({answer[:1]!r})")
    # Bytes after the OK line already belong to the worker: hand them on first.
    stream = asyncio.StreamReader(limit=_MAX_LINE)
    if lines.buf:
        stream.feed_data(bytes(lines.buf))

    async def pump() -> None:
        try:
            while chunk := await lines.reader.read(256 * 1024):
                stream.feed_data(chunk)
        except (OSError, ConnectionError):
            pass
        finally:
            stream.feed_eof()

    task = asyncio.ensure_future(pump())
    # Keep the pump alive as long as the writer: closing it ends the stream.
    _original_close = writer.close

    def close() -> None:
        task.cancel()
        with contextlib.suppress(Exception):
            _original_close()

    writer.close = close  # type: ignore[method-assign]
    return stream, writer

"""Symlink-resolution worker, run as a SCRIPT in its own interpreter.

NOT PART OF THE POOL. This is the subprocess pool's FIRST CONSUMER: the pool is a
general primitive for syscall-shaped work, and this file is one op that happens to
use it. It sits in the package only because the pool is landing in the same change;
it belongs next to the sensitive-path resolver it serves and moves there when that
caller is wired up. Read the pool's own contract as the generic thing, and read this
as an example of meeting it.

Launched as ``python -S <this file>`` by :mod:`kiro_crew.subprocess_pool.executor`,
never imported.  Running it as a script rather than as ``-m
kiro_crew.subprocess_pool._child_realpath`` is the whole point: importing it as a module
would execute ``kiro_crew/__init__.py`` first and drag the gateway's dependency
graph into a process whose reason for existing is to start in about ten
milliseconds.  So this file imports STDLIB ONLY, and nothing here may ever import
``kiro_crew`` -- a violation does not fail loudly, it just makes every child start
cost seconds instead of milliseconds.

It answers exactly one kind of question: "what are the symlink-resolved spellings
of this path".  It does not execute, compile, import or evaluate anything derived
from the request; the only thing it does with the bytes it is handed is hand them
to ``os.path.realpath`` and ``pathlib.Path.resolve``.  That is the trust boundary:
the child is strictly less capable than its parent, so a malicious path cannot do
more here than it could in the thread this replaces.

Framing is a 4-byte big-endian length prefix in both directions, NEVER a newline
terminator: a POSIX filename may contain ``\\n``, and a newline-framed protocol
would let a crafted name split one request into two and shift every later answer
onto the wrong path.  Path bytes cross the wire as bytes -- ``os.fsencode`` on the
way in, ``os.fsdecode`` on the way out -- so a name that is not valid UTF-8
round-trips through surrogateescape unchanged instead of being replaced.
"""

from __future__ import annotations

import os
import pathlib
import struct
import sys

# Protocol constants.  Mirrored in ``subprocess_pool``; the two copies are pinned
# against each other by ``test_protocol_round_trips``.  They are duplicated
# rather than shared because sharing them would mean importing a ``kiro_crew``
# module here, which is the one thing this file may not do.
_LEN = struct.Struct(">I")
_REQ_HEADER = struct.Struct(">IB")  # request id, op code

OP_REALPATH_SPELLINGS = 1

STATUS_OK = 0
STATUS_ERROR = 1


def _read_exactly(stream, count: int) -> bytes | None:
    """*count* bytes from *stream*, or ``None`` at a clean end of input.

    A short read is not an error on a pipe, so this loops.  ``None`` means the
    parent closed its end, which is the child's ordinary shutdown signal.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def realpath_spellings(raw_path: bytes) -> list[bytes]:
    """The resolved spellings of *raw_path*, as filesystem bytes.

    Deliberately mirrors ``security.paths._resolved_spellings``, including WHICH
    exceptions are swallowed per spelling, because the parent substitutes this for
    that function and a different exception set would change which paths end up
    with no resolved form -- and a path with no resolved form is refused.
    """
    out: list[bytes] = []
    seen: set[bytes] = set()
    text = os.fsdecode(raw_path)

    def _add(value: str) -> None:
        encoded = os.fsencode(value)
        if encoded not in seen:
            seen.add(encoded)
            out.append(encoded)

    try:
        _add(os.path.realpath(text))
    except (OSError, ValueError):
        pass
    try:
        _add(str(pathlib.Path(text).resolve()))
    except (OSError, ValueError, RuntimeError):
        pass
    return out


def _pack_strings(values: list[bytes]) -> bytes:
    body = [_LEN.pack(len(values))]
    for value in values:
        body.append(_LEN.pack(len(value)))
        body.append(value)
    return b"".join(body)


def _handle(body: bytes) -> bytes:
    """One response body for one request body.

    Every failure becomes a STATUS_ERROR response carrying the exception's CLASS
    NAME and nothing else.  No message and no path: the request path is
    agent-supplied and the gateway's whole reason for resolving it is to keep it
    out of clear text, so it must not travel back in an error string either.

    That includes a body too short to hold a header, which is answered with request
    id 0.  The parent never issues 0, so it reads as "your frame was unreadable",
    fails closed, and discards this child -- which is right, because a parent that
    sent an unreadable header has lost protocol sync.  Parsing the header inside
    the try is what keeps a malformed frame from killing the child instead.
    """
    request_id = 0
    try:
        request_id, op = _REQ_HEADER.unpack_from(body, 0)
        payload = body[_REQ_HEADER.size :]
        if op != OP_REALPATH_SPELLINGS:
            raise ValueError("unknown op")
        answer = _pack_strings(realpath_spellings(payload))
    except BaseException as exc:  # noqa: BLE001 - a child that dies is a wedge
        name = type(exc).__name__.encode("ascii", "replace")
        return _LEN.pack(request_id)[:4] + bytes([STATUS_ERROR]) + name
    return _LEN.pack(request_id)[:4] + bytes([STATUS_OK]) + answer


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        header = _read_exactly(stdin, _LEN.size)
        if header is None:
            return 0
        (length,) = _LEN.unpack(header)
        body = _read_exactly(stdin, length)
        if body is None:
            return 0
        response = _handle(body)
        stdout.write(_LEN.pack(len(response)))
        stdout.write(response)
        stdout.flush()


if __name__ == "__main__":
    sys.exit(main())

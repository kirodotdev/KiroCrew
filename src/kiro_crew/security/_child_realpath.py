"""Symlink-resolution worker, run as a SCRIPT in its own interpreter.

NOT PART OF THE POOL. This is :mod:`kiro_crew.subprocess_pool`'s first consumer:
the pool is a general primitive for syscall-shaped work, and this file is the two
ops the sensitive-path resolver (:mod:`kiro_crew.security.paths_resolver`) asks of it.  It
lives beside that resolver because the resolver is the only thing that knows what
its answers mean.  Read the pool's own contract as the generic thing, and read this
as an example of meeting it.

Launched as ``python -S <this file>`` by :mod:`kiro_crew.subprocess_pool.executor`;
the child never imports it.  Running it as a script rather than as ``-m
kiro_crew.security._child_realpath`` is the whole point: importing it as a module
would execute ``kiro_crew/__init__.py`` first and drag the gateway's dependency
graph into a process whose reason for existing is to start in about ten
milliseconds.  So this file imports STDLIB ONLY, and nothing here may ever import
``kiro_crew`` -- a violation does not fail loudly, it just makes every child start
cost seconds instead of milliseconds.  The gateway's resolver does import ONE name
from here, :func:`realpaths_sharing_folders`, so a batch resolved in the gateway and
one resolved in the child are read by the same walk.

It answers exactly one kind of question, in two shapes: "what are the
symlink-resolved spellings of this path" (one candidate), and "what is the
``realpath`` of each of these paths" (the anchor rebuild, a few hundred paths in
one frame so the parent pays one round trip rather than one per anchor).  It does not
execute, compile, import or evaluate anything derived from the request; the only
thing it does with the bytes it is handed is hand them to ``os.path.realpath`` and
``pathlib.Path.resolve``.  That is the trust boundary: the child is strictly less
capable than its parent, so a malicious path cannot do more here than it could in
the thread this replaces.

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
OP_REALPATH_MANY = 2

STATUS_OK = 0
STATUS_ERROR = 1

# The file-type bits of ``st_mode`` and the value a symbolic link carries in them
# (``stat.S_IFMT`` and ``stat.S_IFLNK``), spelled here so this file's imports stay the
# four it starts with.
_S_IFMT = 0o170000
_S_IFLNK = 0o120000


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


def _unpack_strings(payload: bytes) -> list[bytes]:
    """Inverse of :func:`_pack_strings`; ``ValueError`` on a truncated frame."""
    (count,) = _LEN.unpack_from(payload, 0)
    offset = _LEN.size
    out: list[bytes] = []
    for _ in range(count):
        (size,) = _LEN.unpack_from(payload, offset)
        offset += _LEN.size
        if len(payload) < offset + size:
            raise ValueError("truncated value")
        out.append(payload[offset : offset + size])
        offset += size
    return out


def _realpath_sharing_folders(path: str, plain: set[str]) -> str:
    """``os.path.realpath(path)``, reading each folder on the way once per *plain*.

    ``realpath`` walks a path from the root, one ``lstat`` per folder, so a batch of
    leaves under one home reads that home's folders again for every leaf. *plain*
    holds the prefixes this batch has read and found NOT to be links, and lives for
    ONE batch, so the next path reads only below the folders it shares.

    The answer is ``realpath``'s. A path with no link on the way resolves to itself:
    each prefix that is not a link is kept, and one that is missing or unreadable is
    kept as spelled, which is what non-strict ``realpath`` does one folder at a time.
    The moment a prefix IS a link the WHOLE path goes to ``os.path.realpath``, so
    targets, chains, ``..`` inside a target and loops are resolved by the standard
    library with its own state, never composed here. A path this walk does not read
    plainly -- relative, a ``//`` root, an empty, ``.`` or ``..`` name, any path on a
    platform whose separator is not ``/`` -- goes to ``os.path.realpath`` whole too.
    """
    if os.sep != "/" or not path.startswith("/") or path.startswith("//"):
        return os.path.realpath(path)
    names = path.split(os.sep)[1:]
    if not all(name and name != "." and name != ".." for name in names):
        return os.path.realpath(path)
    prefix = ""
    for name in names:
        prefix = f"{prefix}{os.sep}{name}"
        if prefix in plain:
            continue
        try:
            is_link = os.lstat(prefix).st_mode & _S_IFMT == _S_IFLNK
        except OSError:
            is_link = False
        if is_link:
            return os.path.realpath(path)
        plain.add(prefix)
    return path


def realpaths_sharing_folders(paths: list[str]) -> list[str | None]:
    """``os.path.realpath`` of every path, in order; ``None`` where it raised.

    One batch is one view of the folders the paths share: each is read once, where
    one ``realpath`` per path reads it once per path. Nothing is kept past the batch.
    """
    plain: set[str] = set()
    out: list[str | None] = []
    for path in paths:
        try:
            out.append(_realpath_sharing_folders(path, plain))
        except (OSError, ValueError):
            out.append(None)
    return out


def realpath_many(raw_paths: list[bytes]) -> list[bytes]:
    """``os.path.realpath`` of every path, in order; ``b""`` where it raised.

    Mirrors ``security.paths._realpath_or_none`` (same swallowed exceptions) for
    the anchor rebuild, which asks about a few hundred paths per build: one frame each way
    instead of one round trip per anchor.  The empty answer is unambiguous because
    the parent absolutizes before sending, so a real answer is never empty.
    """
    plain: set[str] = set()
    out: list[bytes] = []
    for raw in raw_paths:
        try:
            out.append(os.fsencode(_realpath_sharing_folders(os.fsdecode(raw), plain)))
        except (OSError, ValueError):
            out.append(b"")
    return out


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
        if op == OP_REALPATH_SPELLINGS:
            answer = _pack_strings(realpath_spellings(payload))
        elif op == OP_REALPATH_MANY:
            answer = _pack_strings(realpath_many(_unpack_strings(payload)))
        else:
            raise ValueError("unknown op")
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

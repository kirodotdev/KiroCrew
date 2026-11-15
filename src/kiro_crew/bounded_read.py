"""Read up to a limit from a stream without asking for the limit in one request.

A capped reader takes one unit past its cap, so "at the cap" and "over it" are told
apart by the length that comes back. Spelled ``stream.read(cap + 1)``, that asks the
stream for the whole cap at once, and CPython allocates the requested size before the
first byte arrives: a 4 KB file read under a 50 MB cap costs a 50 MB allocation. That
is cheap where the allocator hands out untouched pages lazily and costs milliseconds
per read where it does not (Windows).

:func:`bounded_read` returns what ``stream.read(limit)`` returns and never requests
more than the stream is known to hold. Every capped read of a file, a descriptor or
an archive member goes through it or through :func:`bounded_read_fd`, and
``test/test_bounded_read_ratchet.py`` refuses a new ``read(cap + 1)`` spelled any
other way. An HTTP response body is the one exception that test records: the standard
library clips that request to the declared length, so the cap is not allocated.
"""

from __future__ import annotations

import os
from typing import Protocol, TypeVar

#: The size of one request once nothing more is known about the stream. Small enough
#: that the allocation is free on every platform, large enough that a file of a few
#: megabytes that grew past its recorded size is still drained in tens of calls.
READ_CHUNK_BYTES = 64 * 1024

_Data = TypeVar("_Data", bytes, str)
_Data_co = TypeVar("_Data_co", bytes, str, covariant=True)


class _Reader(Protocol[_Data_co]):
    def read(self, size: int, /) -> _Data_co: ...


def _held(source: object) -> int:
    """How much the open file behind *source* holds, or 0 when that cannot be known.

    A size is a hint for the first request and nothing else: a pipe, a socket, a
    ``/proc`` file and an archive member all report none, and a regular file may grow
    after it is sampled.
    """
    try:
        return os.fstat(source.fileno()).st_size  # type: ignore[attr-defined]
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def bounded_read(source: _Reader[_Data], limit: int) -> _Data:
    """At most *limit* bytes of *source* (characters, for a text stream).

    The result is what ``source.read(limit)`` returns from a blocking stream: every
    unit up to end of stream, or exactly *limit* of them when the stream holds more.
    A caller that passes ``cap + 1`` and refuses a result longer than ``cap`` keeps
    that refusal for a file at the cap, one unit over it, far over it, and one that
    grows while it is read.

    The first request is sized by what the file holds (one unit more, so a file that
    did not change is taken in one call), and each later one by
    :data:`READ_CHUNK_BYTES`, so growth after the size was sampled is read to the
    limit without ever asking for the limit. Reading stops at the first empty answer.

    A negative *limit* raises ``ValueError``: on ``read`` it means "everything", which
    is the request this function exists to keep callers from making.
    """
    if limit < 0:
        raise ValueError(f"bounded_read limit must not be negative, got {limit}")
    if limit == 0:
        return source.read(0)
    held = _held(source)
    request = min(limit, held + 1) if held > 0 else READ_CHUNK_BYTES
    parts: list[_Data] = []
    left = limit
    while left > 0:
        chunk = source.read(min(left, request))
        if not chunk:
            if not parts:
                return chunk
            break
        parts.append(chunk)
        left -= len(chunk)
        request = READ_CHUNK_BYTES
    # shortcut: a stream that reported no size is joined from its chunks, so its peak
    # is twice its length for a moment; grow one buffer in place if a cap ever makes
    # that matter. A file with a size is one request and is returned as read.
    return parts[0] if len(parts) == 1 else parts[0][:0].join(parts)


class _Descriptor:
    """A raw descriptor as the stream :func:`bounded_read` drains."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def read(self, size: int, /) -> bytes:
        return os.read(self._fd, size)


def bounded_read_fd(fd: int, limit: int) -> bytes:
    """At most *limit* bytes from the descriptor *fd*, read to end of file.

    ``os.read(fd, limit)`` allocates *limit* the same way a stream does, and returns
    after one system call even when the file holds more; this drains to end of file
    or to *limit*, whichever comes first.
    """
    return bounded_read(_Descriptor(fd), limit)

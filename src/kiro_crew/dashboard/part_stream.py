"""Stream one multipart part to disk without ever publishing a partial file.

Three handlers write an uploaded part to a temp file: composer uploads
(``handlers/files.py``), state import (``handlers/portability.py``, through
:func:`stream_part_off_loop`) and knowledge ingest (``handlers/knowledge_ingest.py``).
They share this one implementation because
every way of getting it wrong is a variation on the same thing: a coroutine
owning a file it can be cancelled away from.

The failure modes, because the invariant is only legible next to what it
prevents:

1. An ignored ``os.write`` short count -- a truncated file that looks complete.
2. ``CancelledError`` derives from ``BaseException``, so ``except Exception``
   lets a gateway shutdown past every cleanup.
3. Cancellation racing ``to_thread(open)`` -- the worker creates the file
   *after* cleanup has run.
4. A cleanup callback registered before the shielded await, so the descriptor
   is closed before the coroutine resumes: ``EBADF`` on every upload.
5. ``close``/``unlink`` running on the serving loop; and a double close by
   descriptor *number* hitting a number the kernel has reassigned.
6. Cleanup keyed on ``task.result()`` cannot run for a directly cancelled open
   task -- the one case that needs it.
7. Cleanup keyed on the path instead races the worker the other way: the
   callback fires on cancellation and unlinks *before* the worker creates.

**The invariant.** A cancellable owner cannot both offload its cleanup and
guarantee it: offloading needs an await or a callback, and each introduces an
ordering the owner does not control -- which is precisely how (3), (6) and (7)
arise, in both directions. So ownership lives in a **synchronous context
manager**: ``__enter__`` creates, ``__exit__`` discards unless the body
committed. ``with`` guarantees ``__exit__`` on every exit including
``CancelledError``, and because no ``await`` sits between the create and the
cleanup's registration, nothing can race it and no result has to be fetched from
a task that may never produce one.

The deliberate cost, stated plainly rather than hidden: ``__enter__``'s single
``os.open`` and ``__exit__``'s single ``close``+``unlink`` run on the event loop.
That is bounded work on one freshly-named local path -- microseconds -- and it
buys an ordering guarantee that no awaited off-loop step provides. The *bulk*
cleanup of (5) -- a request unlinking up to 20 published paths, a 512 MB video
among them -- still runs in a worker, at the caller's level. Writes, the commit, and the sniff all run in workers too: only
the create/discard of one temp is on the loop, and only because correctness
requires it.

**Off the loop entirely.** :func:`stream_part_off_loop` serves a destination on a
filesystem that may stall (a data home on NFS), where even that one on-loop create
would freeze the gateway. It does not offload the coroutine's cleanup; it takes
ownership away from the coroutine. ONE dedicated worker thread runs the create,
every write, the close and the discard, in the order they were submitted.
``Executor.submit`` is synchronous, so the discard is queued with no ``await``
before it on every failure, and that queue runs it after whatever create or write
was in flight. (3), (6) and (7) each need a second thread or a result to wait on,
and there is neither. Nothing is renamed there: its one caller owns a private
per-request directory and uses the file only after the call has returned.

Two further properties :func:`stream_part_to_file`'s callers rely on:

* **Atomic publish.** ``dest`` is created solely by ``os.replace`` from a
  fully-written temp, so no failure path can leave anything at ``dest`` that
  looks complete. A reader either sees no file or sees all of it.
* **Sniff before disk.** The signature check runs on the first
  ``SNIFF_BYTES`` while they are still buffered in memory, so content that
  fails it never reaches the filesystem at all (CWE-434).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import IO, Callable

from aiohttp.multipart import BodyPartReader

from kiro_crew.atomic_write import replace_with_retry

#: Bytes buffered before the signature predicate is consulted. Every container
#: signature the callers check lives well inside this window (the MP4 family's
#: ``ftyp`` box ends at 12, EBML's magic is 4, PNG's is 8).
SNIFF_BYTES = 16

#: Read size for the streaming loop, and deliberately NOT a caller-tunable
#: parameter. It bounds the one blocking call this module leaves on the event
#: loop: ``__exit__``'s ``close()`` contends for the ``BufferedWriter``'s own
#: lock, so if cancellation arrives while a worker is inside
#: ``to_thread(sink.write, chunk)``, the on-loop close waits for that write to
#: finish. The wait is therefore one chunk of I/O, which is why the number is
#: small enough to be uninteresting (64 KB is well under a single disk write's
#: latency floor) rather than merely "fast on my machine" -- at the 1 MB this
#: started as, a slow or network filesystem made that wait a real stall.
#:
#: It is a module constant rather than an argument on purpose: a value a caller
#: can raise is not a bound, and the guarantee here is only as good as its
#: worst caller. Raising it means re-arguing the trade above.
CHUNK_BYTES = 64 * 1024

#: Appended to the destination name for the file a part streams into before it is
#: committed; a caller sizing names to a file-name limit must leave room for it.
TEMP_SUFFIX = ".part"


class PartTooLarge(Exception):
    """The part exceeded the caller's byte ceiling. Nothing was published."""

    def __init__(self, total: int, limit: int) -> None:
        super().__init__(f"part exceeded {limit} bytes (read {total})")
        self.total = total
        self.limit = limit


class PartContentMismatch(Exception):
    """The leading bytes failed the caller's signature predicate."""

    def __init__(self, head: bytes) -> None:
        super().__init__("part content does not match its claimed type")
        self.head = head


class _TempSink:
    """Owns one temp file for the duration of a ``with`` block.

    Synchronous on purpose -- see this module's docstring. ``__exit__`` is the
    only cleanup path and it cannot be skipped by cancellation or raced by a
    worker, because the file already exists when the block is entered and the
    sink object (not a descriptor number) is what gets closed.
    """

    def __init__(self, dest: Path) -> None:
        self.dest = dest
        self.tmp = dest.with_name(dest.name + TEMP_SUFFIX)
        self.sink: IO[bytes] | None = None
        self.committed = False

    def __enter__(self) -> _TempSink:
        # O_EXCL: never adopt a file this request did not create. 0o600 because
        # an upload may carry anything and the directory is shared. O_BINARY is
        # 0 off Windows and load-bearing on it: without it the CRT translates
        # newlines, so any payload containing 0x0A commits bytes that differ
        # from what was uploaded -- a silently corrupted video or archive.
        fd = os.open(
            str(self.tmp),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            self.sink = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                self.tmp.unlink()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.committed:
            return
        # close() is idempotent and owns its own descriptor, so this can never
        # close a number the kernel has reassigned to someone else.
        if self.sink is not None:
            with contextlib.suppress(OSError):
                self.sink.close()
        with contextlib.suppress(OSError):
            self.tmp.unlink()

    def write(self, data: bytes) -> None:
        """Append *data*. Call from a worker; BufferedWriter loops short writes."""
        assert self.sink is not None  # inside the with-block by construction
        self.sink.write(data)

    def commit(self) -> None:
        """Close and publish atomically. Call from a worker.

        Close and rename happen in this one call so no ``await`` can land
        between them, and `committed` is set only after the rename succeeds --
        so a failed publish still leaves ``__exit__`` responsible for the temp.
        """
        assert self.sink is not None
        self.sink.close()
        replace_with_retry(self.tmp, self.dest)
        self.committed = True


async def stream_part_to_file(
    part: BodyPartReader,
    dest: Path,
    *,
    max_bytes: int,
    accepts: Callable[[bytes], bool] | None = None,
) -> int:
    """Stream *part* to *dest*, or raise having published nothing.

    Returns the byte count written. Raises :class:`PartTooLarge` when the part
    exceeds *max_bytes*, :class:`PartContentMismatch` when *accepts* rejects the
    leading bytes, and propagates anything else (including
    ``asyncio.CancelledError``) after cleaning up.

    *accepts* is consulted once, on the first ``SNIFF_BYTES`` -- which are held
    in memory until it has answered, so rejected content never reaches disk.
    A part shorter than that window is judged on what arrived, since every
    signature the callers check is shorter than the window.
    """
    total = 0
    head = bytearray()
    verified = accepts is None

    with _TempSink(dest) as sink:
        while True:
            chunk = await part.read_chunk(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PartTooLarge(total, max_bytes)
            if verified:
                await asyncio.to_thread(sink.write, chunk)
                continue
            head.extend(chunk)
            if len(head) < SNIFF_BYTES:
                continue  # too few bytes to judge yet -- keep holding them
            assert accepts is not None
            if not accepts(bytes(head)):
                raise PartContentMismatch(bytes(head))
            verified = True
            await asyncio.to_thread(sink.write, bytes(head))
            head.clear()
        if not verified:
            # The part ended inside the sniff window. Judge what arrived.
            assert accepts is not None
            if not accepts(bytes(head)):
                raise PartContentMismatch(bytes(head))
            await asyncio.to_thread(sink.write, bytes(head))
        await asyncio.to_thread(sink.commit)
    return total


async def stream_part_off_loop(
    part: BodyPartReader,
    open_sink: Callable[[], IO[bytes]],
    discard: Callable[[], None],
    *,
    max_bytes: int,
) -> int:
    """Stream *part* into the file *open_sink* creates, with no filesystem call on the loop.

    Returns the byte count written. *open_sink* creates the destination and returns
    it open for writing; *discard* removes whatever *open_sink* created and must be a
    no-op when it never ran. All three, every write and the close run on one worker
    thread in submission order (this module's docstring says why that is safe).

    On any failure -- :class:`PartTooLarge`, a failed read or write, a
    ``CancelledError`` -- the file is closed and *discard* has run before this
    raises. A second cancellation abandons only that wait: the discard is already
    queued and still runs.
    """
    owner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="part-stream-owner")
    sinks: list[IO[bytes]] = []
    closed = False
    try:
        await asyncio.wrap_future(owner.submit(lambda: sinks.append(open_sink())))
        total = 0
        while chunk := await part.read_chunk(CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise PartTooLarge(total, max_bytes)
            await asyncio.wrap_future(owner.submit(sinks[0].write, chunk))
        await asyncio.wrap_future(owner.submit(sinks[0].close))
        closed = True
        return total
    finally:
        if not closed:
            discarded = owner.submit(_close_and_discard, sinks, discard)
        owner.shutdown(wait=False)
        if not closed:
            await asyncio.shield(asyncio.wrap_future(discarded))


def _close_and_discard(sinks: list[IO[bytes]], discard: Callable[[], None]) -> None:
    """Close the sink if one was opened, then discard. Runs on the owner thread."""
    for sink in sinks:
        with contextlib.suppress(OSError):
            sink.close()
    discard()


__all__ = [
    "CHUNK_BYTES",
    "SNIFF_BYTES",
    "PartContentMismatch",
    "PartTooLarge",
    "stream_part_off_loop",
    "stream_part_to_file",
]

"""Stream one multipart part to disk without ever publishing a partial file.

Three handlers write an uploaded part to a temp file: composer uploads
(``handlers/files.py``), state import (``handlers/portability.py``) and knowledge
ingest (``handlers/knowledge.py``). Each had grown its own copy, and the copy in
``files.py`` took **seven** blocking review findings in seven rounds before the
shape below settled. Every one of them was a variation on the same thing: a
coroutine owning a file it can be cancelled away from.

The ledger, because the invariant is only legible next to what it prevents:

1. ``os.write`` short count ignored -- a truncated file that looks complete.
2. ``CancelledError`` derives from ``BaseException``, so ``except Exception``
   let a gateway shutdown past every cleanup.
3. Cancellation raced ``to_thread(open)`` -- the worker created the file *after*
   cleanup had run.
4. A fix for (3) registered its callback before the shielded await, so the
   descriptor was closed before the coroutine resumed: ``EBADF`` on every upload.
5. Cleanup ran ``close``/``unlink`` on the serving loop; and a double close by
   descriptor *number* could hit a number the kernel had reassigned.
6. Cleanup keyed on ``task.result()`` could not run for a directly cancelled
   open task -- the one case that needed it.
7. Cleanup keyed on the path instead raced the worker the other way: the
   callback fired on cancellation and unlinked *before* the worker created.

**The invariant.** A cancellable owner cannot both offload its cleanup and
guarantee it: offloading needs an await or a callback, and each introduces an
ordering the owner does not control -- which is precisely how (3), (6) and (7)
happened, in both directions. So ownership lives in a **synchronous context
manager**: ``__enter__`` creates, ``__exit__`` discards unless the body
committed. ``with`` guarantees ``__exit__`` on every exit including
``CancelledError``, and because no ``await`` sits between the create and the
cleanup's registration, nothing can race it and no result has to be fetched from
a task that may never produce one.

The deliberate cost, stated plainly rather than hidden: ``__enter__``'s single
``os.open`` and ``__exit__``'s single ``close``+``unlink`` run on the event loop.
That is bounded work on one freshly-named local path -- microseconds -- and it
buys an ordering guarantee that no off-loop arrangement provides. The *bulk*
cleanup that finding (5) was actually about (a request unlinking up to 20
published paths, a 512 MB video among them) still runs in a worker, at the
caller's level. Writes, the commit, and the sniff all run in workers too: only
the create/discard of one temp is on the loop, and only because correctness
requires it.

Two further properties the callers rely on:

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
from pathlib import Path
from types import TracebackType
from typing import IO, Callable

from aiohttp.multipart import BodyPartReader

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
        self.tmp = dest.with_name(dest.name + ".part")
        self.sink: IO[bytes] | None = None
        self.committed = False

    def __enter__(self) -> _TempSink:
        # O_EXCL: never adopt a file this request did not create. 0o600 because
        # an upload may carry anything and the directory is shared. O_BINARY is
        # 0 off Windows and load-bearing on it: without it the CRT translates
        # newlines, so any payload containing 0x0A commits bytes that differ
        # from what was uploaded -- a silently corrupted video or archive. This
        # flag was on the pre-extraction opener and got dropped in the move;
        # `test_open_flags_include_o_binary` now pins it.
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
        os.replace(self.tmp, self.dest)
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


__all__ = [
    "CHUNK_BYTES",
    "SNIFF_BYTES",
    "PartContentMismatch",
    "PartTooLarge",
    "stream_part_to_file",
]

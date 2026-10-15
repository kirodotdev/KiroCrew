"""Admitting an arriving bundle and reading its body to a parsed document.

The receiving side's bounds: how many uploads and expansions may run at once, the
memory an import reserves before it parses, the disk room a streamed body and its gunzip
need, and the body read that streams the request to disk, sniffs gzip, measures the
document and loads it. Every value an admission rebinds at run time lives here, beside
the only code that reads and writes it.

Owner of :mod:`kiro_crew.dashboard.session_transfer`, which is its only import path:
callers and tests reach every name here as ``session_transfer.<name>``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.session_transfer_staging import (
    _CHUNK_BYTES,
    _import_tmp_dir,
    _rm_import_temps,
)
from kiro_crew.instances.constants import (
    DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS,
    SESSION_IMPORT_MEMORY_WAIT_SECS,
)

#: How many bodies may be arriving at once, and how many may be waiting to.
#:
#: There is no per-body size ceiling any more (arrival streams to disk, so a large
#: session is copied rather than refused), so this permit is what keeps the SUM
#: bounded: an arriving bundle is resident — first as the parsed document, then
#: through redaction and persistence — for as long as its arrival takes, and N
#: unbounded arrivals at once are what exhaust the host, not any single one. So a
#: permit covers the whole arrival: see :func:`_read_bundle_body`. Two in flight
#: bounds resident work to roughly twice one bundle; a small queue absorbs
#: ordinary bursts (a person installing several files) while anything past it is
#: refused immediately rather than parked, because a queue that grows without
#: limit is the same failure with a delay in front of it. Streaming to disk bounds
#: the ARRIVAL itself (bytes never accumulate in memory); this permit bounds how
#: many parsed bundles are resident at once.
_MAX_CONCURRENT_EXPANSIONS = 2


_MAX_QUEUED_EXPANSIONS = 4


#: Guards the two counters below. A plain lock rather than a semaphore because the
#: WAITING count has to be testable before waiting, which a semaphore does not
#: expose. Loop-bound: created lazily so importing this module binds no loop.
_expansion_lock: asyncio.Lock | None = None


_expansion_slots: asyncio.Semaphore | None = None


_expansion_waiting = 0


#: The least a parse costs in memory, as a multiple of the document's size on
#: disk: the decoded text and the strings built from it are resident together
#: until the parse returns (measured at about 2.05 for ASCII text).
#: :func:`_measure_document` raises it for text that decodes wider than its
#: bytes; see :func:`_parse_factor`.
_PARSE_MEMORY_FACTOR = 3


#: A byte that begins a four-byte UTF-8 sequence: a character past the BMP,
#: which CPython stores as four bytes per character for the WHOLE string.
_NON_BMP_LEAD = re.compile(rb"[\xf0-\xf4]")


#: A byte that begins a character at or past U+0100, stored as two bytes per
#: character (the lone surrogates ``surrogatepass`` admits included).
_WIDE_LEAD = re.compile(rb"[\xc4-\xef]")


#: A ``\uXXXX`` escape of a surrogate: text that is ASCII on disk whose parsed
#: string holds a character past the BMP.
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89abAB]")


#: Any ``\uXXXX`` escape past Latin-1 (an overcount only reserves more).
_WIDE_ESCAPE = re.compile(rb"\\u(?!00)")


#: A complete JSON string once its escapes are removed, for counting the
#: structural marks that lie outside strings.
_JSON_STRING = re.compile(rb'"[^"]*"')


#: What each message costs on top of its text once parsed: the raw object, the
#: validated one and the row built from it are dictionaries resident together.
#: Measured at about 620 bytes for a one-character message against 45 on disk,
#: so a session of many short messages would outgrow the factor above alone;
#: this rounds the measurement up.
_PER_MESSAGE_BYTES = 1024


#: The key every message carries, counted to size the allowance above. In JSON
#: text a quote inside a string is escaped, so these bytes appear only where a
#: message's ``role`` key (or the rare bare "role" value) does; an overcount
#: only reserves more.
_MESSAGE_MARKER = b'"role"'


#: What each JSON value costs once parsed, whatever the document's shape. Every
#: value but the outermost follows one of :data:`_STRUCTURAL_MARKS` (an array's
#: first element its ``[``, a later one its ``,``, an object's key its ``{`` or
#: ``,``, its value the ``:``), so their count bounds the objects a parse builds.
#: Measured at up to 72 bytes a mark (a dict of one float, an empty object in a
#: list is 36); this rounds up. Only marks outside strings are counted, so text
#: that happens to contain ``{`` or ``,`` reserves nothing for them.
_PER_VALUE_BYTES = 96


_STRUCTURAL_MARKS = (b"{", b"[", b",", b":")


#: Free space an arrival leaves on the crew home's volume. The body and its
#: decompressed copy stream to disk with no size ceiling, so this is what stops a
#: small gzip that expands without end from filling the volume.
_DISK_HEADROOM_BYTES = 1024 * 1024 * 1024


#: How often, in chunks written, the free space is read again: on the first
#: chunk, so a small body is checked too, then every 4 MiB.
_DISK_CHECK_EVERY_CHUNKS = 16


#: Memory an admitted import leaves free for everything else on the host.
_MEMORY_HEADROOM_BYTES = 512 * 1024 * 1024


#: How often an arrival waiting for memory looks again. Slow on purpose: a large
#: import is allowed to be late, and a probe per second per waiter is not free.
_MEMORY_POLL_SECS = 2.0


#: Bytes reserved by admitted imports that have not finished. Loop-bound, and
#: read and written with no await in between, so no lock is needed.
_memory_reserved = 0


#: gzip's own framing magic (RFC 1952 §2.3.1). The body format is sniffed from
#: these two bytes and NOT from ``Content-Type``: the export endpoint answers
#: ``application/gzip``, a browser upload of that same file may send
#: ``application/octet-stream`` or nothing at all, and the tunnel's
#: server-to-server caller sends ``application/json``. Sniffing the bytes keeps
#: all three working without asking any caller to relabel what it already sends.
_GZIP_MAGIC = b"\x1f\x8b"


def _reject(reason: str, code: str) -> web.Response:
    """Return a 400 validation failure carrying a machine-readable ``code``.

    Every non-2xx body here needs ``code``: ``test_error_code_contract.py``
    ratchets on it, and a coded body is what lets the sending instance
    distinguish "peer is too old to understand this bundle" from "bundle was
    malformed" without parsing prose.

    The status is a literal 400 rather than a parameter on purpose — the
    contract gate reads the status statically, and a variable one lands in its
    "cannot decide" bucket. The single non-400 rejection (the slot cap) spells
    its own status out at the call site.
    """
    return web.json_response({"error": reason, "code": code}, status=400)


class _DiskFull(Exception):
    """Writing more of the arrival would leave its volume with too little free space."""


#: How many request bodies may be streaming to disk at once. Each holds a
#: staging-file descriptor for as long as its sender keeps making progress, and
#: the expansion permit is taken only after the body is on disk, so this is the
#: bound on descriptors held by arrivals. Past it an upload is refused before
#: any file is opened.
_MAX_CONCURRENT_UPLOADS = 8


_uploads_in_flight = 0


class _UploadsBusy(Exception):
    """Too many request bodies are already streaming to disk."""


@contextlib.contextmanager
def _upload_admission() -> Any:
    """Admit one body upload, or refuse. **Loop-bound.**

    No await separates the check from the increment, so the count is exact on
    the one loop that runs imports.

    Raises:
        _UploadsBusy: when :data:`_MAX_CONCURRENT_UPLOADS` bodies are in flight.
    """
    global _uploads_in_flight
    if _uploads_in_flight >= _MAX_CONCURRENT_UPLOADS:
        raise _UploadsBusy(_uploads_in_flight)
    _uploads_in_flight += 1
    try:
        yield
    finally:
        _uploads_in_flight -= 1


class _ExpansionBusy(Exception):
    """Too many bodies are already arriving or waiting to arrive."""


@contextlib.asynccontextmanager
async def _expansion_admission() -> Any:
    """Admit one arrival, or refuse. **Loop-bound.**

    Bounds how many bundles are resident at once to
    :data:`_MAX_CONCURRENT_EXPANSIONS`. A caller past the queue limit is refused
    straight away rather than parked, so the waiting set cannot itself become the
    allocation.

    Raises:
        _ExpansionBusy: when the queue is full.
    """
    global _expansion_lock, _expansion_slots, _expansion_waiting
    if _expansion_lock is None:
        _expansion_lock = asyncio.Lock()
    if _expansion_slots is None:
        _expansion_slots = asyncio.Semaphore(_MAX_CONCURRENT_EXPANSIONS)

    async with _expansion_lock:
        if _expansion_waiting >= _MAX_QUEUED_EXPANSIONS:
            raise _ExpansionBusy(_expansion_waiting)
        _expansion_waiting += 1
    try:
        await _expansion_slots.acquire()
    finally:
        async with _expansion_lock:
            _expansion_waiting -= 1
    try:
        yield
    finally:
        _expansion_slots.release()


class _NeverFits(Exception):
    """The document needs more memory to parse than the host has in total."""


class _MemoryWaitTimedOut(Exception):
    """The host did not free enough memory within the wait budget."""


def _cgroup_memory_bounds() -> tuple[int | None, int | None]:
    """The tightest memory limit on this process's own cgroup ancestry and the
    headroom left under it, in bytes; ``None`` for either when no limit applies
    or it cannot be read. **Blocking IO (small /proc and /sys reads).**

    A gateway in a memory-limited unit or container (the dev-fleet pod unit sets
    ``MemoryMax``) is killed at that limit however much the host has free, so
    the host-wide readings alone would admit what the gateway cannot hold. The
    walk and the headroom reading are the ones subagent sizing uses, so both
    budgets see the same ceiling.
    """
    if not platform_compat.IS_LINUX:
        return None, None
    from kiro_crew import subagent

    limit: int | None = None
    for leaf, mount, v2 in subagent._cgroup_memory_roots():
        name = "memory.max" if v2 else "memory.limit_in_bytes"
        directory = leaf
        while True:
            value = subagent._read_int_file(str(directory / name))
            if value is not None and 0 <= value < subagent._CGROUP_UNLIMITED:
                limit = value if limit is None else min(limit, value)
            if directory == mount:
                break
            directory = directory.parent
    headroom_gb = subagent._container_cgroup_available_gb()
    headroom = int(headroom_gb * 1024**3) if headroom_gb >= 0 else None
    return limit, headroom


def _gateway_memory() -> tuple[int | None, int | None]:
    """Total and available memory, in bytes, that this gateway can actually use:
    the host's readings clamped to its cgroup's limit and headroom.
    **Blocking IO.** ``None`` for a reading nothing can supply, kept apart from
    ``0``: a cgroup with no headroom left reads ``0`` available, which must
    wait, not be mistaken for an unreadable host."""
    mib = 1024 * 1024
    limit, headroom = _cgroup_memory_bounds()
    return (
        _tighter(platform_compat.host_total_mib() * mib or None, limit),
        _tighter(platform_compat.host_available_mib() * mib or None, headroom),
    )


def _tighter(a: int | None, b: int | None) -> int | None:
    """The smaller of two readings, either of which may be missing."""
    readings = [x for x in (a, b) if x is not None]
    return min(readings) if readings else None


@contextlib.asynccontextmanager
async def _memory_admission(
    doc_bytes: int, messages: int = 0, values: int = 0, factor: int = _PARSE_MEMORY_FACTOR
) -> Any:
    """Reserve the memory a *doc_bytes* document of *messages* messages and at
    most *values* JSON values needs to parse. **Loop-bound.**

    The estimate is the text (*factor* times its size, from
    :func:`_parse_factor`), plus
    an allowance for every value the parse can build (:data:`_PER_VALUE_BYTES`),
    plus one for the validated copy and row each message gets
    (:data:`_PER_MESSAGE_BYTES`). The value term is what holds for a document
    of any shape: an array of empty objects costs about 24 times its text.

    The size ceiling is gone, so what keeps a large import from exhausting the
    gateway is this: an arrival is admitted to parse only once the host has the
    memory for it, net of what other admitted imports have reserved and of a
    fixed headroom. Short of that it WAITS, re-reading every
    :data:`_MEMORY_POLL_SECS`, rather than being refused, so a large session
    imports late instead of not at all. Two large imports therefore run one
    after the other rather than side by side.

    Refused only when waiting cannot help: the estimate exceeds the host's total
    memory (:class:`_NeverFits`), or nothing was freed within
    ``SESSION_IMPORT_MEMORY_WAIT_SECS`` (:class:`_MemoryWaitTimedOut`, which the
    caller answers as retryable). A host whose memory cannot be read is not
    gated at all, the same fail-open contract ``host_available_mib`` documents.

    The reservation is held until the caller's stack exits, because the parsed
    document stays resident through validation, redaction and persistence.
    """
    global _memory_reserved
    need = doc_bytes * factor + values * _PER_VALUE_BYTES + messages * _PER_MESSAGE_BYTES
    total, available = await asyncio.to_thread(_gateway_memory)
    if total is not None and need > total - _MEMORY_HEADROOM_BYTES:
        raise _NeverFits(need, total)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SESSION_IMPORT_MEMORY_WAIT_SECS
    while True:
        if available is None:
            break  # unreadable host: fail open
        if available - _memory_reserved - _MEMORY_HEADROOM_BYTES >= need:
            break
        if loop.time() >= deadline:
            raise _MemoryWaitTimedOut(need, available)
        await asyncio.sleep(_MEMORY_POLL_SECS)
        available = (await asyncio.to_thread(_gateway_memory))[1]
    # No await between the check above and this increment, so two waiters that
    # read the same free memory cannot both claim it.
    _memory_reserved += need
    try:
        yield
    finally:
        _memory_reserved -= need


def _gunzip_file(src: Path, dst: Path) -> int:
    """Stream-decompress the gzip file *src* to *dst*. **Blocking IO+CPU, thread-safe.**

    Reads compressed input and writes decompressed output a chunk at a time, so
    neither side is ever fully resident: memory is bounded by one chunk and the
    arrival is bounded by disk. There is no size ceiling; what stops a bomb is the
    volume's free space, re-read as the output grows (:func:`_require_disk_room`).

    ``wbits=16 + MAX_WBITS`` selects gzip framing (a bare zlib stream is not
    accepted — the file this reads is what the export endpoint wrote).

    Returns the decompressed byte count.

    Raises:
        _DiskFull: when the volume is down to its headroom.
        zlib.error: if *src* is not a well-formed, single-member gzip stream.
    """
    dobj = zlib.decompressobj(16 + zlib.MAX_WBITS)
    produced = 0
    written_chunks = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while not dobj.eof:
            block = fin.read(_CHUNK_BYTES)
            if not block:
                # Input exhausted before the gzip trailer: a truncated stream.
                break
            data = block
            while data and not dobj.eof:
                out = dobj.decompress(data, _CHUNK_BYTES)
                if out:
                    produced += len(out)
                    written_chunks += 1
                    if written_chunks == 1 or written_chunks % _DISK_CHECK_EVERY_CHUNKS == 0:
                        _require_disk_room(dst)
                    fout.write(out)
                # Whatever the output limit left unprocessed this call; empty when
                # the block was fully consumed, so the outer loop reads more input.
                data = dobj.unconsumed_tail
        # A member that ends exactly on a read boundary leaves ``unused_data``
        # empty with the rest of the file still unread, so look for it too.
        trailing = dobj.eof and not dobj.unused_data and bool(fin.read(1))
    if not dobj.eof:
        raise zlib.error("incomplete gzip stream")
    if dobj.unused_data or trailing:
        # A second gzip member. The export endpoint writes exactly one, so a
        # concatenated file is not something this produced; refusing beats
        # decoding the first member and silently dropping the rest.
        raise zlib.error("trailing data after the gzip stream")
    return produced


def _parse_factor(text_width: int, string_width: int) -> int:
    """The parse's memory cost as a multiple of the document's size on disk.

    The decoded text costs *text_width* bytes a byte on disk (CPython sizes a
    whole string by its widest character), and the strings the parse builds can
    together cost *string_width* a byte, twice over while an escaped string is
    assembled beside its result. Measured peaks stay under this on every shape
    tried: 2.05 for ASCII, 5.05 for ASCII carrying one raw emoji, 7.25 for one
    large escaped string with one emoji, 8.0 for one large raw one.
    """
    return max(_PARSE_MEMORY_FACTOR, text_width + 2 * string_width)


def _measure_document(path: Path) -> tuple[int, int, int, int]:
    """The size of the document at *path*, how many messages it holds, how many
    JSON values it can hold at most, and what parsing it costs as a multiple of
    its size (:func:`_parse_factor`). **Blocking IO, thread-safe.**

    Read a chunk at a time so the document is never resident for it. Messages
    are :data:`_MESSAGE_MARKER` occurrences; each chunk keeps a short tail of the
    previous one so a marker or escape split across a boundary is still seen,
    once. Values are :data:`_STRUCTURAL_MARKS` outside strings: escapes are
    dropped first so every remaining quote delimits a string, string contents
    are cut out, and a string still open at the end of a chunk carries over.
    """
    size = path.stat().st_size
    messages = 0
    values = 0
    text_width = 1
    string_width = 1
    keep = max(len(_MESSAGE_MARKER), 6) - 1
    tail = b""
    in_string = False
    pending = b""  # trailing backslashes held so an escape pair is never split
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_BYTES)
            if not chunk:
                break
            window = tail + chunk
            messages += window.count(_MESSAGE_MARKER)
            if text_width < 4:
                if _NON_BMP_LEAD.search(chunk):
                    text_width = 4
                elif text_width < 2 and _WIDE_LEAD.search(chunk):
                    text_width = 2
            if string_width < 4:
                if _SURROGATE_ESCAPE.search(window):
                    string_width = 4
                elif string_width < 2 and _WIDE_ESCAPE.search(window):
                    string_width = 2
            tail = window[-keep:]

            stream = pending + chunk
            body = stream.rstrip(b"\\")
            # A trailing run pairs up into complete ``\\`` escapes from its
            # start, so only an odd last backslash waits for the next chunk and
            # the carry stays one byte however long the run.
            pending = b"\\" * ((len(stream) - len(body)) % 2)
            # Escapes occur only inside strings, so removing them anywhere is
            # safe; ``\\`` goes first so ``\\"`` leaves its closing quote.
            body = body.replace(b"\\\\", b"").replace(b'\\"', b"")
            if in_string:
                end = body.find(b'"')
                if end < 0:
                    continue
                body = body[end + 1 :]
                in_string = False
            body = _JSON_STRING.sub(b"", body)
            opened = body.find(b'"')
            if opened >= 0:
                body = body[:opened]
                in_string = True
            values += sum(body.count(mark) for mark in _STRUCTURAL_MARKS)
    string_width = max(string_width, text_width)
    return size, messages, values, _parse_factor(text_width, string_width)


def _load_json_file(path: Path) -> Any:
    """Parse the JSON document at *path*. **Blocking IO+CPU, thread-safe.**

    Decodes to text as it reads and parses that, rather than reading the bytes and
    handing them to ``json.loads``, which decodes a second full copy internally:
    the raw bytes are never resident beside the decoded text and the document,
    which is a whole body's worth less peak memory on a large import. The decode
    is ``surrogatepass``, the same one ``json.loads`` applies to bytes, so a lone
    surrogate an exported transcript can legitimately carry (``\\ud800``)
    round-trips as the same character instead of raising.
    """
    with open(path, encoding="utf-8", errors="surrogatepass") as f:
        return json.loads(f.read())


def _new_import_temp(suffix: str) -> Path:
    """Create an empty temp file for an arrival and return its path. **Blocking.**

    Directory creation, ``mkstemp`` and the close are all filesystem calls a slow
    volume can stall, so the caller runs this in a worker.
    """
    fd, name = tempfile.mkstemp(dir=str(_import_tmp_dir()), suffix=suffix)
    os.close(fd)
    return Path(name)


def _free_bytes(path: Path) -> int | None:
    """Free bytes on the volume holding *path*, or ``None`` if unreadable. **Blocking.**"""
    try:
        return shutil.disk_usage(path.parent).free
    except OSError:
        return None


def _require_disk_room(path: Path) -> None:
    """Raise :class:`_DiskFull` if the volume holding *path* has less than
    :data:`_DISK_HEADROOM_BYTES` free. **Blocking.**

    With no size ceiling, a small gzip body can expand to anything, so the write
    itself is what has to stop before the crew home's volume fills. A volume
    whose free space cannot be read is not gated.
    """
    free = _free_bytes(path)
    if free is not None and free < _DISK_HEADROOM_BYTES:
        raise _DiskFull(free)


#: Bytes arriving uploads have been cleared to write and are still writing.
#: Loop-bound: read and updated with no await between the check and the update.
_disk_inflight = 0


async def _reserve_disk(path: Path, nbytes: int) -> None:
    """Clear *nbytes* for writing to *path*'s volume, or raise :class:`_DiskFull`.

    Uploads stream outside every concurrency permit, so each one's write counts
    against the free space the others have already been cleared to use: the
    headroom holds however many arrive at once. The caller releases the
    reservation with :func:`_release_disk` once the write returns, after which
    the volume's own free space reflects it.
    """
    global _disk_inflight
    free = await asyncio.to_thread(_free_bytes, path)
    if free is not None and free - _disk_inflight - nbytes < _DISK_HEADROOM_BYTES:
        raise _DiskFull(free)
    _disk_inflight += nbytes


def _release_disk(nbytes: int) -> None:
    global _disk_inflight
    _disk_inflight -= nbytes


async def _write_reserved(fout: Any, data: bytes, dst: Path) -> asyncio.Future[Any]:
    """Start writing *data* under a disk reservation; returns the write future.

    The reservation is released by the write's own completion, so a cancellation
    that abandons the await cannot drop it while the worker is still writing.
    """
    await _reserve_disk(dst, len(data))
    write = asyncio.ensure_future(asyncio.to_thread(fout.write, data))
    write.add_done_callback(lambda _f: _release_disk(len(data)))
    return write


def _disk_full_response() -> web.Response:
    """``507`` for an arrival its volume has no room for. Retryable once space is
    freed; nothing was imported."""
    return web.json_response(
        {
            "error": "not enough free disk space to import this session",
            "code": "transfer_disk_full",
        },
        status=507,
    )


async def _stalling_chunks(
    request: web.Request, stall: asyncio.Timeout, loop: asyncio.AbstractEventLoop
) -> AsyncIterator[bytes]:
    """Yield the request body in chunks, moving *stall* ahead on each one.

    Each chunk that arrives is progress, so the deadline is always
    :data:`DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS` past the last one; a sender
    that goes quiet lets it lapse and ``TimeoutError`` ends the read.
    """
    async for chunk in request.content.iter_chunked(_CHUNK_BYTES):
        stall.reschedule(loop.time() + DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS)
        yield chunk


async def _stream_request_to_file(request: web.Request, dst: Path) -> tuple[bool, int]:
    """Stream the request body to *dst* in chunks. Returns ``(is_gzip, bytes_written)``.

    Reads ``request.content`` (the raw ``StreamReader``) and NEVER
    ``request.read()`` / ``.post()`` / ``.json()``: those buffer the whole body and
    are the calls aiohttp enforces ``client_max_size`` in, so reading the stream
    directly is what lets a session of any size arrive (the streaming multipart
    reader in ``dashboard/file_api/uploads.py`` bypasses the same limit the same way). The body
    lands on disk a chunk at a time, so memory is bounded by the write rather than
    by the body's size.

    Writes go through a worker thread so a slow filesystem cannot stall the event
    loop. The format is sniffed from the body's own first two bytes, not
    ``Content-Type``: the export answers ``application/gzip``, a browser upload of
    that file sends whatever its platform guesses, and the tunnel sends
    ``application/json``.

    Raises:
        _DiskFull: when the volume is down to its headroom.
    """
    is_gzip: bool | None = None
    head = b""
    total = 0
    # Every file operation, open and close included, runs in a worker: a close
    # flushes, and a flush on a slow filesystem is exactly the stall this keeps
    # off the loop. ``pending`` is the write in flight, if any; a cancellation
    # arriving while it runs must not close the file under it.
    fout = await asyncio.to_thread(open, dst, "wb")
    pending: asyncio.Future[Any] | None = None
    loop = asyncio.get_running_loop()
    try:
        # No total deadline, because a body has no size ceiling; a no-progress
        # one instead, moved ahead on every chunk, so a sender that stops
        # sending cannot hold the connection and its temp file forever.
        async with asyncio.timeout(DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS) as stall:
            async for chunk in _stalling_chunks(request, stall, loop):
                if is_gzip is None:
                    # The magic is two bytes and a network chunk can be one, so
                    # the format is decided once two bytes are in hand, never
                    # from a shorter prefix that would read every gzip body as
                    # plain JSON.
                    head += bytes(chunk)
                    if len(head) < 2:
                        continue
                    is_gzip = head[:2] == _GZIP_MAGIC
                    chunk, head = head, b""
                total += len(chunk)
                pending = await _write_reserved(fout, chunk, dst)
                await asyncio.shield(pending)
                pending = None
        if head:
            # A body shorter than the magic: not gzip, and still its own bytes.
            total += len(head)
            pending = await _write_reserved(fout, head, dst)
            await asyncio.shield(pending)
            pending = None
    finally:
        if pending is not None:
            with contextlib.suppress(BaseException):
                await asyncio.shield(pending)
        await asyncio.shield(asyncio.to_thread(fout.close))
    return bool(is_gzip), total


async def _read_bundle_body(
    request: web.Request, keep: contextlib.AsyncExitStack
) -> tuple[Any, web.Response | None]:
    """Read the request body as a bundle document. Returns ``(body, error)``.

    Accepts BOTH shapes the two callers send, distinguished by the body's own first
    two bytes: **gzip** — the file ``GET /api/chat/slots/{key}/export`` hands the
    user, byte for byte — and **plain JSON**, what the tunnel's server-to-server
    ``send_session_bundle`` posts. Sniffing the magic rather than branching on
    ``Content-Type`` keeps a browser upload, a peer's plain POST and the export file
    all working without asking any caller to relabel what it already sends.

    **The body streams to disk; it is never held in memory.** The raw body is
    written to a temp file under the crew home a chunk at a time
    (:func:`_stream_request_to_file`), a gzip body is stream-decompressed to a
    second temp file (:func:`_gunzip_file`), and only the parse loads the document.
    Reading ``request.content`` rather than ``request.read()`` is deliberate: it
    bypasses the Application's ``client_max_size`` — matching the streaming multipart
    upload path — so a session of any size arrives rather than being refused, which
    is the owner's decision that a transfer is never blocked by size. Memory safety
    comes from the disk write, the concurrency permit and the memory admission
    (:func:`_memory_admission`) in front of the parse, not from a size ceiling.

    **There is no size wall.** A gzip bomb is stopped by the disk it would fill,
    not by a byte ceiling: both the raw stream and the decompression stop once the
    volume is down to its headroom (``507 transfer_disk_full``).

    **The permit spans the rest of the arrival.** :func:`_expansion_admission` is
    entered on *keep*, the CALLER's stack, once the body is on disk, so it is
    still held when this returns. A parsed bundle stays resident — through
    validation, redaction and persistence — until the arrival finishes, and N
    unbounded arrivals at once are the sum the permit bounds; releasing it here
    would leave that count unbounded. The upload itself is not under the permit:
    it holds one chunk of memory however large or slow it is, a sender that goes
    quiet is cut off by the no-progress deadline, and the number streaming at
    once is capped by :func:`_upload_admission`. The temp files, by
    contrast, are removed as soon as the document is parsed — it is the parsed
    bundle that must be bounded, not the bytes on disk.

    Args:
        request: the arriving request; its body stream is read once.
        keep: the arrival's own stack, which the arrival permit is entered on.
    """
    # The upload slot bounds how many staging descriptors arrivals hold; it is
    # taken before the file is opened and released when the stream ends.
    upload_slot = contextlib.ExitStack()
    try:
        upload_slot.enter_context(_upload_admission())
    except _UploadsBusy:
        return None, web.json_response(
            {
                "error": "too many imports are uploading; please retry",
                "code": "transfer_uploads_busy",
            },
            status=429,
        )
    try:
        raw_path = await asyncio.to_thread(_new_import_temp, ".body")
    except OSError:
        # A full or inode-exhausted volume fails here, before any body is read;
        # it is the same condition the disk headroom answers, so the same code.
        upload_slot.close()
        return None, _disk_full_response()
    except BaseException:
        upload_slot.close()
        raise
    dec_path: Path | None = None
    try:
        try:
            with upload_slot:
                is_gzip, _total = await _stream_request_to_file(request, raw_path)
        except _DiskFull:
            return None, _disk_full_response()
        except Exception:
            # A client that hung up mid-upload, a malformed transfer-encoding.
            # Nothing durable was created beyond the temp cleaned up below.
            return None, _reject("could not read the request body", "transfer_body_unreadable")

        # Taken only once the body is on disk: an upload holds one chunk of
        # memory however long it takes, so a slow sender must not hold a permit
        # that every other import is waiting on.
        try:
            await keep.enter_async_context(_expansion_admission())
        except _ExpansionBusy:
            # Retryable and the sender is at no fault, so it gets a status that
            # says so. 429 rather than 400 for the same reason the slot cap does:
            # the body was fine, the host is busy.
            return None, web.json_response(
                {
                    "error": "too many imports are arriving; please retry",
                    "code": "transfer_expansion_busy",
                },
                status=429,
            )

        src = raw_path
        if is_gzip:
            try:
                dec_path = await asyncio.to_thread(_new_import_temp, ".json")
            except OSError:
                return None, _disk_full_response()
            try:
                await asyncio.to_thread(_gunzip_file, raw_path, dec_path)
            except _DiskFull:
                return None, _disk_full_response()
            except Exception:
                # Corrupt or truncated gzip. A DISTINCT code from bad JSON: the
                # sender needs to know its file did not survive the trip, not go
                # looking for a syntax error in a document it never wrote by hand.
                return None, _reject("could not decompress the bundle", "transfer_invalid_gzip")
            src = dec_path

        try:
            doc_bytes, messages, values, factor = await asyncio.to_thread(_measure_document, src)
            await keep.enter_async_context(_memory_admission(doc_bytes, messages, values, factor))
        except _NeverFits as never:
            need_gib = never.args[0] / (1024**3)
            # The budget the estimate was compared against, not the raw total.
            budget_gib = max(0, never.args[1] - _MEMORY_HEADROOM_BYTES) / (1024**3)
            return None, web.json_response(
                {
                    "error": (
                        f"this session needs about {need_gib:.1f} GiB of memory to import; "
                        f"this machine can spare {budget_gib:.1f} GiB"
                    ),
                    "code": "transfer_bundle_too_large",
                },
                status=413,
            )
        except _MemoryWaitTimedOut:
            # Retryable and the sender is at no fault: the body was fine, the
            # host is short of memory right now.
            return None, web.json_response(
                {
                    "error": "this machine is short of memory right now; please retry",
                    "code": "transfer_expansion_busy",
                },
                status=429,
            )
        try:
            body = await asyncio.to_thread(_load_json_file, src)
        except Exception:
            return None, _reject("invalid JSON body", "transfer_invalid_json")
        return body, None
    finally:
        # Synchronous on purpose: a cancellation mid-arrival must still reclaim the
        # temp files, and awaiting inside a cancelled coroutine's finally is not
        # dependable. Two local unlinks are microseconds on the loop.
        _rm_import_temps(raw_path, dec_path)


# Each exception class is named by the module callers import it from: a
# raised class renders as ``module.qualname`` in a traceback and an error chain.
_DiskFull.__module__ = "kiro_crew.dashboard.session_transfer"
_UploadsBusy.__module__ = "kiro_crew.dashboard.session_transfer"
_ExpansionBusy.__module__ = "kiro_crew.dashboard.session_transfer"
_NeverFits.__module__ = "kiro_crew.dashboard.session_transfer"
_MemoryWaitTimedOut.__module__ = "kiro_crew.dashboard.session_transfer"

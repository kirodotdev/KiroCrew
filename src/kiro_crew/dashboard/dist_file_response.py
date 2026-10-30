"""Serve one file of the React build without touching the loop's default executor.

``web.FileResponse`` runs its ``stat`` + ``open``, its chunked reads and its
``close`` through ``loop.run_in_executor(None, ...)``: the default executor, which
hundreds of other call sites in the gateway share. When those calls hold every
default worker, ``/`` (resolved on its own pool) still answers in milliseconds
while every ``/assets/*.js`` request waits for a free worker, and the dashboard
loads its shell and then stays blank. So the build's files are opened, read and
closed on :func:`kiro_crew.executors.static_asset_executor`, a small pool only
these routes use: a saturated default executor cannot blank the page, and a
flood of asset requests queues among itself.

Kept from ``FileResponse``: the open happens in ``prepare()`` (so the status is
final only there, which the ``/assets`` cache-control finalizer accounts for); a
precompressed ``.br``/``.gz`` sibling (a regular file, never a link) chosen by
``Accept-Encoding``; the guessed content type; ``ETag``/``Last-Modified`` with the
``412`` and ``304`` they decide; one byte range (``206``, ``416``, ``If-Range``),
which the bundled feature videos under ``/app-assets`` need to seek and, on
WebKit, to play at all; ``403`` for an unreadable file and ``404`` for a missing
one. Unlike ``FileResponse``, a file that exists but cannot be opened logs one
warning naming it and the OS error, so it does not look like a missing build.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time
from email.utils import formatdate
from pathlib import Path
from stat import S_ISREG
from typing import IO, cast

from aiohttp import hdrs, web, web_fileresponse
from aiohttp.abc import AbstractStreamWriter

from kiro_crew.dashboard.conditional_get import is_not_modified
from kiro_crew.executors import static_asset_executor

logger = logging.getLogger(__name__)

# aiohttp's own table, the one the font types are registered on, so a build file
# gets the type ``FileResponse`` gave it. It exists from aiohttp 3.10; the declared
# floor is 3.9, which guesses with the stdlib, so read it without importing it.
_AIOHTTP_CONTENT_TYPES: mimetypes.MimeTypes | None = getattr(
    web_fileresponse, "CONTENT_TYPES", None
)

# Bytes read per hop. ``write`` waits for the client to drain, so a slow reader
# holds at most one chunk per connection in memory.
_CHUNK_SIZE = 256 * 1024
# Checked in this order, as aiohttp's static handler does.
_ENCODED_SIBLINGS = (("br", ".br"), ("gzip", ".gz"))
_FALLBACK_CONTENT_TYPE = "application/octet-stream"

# Futures of the fire-and-forget closes, held so they are not collected early.
_PENDING_CLOSES: set[asyncio.Future[None]] = set()

# When a build file that exists but cannot be opened was last logged, per errno
# (monotonic seconds). A process that cannot open files fails every asset of a
# page load with the same errno, so one line per errno per interval names the
# first path and the error without repeating it for each chunk.
_UNOPENABLE_LAST_LOGGED: dict[int | None, float] = {}
_UNOPENABLE_LOG_INTERVAL_S = 600.0


def _log_unopenable(path: Path, error: OSError) -> None:
    """Warn that ``path`` exists but cannot be opened, at most once per errno per interval.

    Without it the answer is a 404 (403 for a permission error) that looks
    exactly like a missing file, and the log says nothing an operator can act on.
    """
    now = time.monotonic()
    last = _UNOPENABLE_LAST_LOGGED.get(error.errno)
    if last is not None and now - last < _UNOPENABLE_LOG_INTERVAL_S:
        return
    _UNOPENABLE_LAST_LOGGED[error.errno] = now
    logger.warning(
        "Build file %r cannot be read: %s. Requests for it answer 404 (403 for a "
        "permission error). Logged once per error every %d seconds.",
        str(path),
        error,
        int(_UNOPENABLE_LOG_INTERVAL_S),
    )


def _open_variant(path: Path, accept_encoding: str) -> tuple[IO[bytes], os.stat_result, str | None]:
    """Open the best variant of ``path`` for ``accept_encoding``.

    A sibling is used only when it is a regular file: ``lstat``, so a link
    planted beside a chunk cannot widen what the confined ``path`` reaches. The
    ``stat`` comes from the open descriptor, so it describes the bytes sent.
    Raises ``OSError`` when ``path`` itself cannot be opened.
    """
    for encoding, suffix in _ENCODED_SIBLINGS:
        if encoding not in accept_encoding:
            continue
        sibling = path.with_name(path.name + suffix)
        try:
            link_st = sibling.lstat()
            if not S_ISREG(link_st.st_mode):
                continue
            fobj = open(sibling, "rb")  # noqa: SIM115 - closed by the response
        except OSError:
            continue
        st = os.fstat(fobj.fileno())
        if (st.st_dev, st.st_ino) != (link_st.st_dev, link_st.st_ino):
            # Swapped for a link between the lstat and the open: not the file checked.
            fobj.close()
            continue
        return fobj, st, encoding
    fobj = open(path, "rb")  # noqa: SIM115 - closed by the response
    return fobj, os.fstat(fobj.fileno()), None


def _conditional_status(request: web.BaseRequest, etag: str, mtime: float) -> int | None:
    """``412``, ``304`` or ``None`` (serve), in ``FileResponse``'s order.

    Preconditions first: ``If-Match`` (strong comparison), else
    ``If-Unmodified-Since``; a failed one is ``412``. Then ``If-None-Match``
    (weak comparison), else ``If-Modified-Since``; a match is ``304``, decided by
    :func:`~kiro_crew.dashboard.conditional_get.is_not_modified`. Dates compare
    against the whole-second ``Last-Modified`` this response sends (RFC 9110
    13.1.3-13.1.4), not the raw float mtime ``FileResponse`` uses: a client
    echoing that header back then gets ``304`` / ``200``, not ``200`` / ``412``,
    whatever fraction of a second the file's mtime carries.
    """
    if_match = request.if_match
    if if_match is not None:
        if not any(tag.value == "*" or (tag.value == etag and not tag.is_weak) for tag in if_match):
            return 412
    else:
        unmodified_since = request.if_unmodified_since
        if unmodified_since is not None and int(mtime) > unmodified_since.timestamp():
            return 412
    # aiohttp prepares a response with the handler's ``web.Request``; the helper
    # reads only the validator headers, which ``BaseRequest`` already carries.
    not_modified = is_not_modified(cast(web.Request, request), f'"{etag}"', last_modified=mtime)
    return 304 if not_modified else None


def _selected_range(
    request: web.BaseRequest, size: int, mtime: float
) -> tuple[int, int, bool] | None:
    """``(offset, count, partial)`` to send, or ``None`` for ``416``.

    ``FileResponse``'s rules: an ``If-Range`` date older than the file, or no
    ``Range``, sends the whole file; a suffix range (``bytes=-N``) is clamped to
    the file; an end past the file is the file's end; a start at or past the end,
    or a header aiohttp cannot parse (including several ranges), is ``416``.
    """
    if_range = request.if_range
    if if_range is not None and int(mtime) > if_range.timestamp():
        return 0, size, False
    try:
        rng = request.http_range
    except ValueError:
        return None
    start, end = rng.start, rng.stop
    if start is None:
        return 0, size, False
    if start < 0 and end is None:
        start = max(start + size, 0)
        count = size - start
    else:
        count = min(end if end is not None else size, size) - start
    if start >= size:
        return None
    return start, count, True


def _guess_type(path: Path) -> str | None:
    """The content type ``FileResponse`` would send for ``path``, or ``None``."""
    table = _AIOHTTP_CONTENT_TYPES
    return (table.guess_type(path) if table is not None else mimetypes.guess_type(path))[0]


def _close_later(fobj: IO[bytes]) -> None:
    """Close ``fobj`` on the static-asset pool without waiting for it.

    Not awaited, so the connection can serve its next request at once.
    """
    close = asyncio.get_running_loop().run_in_executor(static_asset_executor(), fobj.close)
    _PENDING_CLOSES.add(close)
    close.add_done_callback(_PENDING_CLOSES.discard)


class DistFileResponse(web.StreamResponse):
    """Stream ``path`` (already confined to the build) using only the static-asset pool."""

    def __init__(self, path: Path) -> None:
        super().__init__(status=200)
        self._path = path

    async def prepare(self, request: web.BaseRequest) -> AbstractStreamWriter | None:
        if self.prepared:
            return await super().prepare(request)
        loop = asyncio.get_running_loop()
        pool = static_asset_executor()
        accept_encoding = request.headers.get(hdrs.ACCEPT_ENCODING, "").lower()
        try:
            fobj, st, encoding = await loop.run_in_executor(
                pool, _open_variant, self._path, accept_encoding
            )
        except PermissionError as exc:
            _log_unopenable(self._path, exc)
            self.set_status(403)
            return await super().prepare(request)
        except OSError as exc:
            if not isinstance(exc, (FileNotFoundError, NotADirectoryError)):
                _log_unopenable(self._path, exc)
            self.set_status(404)
            return await super().prepare(request)
        try:
            if not S_ISREG(st.st_mode):
                self.set_status(403)
                return await super().prepare(request)
            etag = f"{st.st_mtime_ns:x}-{st.st_size:x}"
            self.headers[hdrs.ETAG] = f'"{etag}"'
            self.headers[hdrs.LAST_MODIFIED] = formatdate(st.st_mtime, usegmt=True)
            conditional = _conditional_status(request, etag, st.st_mtime)
            if conditional is not None:
                self.set_status(conditional)
                return await super().prepare(request)
            selected = _selected_range(request, st.st_size, st.st_mtime)
            if selected is None:
                self.headers[hdrs.CONTENT_RANGE] = f"bytes */{st.st_size}"
                self.set_status(416)
                return await super().prepare(request)
            offset, count, partial = selected
            if partial:
                self.set_status(206)
                self.headers[hdrs.CONTENT_RANGE] = (
                    f"bytes {offset}-{offset + count - 1}/{st.st_size}"
                )
            return await self._stream(request, fobj, offset, count, encoding)
        finally:
            _close_later(fobj)

    async def _stream(
        self,
        request: web.BaseRequest,
        fobj: IO[bytes],
        offset: int,
        count: int,
        encoding: str | None,
    ) -> AbstractStreamWriter | None:
        if hdrs.CONTENT_TYPE not in self.headers:
            self.content_type = _guess_type(self._path) or _FALLBACK_CONTENT_TYPE
        if encoding is not None:
            self.headers[hdrs.CONTENT_ENCODING] = encoding
            self.headers[hdrs.VARY] = hdrs.ACCEPT_ENCODING
        self.headers[hdrs.ACCEPT_RANGES] = "bytes"
        self.content_length = count
        writer = await super().prepare(request)
        if request.method != hdrs.METH_HEAD:
            loop = asyncio.get_running_loop()
            pool = static_asset_executor()
            if offset:
                await loop.run_in_executor(pool, fobj.seek, offset)
            remaining = count
            while remaining > 0:
                chunk = await loop.run_in_executor(pool, fobj.read, min(_CHUNK_SIZE, remaining))
                if not chunk:
                    # Shrank while being sent: the body is shorter than the
                    # declared length, so this connection cannot carry another
                    # response.
                    self.force_close()
                    break
                await self.write(chunk)
                remaining -= len(chunk)
        await self.write_eof()
        return writer

"""File transfer routes: ``/api/file-watch``, ``/api/file-download`` and the Range-capable ``/api/file-stream``."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import mimetypes
import os
import threading
import urllib.parse
from typing import TYPE_CHECKING, BinaryIO

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.files import (
        _DOWNLOAD_CHUNK_STALL_SECS,
        _DOWNLOAD_SCAN_MAX_BYTES,
        _STREAM_CHUNK_BYTES,
        _STREAM_MAX_BYTES,
        _STREAM_TEXT_PROBE_BYTES,
        FILE_READ_SCHEMA,
        ValidationError,
        _open_checked_file,
        _open_refusal,
        _OpenDenied,
        _PathProbeBusy,
        _probe_busy_response,
        _probe_request_path,
        _project_relative_stall_path,
        _resolve_project_relative,
        _run_path_probe,
        _sel,
        _sniff_media_type,
        binary_content_is_flagged,
        logger,
        redact,
        require_owner_dashboard_request,
        validate_tool_args,
    )


async def api_file_watch(request: web.Request) -> web.StreamResponse:
    """GET /api/file-watch?path=... — SSE stream of file content changes."""
    owner_denied = await require_owner_dashboard_request(request, "file_watch")
    if owner_denied is not None:
        return owner_denied
    raw_path = request.query.get("path", "")
    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_watch", outcome="denied", resources=raw_path
        )
        return web.json_response({"error": "invalid input"}, status=400)

    # Off-loop: validation and the stat are filesystem syscalls that must not
    # run on the event loop (see _probe_request_path).
    try:
        probe = await _run_path_probe(_probe_request_path, raw_path)
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw_path, tool_name="file_watch")
    path = probe.path
    if not path:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_watch", outcome="denied", resources=raw_path
        )
        return web.json_response({"error": "invalid or forbidden path"}, status=400)

    if not probe.is_file:
        _sel().log_tool_invocation(
            session_key="dashboard", tool_name="file_watch", outcome="not_found", resources=path
        )
        return web.json_response({"error": "not found"}, status=404)

    _sel().log_tool_invocation(
        session_key="dashboard", tool_name="file_watch", outcome="success", resources=path
    )

    # Taken BEFORE the stream is prepared: a refused probe here is still an
    # ordinary JSON answer, whereas once headers are out only the stream exists.
    try:
        resolved_at_start = await _run_path_probe(os.path.realpath, path)
    except _PathProbeBusy:
        return _probe_busy_response(resource=path, tool_name="file_watch")

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"

    poll_interval = 1.0
    read_cap = 512_000
    last_mtime: float = 0.0
    last_content = ""

    def _read_file(p: str, cap: int) -> str:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            return f.read(cap)

    try:
        await resp.prepare(request)
        while not (request.transport is None or request.transport.is_closing()):
            # Each poll tick is a probe on the watched path. A refused tick is
            # skipped, not fatal: the stream is already open, the pool is busy
            # rather than the file gone, and the next tick tries again.
            try:
                stat = await _run_path_probe(os.stat, path)
                mtime = stat.st_mtime
            except (FileNotFoundError, _PathProbeBusy):
                await asyncio.sleep(poll_interval)
                continue

            if mtime != last_mtime:
                last_mtime = mtime
                try:
                    current_resolved = await _run_path_probe(os.path.realpath, path)
                except _PathProbeBusy:
                    # Do not read: the symlink re-check is what guards the read,
                    # and an unchecked read is the thing it exists to prevent.
                    last_mtime = 0.0
                    await asyncio.sleep(poll_interval)
                    continue
                if current_resolved != resolved_at_start:
                    logger.warning(
                        "file-watch: symlink changed after validation: %s -> %s",
                        resolved_at_start,
                        current_resolved,
                    )
                    _sel().log_tool_invocation(
                        session_key="dashboard",
                        tool_name="file_watch",
                        outcome="denied",
                        resources=path,
                    )
                    break
                try:
                    content = await asyncio.to_thread(_read_file, current_resolved, read_cap)
                    # A read that filled the cap saw only the head of the file,
                    # so an equal head says nothing about the rest: a write past
                    # the cut changes the mtime and nothing this read can see.
                    capped = len(content) >= read_cap
                    # NOT an owner-view seam, on purpose: this stream also
                    # serves file-backed artifact live reload, and neither
                    # consumer renders the frame -- both re-read through
                    # api_file_read, which is the one seam the owner's
                    # credential-redaction switch applies to.
                    content = redact(content)
                except Exception:
                    logger.warning("file-watch read error for %s", path, exc_info=True)
                    await asyncio.sleep(poll_interval)
                    continue

                if content != last_content or capped:
                    # A capped write that left the head unchanged is a signal
                    # only: both consumers re-read on it, so the frame carries
                    # no body rather than the same 512,000 characters again.
                    body = content if content != last_content else ""
                    last_content = content
                    # ensure_ascii=False keeps multi-byte content (e.g. CJK)
                    # inspectable as-is in DevTools instead of \uXXXX escapes,
                    # and produces smaller payloads. Body bytes are still
                    # valid UTF-8 because we explicitly .encode() below.
                    payload = json.dumps({"content": body, "mtime": mtime}, ensure_ascii=False)
                    await resp.write(f"data: {payload}\n\n".encode("utf-8"))

            await asyncio.sleep(poll_interval)
    except (ConnectionResetError, asyncio.CancelledError, ClientConnectionResetError):
        pass

    return resp


async def api_file_download(request: web.Request) -> web.StreamResponse:
    """GET /api/file-download?path=... — download a file as raw bytes.

    Sibling of /api/file-read. file-read decodes content as UTF-8 with
    errors='replace' to render text in the markdown panel; that mode
    corrupts binary files (.docx, .pdf, images) by replacing non-text
    bytes with U+FFFD. This endpoint streams the original bytes, sets
    Content-Disposition: attachment, and applies X-Content-Type-Options:
    nosniff to keep the browser from rendering the response inline.

    Security: same path-validation as file-read (validate_tool_args,
    _validate_dashboard_path, sensitive-path filter). Symlinks rejected
    via O_NOFOLLOW. The route is owner-only, so the content is NOT scanned:
    the owner downloads their own file as-is. Binary files are served
    without a MIME allowlist, since attachment disposition + nosniff
    prevents inline rendering on the dashboard origin.

    A file up to _DOWNLOAD_SCAN_MAX_BYTES is read whole. A larger file
    streams in bounded chunks so it is never held in memory whole; see
    :func:`_stream_unscanned_download`. Small or large is decided by the fstat
    of the ONE descriptor every byte is read from, so no other file can be
    swapped in between the size check and the send.
    """
    owner_denied = await require_owner_dashboard_request(request, "file_download")
    if owner_denied is not None:
        return owner_denied
    # Path validation happens inside ``_open_checked_file``, which keeps the
    # late-binding ``handlers`` alias so tests can still monkey-patch
    # ``_validate_dashboard_path`` (legitimate circular-import workaround,
    # listed as an exception in the top-level-imports rule).
    raw_path = request.query.get("path", "")
    # Resolve relative paths against project dir when resolve=1 (mirrors
    # api_file_read). Off-loop: the resolution is a pair of realpath calls.
    if request.query.get("resolve") == "1":
        try:
            raw_path, _resolve_err = await _run_path_probe(
                _resolve_project_relative,
                raw_path,
                stall_path=_project_relative_stall_path(raw_path),
            )
        except _PathProbeBusy:
            return _probe_busy_response(resource=raw_path, tool_name="file_download")
        if _resolve_err == "cannot_resolve":
            return web.json_response(
                {"error": "cannot resolve: no project dir configured"},
                status=400,
            )
        if _resolve_err == "outside_project":
            return web.json_response(
                {"error": "path outside project directory"},
                status=400,
            )

    try:
        validate_tool_args({"path": raw_path}, FILE_READ_SCHEMA)
    except ValidationError:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_download",
            outcome="denied",
            resources=raw_path,
        )
        return web.json_response({"error": "invalid input"}, status=400)

    # The checked prefix shared with file-raw, file-stream and file-sheet:
    # validate, sensitive-path filter, nofollow open, fstat. No header sniff:
    # this endpoint serves attachment + nosniff rather than choosing a content
    # type. Synchronous file I/O, so it runs on the bounded transfer pool.
    try:
        checked = await _run_path_probe(
            functools.partial(_open_checked_file, raw_path, tool_name="file_download"),
            transfer=True,
            stall_path=raw_path,
        )
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw_path, tool_name="file_download")
    if isinstance(checked, _OpenDenied):
        return _open_refusal(checked, tool_name="file_download").response
    f, path = checked.file, checked.path
    # Set while a chunk read of the unscanned stream may still be running on a
    # pool thread: after a stall (or a cancelled request) that thread holds the
    # file's buffer lock, and a close would wait on it.
    read_in_flight = [False]
    try:
        if checked.size > _DOWNLOAD_SCAN_MAX_BYTES:
            return await _stream_unscanned_download(request, f, path, checked.size, read_in_flight)
        # Bounded read on the same fd: one byte past the threshold says the
        # file grew after the fstat, and a file over it is never scanned whole.
        # Marked in flight like a stream chunk: an execution timeout or a
        # cancellation leaves the read running on its pool thread, so the
        # close below must not wait on it.
        read_in_flight[0] = True
        try:
            data = await _run_path_probe(
                f.read, _DOWNLOAD_SCAN_MAX_BYTES + 1, transfer=True, stall_path=path
            )
        except _PathProbeBusy:
            # Refused before it ran: no read holds the file.
            read_in_flight[0] = False
            return _probe_busy_response(resource=path, tool_name="file_download")
        except TimeoutError:
            # Past the execution ceiling. TimeoutError is an OSError, so it is
            # caught here first: the read is still running, the flag stays set.
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="file_download",
                outcome="failure",
                resources=path,
                error="read_stalled",
            )
            return web.json_response(
                {"error": "cannot read file", "code": "read_failed"}, status=500
            )
        except OSError:
            read_in_flight[0] = False
            logger.exception("file_download read failed for %s", path)
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="file_download",
                outcome="failure",
                resources=path,
            )
            return web.json_response(
                {"error": "cannot read file", "code": "read_failed"}, status=500
            )
        read_in_flight[0] = False
    finally:
        # A close that may wait on a read still running -- the pool is full, or
        # a chunk read stalled or was abandoned -- goes to a thread of its own:
        # it needs no pool admission, never blocks the loop or this response,
        # and only ever waits on this one file.
        deferred = read_in_flight[0]
        if not deferred:
            try:
                await _run_path_probe(f.close, transfer=True, stall_path=path)
            except _PathProbeBusy:
                deferred = True
        if deferred:
            threading.Thread(target=f.close, name="file-download-close", daemon=True).start()
    if len(data) > _DOWNLOAD_SCAN_MAX_BYTES:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_download",
            outcome="denied",
            resources=path,
            error="file_grew_during_read",
        )
        return web.json_response(
            {"error": "file changed while it was read; retry", "code": "file_changed"},
            status=409,
        )

    safe_name = urllib.parse.quote(os.path.basename(path), safe="")
    content_type, _ = mimetypes.guess_type(path)
    if not content_type:
        content_type = "application/octet-stream"

    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="file_download",
        outcome="success",
        resources=path,
    )
    return web.Response(
        body=data,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _download_read_chunk(f: BinaryIO, size: int) -> bytes:
    """One chunk of an unscanned download: a plain read on the transfer pool."""
    return f.read(size)


async def _stream_unscanned_download(
    request: web.Request, f: BinaryIO, path: str, size: int, read_in_flight: list[bool]
) -> web.StreamResponse:
    """Serve a download over the in-memory threshold by streaming it.

    The file streams from *f* -- the descriptor whose fstat gave
    *size* -- in chunks of ``_STREAM_CHUNK_BYTES``, and never past *size*: a
    file that grows while it streams is cut at what the fstat said. A file that
    shrinks ends the response short and drops the connection, so the client
    sees a failed download rather than a short file it takes for whole.
    """
    name = os.path.basename(path)
    content_type, _ = mimetypes.guess_type(path)
    resp = web.StreamResponse(
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name, safe='')}",
            "Content-Type": content_type or "application/octet-stream",
            "X-Content-Type-Options": "nosniff",
        },
    )
    resp.content_length = size
    # SEL: the ALLOW decision is recorded before any byte moves.
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="file_download",
        outcome="success",
        resources=path,
        error="unscanned_large_file",
    )
    await resp.prepare(request)
    remaining = size
    while remaining > 0:
        # Each chunk is one hop on the bounded transfer pool, never the shared
        # default executor, and is given _DOWNLOAD_CHUNK_STALL_SECS: a read on
        # a hung mount then costs one transfer worker and ends this response,
        # instead of parking default-executor threads every file route needs.
        error = ""
        # Cleared only once the read has returned: a stall or a cancellation
        # leaves it set, so the caller does not wait on the abandoned read.
        read_in_flight[0] = True
        try:
            chunk = await asyncio.wait_for(
                _run_path_probe(
                    _download_read_chunk,
                    f,
                    min(_STREAM_CHUNK_BYTES, remaining),
                    transfer=True,
                    stall_path=path,
                ),
                timeout=_DOWNLOAD_CHUNK_STALL_SECS,
            )
        except _PathProbeBusy:
            chunk, error = b"", "unscanned_large_file_pool_busy"
        except asyncio.TimeoutError:
            chunk, error = b"", "unscanned_large_file_read_stalled"
        except OSError:
            chunk = b""
        if not error:
            read_in_flight[0] = False
        if not chunk:
            # Shrunk, unreadable, stalled or refused mid-stream: the announced
            # length cannot be met, so drop the connection rather than end it
            # cleanly, and the client sees a failed download.
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="file_download",
                outcome="failure",
                resources=path,
                error=error or "unscanned_large_file_short_read",
            )
            resp.force_close()
            return resp
        remaining -= len(chunk)
        try:
            await resp.write(chunk)
        except (ConnectionResetError, ConnectionError, ClientConnectionResetError):
            return resp  # client hung up
    with contextlib.suppress(Exception):
        await resp.write_eof()
    return resp


def _parse_range_header(value: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``bytes=`` header against ``size``.

    Returns (start, end) inclusive, or None for an unsatisfiable or
    malformed header. Multi-range requests are treated as malformed --
    <audio>/<video> elements only ever issue single ranges, and multipart
    responses would complicate the reader for no consumer.
    """
    if not value.startswith("bytes="):
        return None
    spec = value[len("bytes=") :]
    if "," in spec or "-" not in spec:
        return None
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":
            # suffix form: last N bytes
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


async def api_file_stream(request: web.Request) -> web.StreamResponse:
    """GET /api/file-stream?path=... -- serve audio/video with Range support.

    Powers inline <video>/<audio> playback in the file viewer. file-raw is
    unsuitable for media: it whole-reads the file into memory, rejects
    anything over the upload cap, and ignores Range headers -- and seeking in
    a media element requires 206 Partial Content. This endpoint follows the
    same security pattern (dashboard path validation, sensitive-path block,
    symlink-refusing open, content sniffing before serving) but streams
    bounded chunks off the event loop, so memory stays constant regardless
    of file size. All reads go through the SAME fd the header was sniffed
    from, so the served bytes cannot be swapped after the check.

    Accepted gap (documented, not a defect): the redaction probe covers the
    first 64 KiB. Complete coverage is unreachable for a Range endpoint --
    the client controls byte offsets, so any pattern scan can be split
    across range boundaries -- and the sibling binary-serving endpoint
    (file-raw) performs no content scan at all. The probe exists to catch
    the honest-mistake shape: a text file wearing a forged media magic.
    """
    owner_denied = await require_owner_dashboard_request(request, "file_stream")
    if owner_denied is not None:
        return owner_denied

    def _log(outcome: str, res: str) -> None:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="file_stream",
            outcome=outcome,
            resources=res,
        )

    raw_path = request.query.get("path", "")
    resolve_requested = request.query.get("resolve") == "1"

    def _open_media(raw: str) -> tuple:
        """Validate, open, and sniff the media file. Runs on a worker thread.

        The open-and-check prefix is the shared :func:`_open_checked_file`
        (validate -> sensitive-path -> nofollow open -> fstat cap), with this
        endpoint's stream cap passed in as policy; what stays here is the
        endpoint's own policy: relative-path resolution against the project
        dir (the resolve=1 contract shared with file-read/file-download), the
        media sniff, and the text probe. Returns either
        ("ok", file_object, size, content_type, path) or a refusal tuple
        ("refused", code, path_for_log).
        """
        if resolve_requested:
            try:
                raw, resolve_err = _resolve_project_relative(raw)
            except ValueError:
                # A malformed path (embedded NUL) is an invalid path, not a
                # crash -- same verdict the shared prefix gives one.
                return ("refused", "invalid_path", raw)
            if resolve_err:
                return ("refused", resolve_err, raw)
        checked = _open_checked_file(
            raw,
            tool_name="file_stream",
            fstat_cap=_STREAM_MAX_BYTES,
            log_open_failure=False,
        )
        if isinstance(checked, _OpenDenied):
            return ("refused", checked.code, checked.path)
        fobj, size, validated = checked.file, checked.size, checked.path
        try:
            header = fobj.read(16)
            content_type = _sniff_media_type(header)
            if not content_type:
                fobj.close()
                return ("refused", "not_media", validated)
            # Sibling-control parity: file-download refuses text content that
            # redact() flags. Media magics can be weak (the bare mp3 frame
            # sync is two bytes), so a credential-bearing TEXT file with a
            # forged prefix must not stream out here. A non-UTF-8 probe takes
            # the shared binary scan, as file-download does.
            probe = header + fobj.read(_STREAM_TEXT_PROBE_BYTES - len(header))
            try:
                probe_text = probe.decode("utf-8")
                flagged = redact(probe_text) != probe_text
            except UnicodeDecodeError:
                flagged = binary_content_is_flagged(probe)
            if flagged:
                fobj.close()
                return ("refused", "content_redacted", validated)
            fobj.seek(0)
        except Exception:
            with contextlib.suppress(Exception):
                fobj.close()
            return ("refused", "read_failed", validated)
        return ("ok", fobj, size, content_type, validated)

    try:
        result = await _run_path_probe(_open_media, raw_path)
    except _PathProbeBusy:
        return _probe_busy_response(resource=raw_path, tool_name="file_stream")
    if result[0] == "refused":
        _, code, res = result
        if code == "not_found":
            outcome = "not_found"
        elif code == "read_failed":
            outcome = "failure"
        else:
            outcome = "denied"
        _log(outcome, res)
        # One literal response per refusal class: the error-response contract
        # requires the {"error", "code"} body and the status to be statically
        # checkable at each call site.
        if code == "invalid_path":
            return web.json_response(
                {"error": "invalid or forbidden path", "code": "invalid_path"}, status=400
            )
        if code == "cannot_resolve":
            return web.json_response(
                {"error": "cannot resolve: no project dir configured", "code": "cannot_resolve"},
                status=400,
            )
        if code == "outside_project":
            return web.json_response(
                {"error": "path outside project directory", "code": "outside_project"},
                status=400,
            )
        if code == "sensitive_path":
            return web.json_response(
                {"error": "sensitive path blocked", "code": "sensitive_path"}, status=403
            )
        if code == "not_found":
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        if code == "symlink_refused":
            return web.json_response(
                {"error": "symlinks not allowed", "code": "symlink_refused"}, status=403
            )
        if code == "file_too_large":
            return web.json_response(
                {"error": "file too large", "code": "file_too_large"}, status=413
            )
        if code == "not_media":
            return web.json_response(
                {"error": "file content is not a supported media format", "code": "not_media"},
                status=415,
            )
        if code == "content_redacted":
            return web.json_response(
                {"error": "file content was redacted; stream aborted", "code": "content_redacted"},
                status=400,
            )
        return web.json_response({"error": "cannot read file", "code": "read_failed"}, status=500)
    _, f, size, content_type, path = result

    try:
        start, end = 0, size - 1
        status = 200
        range_header = request.headers.get("Range")
        if range_header:
            parsed = _parse_range_header(range_header, size)
            if parsed is None:
                _log("denied", path)
                return web.json_response(
                    {"error": "range not satisfiable", "code": "bad_range"},
                    status=416,
                    headers={"Content-Range": f"bytes */{size}"},
                )
            start, end = parsed
            status = 206

        resp = web.StreamResponse(status=status)
        resp.content_type = content_type
        resp.content_length = end - start + 1
        resp.headers["Accept-Ranges"] = "bytes"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # inline (not attachment) keeps playback in the <video>/<audio> element,
        # while naming the file so a player-initiated download (the native media
        # controls' Download) saves under the real name instead of "file-stream"
        # -- the last segment of this endpoint's URL, which the browser would
        # otherwise use. Sibling parity with api_file_download's Content-Disposition.
        safe_name = urllib.parse.quote(os.path.basename(path), safe="")
        resp.headers["Content-Disposition"] = f"inline; filename*=UTF-8''{safe_name}"
        if status == 206:
            resp.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        # SEL: record the ALLOW decision before any bytes move. prepare() and
        # the write loop can be cancelled by a client disconnect, and a
        # permitted read must never leave the audit trail empty because the
        # client hung up first.
        _log("success", path)
        await resp.prepare(request)

        await asyncio.to_thread(f.seek, start)
        remaining = end - start + 1
        while remaining > 0:
            try:
                chunk = await asyncio.to_thread(f.read, min(_STREAM_CHUNK_BYTES, remaining))
            except OSError:
                # A mid-stream filesystem error must leave a SEL outcome; the
                # response is already streaming so all we can do is stop short.
                _log("failure", path)
                raise
            if not chunk:
                break  # file truncated under us; the announced length just ends short
            remaining -= len(chunk)
            try:
                await resp.write(chunk)
            except (ConnectionResetError, ConnectionError):
                break  # client hung up (scrubbing, tab close) -- normal for media
        with contextlib.suppress(Exception):
            await resp.write_eof()
        return resp
    finally:
        # close() can wait on the buffered-file lock while a worker-thread
        # read is in flight (task cancellation), so it must not run on the
        # event loop either.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(f.close)

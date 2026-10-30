"""Portability API handlers — export/import KiroCrew state as zip."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
import threading
import uuid
from pathlib import Path

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from kiro_crew.dashboard import part_stream
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.portability import (
    apply_import_zip,
    is_agent_template_stem,
    manifest_is_memory_only,
    plan_export,
    preview_agent_templates,
    stream_export_zip,
    unbundled_agent_templates,
    validate_import_zip,
)
from kiro_crew.sel import sel as _sel_fn  # circular import — sel imports lazily
from kiro_crew.snapshot import NamedStoresInUse, SourceComponentUnsound

logger = logging.getLogger(__name__)

#: Pieces buffered between the (threaded) zip builder and the async response write
#: loop. With the per-piece byte cap below, depth x cap bounds the bytes the queue
#: retains, so a slow client cannot make the builder run ahead and hold the whole
#: archive in RAM.
_EXPORT_STREAM_QUEUE_DEPTH = 8

#: Largest piece the builder hands over at once. A single zip write can be a whole
#: SQLite backup, so the sink splits an oversized write into pieces of this size and
#: spends one queue slot per piece -- the bound is on bytes, not just piece count.
_EXPORT_STREAM_CHUNK_BYTES = 256 * 1024

#: How long the builder thread waits on a full queue before rechecking the abort
#: flag. Short, because this is only the latency with which a disconnected client
#: unblocks the builder (and releases the per-chat history lock it holds); the
#: steady-state cost is a free slot opening as the drain loop writes.
_EXPORT_STREAM_PUT_POLL_SECS = 0.25

#: Deadline for a single ``resp.write()`` of one chunk to the client. A client
#: that stops reading mid-download (a paused browser download, a wedged proxy)
#: leaves the socket open with a full send buffer, so ``resp.write()`` blocks
#: indefinitely -- and the builder thread stays parked inside the per-chat history
#: lock (`hold_stores_for_read` / `locked_stems`), wedging every write to that chat
#: for as long as the stalled client holds the connection. Bounding the write turns
#: that unbounded hang into a teardown. Generous on purpose: a real client on a slow
#: link must be able to drain a 256 KiB piece well within this, so the only writes it
#: trips are ones that make no progress at all.
_EXPORT_STREAM_WRITE_TIMEOUT_SECS = 60.0


class _ExportAborted(Exception):
    """Raised inside the builder thread when the stream is being torn down, so the
    build unwinds (releasing its history lock) instead of parking on a full queue."""


def _abort_connection(request: web.Request) -> None:
    """Abort the underlying transport so no chunked terminator is written.

    aiohttp's ``RequestHandler.finish_response`` calls ``write_eof()`` on whatever
    the handler returns, which on a chunked ``StreamResponse`` emits the ``0\\r\\n``
    terminator -- making a half-written archive look complete. For a mid-stream
    failure we must prevent that, so the client's read fails (an incomplete-body
    error) rather than returning a short "complete" zip. Aborting the transport is
    the one way to stop the terminator; it is best-effort (the transport may
    already be gone on a client disconnect)."""
    transport = request.transport
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.abort()


#: Ceiling for an uploaded state archive, aligned with the bomb guard that
#: already governs this path: `portability._MAX_IMPORT_UNCOMPRESSED` admits 2 GiB
#: *uncompressed*, so a compressed archive under 2 GiB cannot be refused here
#: without the two limits contradicting each other.
#:
#: A tighter ceiling (512 MB, say) is a NEW restriction rather than a restored
#: one -- the upload streams to disk, so nothing downstream needs it -- and it
#: would 413 a legitimate export exactly when the source machine may be gone.
#: The cap exists only so an unbounded upload cannot fill the disk, and 2 GiB is
#: the largest value consistent with the guard downstream.
_MAX_IMPORT_BYTES = 2 * 1024**3

#: Export response header naming the agent templates the bundle does not carry.
UNBUNDLED_TEMPLATES_HEADER = "X-Kirocrew-Unbundled-Templates"

#: Export response header: how many chats were left out so the archive stays within
#: what import accepts (`portability._export_sessions`). A bare non-negative integer.
SESSIONS_SKIPPED_SIZE_HEADER = "X-Kirocrew-Sessions-Skipped-Size"


def _sel():
    return _sel_fn()


def _requested_components(
    request: web.Request,
) -> tuple[list[str] | None, web.Response | None]:
    """The ``?components=`` selector: absent means the whole install, ``memory`` memory only.

    Only those two are offered from the dashboard, so anything else is refused with a
    code rather than silently widened to the whole install.
    """
    raw = request.query.get("components")
    if raw is None or raw == "":
        return None, None
    if raw == "memory":
        return ["memory"], None
    return None, web.json_response(
        {"error": "components must be 'memory'", "code": "invalid_components"}, status=400
    )


#: Most bundled agent templates one import request may pick.
_MAX_PICKED_TEMPLATES = 200


def _requested_agent_templates(
    request: web.Request,
) -> tuple[tuple[str, ...], web.Response | None]:
    """The ``?agent_templates=`` pick: comma-separated bundled template names to
    install. Absent or empty installs none; a name that is not a plain file name is
    refused rather than dropped."""
    raw = request.query.get("agent_templates", "")
    names = tuple(name for name in raw.split(",") if name) if raw else ()
    if len(names) > _MAX_PICKED_TEMPLATES or not all(is_agent_template_stem(n) for n in names):
        return (), web.json_response(
            {
                "error": "agent_templates must name bundled templates",
                "code": "invalid_agent_templates",
            },
            status=400,
        )
    return names, None


async def _read_upload_file(request: web.Request) -> tuple[Path | None, web.Response | None]:
    """Read a multipart file upload into a temp file. Returns (path, None) or (None, error_response)."""
    reader = await request.multipart()
    part = await reader.next()
    if part is None or not isinstance(part, BodyPartReader) or part.name != "file":
        return None, web.json_response(
            {"error": "file field required", "code": "file_field_required"}, status=400
        )

    # A single path rather than a scratch directory: the callers below already
    # own the returned file and unlink it in their `finally`, so a directory
    # would be a second thing to clean and nobody is cleaning it.
    dest = Path(tempfile.gettempdir()) / f"kc_import_{uuid.uuid4().hex}.zip"
    try:
        await part_stream.stream_part_to_file(part, dest, max_bytes=_MAX_IMPORT_BYTES)
    except part_stream.PartTooLarge:
        limit_mb = _MAX_IMPORT_BYTES // (1024 * 1024)
        return None, web.json_response(
            {
                "error": f"archive too large (max {limit_mb} MB)",
                "code": "import_archive_too_large",
            },
            status=413,
        )
    return dest, None


async def api_portability_export(request: web.Request) -> web.StreamResponse:
    """GET /api/portability/export — download Kiro Crew state as zip.

    Owner-only. The archive is the whole install -- config, every workspace file, the
    default store's memory and every named store's memory -- so a dashboard
    subject that is not the owner (an allow-listed messaging user holding a
    ``!dashboard`` token, say) must not be able to pull it. This aggregate export
    requires owner permission independently of member memory visibility.

    ``?include_sessions=true`` adds the persistent dashboard chats
    (`portability._write_sessions`); without it the archive carries none.
    ``?include_agent_templates=true`` adds the agent templates the crews name, with
    their MCP server ``env`` and ``headers`` values replaced by a placeholder
    (`portability.bundled_agent_templates`); without it the archive carries none.

    The archive is STREAMED: a stat-only pre-pass (`portability.plan_export`) fixes
    the filename and both advisory headers, the response head goes out on
    ``prepare()``, and the zip is written chunk by chunk as it is built, so the
    first byte reaches the client in milliseconds. Every member streams -- files in
    fixed-size reads, each SQLite store backed up to a temp file and copied in the
    same way -- so neither the archive nor any one member is ever held whole in
    memory. This is what lets a large export complete through a proxy or tunnel
    with a first-byte timeout.

    An error BEFORE ``prepare()`` is a coded 500. An error AFTER the stream has
    begun cannot become an HTTP status, so it is fail-safe: the connection is
    aborted (``write_eof`` is skipped), the client sees a truncated, incomplete
    download rather than a silently short "complete" zip, and the SEL outcome is
    recorded as an error.
    """
    if "user" not in request or not request["user"]:
        return web.json_response(
            {"error": "authentication required", "code": "auth_required"}, status=401
        )
    denied = await require_owner_dashboard_request(request, "portability.export")
    if denied is not None:
        return denied
    caller = request["user"]
    # Opt-in per export: chats ride only when this request asks for them.
    include_sessions = request.query.get("include_sessions", "").lower() in ("1", "true")
    include_agent_templates = request.query.get("include_agent_templates", "").lower() in (
        "1",
        "true",
    )
    components, bad = _requested_components(request)
    if bad is not None:
        return bad
    memory_only = components == ["memory"]
    if memory_only:
        include_sessions = False
        include_agent_templates = False

    # Phase 1 -- stat only, no bytes built. This can still fail (a filesystem error
    # in the pre-pass); while nothing has been sent, a failure is a coded 500.
    try:
        plan = await asyncio.to_thread(
            plan_export,
            include_sessions=include_sessions,
            memory_only=memory_only,
            include_agent_templates=include_agent_templates,
        )
    except Exception as e:
        logger.exception("Export planning failed")
        _sel().log_api_access(
            caller=caller,
            operation="portability.export",
            outcome="error",
            error=str(e),
        )
        return web.json_response({"error": "Export failed", "code": "export_failed"}, status=500)

    ts = plan.created_at.replace(":", "").replace("-", "")
    filename = f"kirocrew-{'memory-' if memory_only else ''}export-{ts}.zip"

    skipped_size = plan.sessions_skipped
    if not isinstance(skipped_size, int) or isinstance(skipped_size, bool) or skipped_size < 0:
        skipped_size = 0

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    # The body is the archive, so the warning rides a header: the agent templates
    # the crews name that the bundle does not carry. JSON with ASCII escapes, so
    # a hand-edited name cannot break the header line. Computed from the plan's
    # bundled set, so it is known before the first byte, like the archive itself.
    # A memory export carries no crews or config, so the template warning does not apply.
    bundled = [s for s in plan.bundled_templates if isinstance(s, str)]
    unbundled: list[str]
    if memory_only:
        unbundled, more = [], 0
    elif bundled:
        unbundled, more = await asyncio.to_thread(unbundled_agent_templates, bundled)
    else:
        unbundled, more = await asyncio.to_thread(unbundled_agent_templates)
    # Templates the walk reached past the bundle cap that no crew names directly.
    past_cap = plan.agent_templates_past_cap
    if isinstance(past_cap, int) and not isinstance(past_cap, bool) and past_cap > 0:
        more += past_cap
    if unbundled or more:
        headers[UNBUNDLED_TEMPLATES_HEADER] = json.dumps(unbundled + ([f"+{more}"] if more else []))
    if include_sessions:
        headers[SESSIONS_SKIPPED_SIZE_HEADER] = str(skipped_size)

    # Phase 2 -- stream. The head (including both advisory headers) goes out now,
    # defeating a first-byte timeout; the zip bytes follow as they are built.
    resp = web.StreamResponse()
    resp.content_type = "application/zip"
    for key, value in headers.items():
        resp.headers[key] = value
    # Apply the dashboard's cache-control and security headers (no-store, nosniff,
    # CSP, HSTS, ...) to the head before it is sent. ``no_cache_middleware`` sets
    # these on the response it receives back, but a streamed export flushes its own
    # head on ``prepare()`` inside this handler, before the middleware runs -- so
    # the handler must set them itself or the export head carries none. The helper
    # uses ``setdefault``, so the middleware's later call is a no-op and the two
    # never disagree. Imported from the composed ``server`` facade (not the raw
    # ``server_runtime`` module) at call time: the facade binds the runtime globals
    # the helper reads, and a call-time import sidesteps the handler<->server cycle.
    from kiro_crew.dashboard.server import _apply_security_headers

    _apply_security_headers(resp, request.app, request.path, request)
    await resp.prepare(request)

    # Bridge the synchronous zip build (run in ONE worker thread) to the async
    # response. The builder hands each chunk to the event loop via
    # call_soon_threadsafe -> asyncio.Queue, which the drain loop awaits with
    # resp.write(). No blocking queue read runs on an executor thread, so a
    # finished request leaks no parked thread.
    #
    # Backpressure + teardown are a threading.Semaphore and an abort flag the
    # builder checks: the builder must ACQUIRE a slot before handing off a chunk
    # (the drain loop releases one after each write), so a slow client cannot make
    # the builder run ahead and buffer the whole archive. The acquire is a
    # bounded-wait loop that rechecks the abort flag, so a client that hangs up (or
    # a cancelled handler) always unblocks the builder promptly -- otherwise it
    # would stay parked holding the per-chat history lock and wedge writes to that
    # chat.
    out_queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    slots = threading.Semaphore(_EXPORT_STREAM_QUEUE_DEPTH)
    abort = threading.Event()
    loop = asyncio.get_running_loop()
    bytes_sent = 0

    def _emit(item: tuple[str, object]) -> None:
        # Scheduled onto the loop from the builder thread; never raises there.
        with contextlib.suppress(RuntimeError):  # loop closed during teardown
            loop.call_soon_threadsafe(out_queue.put_nowait, item)

    def _sink(chunk: bytes) -> None:
        if not chunk:
            return
        # A single zip write can be a whole SQLite backup, so split it into
        # fixed-size pieces and spend one backpressure slot per piece. This bounds
        # the BYTES the queue retains (depth x piece size), not just the count of
        # chunks -- a slow client cannot make a database-sized member sit whole in
        # the queue. Acquire rechecks the abort flag so teardown frees us at once.
        view = memoryview(chunk)
        for start in range(0, len(view), _EXPORT_STREAM_CHUNK_BYTES):
            piece = view[start : start + _EXPORT_STREAM_CHUNK_BYTES]
            while not slots.acquire(timeout=_EXPORT_STREAM_PUT_POLL_SECS):
                if abort.is_set():
                    raise _ExportAborted
            if abort.is_set():
                slots.release()
                raise _ExportAborted
            _emit(("chunk", bytes(piece)))

    def _build() -> dict:
        try:
            manifest_out = stream_export_zip(_sink, plan)
            _emit(("done", manifest_out))
            return manifest_out
        except BaseException as exc:  # noqa: BLE001 -- reported to the drain loop
            _emit(("error", exc))
            raise

    build_future = asyncio.ensure_future(asyncio.to_thread(_build))

    async def _teardown_builder() -> None:
        # Tell the builder to stop and free every backpressure slot it might be
        # waiting on, then let the thread unwind (releasing its history lock).
        abort.set()
        for _ in range(_EXPORT_STREAM_QUEUE_DEPTH):
            slots.release()
        with contextlib.suppress(BaseException):
            await build_future

    manifest: dict = {}
    try:
        while True:
            kind, payload = await out_queue.get()
            if kind == "chunk":
                assert isinstance(payload, bytes)
                bytes_sent += len(payload)
                try:
                    await asyncio.wait_for(resp.write(payload), _EXPORT_STREAM_WRITE_TIMEOUT_SECS)
                except (asyncio.TimeoutError, TimeoutError):
                    # The client stopped reading mid-stream: the socket is open but
                    # its send buffer is full, so this write made no progress for
                    # the whole deadline. Left alone it would block here forever
                    # while the builder thread stays parked inside the per-chat
                    # history lock, wedging writes to that chat. Tear the builder
                    # down (releasing its lock), abort the transport so no chunked
                    # terminator makes the half archive look complete, and stop.
                    await _teardown_builder()
                    _abort_connection(request)
                    _sel().log_api_access(
                        caller=caller,
                        operation="portability.export",
                        outcome="error",
                        error="client stalled mid-stream",
                    )
                    return resp
                except (ConnectionResetError, ConnectionError):
                    # The client hung up mid-download: the write itself failed on a
                    # dead socket. Scoped to the write (not around ``build_future``)
                    # so a ConnectionError raised by the BUILDER instead falls to the
                    # generic handler below and is logged as a build failure, not a
                    # client disconnect. Release the builder and its lock, record it.
                    await _teardown_builder()
                    _abort_connection(request)
                    _sel().log_api_access(
                        caller=caller,
                        operation="portability.export",
                        outcome="error",
                        error="client disconnected mid-stream",
                    )
                    return resp
                slots.release()  # a slot opens as this chunk leaves
                continue
            if kind == "error":
                # The builder re-raises after emitting this; `await build_future`
                # below surfaces that exception with its traceback intact.
                break
            # kind == "done"
            manifest = payload if isinstance(payload, dict) else {}
            break
        await build_future
    except asyncio.CancelledError:
        # aiohttp cancels the handler when the client disconnects. Tear the builder
        # down (releasing its history lock) and abort the connection so no chunked
        # terminator is written, then propagate the cancellation.
        await _teardown_builder()
        _abort_connection(request)
        _sel().log_api_access(
            caller=caller,
            operation="portability.export",
            outcome="error",
            error="export cancelled mid-stream",
        )
        raise
    except Exception as e:  # noqa: BLE001 -- build raised after the head went out
        await _teardown_builder()
        logger.exception("Export streaming failed after headers were sent")
        _sel().log_api_access(
            caller=caller,
            operation="portability.export",
            outcome="error",
            error=str(e),
        )
        # Fail safe. aiohttp's finish_response would otherwise call write_eof() on
        # the returned response and emit the chunked terminator -- making a half
        # archive look complete, exactly what must not happen. Abort the transport
        # so no terminator is sent and the client's read fails cleanly.
        _abort_connection(request)
        return resp

    contents = manifest.get("contents", {}) if isinstance(manifest, dict) else {}
    logged_skipped = contents.get("sessions_skipped_size", skipped_size)
    if (
        not isinstance(logged_skipped, int)
        or isinstance(logged_skipped, bool)
        or logged_skipped < 0
    ):
        logged_skipped = 0
    _sel().log_api_access(
        caller=caller,
        operation="portability.export",
        outcome="ok",
        resources=f"size={bytes_sent}"
        + (",components=memory" if memory_only else "")
        + (
            f",sessions={contents.get('session_count', 0)},sessions_skipped_size={logged_skipped}"
            if include_sessions
            else ""
        )
        + (
            f",agent_templates={contents.get('agent_template_count', 0)}"
            if include_agent_templates
            else ""
        ),
    )
    await resp.write_eof()
    return resp


async def api_portability_import(request: web.Request) -> web.Response:
    """POST /api/portability/import — upload and apply a Kiro Crew export zip.

    Owner-only, like the export: an import rewrites the install's memory -- every named
    store's included -- and its config. Merge (the default) never overwrites a settings
    document this install has; see `portability._merge_settings`.
    """
    if "user" not in request or not request["user"]:
        return web.json_response(
            {"error": "authentication required", "code": "auth_required"}, status=401
        )
    denied = await require_owner_dashboard_request(request, "portability.import")
    if denied is not None:
        return denied
    caller = request["user"]
    mode = request.query.get("mode", "merge")
    if mode not in ("merge", "replace"):
        return web.json_response(
            {"error": "mode must be 'merge' or 'replace'", "code": "invalid_import_mode"},
            status=400,
        )
    components, bad = _requested_components(request)
    if bad is not None:
        return bad
    memory_only = components == ["memory"]
    if memory_only and mode != "merge":
        return web.json_response(
            {"error": "a memory-only import is merge only", "code": "memory_only_merge_only"},
            status=400,
        )
    picked_templates, bad = _requested_agent_templates(request)
    if bad is not None:
        return bad

    zip_path, err_resp = await _read_upload_file(request)
    if err_resp is not None:
        return err_resp
    assert zip_path is not None

    try:
        ok, error, manifest = await asyncio.to_thread(validate_import_zip, zip_path)
        if not ok:
            _sel().log_api_access(
                caller=caller,
                operation="portability.import",
                outcome="denied",
                error=error,
            )
            return web.json_response(
                {"ok": False, "error": error, "code": "import_archive_invalid"}, status=400
            )
        # A memory bundle carries nothing else, so a Replace from it would reset every
        # other component to absent. Merge is the only mode that makes sense for it.
        if mode != "merge" and manifest_is_memory_only(manifest):
            _sel().log_api_access(
                caller=caller,
                operation="portability.import",
                outcome="denied",
                error="memory-only archive with mode=replace",
            )
            return web.json_response(
                {
                    "ok": False,
                    "error": "this archive holds memory only; import it with Merge",
                    "code": "memory_only_merge_only",
                },
                status=400,
            )

        # The running gateway's notification-settings store, so restored mutes apply at
        # once instead of being written back over by its in-memory copy.
        channel_settings = getattr(request.app.get("state"), "notification_channel_settings", None)
        summary = await asyncio.to_thread(
            apply_import_zip,
            zip_path,
            mode,
            channel_settings=channel_settings,
            memory_only=memory_only or manifest_is_memory_only(manifest),
            **({"agent_templates": picked_templates} if picked_templates else {}),
        )

        # `staging` is recorded here, not only returned. Nothing renders it, and
        # whether an import was pinned, mixed or unpinned is a security property of the
        # operation, so the audit trail is where it belongs more than a UI badge does. The
        # response still carries it for whatever renders it later.
        #
        # A refused component merge (the cron merge can refuse and import nothing)
        # is logged as `partial`, not `ok` -- a flat ok would make the audit trail
        # agree with a summary that claims a merge that never happened.
        refused = summary.get("refused_merges") or []
        _sel().log_api_access(
            caller=caller,
            operation="portability.import",
            outcome="partial" if refused else "ok",
            resources=(
                f"mode={mode},items={len(summary.get('items', []))},"
                f"staging={summary.get('staging', 'unknown')}"
                + (",components=memory" if summary.get("components") == ["memory"] else "")
                + (
                    f",agent_templates_installed={len(summary['agent_templates_installed'])}"
                    if summary.get("agent_templates_installed")
                    else ""
                )
                + (f",refused={';'.join(refused)}" if refused else "")
            ),
        )

        return web.json_response({"ok": True, "summary": summary, "manifest": manifest})
    except (SourceComponentUnsound, NamedStoresInUse) as e:
        # A refusal, not a failure: both are raised before anything moves (a torn database
        # in the archive; a named store open in some process), and the operator needs the
        # sentence that says which. A 500 "Import failed" would read as the tool breaking
        # rather than the archive or the moment being wrong. The `code` tells the two apart
        # without parsing the sentence: one is retried after stopping what holds the store,
        # the other needs a different archive.
        code = "named_store_in_use" if isinstance(e, NamedStoresInUse) else "import_source_unsound"
        _sel().log_api_access(
            caller=caller,
            operation="portability.import",
            outcome="denied",
            error=str(e),
        )
        return web.json_response({"ok": False, "code": code, "error": str(e)}, status=409)
    except Exception as e:
        logger.exception("Import failed")
        _sel().log_api_access(
            caller=caller,
            operation="portability.import",
            outcome="error",
            error=str(e),
        )
        return web.json_response(
            {"ok": False, "error": "Import failed", "code": "import_failed"}, status=500
        )
    finally:
        zip_path.unlink(missing_ok=True)


async def api_portability_preview(request: web.Request) -> web.Response:
    """POST /api/portability/preview — validate and preview a zip without applying."""
    if "user" not in request or not request["user"]:
        return web.json_response(
            {"error": "authentication required", "code": "auth_required"}, status=401
        )
    caller = request["user"]

    zip_path, err_resp = await _read_upload_file(request)
    if err_resp is not None:
        return err_resp
    assert zip_path is not None

    try:
        ok, error, manifest = await asyncio.to_thread(validate_import_zip, zip_path)
        if not ok:
            _sel().log_api_access(
                caller=caller,
                operation="portability.preview",
                outcome="denied",
                error=error,
            )
            return web.json_response({"ok": False, "error": error})

        _sel().log_api_access(
            caller=caller,
            operation="portability.preview",
            outcome="ok",
        )
        templates, more_templates = await asyncio.to_thread(preview_agent_templates, zip_path)
        body: dict = {"ok": True, "manifest": manifest, "agent_templates": templates}
        if more_templates:
            body["agent_templates_more"] = more_templates
        return web.json_response(body)
    except Exception as e:
        logger.exception("Preview failed")
        _sel().log_api_access(
            caller=caller,
            operation="portability.preview",
            outcome="error",
            error=str(e),
        )
        return web.json_response(
            {"ok": False, "error": "Preview failed", "code": "preview_failed"}, status=500
        )
    finally:
        zip_path.unlink(missing_ok=True)

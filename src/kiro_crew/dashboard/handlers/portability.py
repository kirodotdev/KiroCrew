"""Portability API handlers — export/import KiroCrew state as zip."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
from pathlib import Path

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from kiro_crew.dashboard import part_stream
from kiro_crew.portability import apply_import_zip, create_export_zip, validate_import_zip
from kiro_crew.sel import sel as _sel_fn  # circular import — sel imports lazily

logger = logging.getLogger(__name__)

#: Ceiling for an uploaded state archive, aligned with the bomb guard that
#: already governs this path: `portability._MAX_IMPORT_UNCOMPRESSED` admits 2 GiB
#: *uncompressed*, so a compressed archive under 2 GiB cannot be refused here
#: without the two limits contradicting each other.
#:
#: An earlier revision set this to 512 MB and justified it as restoring a bound
#: streaming had removed. That was wrong on the facts: the previous
#: implementation already streamed to disk with no cap at all, so 512 MB was a
#: NEW restriction that could 413 a legitimate export -- exactly when the source
#: machine may be gone. The cap exists only so an unbounded upload cannot fill
#: the disk, and 2 GiB is the largest value consistent with the guard downstream.
_MAX_IMPORT_BYTES = 2 * 1024**3


def _sel():
    return _sel_fn()


async def _read_upload_file(request: web.Request) -> tuple[Path | None, web.Response | None]:
    """Read a multipart file upload into a temp file. Returns (path, None) or (None, error_response)."""
    reader = await request.multipart()
    part = await reader.next()
    if part is None or not isinstance(part, BodyPartReader) or part.name != "file":
        return None, web.json_response({"error": "file field required"}, status=400)

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


async def api_portability_export(request: web.Request) -> web.Response:
    """GET /api/portability/export — download KiroCrew state as zip."""
    if "user" not in request or not request["user"]:
        return web.json_response({"error": "authentication required"}, status=401)
    caller = request["user"]
    try:
        zip_bytes, manifest = await asyncio.to_thread(create_export_zip)
    except Exception as e:
        logger.exception("Export failed")
        _sel().log_api_access(
            caller=caller,
            operation="portability.export",
            outcome="error",
            error=str(e),
        )
        return web.json_response({"error": "Export failed"}, status=500)

    ts = manifest.get("created_at", "unknown").replace(":", "").replace("-", "")
    filename = f"kirocrew-export-{ts}.zip"

    _sel().log_api_access(
        caller=caller,
        operation="portability.export",
        outcome="ok",
        resources=f"size={len(zip_bytes)}",
    )

    return web.Response(
        body=zip_bytes,
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


async def api_portability_import(request: web.Request) -> web.Response:
    """POST /api/portability/import — upload and apply a KiroCrew export zip."""
    if "user" not in request or not request["user"]:
        return web.json_response({"error": "authentication required"}, status=401)
    caller = request["user"]
    mode = request.query.get("mode", "merge")
    if mode not in ("merge", "replace"):
        return web.json_response({"error": "mode must be 'merge' or 'replace'"}, status=400)

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
            return web.json_response({"ok": False, "error": error}, status=400)

        summary = await asyncio.to_thread(apply_import_zip, zip_path, mode)

        # `staging` is recorded here, not only returned. Review pointed out that nothing
        # renders it, which made a field added for truthfulness invisible to everyone -- and
        # whether an import was pinned, mixed or unpinned is a security property of the
        # operation, so the audit trail is where it belongs more than a UI badge does. The
        # response still carries it for whatever renders it later.
        #
        # A refused component merge (issue #8217: the cron merge can refuse and
        # import nothing) is logged as `partial`, not `ok` -- a flat ok here made
        # the audit trail agree with a summary that claimed a merge that never
        # happened.
        refused = summary.get("refused_merges") or []
        _sel().log_api_access(
            caller=caller,
            operation="portability.import",
            outcome="partial" if refused else "ok",
            resources=(
                f"mode={mode},items={len(summary.get('items', []))},"
                f"staging={summary.get('staging', 'unknown')}"
                + (f",refused={';'.join(refused)}" if refused else "")
            ),
        )

        return web.json_response({"ok": True, "summary": summary, "manifest": manifest})
    except Exception as e:
        logger.exception("Import failed")
        _sel().log_api_access(
            caller=caller,
            operation="portability.import",
            outcome="error",
            error=str(e),
        )
        return web.json_response({"ok": False, "error": "Import failed"}, status=500)
    finally:
        zip_path.unlink(missing_ok=True)


async def api_portability_preview(request: web.Request) -> web.Response:
    """POST /api/portability/preview — validate and preview a zip without applying."""
    if "user" not in request or not request["user"]:
        return web.json_response({"error": "authentication required"}, status=401)
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
        return web.json_response({"ok": True, "manifest": manifest})
    except Exception as e:
        logger.exception("Preview failed")
        _sel().log_api_access(
            caller=caller,
            operation="portability.preview",
            outcome="error",
            error=str(e),
        )
        return web.json_response({"ok": False, "error": "Preview failed"}, status=500)
    finally:
        zip_path.unlink(missing_ok=True)

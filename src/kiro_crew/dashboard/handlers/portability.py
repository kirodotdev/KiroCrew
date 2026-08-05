"""Portability API handlers — export/import KiroCrew state as zip."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiohttp import web
from aiohttp.multipart import BodyPartReader

from kiro_crew.portability import apply_import_zip, create_export_zip, validate_import_zip
from kiro_crew.sel import sel as _sel_fn  # circular import — sel imports lazily

logger = logging.getLogger(__name__)


def _sel():
    return _sel_fn()


async def _read_upload_file(request: web.Request) -> tuple[Path | None, web.Response | None]:
    """Read a multipart file upload into a temp file. Returns (path, None) or (None, error_response)."""
    reader = await request.multipart()
    part = await reader.next()
    if part is None or not isinstance(part, BodyPartReader) or part.name != "file":
        return None, web.json_response({"error": "file field required"}, status=400)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        while True:
            chunk = await part.read_chunk(65536)
            if not chunk:
                break
            tmp.write(chunk)
        tmp.close()
        return Path(tmp.name), None
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


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

        _sel().log_api_access(
            caller=caller,
            operation="portability.import",
            outcome="ok",
            resources=f"mode={mode},items={len(summary.get('items', []))}",
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

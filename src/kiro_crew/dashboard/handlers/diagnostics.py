"""Dashboard API handlers for the "Report a Problem" diagnostics flow.

POST /api/diagnostics/collect          — build a redacted diagnostics bundle
GET  /api/diagnostics/download/{name}  — download a previously-built bundle

Both share the collector engine in :mod:`kiro_crew.diagnostics`, the exact same
code path the ``kirocrew doctor --bundle`` CLI uses. Auth + host validation are
applied globally by the dashboard middleware chain, so these handlers add no
per-endpoint auth. The blocking collect runs off the event loop via
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse

from aiohttp import web

from kiro_crew import diagnostics
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)


def _diagnostics_dir():
    return config_dir() / "diagnostics"


def _sel():
    """Late-binding sel() for test monkeypatch compatibility."""
    # circular import: the parent package `kiro_crew.dashboard.handlers` imports
    # this module at package-init time, so it cannot be imported at module scope
    # here. Resolving it lazily also keeps `sel` looked up on the package object
    # at call time, which is what lets tests monkeypatch `handlers.sel`.
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


async def api_diagnostics_collect(request: web.Request) -> web.Response:
    """POST /api/diagnostics/collect — collect + redact + zip diagnostics.

    Body (all optional): ``{"note": str, "include_logs": bool}``.
    Returns the bundle metadata plus a ``download_url`` and the pre-filled
    GitHub issue URL.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "request body must be an object", "code": "invalid_body"},
            status=400,
        )

    note = str(body.get("note") or "")
    include_logs = bool(body.get("include_logs", True))

    try:
        result = await asyncio.to_thread(
            diagnostics.collect_bundle,
            note=note,
            include_logs=include_logs,
            output_dir=_diagnostics_dir(),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("diagnostics collection failed")
        return web.json_response(
            {"error": f"collection failed: {exc}", "code": "collection_failed"},
            status=500,
        )

    payload = result.as_dict()
    payload["download_url"] = "/api/diagnostics/download/" + urllib.parse.quote(
        result.filename, safe=""
    )
    return web.json_response(payload)


async def api_diagnostics_download(request: web.Request) -> web.StreamResponse:
    """GET /api/diagnostics/download/{filename} — stream a bundle zip.

    Scoped strictly to ``<data_home>/diagnostics``; path traversal and
    non-``.zip`` names are rejected. Served as ``application/zip`` directly, so
    it does not touch the shared binary-MIME allowlist.
    """
    filename = request.match_info["filename"]
    base = _diagnostics_dir().resolve()
    path = (base / filename).resolve()

    if not path.is_relative_to(base) or path.suffix != ".zip" or not path.is_file():
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="diagnostics_download",
            tool_kind="download",
            outcome="denied",
            error=f"rejected: {filename}",
        )
        return web.json_response({"error": "forbidden", "code": "forbidden"}, status=403)

    _sel().log_tool_invocation(
        session_key="api",
        source="api",
        tool_name="diagnostics_download",
        tool_kind="download",
        outcome="allowed",
        resources=path.name,
    )
    safe_name = urllib.parse.quote(path.name, safe="")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f"attachment; filename*=UTF-8''{safe_name}",
            "X-Content-Type-Options": "nosniff",
        },
    )

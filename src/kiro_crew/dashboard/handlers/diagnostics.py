"""Dashboard API handlers for the "Report a Problem" diagnostics flow.

POST /api/diagnostics/collect          — build a redacted diagnostics bundle
GET  /api/diagnostics/download/{name}  — download a previously-built bundle

Both share the collector engine in :mod:`kiro_crew.diagnostics`, the exact same
code path the ``kirocrew doctor --bundle`` CLI uses. Auth + host validation are
applied globally by the dashboard middleware chain. Both handlers are also
owner-gated: a bundle carries the owner's gateway and chat logs, so only the
dashboard owner may build or download one. The blocking collect and the
download's path checks run off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from pathlib import Path

from aiohttp import web

from kiro_crew import diagnostics
from kiro_crew.config.fields import _coerce_bool
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

logger = logging.getLogger(__name__)


def _diagnostics_dir():
    return config_dir() / "diagnostics"


def _staged_update_alignment() -> str | None:
    """``versions.txt``'s ``version_alignment:`` for an update this gateway staged.

    The one side-by-side update known in here: the restart-ready note names it, no
    live-target pointer sends the next start elsewhere, and ``restart_reaches``
    confirms a restart still runs it, since a later install can move the stable
    link without clearing the note. None (not checked) otherwise.
    """
    # In-function: the gateway has these loaded already, and the package imports
    # this module at init time.
    from kiro_crew import update_restart_ready
    from kiro_crew.cli_server import _format_version_line
    from kiro_crew.platform import wheel_apply
    from kiro_crew.service import live_target

    note = update_restart_ready.current()
    if note is None or note.reason != update_restart_ready.REASON_STAGED or not note.version:
        return None
    try:
        if live_target.read_target() is not None or not wheel_apply.restart_reaches(note.version):
            return None
    except Exception:  # noqa: BLE001 -- one unreadable line must not fail the bundle
        return None
    return _format_version_line({"version": diagnostics.__version__}, note.version, note.since)


def _sel():
    """Late-binding sel() for test monkeypatch compatibility."""
    # circular import: the parent package `kiro_crew.dashboard.handlers` imports
    # this module at package-init time, so it cannot be imported at module scope
    # here. Resolving it lazily also keeps `sel` looked up on the package object
    # at call time, which is what lets tests monkeypatch `handlers.sel`.
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _audit_collect_allowed_sync(include_logs: bool) -> None:
    """Best-effort SEL record of an allowed collect; never raises.

    Blocking body of :func:`_audit_collect_allowed`: the first ``sel()`` of a
    process constructs the log, so handlers reach this through the async
    wrapper, which runs it off the event loop.
    """
    try:
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="diagnostics_collect",
            tool_kind="collect",
            outcome="allowed",
            resources=f"include_logs={include_logs}",
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit for diagnostics collect failed", exc_info=True)


async def _audit_collect_allowed(include_logs: bool) -> None:
    """Record an allowed collect without stalling the event loop."""
    try:
        await asyncio.to_thread(_audit_collect_allowed_sync, include_logs)
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.debug("SEL audit dispatch for diagnostics collect failed", exc_info=True)


async def api_diagnostics_collect(request: web.Request) -> web.Response:
    """POST /api/diagnostics/collect — collect + redact + zip diagnostics.

    Body (all optional): ``{"note": str, "include_logs": bool}``.
    Returns the bundle metadata plus a ``download_url`` and the pre-filled
    GitHub issue URL.
    """
    owner_denied = await require_owner_dashboard_request(request, "diagnostics.collect")
    if owner_denied is not None:
        return owner_denied
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
    # A spelled "false"/"0"/"no"/"off" leaves the logs out; any other value
    # keeps the truthiness it has always been read with.
    raw_include_logs = body.get("include_logs", True)
    include_logs = _coerce_bool(raw_include_logs, bool(raw_include_logs))

    # The owner gate audits only its refusals; record the allowed build here,
    # before the collector reads the owner's logs.
    await _audit_collect_allowed(include_logs)
    try:
        alignment = await asyncio.to_thread(_staged_update_alignment)
        result = await asyncio.to_thread(
            diagnostics.collect_bundle,
            note=note,
            include_logs=include_logs,
            output_dir=_diagnostics_dir(),
            version_alignment=alignment,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("diagnostics collection failed")
        # The dashboard renders ``error`` verbatim into a localized UI, so raw
        # exception text (which can carry paths or internal detail) must not
        # reach the client. The detail stays in the server log above.
        return web.json_response(
            {"error": "collection failed", "code": "collection_failed"},
            status=500,
        )

    payload = result.as_dict()
    payload["download_url"] = "/api/diagnostics/download/" + urllib.parse.quote(
        result.filename, safe=""
    )
    return web.json_response(payload)


def _servable_bundle_path(filename: str) -> Path | None:
    """The bundle ``filename`` names, or ``None`` when it may not be served.

    Blocking: both ``resolve()`` calls and the ``is_file()`` stat touch the
    filesystem, so :func:`api_diagnostics_download` runs this with
    ``asyncio.to_thread``. ``None`` covers a name that escapes the diagnostics
    directory, a non-``.zip`` name and a name that is not a regular file.
    """
    base = _diagnostics_dir().resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base) or path.suffix != ".zip" or not path.is_file():
        return None
    return path


async def api_diagnostics_download(request: web.Request) -> web.StreamResponse:
    """GET /api/diagnostics/download/{filename} — stream a bundle zip.

    Scoped strictly to ``<data_home>/diagnostics``; path traversal and
    non-``.zip`` names are rejected. Served as ``application/zip`` directly, so
    it does not touch the shared binary-MIME allowlist.
    """
    owner_denied = await require_owner_dashboard_request(request, "diagnostics.download")
    if owner_denied is not None:
        return owner_denied
    filename = request.match_info["filename"]
    path = await asyncio.to_thread(_servable_bundle_path, filename)

    if path is None:
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

"""Security Scanner — builtin backend routes.

Registered at gateway startup by ``apps/routes.py:register_app_routes`` via the
manifest ``backend.routes: "backend.routes:register_routes"`` field. Follows the
builtin convention (see issue_radar/code_review_sage): ``register_routes(app)``
hard-codes the ``/api/apps/security-scanner/*`` paths on the router, and every
handler is wrapped in ``_require_enabled`` (deny-by-default, since the app ships
``defaultEnabled: false`` and routes exist even while disabled).

Auth for the ``/api/apps/*`` surface is handled by the dashboard's same-origin
auth middleware, as for every other builtin. Handlers take ``request`` only and
delegate to the pure :class:`~kiro_crew.apps.builtins.security_scanner.lib.service.ScannerService`.

The two mutating routes are read/ingest only. Actually running a scan is launched
by the UI via a background agent slot (``/api/chat?ws=1``) running the
``security-scan`` skill — the backend never itself drives the adversarial
machinery (see SECURITY_NOTES.md).
"""
from __future__ import annotations

import asyncio
import logging
import os
from functools import wraps
from pathlib import Path

from aiohttp import web

from kiro_crew.apps.builtins.security_scanner.lib.service import ScannerService
from kiro_crew.apps.manager import app_data_dir, is_app_enabled

logger = logging.getLogger("kirocrew.app.security-scanner")

APP_NAME = "security-scanner"


def _data_dir() -> Path:
    """Resolve the app's runtime data dir.

    Defaults to the platform-standard app-scoped data dir
    (``$KIROCREW_HOME/apps/security-scanner/data`` via
    :func:`kiro_crew.apps.manager.app_data_dir`), matching every other builtin.
    Overridable via ``SECURITY_SCANNER_DATA`` for tests and isolated pods."""
    override = os.environ.get("SECURITY_SCANNER_DATA")
    if override:
        return Path(os.path.expanduser(override))
    return app_data_dir(APP_NAME)


def _service() -> ScannerService:
    return ScannerService(_data_dir())


def _require_enabled(handler):
    """Deny requests when Security Scanner is disabled (deny-by-default). Routes
    are registered once at startup, so a default-disabled app would otherwise
    stay callable. ``is_app_enabled`` is a synchronous installed.json read, so
    it runs off the event loop."""
    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response({"error": "security-scanner is disabled"}, status=403)
        return await handler(request)

    return _wrapped


# ---- handlers (request-only; delegate to the pure service) ------------------


async def _status(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(lambda: _service().status()))


async def _findings(request: web.Request) -> web.Response:
    status = request.query.get("status")
    topic = request.query.get("topic")
    data = await asyncio.to_thread(lambda: _service().list_findings(status=status, topic=topic))
    return web.json_response({"findings": data})


async def _finding_detail(request: web.Request) -> web.Response:
    finding_id = request.match_info.get("id", "")
    found = await asyncio.to_thread(lambda: _service().get_finding(finding_id))
    if found is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(found)


async def _knowledge(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(lambda: _service().knowledge_overview()))


async def _scans(request: web.Request) -> web.Response:
    data = await asyncio.to_thread(lambda: _service().recent_scans())
    return web.json_response({"scans": data})


async def _ingest(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    text = str(body.get("text", ""))
    topic_hint = str(body.get("topic", ""))
    if not text.strip():
        return web.json_response({"error": "text is required"}, status=400)
    result = await asyncio.to_thread(lambda: _service().ingest_report_text(text, topic_hint))
    return web.json_response(result)


async def _scan_request(request: web.Request) -> web.Response:
    """Record a scan request + report lock state. The scan itself is launched by
    the UI via a background agent slot running the security-scan skill; the
    backend does not drive scans or exploits."""
    svc = _service()
    await asyncio.to_thread(svc.recover_interrupted)
    return web.json_response(
        {
            "running": svc.lock.is_held(),
            "note": "launch the scan from the UI (a background agent slot runs the security-scan skill)",
        }
    )


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application.

    Single-argument signature + hard-coded ``/api/apps/security-scanner/*``
    paths match every other builtin (see issue_radar's register_routes and the
    ``_mod.register_routes(app)`` call site in dashboard/server.py)."""
    p = "/api/apps/security-scanner"
    app.router.add_get(f"{p}/status", _require_enabled(_status))
    app.router.add_get(f"{p}/findings", _require_enabled(_findings))
    app.router.add_get(f"{p}/findings/{{id}}", _require_enabled(_finding_detail))
    app.router.add_get(f"{p}/knowledge", _require_enabled(_knowledge))
    app.router.add_get(f"{p}/scans", _require_enabled(_scans))
    app.router.add_post(f"{p}/knowledge/ingest", _require_enabled(_ingest))
    app.router.add_post(f"{p}/scan", _require_enabled(_scan_request))

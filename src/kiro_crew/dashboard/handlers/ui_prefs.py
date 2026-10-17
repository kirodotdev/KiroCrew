"""Dashboard endpoints for the browser-side UI preference backup.

``GET /api/ui-prefs``  -> ``{"prefs": {key: value}}``
``PUT /api/ui-prefs``  -> body ``{"prefs": {key: value | null}}``, merge patch,
                          returns the merged mapping.

The store is :mod:`kiro_crew.ui_prefs`; see that module for why the backup
exists and why it is not part of ``config.json``. These handlers add only the
wire shape, a single-writer lock, and the request-level validation that turns a
bad patch into a 400 instead of a 500.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.ui_prefs import (
    MAX_REQUEST_BYTES,
    UiPrefsError,
    load_ui_prefs,
    merge_ui_prefs,
)

logger = logging.getLogger(__name__)

#: One writer at a time. ``merge_ui_prefs`` is read-modify-write, so two
#: concurrent PUTs (two tabs flushing at once) would otherwise race and one
#: patch would be lost. Created lazily so importing this module does not need a
#: running loop.
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def api_ui_prefs(request: web.Request) -> web.Response:
    """GET/PUT /api/ui-prefs — read or merge the browser UI preference backup.

    BOTH methods are owner-gated. The write gate is obvious (a viewer must not
    overwrite the owner's settings), but the READ needs one too: these values are
    the owner's, and some of them name real paths on the host (the file
    explorer's saved state, the cloud launch defaults). An authenticated
    non-owner on a shared dashboard would otherwise disclose them with one GET
    and hydrate them into their own browser. Nothing is lost by gating the read:
    a non-owner can never have written a backup, so there is none to fetch.
    """
    denied = await require_owner_dashboard_request(request, f"ui_prefs.{request.method.lower()}")
    if denied is not None:
        return denied

    if request.method == "GET":
        prefs = await asyncio.to_thread(load_ui_prefs)
        return web.json_response({"prefs": prefs})

    # PUT. The gate above runs BEFORE body validation so a non-owner cannot tell
    # a malformed patch from a rejected one. read_bounded_json is the sanctioned
    # reader: it caps the body, enforces the object shape, and catches the two
    # parse failures a bare `await request.json()` lets escape as a 500 — an
    # unknown Content-Type charset (LookupError) and deeply nested JSON
    # (RecursionError).
    body, error = await read_bounded_json(request, max_bytes=MAX_REQUEST_BYTES)
    if error is not None:
        return error
    assert body is not None  # allow_absent=False: a non-None body or an error
    patch = body.get("prefs")
    if not isinstance(patch, dict):
        raise web.HTTPBadRequest(text="'prefs' must be an object")

    async with _get_lock():
        try:
            merged = await asyncio.to_thread(merge_ui_prefs, patch)
        except UiPrefsError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        except OSError as exc:
            # A full or read-only data home must not 500 the dashboard: the
            # client treats a failed flush as "keep the local copy and retry".
            logger.warning("ui-prefs: write failed: %s", exc)
            raise web.HTTPServiceUnavailable(text="could not persist UI preferences") from None

    return web.json_response({"prefs": merged})

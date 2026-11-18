"""``dashboard_capture``: read the caller's own page as the person's open tab shows it.

Two routes, one per half (:mod:`kiro_crew.dashboard.dashboard_capture` holds the
limits):

* ``GET /api/agent-panel/dashboard/capture`` -- the AGENT asks. Strict-internal like
  every route under this prefix, and vetted by :func:`_resolve_dashboard_caller`, the
  gate the other ``dashboard_*`` tools ask, so the page asked for is the caller's own
  and never one named in the request. It sends one ``dashboard_capture`` frame
  (``{request_id, slug}``) to the owner's sockets and waits at most
  ``CAPTURE_WAIT_SECONDS`` for an answer.
* ``POST /api/members/{slug}/dashboard/capture`` -- a TAB answers. Owner-only and
  app tokens refused, like the page read it belongs to. Accepted only for the request
  that is pending for that slug, within the size caps; anything else is refused and
  dropped.

Which tab: every owner tab gets the frame, and only one that is VISIBLE and shows that
Bot's live page answers (the tab's own rule, ``dashboardCapture.ts``). The first valid
answer wins; a later one is refused, so several tabs are never merged. No tab, only
hidden tabs, or no answer in time is ``not_captured`` with ``no_tab``.

READ ONLY: nothing on the page, its record or its versions is written, and no answer
is stored or logged with its content.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from aiohttp import web

import kiro_crew.dashboard.dashboard_capture as _capture
from kiro_crew.dashboard.handlers.agent_panel_dashboard import _resolve_dashboard_caller

logger = logging.getLogger("kiro_crew.dashboard.handlers.agent_panel")

#: A reply body larger than this is refused before it is parsed. Every character at
#: four UTF-8 bytes, plus JSON quoting and keys, stays under it.
CAPTURE_BODY_MAX: Final[int] = (
    4
    * (
        _capture.TEXT_MAX_CHARS
        + _capture.FIELDS_MAX * (_capture.FIELD_LABEL_MAX + _capture.FIELD_VALUE_MAX)
    )
    + 8192
)

#: Said when the caller is a root session, whose page is a template page.
ROOT_SESSION: Final[str] = (
    "a root session's Dashboard is a template page, and only a Bot's own page is captured"
)


#: Capture sends still running after their request was answered, held so the event
#: loop's weak reference is not the only one. Each removes itself when it ends.
_IN_FLIGHT: set[asyncio.Future[int]] = set()


def _not_captured(reason: str) -> web.Response:
    return web.json_response(
        {"capture": {"status": _capture.STATUS_NOT_CAPTURED, "reason": reason}}
    )


async def _deliver(state: Any, request_id: str, slug: str) -> int:
    deliver = getattr(state, "deliver_ws_owners", None)
    if deliver is None:
        return 0
    try:
        return int(
            await deliver(_capture.WS_DASHBOARD_CAPTURE, {"request_id": request_id, "slug": slug})
        )
    except Exception:
        logger.debug("dashboard_capture delivery failed", exc_info=True)
        return 0


async def api_dashboard_capture(request: web.Request) -> web.Response:
    """GET /api/agent-panel/dashboard/capture -- your own page, from the person's tab.

    ``capture.status`` is ``captured`` with ``text``, ``fields`` and ``truncated``;
    ``unavailable`` with the tab's ``reason`` (``static_page``, ``not_loaded``); or
    ``not_captured`` with ``no_tab`` or ``busy``.
    """
    resolved, refusal = await _resolve_dashboard_caller(request, "dashboard_capture")
    if refusal is not None:
        return refusal
    assert resolved is not None
    slug, crew_name, _slot = resolved
    if request.query:
        return web.json_response(
            {"error": "dashboard_capture takes no arguments", "code": "validation_error"},
            status=400,
        )
    if not crew_name:
        return web.json_response(
            {"capture": {"status": _capture.STATUS_NOT_CAPTURED, "reason": ROOT_SESSION}}
        )
    state = request.app["state"]
    hub = _capture.capture_hub_for(state)
    try:
        pending = hub.begin(slug)
    except _capture.CaptureError:
        return _not_captured(_capture.REASON_BUSY)
    # ONE deadline for the send and the answer, raced: an owner socket that is slow to
    # drain must not hold back an answer another tab already gave. The send is left
    # to finish on its own rather than cancelled, because cancelling a socket write
    # half way could break that tab's stream; the hub bounds it.
    delivery = asyncio.ensure_future(_deliver(state, pending.request_id, slug))
    _IN_FLIGHT.add(delivery)
    delivery.add_done_callback(_IN_FLIGHT.discard)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _capture.CAPTURE_WAIT_SECONDS
        waiting: set[asyncio.Future[Any]] = {pending.future, delivery}
        while not pending.future.done():
            left = deadline - loop.time()
            if left <= 0:
                return _not_captured(_capture.REASON_NO_TAB)
            await asyncio.wait(waiting, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            if delivery in waiting and delivery.done():
                waiting.discard(delivery)
                if delivery.result() == 0 and not pending.future.done():
                    return _not_captured(_capture.REASON_NO_TAB)
        result = pending.future.result()
    finally:
        hub.end(pending.request_id)
    return web.json_response({"capture": result})


async def api_member_dashboard_capture(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/dashboard/capture -- one tab's answer to a capture frame."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
    from kiro_crew.dashboard.handlers.members import _deny_app_caller

    denied = await _deny_app_caller(request, "members.dashboard_capture")
    if denied is None:
        denied = await require_owner_dashboard_request(request, "members.dashboard_capture")
    if denied is not None:
        return denied
    slug = request.match_info.get("slug", "")
    if request.content_length is None or request.content_length > CAPTURE_BODY_MAX:
        return web.json_response(
            {"error": "the capture is too large", "code": "capture_too_large"}, status=413
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    try:
        _capture.capture_hub_for(request.app["state"]).deliver(slug, body)
    except _capture.CaptureError as exc:
        return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)
    return web.json_response({"ok": True})

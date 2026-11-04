"""The turn replay's read routes: list a chat's recorded turns, read one, fetch a frame.

Same trust class as the live card's routes (``handlers/browser_live.py``): the
dashboard OWNER's and nobody else's. In order:

* the verified internal secret is REFUSED (403 ``internal_secret_required``): the
  agent must not read back its own recordings through the gateway;
* :func:`require_owner_dashboard_request`: anyone else is 403 ``owner_only``,
  before the session is even parsed, so a refusal says nothing about whether a
  session or a recording exists;
* the session must be a slot this dashboard owns, and the turn and frame must
  exist: otherwise 404 ``not_found``, one code for every miss.

Path parameters are matched against strict patterns before any lookup; the
recording store maps the session onto a hashed directory name and opens files
with ``O_NOFOLLOW`` under its own root, so no parameter reaches a path.

Routes (``GET``, registered in ``routes/browser_live.py``):
``/api/browser/live/{session}/recordings``,
``/api/browser/live/{session}/recordings/{turn_id}`` and
``/api/browser/live/{session}/recordings/{turn_id}/frames/{i}.jpg``.
Every answer is ``Cache-Control: no-store``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from aiohttp import web

from kiro_crew.browser_harness.live_hub import redact_wire
from kiro_crew.browser_harness.recording import FRAME_NAME_RE, TURN_ID_RE, get_recorder
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.browser_policy import browse_refusal
from kiro_crew.dashboard.state import _normalize_slot_key

logger = logging.getLogger(__name__)

#: The slot-key alphabet the card's POST routes accept, bounded the same way.
_SESSION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _json(body: object) -> web.Response:
    return web.json_response(body, headers=_NO_STORE)


def _not_found() -> web.Response:
    return web.json_response(
        {"error": "not found", "code": "not_found"}, status=404, headers=_NO_STORE
    )


def _audit(request: web.Request, operation: str, outcome: str, error: str = "") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=str(request.get("user") or "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.debug("SEL audit for %s failed", operation, exc_info=True)


async def _gate(
    request: web.Request, operation: str, *, audit: bool = True
) -> tuple[str | None, web.Response | None]:
    """The owned slot key the path names, or the refusal to answer instead.

    A ``capabilities.browse`` deny refuses the replay too (``browser_policy``); a
    frame is read unaudited, since one replay reads hundreds.
    """
    if request.get("internal_auth") is True:
        _audit(request, operation, "denied", "internal secret refused")
        return None, web.json_response(
            {"error": "forbidden", "code": "internal_secret_required"},
            status=403,
            headers=_NO_STORE,
        )
    refusal = await require_owner_dashboard_request(request, operation)
    if refusal is not None:
        return None, refusal
    refusal = await browse_refusal(audit=audit)
    if refusal is not None:
        return None, refusal
    raw = request.match_info.get("session", "")
    if not _SESSION_RE.fullmatch(raw):
        return None, _not_found()
    key = _normalize_slot_key(raw)
    slots = getattr(request.app["state"], "_slots", None) or {}
    if not key or key not in slots:
        return None, _not_found()
    return key, None


def _turn(request: web.Request) -> str | None:
    turn_id = request.match_info.get("turn_id", "")
    return turn_id if TURN_ID_RE.fullmatch(turn_id) else None


async def api_browser_recordings_list(request: web.Request) -> web.Response:
    """GET /api/browser/live/{session}/recordings: the chat's recorded turns, oldest first."""
    key, refusal = await _gate(request, "browser_recordings_list")
    if refusal is not None:
        return refusal
    assert key is not None
    turns = await asyncio.to_thread(get_recorder().store.list_turns, key)
    return _json({"turns": await asyncio.to_thread(redact_wire, turns)})


async def api_browser_recording_get(request: web.Request) -> web.Response:
    """GET /api/browser/live/{session}/recordings/{turn_id}: one turn's frames and events."""
    key, refusal = await _gate(request, "browser_recording_get")
    if refusal is not None:
        return refusal
    assert key is not None
    turn_id = _turn(request)
    if turn_id is None:
        return _not_found()
    detail = await asyncio.to_thread(get_recorder().store.turn_detail, key, turn_id)
    if detail is None:
        return _not_found()
    # Step labels, URLs and titles leave through here as well as the live event.
    return _json(await asyncio.to_thread(redact_wire, detail))


async def api_browser_recording_frame(request: web.Request) -> web.Response:
    """GET /api/browser/live/{session}/recordings/{turn_id}/frames/{i}.jpg: one masked frame."""
    key, refusal = await _gate(request, "browser_recording_frame", audit=False)
    if refusal is not None:
        return refusal
    assert key is not None
    turn_id = _turn(request)
    match = FRAME_NAME_RE.fullmatch(request.match_info.get("frame", ""))
    if turn_id is None or match is None:
        return _not_found()
    data = await asyncio.to_thread(get_recorder().store.read_frame, key, turn_id, int(match[1]))
    if data is None:
        return _not_found()
    return web.Response(body=data, content_type="image/jpeg", headers=_NO_STORE)


__all__ = [
    "api_browser_recording_frame",
    "api_browser_recording_get",
    "api_browser_recordings_list",
]

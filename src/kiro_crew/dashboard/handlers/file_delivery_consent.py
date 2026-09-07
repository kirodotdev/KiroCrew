"""Owner-gated dashboard endpoints for flagged-file delivery consent.

The ONLY writer of ``file_delivery_consent.json``. The store sits on the keystone
floor so an agent cannot write it with file tools or a shell form, and there is
deliberately no CLI verb -- a terminal command that records a grant on request is
a grant an automated caller can take, and its guard would have to key on an env
var an in-process agent can unset. That leaves exactly one door, and this module
is it.

Every verb, reads included, is refused to anyone but the dashboard OWNER. Three
callers had to be shut out and only the first is obvious:

* an AGENT: already blocked from the file itself by the keystone fence, but an
  app token declaring this route's permission would be the same door's second
  key, so the check is here too rather than relying on the fence alone.
* an APP token: an app could otherwise mint a grant with no human in the loop.
* an allowed MESSAGING user: a Slack allow-listed non-owner running
  ``!dashboard`` authenticates with ``app == ""``, so an app-only check would let
  them authorize delivery of the OWNER's secrets.

Reads are refused for their own reason: the response says which delivery
destinations the owner has blessed, which tells a caller where a flagged file
would land unrefused. ``is_owner_dashboard_request`` already encodes exactly the
rule needed (app present and empty, caller equal to ``owner_id`` or a local-owner
subject), so it is reused rather than re-derived.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from aiohttp import web

from kiro_crew import file_delivery_consent
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_UNKNOWN_CLASS = "unknown_destination_class"


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER on every consent endpoint.

    Async because the denial is AUDITED, and the audit writes to the security event
    log -- a synchronous disk write, whose first call on a fresh gateway also
    initialises the log. Running that on the event loop would let one refused
    request stall every other request and the heartbeat, so it goes through
    ``asyncio.to_thread`` (``no-blocking-call-on-event-loop``). The owner PREDICATE
    itself is pure and stays inline.
    """
    if is_owner_dashboard_request(request):
        return None
    # Names the calling APP, never a credential -- worded to say so plainly, since
    # "token" in a logger literal reads as a possible secret to the SAST rule.
    logger.warning(
        "refused %s: confirming delivery of scanner-flagged files is a dashboard "
        "owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        file_delivery_consent.audit_decision,
        "*",
        outcome="denied",
        detail=f"{operation}: non-owner caller refused",
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _requested_class(request: web.Request) -> str | None:
    """The grantable destination class named by the query, or ``None``.

    Validated against ``GRANTABLE_CLASSES`` rather than parsed loosely, so a
    request naming one of the never-grantable legs (the Slack or channel upload
    path) is refused here as an unknown class and never reaches the store.
    """
    requested = (request.query.get("destination_class") or "").strip()
    return requested if requested in file_delivery_consent.GRANTABLE_CLASSES else None


def _grant_payload(grant: file_delivery_consent.Grant | None) -> dict[str, object] | None:
    if grant is None:
        return None
    return grant.to_dict()


async def api_file_delivery_consent_get(request: web.Request) -> web.Response:
    """GET /api/file-delivery/consent -- the grantable classes and their consent."""
    denied = await _deny_non_owner(request, "file_delivery_consent.read")
    if denied:
        return denied
    grants = {
        name: _grant_payload(await asyncio.to_thread(file_delivery_consent.read_grant, name))
        for name in sorted(file_delivery_consent.GRANTABLE_CLASSES)
    }
    return web.json_response(
        {
            "ok": True,
            "grantable": sorted(file_delivery_consent.GRANTABLE_CLASSES),
            # Surfaced so the settings panel can SAY that the upload legs are
            # permanently excluded rather than merely omitting them, which reads
            # as an oversight.
            "never_grantable": sorted(file_delivery_consent.NEVER_GRANTABLE_CLASSES),
            "labels": file_delivery_consent.CLASS_LABELS,
            "grants": grants,
        }
    )


async def api_file_delivery_consent_post(request: web.Request) -> web.Response:
    """POST /api/file-delivery/consent -- record the owner's confirmation."""
    denied = await _deny_non_owner(request, "file_delivery_consent.grant")
    if denied:
        return denied
    destination_class = _requested_class(request)
    if destination_class is None:
        return web.json_response(
            {"error": "unknown destination class", "code": _CODE_UNKNOWN_CLASS}, status=400
        )
    granted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    grant = await asyncio.to_thread(
        file_delivery_consent.record_grant, destination_class, granted_at=granted_at
    )
    return web.json_response({"ok": True, "grant": grant.to_dict()})


async def api_file_delivery_consent_delete(request: web.Request) -> web.Response:
    """DELETE /api/file-delivery/consent -- withdraw a recorded confirmation."""
    denied = await _deny_non_owner(request, "file_delivery_consent.revoke")
    if denied:
        return denied
    destination_class = _requested_class(request)
    if destination_class is None:
        return web.json_response(
            {"error": "unknown destination class", "code": _CODE_UNKNOWN_CLASS}, status=400
        )
    removed = await asyncio.to_thread(file_delivery_consent.revoke, destination_class)
    return web.json_response({"ok": True, "removed": removed})

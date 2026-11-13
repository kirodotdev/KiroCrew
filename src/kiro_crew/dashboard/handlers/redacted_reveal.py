"""``POST /api/security/redacted-value`` -- the owner reveals a removed credential.

The credential redactor removes a secret from an agent reply before it is
saved; ``security.reveal_store`` keeps that value in gateway memory, and this
route is its ONE reader. The rule it enforces (RFC *Owner reveal*): redacted
values may be revealed only to the owner through the browser; no
agent-reachable path may ever return them; originals never touch disk.

So the gate refuses, before anything is read:

- an internal caller (the loopback secret every MCP tool, agent, crewmate,
  sub-agent and cron uses), whatever identity it claims;
- an app token or an app-scoped request;
- anything that is not a dashboard-user credential;
- a dashboard user who is not the owner (an allow-listed Slack teammate
  running ``!dashboard``, a relay teammate).

Every allowed reveal is written to the security event log SYNCHRONOUSLY
before the value is returned; when the record cannot be written the value is
not returned. The record names who, when and which handle -- never the value.

Known limit, the same one the redaction switch states: the owner gate is an
identity check, not proof a human clicked. An agent driving the owner's own
logged-in browser through computer use could press Show; that reveal is still
audited. An agent the owner runs without the sandbox can already read the
owner's dashboard credential from disk, so it is out of reach of any route.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.security import reveal_store

logger = logging.getLogger(__name__)

_SOURCE = "redacted-reveal"
_MAX_SLOT_CHARS = 512
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _refusal(request: web.Request) -> str:
    """Why this caller may not reveal, or ``""`` for the owner's browser."""
    if request.get("internal_auth") is True or "X-Internal-Secret" in request.headers:
        return "internal caller"
    if request.get("app"):
        return "app caller"
    if request.get("is_dashboard_user") is not True:
        return "not a dashboard user"
    if not is_owner_dashboard_request(request):
        return "not the owner"
    return ""


def _audit(*, caller: str, outcome: str, detail: str, critical: bool) -> bool:
    """One SEL row for a reveal or a refusal; never carries the value."""
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation="redacted_value.reveal",
            outcome=outcome,
            source=_SOURCE,
            resources=detail[:200],
            critical=critical,
        )
        return True
    except Exception:
        logger.debug("could not write the reveal audit event", exc_info=True)
        return False


def _gone() -> web.Response:
    return web.json_response(
        {
            "error": "this value is no longer available; check its source",
            "code": "reveal_unavailable",
        },
        status=410,
        headers=_NO_STORE,
    )


async def api_owner_value_reveal(request: web.Request) -> web.Response:
    """Return ``{"value": ...}`` for one handle, to the owner's browser only."""
    caller = str(request.get("user") or "anonymous")
    why = _refusal(request)
    if why:
        logger.warning("refused a redacted-value reveal: %s (app=%s)", why, request.get("app"))
        await asyncio.to_thread(_audit, caller=caller, outcome="denied", detail=why, critical=False)
        return _owner_denial_response(
            request, "dashboard owner required", "dashboard_owner_required"
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    slot = body.get("slot") if isinstance(body, dict) else None
    handle = body.get("id") if isinstance(body, dict) else None
    if (
        not isinstance(slot, str)
        or not slot
        or len(slot) > _MAX_SLOT_CHARS
        or not isinstance(handle, str)
        or not reveal_store.REVEAL_ID_RE.match(handle)
    ):
        return web.json_response({"error": "invalid body", "code": "invalid_body"}, status=400)
    value = reveal_store.STORE.reveal(handle, scope=slot)
    if value is None:
        await asyncio.to_thread(
            _audit, caller=caller, outcome="unavailable", detail=f"id={handle}", critical=False
        )
        return _gone()
    recorded = await asyncio.to_thread(
        _audit, caller=caller, outcome="allowed", detail=f"id={handle} slot={slot}", critical=True
    )
    if not recorded:
        return web.json_response(
            {
                "error": "the security event log is unavailable, so the value stays hidden",
                "code": "reveal_audit_unavailable",
            },
            status=503,
            headers=_NO_STORE,
        )
    return web.json_response({"value": value}, headers=_NO_STORE)

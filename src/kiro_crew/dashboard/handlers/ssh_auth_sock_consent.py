"""Owner-gated dashboard endpoints for the ``SSH_AUTH_SOCK`` forward consent.

The ONLY writer of ``ssh_auth_sock_consent.json`` in the tree (a hand edit from
outside the sandbox still works and reads identically). The store sits on the
keystone floor so an agent cannot write it with file tools or a shell form, and
this handler opens the path directly rather than through the agent tool gate --
the same one-act shape ``handlers/aws_consent.py`` uses for
``aws_service_consent.json``. There is no CLI verb that records a grant.

Every verb is refused to anyone but the dashboard OWNER: an app token would
otherwise mint a grant with no human in the loop, and an allow-listed messaging
non-owner running ``!dashboard`` authenticates with an empty app. Reads are
refused too: the response says whether the owner's ssh-agent is reachable from
inside a session, which is reconnaissance.

Routes and their shapes (the SPA's contract):

* ``GET    /api/ssh-agent/consent`` -> ``{granted, granted_at, socket_present,
  forwarded_without_consent, forwarded_harnesses, harness_labels}``
* ``POST   /api/ssh-agent/consent`` -> ``{granted: true, granted_at}``
* ``DELETE /api/ssh-agent/consent`` -> ``{granted: false}``
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

from aiohttp import web

from kiro_crew import ssh_auth_sock_consent
from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import (
    is_owner_dashboard_request,
)

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_GRANT_WRITE_FAILED = "ssh_agent_grant_write_failed"
_CODE_WITHDRAW_FAILED = "ssh_agent_withdraw_failed"


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER on every consent endpoint.

    Async because the denial is AUDITED, and the audit is a synchronous disk write
    that goes through ``asyncio.to_thread`` so one refused request cannot stall
    the event loop. The owner PREDICATE itself is pure and stays inline.
    """
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: forwarding the ssh-agent socket into sessions is a dashboard "
        "owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await asyncio.to_thread(
        ssh_auth_sock_consent.audit_decision,
        outcome="refused",
        detail=f"{operation}: non-owner caller refused",
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _consent_view() -> dict[str, object]:
    grant = ssh_auth_sock_consent.read_grant()
    harnesses = ssh_auth_sock_consent.forwarding_harnesses()
    return {
        "granted": grant is not None,
        "granted_at": (grant.granted_at or None) if grant is not None else None,
        "socket_present": ssh_auth_sock_consent.socket_present(),
        "forwarded_without_consent": ssh_auth_sock_consent.forwarded_without_consent(harnesses),
        "forwarded_harnesses": harnesses,
        "harness_labels": ssh_auth_sock_consent.harness_labels(),
    }


async def api_ssh_agent_consent_get(request: web.Request) -> web.Response:
    """GET /api/ssh-agent/consent -- the grant and whether there is a socket to forward."""
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.read")
    if denied:
        return denied
    return web.json_response(await asyncio.to_thread(_consent_view))


async def api_ssh_agent_consent_post(request: web.Request) -> web.Response:
    """POST /api/ssh-agent/consent -- record the owner's consent.

    Answers only after the keystone write landed: a 200 that the store did not
    keep would show "Allowed" while the next spawn still scrubs the socket.
    """
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.grant")
    if denied:
        return denied
    granted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        grant = await asyncio.to_thread(ssh_auth_sock_consent.record_grant, granted_at=granted_at)
    except OSError as exc:
        logger.warning("ssh-agent consent grant write failed: %s", exc)
        await asyncio.to_thread(
            ssh_auth_sock_consent.audit_decision,
            outcome="refused",
            detail=f"grant: storage error ({exc.__class__.__name__})",
        )
        return web.json_response(
            {
                "error": "could not record the grant (a storage write failed); try again",
                "code": _CODE_GRANT_WRITE_FAILED,
            },
            status=500,
        )
    return web.json_response({"granted": True, "granted_at": grant.granted_at})


async def api_ssh_agent_consent_delete(request: web.Request) -> web.Response:
    """DELETE /api/ssh-agent/consent -- withdraw the grant.

    Answers ``{"granted": false}``, 200 even when nothing was recorded. A storage
    failure is a 500, never a 200: a withdrawal the store did not keep would tell
    the owner forwarding stopped while the next spawn still forwards.
    """
    denied = await _deny_non_owner(request, f"{ssh_auth_sock_consent.AUDIT_EVENT}.revoke")
    if denied:
        return denied
    try:
        await asyncio.to_thread(ssh_auth_sock_consent.revoke)
    except OSError as exc:
        logger.warning("ssh-agent consent withdraw failed: %s", exc)
        await asyncio.to_thread(
            ssh_auth_sock_consent.audit_decision,
            outcome="refused",
            detail=f"withdraw: storage error ({exc.__class__.__name__})",
        )
        return web.json_response(
            {
                "error": (
                    "could not withdraw the grant (a storage write failed); "
                    "forwarding may still be allowed -- try Withdraw again"
                ),
                "code": _CODE_WITHDRAW_FAILED,
            },
            status=500,
        )
    return web.json_response({"granted": False})

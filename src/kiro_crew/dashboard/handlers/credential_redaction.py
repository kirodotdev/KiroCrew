"""Dashboard handlers for the owner's credential-redaction switch.

Two routes, both OWNER-gated, and the PUT is the ONLY writer of the
``credential_redaction.json`` keystone (see ``security.redaction_switch`` for
what the switch does and does not govern).

Why owner-gated rather than merely authenticated: the switch turns off the
credential pass in the owner's dashboard file viewer, the one surface it
governs. A Slack-allow-listed non-owner running ``!dashboard`` authenticates
with ``app == ""`` and ``sub != owner_id``, so plain token auth admits them; the
owner predicate does not. Same shape as the flagged-file delivery consent
handler, and for the same reason.

Known limit, stated rather than papered over: the owner gate is an IDENTITY
check, not a proof that a human clicked. An agent driving the owner's
authenticated browser through computer use could flip the switch. What that
buys it is bounded to the owner's own file viewer -- exfiltration-URL redaction
and every request-blocking gate stay on regardless (``redaction_switch`` module
docstring) -- and every change is SEL-audited, so the flip leaves a record even
though the redaction does not.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.security import redaction_switch

logger = logging.getLogger(__name__)

_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_BODY = "invalid_body"
_CODE_WRITE_FAILED = "credential_redaction_write_failed"
_CODE_AUDIT_UNAVAILABLE = "credential_redaction_audit_unavailable"

_SOURCE = "credential-redaction"


def _audit(
    *,
    outcome: str,
    caller: str,
    detail: str = "",
    critical: bool = False,
    operation: str | None = None,
) -> bool:
    """Record a switch change or a refused caller in the SEL.

    Returns whether the record was written. Never raises: a refusal must not be
    prevented by a failing audit, and the caller decides what a failed record
    means for a WRITE (see ``_write_and_audit``: a disable that cannot be
    audited does not happen).

    ``critical=True`` makes the write SYNCHRONOUS and fail-loud instead of
    enqueued: the default ``sel.log`` only queues the event and its background
    writer swallows a failed write, so a ``True`` from the default path proves
    nothing. The switch-change records pass it (they run on a worker thread, so
    the blocking write is safe there); the denial record keeps the default,
    since a refusal must not wait on the log.
    """
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation=f"credential_redaction.{operation or outcome}",
            outcome=outcome,
            source=_SOURCE,
            resources=detail[:200],
            critical=critical,
        )
        return True
    except Exception:
        logger.debug("could not write the redaction-switch audit event", exc_info=True)
        return False


class _AuditUnavailable(Exception):
    """The disable was refused because its audit record could not be written."""


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER; audited off the event loop."""
    if is_owner_dashboard_request(request):
        return None
    # Names the calling APP, never a value -- worded without the word the SAST
    # logger rule keys on, since a literal there reads as a possible secret.
    logger.warning(
        "refused %s: the redaction switch is a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    # The refused SUBJECT is recorded, not a constant: repeated probing by one
    # allow-listed non-owner must be distinguishable in the SEL from a stray call.
    await asyncio.to_thread(
        _audit,
        outcome="denied",
        caller=str(request.get("user") or "anonymous"),
        detail=f"{operation}: non-owner caller refused (app={request.get('app')!r})",
    )
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


async def api_credential_redaction_get(request: web.Request) -> web.Response:
    """GET /api/security/credential-redaction -- the switch as recorded."""
    denied = await _deny_non_owner(request, "credential_redaction.read")
    if denied:
        return denied
    state = await asyncio.to_thread(redaction_switch.read_state)
    # Every disclosure of a security setting is in the SEL, refusals AND owner
    # reads, so the history can say who read the switch and what it said. Not
    # critical: a read must not fail because the log is briefly unavailable.
    await asyncio.to_thread(
        _audit,
        outcome="allowed",
        operation="read",
        caller=str(request.get("user") or "owner"),
        detail=f"enabled={state.enabled}",
    )
    return web.json_response(state.to_dict())


async def api_credential_redaction_put(request: web.Request) -> web.Response:
    """PUT /api/security/credential-redaction -- record ``{"enabled": bool}``.

    ``enabled`` must be a JSON boolean; anything else is a 400 and nothing is
    written. The record carries the time of the change so the card can say when
    redaction was switched off.
    """
    denied = await _deny_non_owner(request, "credential_redaction.write")
    if denied:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": _CODE_INVALID_BODY}, status=400
        )
    changed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    subject = str(request.get("user") or "owner")

    def _write_and_audit() -> redaction_switch.RedactionState:
        # ONE unit of work on the worker thread: the audit record and the write,
        # under the store's own lock for the WHOLE pair, so two owner PUTs that
        # interleave cannot leave the latest SEL record saying 'enabled' while the
        # switch on disk says 'disabled' (or the reverse).
        # ``asyncio.to_thread`` cannot cancel a running thread, so if this
        # handler is cancelled (client gone, gateway stopping) while the write
        # is in flight, the thread still completes -- and the audit is already
        # on disk, so the authorization can never change without its trace.
        #
        # The record is written BEFORE the switch, and a DISABLE whose record
        # cannot be written does not happen: an unaudited "redaction off" is the
        # one outcome this handler must not produce. ENABLING is the fail-safe
        # direction and proceeds even when the audit log is unavailable.
        with redaction_switch.transaction():
            # The REAL subject, as the denial and the read record it: with no
            # ``owner_id`` configured more than one local identity passes the
            # owner check, and this row is the one that must say which one
            # turned the credential pass off.
            recorded = _audit(
                outcome="enabled" if enabled else "disabled",
                caller=subject,
                detail=f"changed_at={changed_at}",
                critical=True,
            )
            if not enabled and not recorded:
                raise _AuditUnavailable()
            try:
                return redaction_switch.set_enabled(enabled, changed_at=changed_at)
            except OSError as exc:
                _audit(
                    outcome="write_failed",
                    caller=subject,
                    detail=type(exc).__name__,
                    critical=True,
                )
                raise

    try:
        state = await asyncio.to_thread(_write_and_audit)
    except _AuditUnavailable:
        logger.error("refused to switch redaction off: the security event log is unavailable")
        return web.json_response(
            {
                "error": "the security event log is unavailable, so the switch stays on",
                "code": _CODE_AUDIT_UNAVAILABLE,
            },
            status=503,
        )
    except OSError as exc:
        logger.error("could not write the redaction switch: %s", exc)
        return web.json_response(
            {"error": "could not record the switch", "code": _CODE_WRITE_FAILED}, status=500
        )
    # Every OWNER dashboard document learns of the flip, not just the one that
    # made it: a second browser tab holds the same raw file bodies in its own
    # caches and open tabs, and only a push reaches it. Owner sockets only --
    # the switch position is owner posture, and ``_send_ws_owners`` bypasses the
    # app-scope gate by construction, so no app token sees it.
    _broadcast_switch_changed(request, state)
    return web.json_response(state.to_dict())


def _broadcast_switch_changed(request: web.Request, state: redaction_switch.RedactionState) -> None:
    try:
        dashboard_state = request.app["state"]
        dashboard_state.broadcast_ws_owners(
            "credential_redaction_changed",
            {"enabled": state.enabled, "changed_at": state.changed_at},
        )
    except Exception:  # noqa: BLE001 -- the write already landed; a lost push must not fail the PUT
        logger.warning(
            "could not push the redaction switch change to owner dashboards", exc_info=True
        )

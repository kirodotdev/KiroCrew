"""Decision-seam consent REST API -- the operator's switch for Jev egress.

``GET /api/decisions/consent``   the keystone plus the endpoint config names now
``PUT /api/decisions/consent``   ``{"enabled": bool}`` -> writes it, bound to that endpoint

Consent is bound to a destination: enabling records the provider endpoint the
config names at that moment, and the gate sends only while the two still agree.
The GET returns both so the card can show the owner WHERE consent would send, and
say so when a later config edit moved the destination out from under it.

This handler is the ONLY writer of ``decisions_consent.json``, and that is what
makes "the agent cannot switch on the egress of its own conversation" true: the
keystone leaf is on ``security._CREW_SECRET_LEAVES`` and mounted read-only in
every sandbox, and this handler opens the path directly rather than through the
agent tool gate. The same shape as ``handlers/aws_consent.py``, for the same
class of decision: consent to send the operator's data to a paid external
service.

**Dashboard OWNER only**, on the read as well as the write. An app token would
otherwise let an agent that can author an app manifest mint a token and flip the
switch it cannot write as a file; a Slack allow-listed non-owner authenticates
with ``app == ""`` and would otherwise consent on the owner's behalf. Reads are
refused too so a non-owner cannot learn whether the owner's messages are being
sent off the machine.

Blocking work is offloaded: the read and the atomic write touch the filesystem,
and the SEL audit can too when the boot-time warm failed, so none of them runs on
the event loop. Every outcome is audited, the successful read included.

The ``kiro_crew.decisions`` package is imported inside the handlers, not at module
top: this module is imported on the gateway boot path (``handlers/__init__``), and
the seam is an optional subsystem that is off until the owner consents, so its
import is paid on the first request that needs it (``no-new-work-on-gateway-boot-path``).
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import _owner_denial_response
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

logger = logging.getLogger(__name__)

#: Machine-readable error codes, per the dashboard error-code contract
#: (``test/test_error_code_contract.py``).
_CODE_OWNER_REQUIRED = "dashboard_owner_required"
_CODE_INVALID_JSON = "invalid_json"
_CODE_INVALID_BODY = "decisions_consent_invalid_body"
_CODE_CORRUPT = "decisions_consent_corrupt"
_CODE_ENDPOINT_CHANGED = "decisions_consent_endpoint_changed"

OP_CONSENT_GET = "decisions_consent_get"
OP_CONSENT_PUT = "decisions_consent_put"


def _sel():
    """Late-binding ``sel()``: the package import is the circular-import exception
    every sibling handler uses, and resolving per call lets tests monkeypatch it."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811 -- circular import

    return _pkg.sel()


async def _audit(
    request: web.Request,
    *,
    operation: str,
    outcome: str,
    error: str = "",
    resources: str = "decisions_consent.json",
) -> None:
    """Best-effort SEL audit; a logging failure never breaks the request.

    Off the event loop when it could block: ``sel()`` is a plain attribute read
    once the boot-time warm succeeded, but a FAILED warm makes the next call retry
    ``_init_locked`` -- key load, a tail read of the log -- on the caller's thread.
    Same gate and hop as ``server._audit_middleware_denial``: two attribute reads
    on the healthy path, a worker thread on the degraded one
    (``no-blocking-call-on-event-loop``).
    """
    from kiro_crew.sel import sel_is_warm

    def _write() -> None:
        _sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation=operation,
            outcome=outcome,
            source="dashboard",
            resources=resources,
            error=error,
        )

    try:
        if sel_is_warm():
            _write()
        else:
            await asyncio.to_thread(_write)
    except Exception:
        logger.warning("SEL logging failed for %s", operation, exc_info=True)


async def _deny_non_owner(request: web.Request, operation: str) -> web.Response | None:
    """Refuse anyone but the dashboard OWNER; see the module docstring for why."""
    if is_owner_dashboard_request(request):
        return None
    logger.warning(
        "refused %s: decision-seam consent is a dashboard owner action (app=%s)",
        operation,
        request.get("app"),
    )
    await _audit(request, operation=operation, outcome="denied", error="non-owner caller refused")
    return _owner_denial_response(request, "dashboard owner required", _CODE_OWNER_REQUIRED)


def _payload(state: dict) -> dict:
    """What both verbs return: the keystone, and the endpoint config names now."""
    from kiro_crew.decisions import consent
    from kiro_crew.decisions import gate as _gate

    configured = _gate.configured_endpoint()
    return {
        "enabled": consent.is_enabled(state),
        "endpoint": consent.consented_endpoint(state),
        "configured_endpoint": configured,
        # Whether a decision would actually be sent right now: consent given, and
        # for THIS address. False with enabled=true is the redirected-config state.
        "permits": consent.permits(configured, state),
    }


async def api_decisions_consent_get(request: web.Request) -> web.Response:
    """GET /api/decisions/consent -- whether, and for which endpoint, the owner consented."""
    denied = await _deny_non_owner(request, OP_CONSENT_GET)
    if denied is not None:
        return denied
    from kiro_crew.decisions import consent

    state = await asyncio.to_thread(consent.load_state)
    payload = _payload(state)
    # Read audited too: WHO learned whether the owner's messages leave the machine
    # is itself a fact an auditor needs, and it pairs with the denied-read row so
    # the log shows every read of the switch, not only the refused ones.
    await _audit(
        request,
        operation=OP_CONSENT_GET,
        outcome="allowed",
        resources=f"decisions_consent.json endpoint={payload['configured_endpoint']}",
    )
    return web.json_response(payload)


async def api_decisions_consent_put(request: web.Request) -> web.Response:
    """PUT /api/decisions/consent -- record ``{"enabled": bool}`` on the keystone.

    Enabling must ECHO the endpoint the owner reviewed (``endpoint`` in the body,
    the ``configured_endpoint`` the GET showed). The config is agent-writable, so
    between the owner's read and their click an agent could point
    ``provider.endpoint`` elsewhere; binding to what the server reads at PUT time
    would then consent to an address the owner never saw. A mismatch is ``409``
    and nothing is written; the card re-reads and shows the new address.

    Outcomes: ``200`` with the new state; ``400`` for a body that is not a JSON
    object carrying a boolean ``enabled`` (plus a string ``endpoint`` when
    enabling); ``403`` for a non-owner; ``409`` when the echoed endpoint is not
    the one config names now; ``500`` for a corrupt keystone, which is left
    byte-identical rather than clobbered (the ``StateCorruptError`` precedent in
    ``handlers/computer_use.py``).
    """
    denied = await _deny_non_owner(request, OP_CONSENT_PUT)
    if denied is not None:
        return denied
    from kiro_crew.decisions import consent
    from kiro_crew.decisions import gate as _gate

    try:
        body = await request.json()
    except Exception:
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_json")
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    # A strict bool, for the same reason the keystone read is a strict identity
    # test: ``"true"`` and ``1`` are not consent.
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
        return web.json_response(
            {"error": 'body must be {"enabled": true|false}', "code": _CODE_INVALID_BODY},
            status=400,
        )

    # Bound to the endpoint the owner REVIEWED, checked against the one the
    # config names now. Equal: consent is for the address on screen, and the one
    # the gate will hold the config to afterwards. Different: the config moved
    # under the owner's review, so refuse and let the card show the new address.
    endpoint = _gate.configured_endpoint()
    if enabled:
        reviewed = consent.normalize_endpoint(body.get("endpoint"))
        if not reviewed:
            await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
            return web.json_response(
                {
                    "error": 'enabling needs the reviewed "endpoint" echoed back',
                    "code": _CODE_INVALID_BODY,
                },
                status=400,
            )
        if reviewed != endpoint:
            await _audit(
                request, operation=OP_CONSENT_PUT, outcome="denied", error="endpoint_changed"
            )
            return web.json_response(
                {
                    "error": "the provider endpoint changed since it was reviewed; read it again",
                    "code": _CODE_ENDPOINT_CHANGED,
                    "configured_endpoint": endpoint,
                },
                status=409,
            )
    try:
        state = await asyncio.to_thread(consent.save_enabled, enabled, endpoint=endpoint)
    except consent.ConsentCorruptError as exc:
        await _audit(request, operation=OP_CONSENT_PUT, outcome="error", error="corrupt")
        return web.json_response(
            {"error": f"decisions_consent.json is unreadable: {exc}", "code": _CODE_CORRUPT},
            status=500,
        )
    # Written and audited as the security decision it is: which way the switch
    # went is the one fact an auditor reconstructing "when did egress start" needs.
    # The endpoint travels in the row: "when did egress start, and to where" is
    # the pair an auditor needs.
    await _audit(
        request,
        operation=OP_CONSENT_PUT,
        outcome="granted" if enabled else "revoked",
        resources=f"decisions_consent.json endpoint={endpoint}",
    )
    return web.json_response(_payload(state))

"""Decision-seam REST API -- the operator's switch for Jev egress, and the strip.

``GET  /api/decisions/consent``   the keystone plus the endpoint config names now
``PUT  /api/decisions/consent``   ``{"enabled": bool}`` -> writes it, bound to that endpoint
``POST /api/decisions/feedback``  a person's verdict on one turn -> one appended log row

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

**Dashboard OWNER only**, on the read as well as the write, and on all four
routes. An app token would otherwise let an agent that can author an app manifest
mint a token and flip the switch it cannot write as a file; a Slack allow-listed
non-owner authenticates with ``app == ""`` and would otherwise consent on the
owner's behalf. Reads are refused too so a non-owner cannot learn whether the
owner's messages are being sent off the machine.

The same gate covers the strip's two routes, for reasons of their own. The
feedback route is a WRITER of the decision log, so an app token that could reach
it could grow that file and pollute the record the operator reads; and the summary
route reports how the operator's own conversations were decided, which is the same
class of fact as whether they are being sent at all.

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
_CODE_FEEDBACK_INVALID_BODY = "decisions_feedback_invalid_body"
#: The append did not land -- a full day-file, a read-only home, a directory
#: someone chmod-ed. 503 rather than 500: the request was valid and the caller may
#: retry once the operator has made room, which is exactly what the WARNING the
#: writer logs tells them to do.
_CODE_FEEDBACK_NOT_RECORDED = "decisions_feedback_not_recorded"

OP_CONSENT_GET = "decisions_consent_get"
OP_CONSENT_PUT = "decisions_consent_put"
OP_FEEDBACK = "decisions_feedback_post"


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
        # The prior-conversation CEILING the owner reviewed. Reported so the card can
        # say what was consented to rather than what config.json currently asks for
        # -- those differ exactly when an agent has raised the config value, which is
        # the case this ceiling exists to make harmless.
        "history_budget_chars": consent.consented_history_budget(state),
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

    ``history_budget_chars`` records the prior-conversation CEILING the owner
    reviewed, and it is here for the same reason ``endpoint`` is: the value in force
    lives in agent-writable ``config.json``, so a budget recorded only there could be
    raised by the agent whose conversation would then be sent. The gate takes the
    smaller of the two.

    It is optional, and an omitted field PRESERVES the recorded ceiling rather than
    clearing it. The consent card sends ``enabled`` and ``endpoint`` only, so a
    default of 0 would make an ordinary switch flip erase a ceiling recorded through
    this same route. The two facts stay independent: the switch says whether the seam
    may send, the ceiling says how much prior conversation it may carry, and each
    moves only when its own field is present. Disabling still clears the ceiling,
    because a later enable must not inherit a budget nobody re-reviewed.

    Outcomes: ``200`` with the new state; ``400`` for a body that is not a JSON
    object carrying a boolean ``enabled`` (plus a string ``endpoint`` when
    enabling, and a non-negative whole ``history_budget_chars`` when present);
    ``403`` for a non-owner; ``409`` when the echoed endpoint is not the one config
    names now; ``500`` for a corrupt keystone, which is left byte-identical rather
    than clobbered (the ``StateCorruptError`` precedent in
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

    # A whole non-negative number, or absent. Validated rather than coerced for the
    # reason every value on this route is: it is written into a security record, so
    # a value nobody can read back as what the owner reviewed is refused, not
    # rounded. A bool is not a budget.
    #
    # Absent is handed on as ``KEEP_HISTORY_BUDGET`` rather than resolved here: the
    # writer resolves it inside its own read-modify-write, so the ceiling written comes
    # from the same read the write is based on. Reading it on this side instead would
    # hold a number read before a concurrent PUT lowered it, and write that number back
    # -- restoring an egress limit somebody just reduced.
    budget = (
        body.get("history_budget_chars", consent.KEEP_HISTORY_BUDGET)
        if isinstance(body, dict)
        else 0
    )
    if budget is not consent.KEEP_HISTORY_BUDGET and (
        isinstance(budget, bool) or not isinstance(budget, int) or budget < 0
    ):
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
        return web.json_response(
            {
                "error": '"history_budget_chars" must be a whole number of 0 or more',
                "code": _CODE_INVALID_BODY,
            },
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
        state = await asyncio.to_thread(
            consent.save_enabled, enabled, endpoint=endpoint, history_budget_chars=budget
        )
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
        resources=(
            f"decisions_consent.json endpoint={endpoint} "
            f"history_budget_chars={consent.consented_history_budget(state)}"
        ),
    )
    return web.json_response(_payload(state))


async def api_decisions_feedback(request: web.Request) -> web.Response:
    """POST /api/decisions/feedback -- record one verdict about one turn.

    Body: ``{"turn_id": str, "verdict": "right"|"wrong"|null, "side": "jev"|"baseline"}``.
    ``verdict: null`` is a real value and means the person TOOK BACK an earlier
    verdict, which the log has to be able to say; ``side`` names which of the two
    answers the verdict is about and is required for every verdict, including a
    cleared one, because a row that names no side names no decision.

    Writes exactly one APPENDED row and never touches an existing one. A verdict
    is a second event about the turn, not a correction of the row that recorded
    it, so a changed mind reads as two rows with two timestamps -- which is what
    makes "when did they change their mind" answerable at all. The append goes
    through the log's own writer, off the event loop, so it inherits the pinned
    destination, the per-file size ceiling and the day-file retention sweep rather
    than re-implementing any of them
    (:mod:`kiro_crew.decisions.log`, ``no-blocking-call-on-event-loop``).

    Outcomes: ``200 {"ok": true}``; ``400`` for a body that is not a JSON object
    with a non-empty ``turn_id``, a PRESENT ``verdict`` key in the allowed set (or
    ``null``) and a side in the allowed set; ``403`` for a non-owner; ``503
    decisions_feedback_not_recorded`` when the append did not land. An omitted
    ``verdict`` is a ``400`` and not a retract: ``null`` is the retract, so a body
    that dropped the field would otherwise record one.

    That last one is the difference between this caller and every other writer of
    the decision log. Elsewhere a dropped row is an observation nobody promised,
    so ``log.append`` swallows it. Here the row IS the person's answer, and the
    day-file has a size ceiling that the turn's own rows share: on a busy sampled
    day the ceiling is reached by round traffic, and a ``200`` would tell the
    owner their verdict was recorded while nothing was written. So the append's
    verdict is read and reported, and the SEL row says ``denied`` for it -- a
    refusal that audits as success is the same lie one layer down.
    """
    denied = await _deny_non_owner(request, OP_FEEDBACK)
    if denied is not None:
        return denied
    from kiro_crew.decisions import log as _log

    try:
        body = await request.json()
    except Exception:
        await _audit(
            request,
            operation=OP_FEEDBACK,
            outcome="denied",
            error="invalid_json",
            resources="decisions log",
        )
        return web.json_response({"error": "invalid JSON", "code": _CODE_INVALID_JSON}, status=400)
    if not isinstance(body, dict):
        body = {}
    turn_id = body.get("turn_id")
    verdict = body.get("verdict")
    side = body.get("side")
    # Validated, not coerced. A row filed under a turn nobody can name, or
    # carrying a verdict outside the pair the reader folds, is a row that makes the
    # summary wrong rather than one that makes it incomplete -- so it is refused
    # here instead of being written and skipped later.
    #
    # ``verdict`` must be PRESENT, and that is a separate clause from its value
    # because ``null`` is a real verdict here: it is how somebody takes an earlier
    # one back. Reading an absent key as that same retract would append an event
    # nobody sent, off a body that just lost the field -- so absence is a refusal
    # and only a written ``null`` clears. The keystone writer draws the same line
    # with ``consent.KEEP_HISTORY_BUDGET``, for the same reason: no in-band value
    # can also mean "not asked".
    valid = (
        isinstance(turn_id, str)
        and turn_id.strip() != ""
        and "verdict" in body
        and (verdict is None or verdict in _log.FEEDBACK_VERDICTS)
        and side in _log.FEEDBACK_SIDES
    )
    if not valid:
        await _audit(
            request,
            operation=OP_FEEDBACK,
            outcome="denied",
            error="invalid_body",
            resources="decisions log",
        )
        return web.json_response(
            {
                "error": (
                    'body must be {"turn_id": str, "verdict": "right"|"wrong"|null, '
                    '"side": "jev"|"baseline"}'
                ),
                "code": _CODE_FEEDBACK_INVALID_BODY,
            },
            status=400,
        )
    row = _log.build_feedback_row(turn_id=turn_id, verdict=verdict, side=side)
    written = await asyncio.to_thread(_log.append, row)
    if not written:
        await _audit(
            request,
            operation=OP_FEEDBACK,
            outcome="denied",
            error="not_recorded",
            resources=f"decisions log verdict={row['verdict']} side={row['side']}",
        )
        return web.json_response(
            {
                "error": "the verdict could not be written to the decision log",
                "code": _CODE_FEEDBACK_NOT_RECORDED,
            },
            status=503,
        )
    await _audit(
        request,
        operation=OP_FEEDBACK,
        outcome="allowed",
        resources=f"decisions log verdict={row['verdict']} side={row['side']}",
    )
    return web.json_response({"ok": True})

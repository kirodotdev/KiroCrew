"""Decision-seam REST API -- the operator's switch for Jev egress, and the strip.

``GET  /api/decisions/consent``   the keystone plus the endpoint config names now
``PUT  /api/decisions/consent``   ``{"enabled": bool}`` -> writes it, bound to that endpoint
                                 plus the ``tool_args`` / ``compaction`` /
                                 ``memory_text`` egress scopes,
                                 each preserved when its field is omitted
``POST /api/decisions/feedback``  a person's verdict on one turn -> one appended log row

Consent is bound to a destination: enabling records the provider endpoint the
config names at that moment, and the gate sends only while the two still agree.
The GET returns both so the card can show the owner WHERE consent would send, and
say so when a later config edit moved the destination out from under it.

Above the owner's switch sits the FLEET's: ``capabilities.decisions``
(``decisions/capability.py``). The two answer different questions -- may my messages
be sent, versus may this machine run the seam at all -- so a managed install can
have an owner who consented in good faith to an endpoint the fleet never approved.
A denial refuses an ENABLING PUT with ``403 decisions_capability_denied``, and both
verbs fold it into ``permits`` so no caller reads "a decision would be sent" under a
pin. A DISABLING PUT still succeeds under a denial: the gate already treats the seam
as off, and refusing the write would trap an owner with a stale ``"enabled": true``
keystone they cannot clear.

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
_CODE_CAPABILITY_DENIED = "decisions_capability_denied"

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


#: Status a point row carries, as the EFFECTIVE answer rather than a switch
#: position. ``off`` covers every reason nothing is sent -- no consent, a moved
#: endpoint, a governance pin -- because a reader acting on the row cares that it is
#: not running, and the card states the reason once above the list rather than
#: per row.
_POINT_ACTIVE = "active"
#: Consent is given and the endpoint holds, but this point sends a category the
#: owner has not agreed to (``tool.risk`` and the keystone's ``tool_args``). Its own
#: value rather than ``off``, because the fix is a switch on the point's own panel
#: and ``off`` would send the reader to the main switch that is already on.
_POINT_NEEDS_SCOPE = "needs_scope"
_POINT_OFF = "off"


def _sampling_admits_anybody() -> bool:
    """Whether ``decisions.bucket`` admits ANY session. Never raises.

    A share of 0 admits nobody, so no point is ever asked and the seam does nothing
    however the keystone reads. Anything unreadable is treated as admitting nobody,
    the same fail-closed direction the gate's own bucket parse takes: reporting a
    point as running on a config this handler could not parse is the one answer a
    reader has no way to check.

    Reads ``config.json`` synchronously, so it belongs on a worker thread: its only
    caller is ``_points``, which both routes reach through ``asyncio.to_thread``.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        decisions = getattr(KiroCrewConfig.load(), "decisions", None)
        return int(getattr(decisions, "bucket", 0)) > 0
    except Exception:
        logger.debug("decisions: sampled share unreadable; reporting no point as running")
        return False


def _points(state: dict, *, permits: bool) -> list[dict]:
    """One row per decision point this build ships, for the card's overview list.

    Projected from the seam's own registry (``gate.DECISION_POINT_NAMES``,
    ``gate.POINT_SCOPE_KEYS``) rather than
    authored here or in the frontend: a build that ships another point lists it with
    no edit on either side, and the set the card draws cannot disagree with the set
    the gate will answer for. The card holds a LABEL per id, the same arrangement
    ``agent_sdk/backend_cards.py`` and the Agent Backend panel use.

    ``status`` is the SERVER's verdict for the same reason ``permits`` is: it is
    derived from the keystone and the governance probe the caller already resolved,
    so the row and the gate cannot report different answers for one point. It is
    never a switch position -- ``off`` is every reason nothing would be sent.

    *permits* is the effective "anything is sent at all" answer, governance denial
    already folded in, so this function performs no probe of its own.

    The SAMPLED SHARE is folded in on the same terms. ``decisions.bucket`` at 0 means
    no session's key falls inside the sample, so every point is asked for nobody and
    does nothing -- and a row reporting ``active`` there would tell a reader the seam
    is working while no turn can reach it. That is the one status word a reader cannot
    check for themselves, so it has to be the effective answer rather than the
    consent-shaped one. ``off`` is already every reason nothing is sent, and a share of
    zero is one of them; the share itself is on screen in the shared block, so a reader
    who sees every row off has the reason one glance away.
    """
    from kiro_crew.decisions import consent
    from kiro_crew.decisions import gate as _gate

    # Read per SCOPE rather than per point, and only for the scopes this build
    # actually has: a point's row says whether ITS OWN consent is recorded, so a
    # hardcoded reader would report the tool-argument answer for a point that needs
    # the transcript scope. One read per distinct scope, reused across its points.
    granted = {
        key: bool(getattr(consent, f"consented_{key}")(state))
        for key in set(_gate.POINT_SCOPE_KEYS.values())
    }
    # Read once for the whole projection rather than per row: it is one config read
    # and every row resolves against the same answer, which is also what keeps the five
    # rows from disagreeing about whether sampling admits anybody.
    sampled = _sampling_admits_anybody()
    rows: list[dict] = []
    for name in _gate.DECISION_POINT_NAMES:
        # From the gate's own scope map, so a point that gains a scope is listed with
        # it and needs no edit here or in the card.
        scope = _gate.POINT_SCOPE_KEYS.get(name)
        if not permits or not sampled:
            status = _POINT_OFF
        elif scope is not None and not granted.get(scope, False):
            status = _POINT_NEEDS_SCOPE
        else:
            status = _POINT_ACTIVE
        rows.append(
            {
                "id": name,
                # Named as the keystone spells it, so the panel's switch and the
                # scope it writes are one string. ``None`` where a point needs no
                # scope beyond consent itself.
                "needs_scope": scope,
                "status": status,
                # Paths the card prints as a pointer rather than offering a control
            }
        )
    return rows


def _payload(state: dict, *, denied: bool) -> dict:
    """What both verbs return: the keystone and the endpoint config names now.

    *denied* subtracts from ``permits`` rather than appearing as a field of its own:
    the effective answer is what a caller acts on, and a separate reason field had no
    reader. It is passed in rather than probed here because the probe is filesystem
    IO and both callers already have a worker thread to spend it on.

    SYNCHRONOUS ON PURPOSE, and both callers reach it through ``asyncio.to_thread``.
    It reads the configured endpoint and the sampled share from ``config.json``, so
    on slow storage a direct call would hold the event loop for the length of a stat
    and a parse, and the gateway's other requests and its heartbeat wait behind it
    (``no-blocking-call-on-event-loop``). One hop covers every read in here rather
    than one hop per read.
    """
    from kiro_crew.decisions import consent
    from kiro_crew.decisions import gate as _gate

    configured = _gate.configured_endpoint()
    permits = consent.permits(configured, state) and not denied
    return {
        "enabled": consent.is_enabled(state),
        "endpoint": consent.consented_endpoint(state),
        "configured_endpoint": configured,
        # Whether a decision would actually be sent right now: consent given, and
        # for THIS address. False with enabled=true is the redirected-config state.
        # A governance denial makes the gate read the keystone as off, so it lands
        # here too: ``permits`` is the EFFECTIVE answer, and folding the denial into
        # it is what keeps the card from claiming a decision would be sent under a
        # pin. The denial is not reported as a field of its own -- nothing reads one,
        # and the surface a caller acts on is the 403 on an enabling write.
        "permits": permits,
        # The prior-conversation CEILING the owner reviewed. Reported so the card can
        # say what was consented to rather than what config.json currently asks for
        # -- those differ exactly when an agent has raised the config value, which is
        # the case this ceiling exists to make harmless.
        "history_budget_chars": consent.consented_history_budget(state),
        # Whether the owner consented to sending TOOL-CALL ARGUMENTS. Reported so the
        # card can show the second switch in the state actually recorded, rather than
        # guessing from ``enabled``: a record written before this scope existed reads
        # false here, which is what the card must draw for it.
        "tool_args": consent.consented_tool_args(state),
        # Whether the owner consented to sending a WHOLE SLOT TRANSCRIPT, the scope
        # ``compaction.keep`` needs. Reported for the reason ``tool_args`` is: the
        # card draws the switch in the state actually recorded, and a record written
        # before this scope existed reads false here rather than inheriting either of
        # the narrower yeses.
        "compaction": consent.consented_compaction(state),
        # Whether the owner consented to sending the TEXT OF RECALLED MEMORIES, on
        # the same terms and reported for the same reason: a record written before
        # this scope existed reads false here, which is what the card must draw for
        # it rather than inferring the scope from ``enabled``.
        "memory_text": consent.consented_memory_text(state),
        # The points this build ships, with the effective status of each. Here
        # rather than on a route of its own because the card reads this payload
        # already and a point's status is a function of the same keystone: a second
        # endpoint would be a second read that could answer differently.
        "points": _points(state, permits=permits),
    }


async def api_decisions_consent_get(request: web.Request) -> web.Response:
    """GET /api/decisions/consent -- whether, and for which endpoint, the owner consented."""
    denied = await _deny_non_owner(request, OP_CONSENT_GET)
    if denied is not None:
        return denied
    from kiro_crew.decisions import consent
    from kiro_crew.decisions.capability import is_decisions_denied

    state = await asyncio.to_thread(consent.load_state)
    # Off the loop for the same reason as the keystone read: profile resolution may
    # read from disk. Audited by the probe itself. Named ``withdrawn`` because
    # ``denied`` above is the owner gate's refusal, a different decision.
    withdrawn = await asyncio.to_thread(is_decisions_denied)
    payload = await asyncio.to_thread(_payload, state, denied=withdrawn)
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


def _consent_write_verb(*asserted: object) -> str:
    """The audit verb for a consent write, from the values it actually carried.

    ``granted`` when any carried value is ``True``, ``revoked`` when every carried value
    is ``False``, and ``allowed`` when the body carried none of them -- a PUT that
    asserts nothing changes nothing, so calling it either would be a claim about egress
    that nobody made.

    A non-boolean is a KEEP sentinel, meaning the field is absent from the body, so it
    is not counted: that is the whole point of taking the verb from the request rather
    than from the recorded state, where a scope revocation beside a still-enabled switch
    reads as a grant.

    Callers pass BOOLEANS, so a field that is not one -- the history ceiling is a number
    -- arrives already reduced to the question this answers: does this value widen egress.
    Keeping that reduction at the call site is what lets one rule cover a switch, a scope
    and a ceiling without this function learning what any of them mean.
    """
    values = [value for value in asserted if isinstance(value, bool)]
    if not values:
        return "allowed"
    return "granted" if any(values) else "revoked"


async def api_decisions_consent_put(request: web.Request) -> web.Response:
    """PUT /api/decisions/consent -- record ``{"enabled": bool}`` on the keystone.

    Enabling must ECHO the endpoint the owner reviewed (``endpoint`` in the body,
    the ``configured_endpoint`` the GET showed). The config is agent-writable, so
    between the owner's read and their click an agent could point
    ``provider.endpoint`` elsewhere; binding to what the server reads at PUT time
    would then consent to an address the owner never saw. A mismatch is ``409``
    and nothing is written; the card re-reads and shows the new address.

    ``enabled`` may be OMITTED, and then the body must name at least one of the fields
    that may move on their own -- either scope, or the history ceiling. Such a
    write moves only that scope: the switch and the endpoint are read from the keystone
    inside the writer's lock and written back unchanged, so it can neither grant nor
    revoke consent. That is the only safe shape for a scope write, because a body
    carrying ``enabled`` can only carry what its sender last read -- and a view read
    before a revoke would turn egress back on. A body naming neither the switch nor a
    scope is a ``400``.

    A scope-only write against a REVOKED keystone leaves it revoked and stores no scope:
    a scope is only meaningful while the seam is on, which is the same rule that clears
    the scopes on a disabling write.

    A scope-only write against a keystone that IS enabled is additionally REFUSED
    unless consent already stands for the endpoint config names -- ``403`` under a
    governance pin, otherwise ``409`` with the address in force. The revoked case
    above is not one of these: it answers ``200`` and records nothing, because there
    is no address to disagree about. ``permits`` is the predicate, the same one the
    GET reports and the gate enforces, so omitting ``enabled`` can only move a
    scope under a consent that already stands and can never record one for an
    address nobody reviewed.

    ``tool_args`` records whether the owner consented to sending TOOL-CALL
    ARGUMENTS, the category ``tool.risk`` needs. Absent PRESERVES the recorded scope
    and disabling clears it, exactly as the ceiling below behaves, so an ordinary
    switch flip cannot grant or erase it by omission. Absent on a keystone that never
    had it reads as false, which is what keeps a consent given before this scope
    existed meaning only what its owner reviewed.

    ``compaction`` records the same thing for a WHOLE SLOT TRANSCRIPT -- the
    conversation text and every tool-call input in it -- which is what
    ``compaction.keep`` sends and neither of the other two scopes covers. It behaves
    identically: absent preserves, disabling clears, a non-boolean is a ``400``, and
    absent on a keystone that never had it reads as false. It is a field of its own
    rather than a wider reading of ``tool_args`` because the owner reviewed that one
    as the arguments of the one call about to run.

    ``memory_text`` records the same answer for the TEXT OF RECALLED MEMORIES, the
    category ``memory.recall`` needs, on identical terms. Independent fields because
    they are independent decisions: an owner may want risky tool calls flagged without
    the contents of their memory store leaving the machine, and any order of those
    answers has to be recordable.

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
    object, or whose ``enabled`` is present and not a boolean (plus a string
    ``endpoint`` when enabling, and a non-negative whole ``history_budget_chars``
    when present); ``403`` for a non-owner, and for an ENABLING or SCOPE-ONLY write
    the ceiling withdrew (``decisions_capability_denied``); ``409`` when the echoed
    endpoint is not the one config names now, and for a scope-only write against a
    keystone ENABLED for a different address than config names. A scope-only write
    against a REVOKED keystone is not a ``409``: it answers ``200`` and records no
    scope, because a scope is only meaningful while the seam is on and there is no
    address to disagree about; ``500`` for a corrupt keystone, which is left
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
    #
    # OMITTED is a scope-only write, and it is the only safe shape for one. A body that
    # carries ``enabled`` can only carry what its sender last read, so a view read
    # before a revoke re-grants egress the owner just withdrew -- and no client-side
    # guard can make such a body safe, because the staleness is in the value itself.
    # Omitting it hands ``KEEP_ENABLED`` to the writer, which resolves the switch and the
    # endpoint from the keystone inside its own lock.
    #
    # It is only a standalone write when the body actually names one of the fields it
    # may move on its own: a body naming neither the switch nor any of them asks for
    # nothing and stays a 400, so an empty or misspelled body is refused rather than
    # silently rewriting the record as itself.
    #
    # The history CEILING counts among them. It is not a scope, but it moves on exactly
    # the same terms -- it is a number on the keystone that the card offers on its own,
    # and raising it is not a review of an address -- so a body carrying only
    # ``history_budget_chars`` is a write this route has to accept. Leaving it out made
    # every ceiling save a 400, which is the whole control dead on the one path an owner
    # uses for it.
    _STANDALONE_FIELDS = ("tool_args", "compaction", "memory_text", "history_budget_chars")
    scope_named = isinstance(body, dict) and any(f in body for f in _STANDALONE_FIELDS)
    enabled = body.get("enabled", consent.KEEP_ENABLED) if isinstance(body, dict) else None
    if enabled is consent.KEEP_ENABLED and not scope_named:
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
        return web.json_response(
            {
                "error": 'body must carry "enabled", or a scope to change on its own',
                "code": _CODE_INVALID_BODY,
            },
            status=400,
        )
    scope_only = enabled is consent.KEEP_ENABLED
    if not scope_only and not isinstance(enabled, bool):
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

    # A bool, or absent. Absent is handed on as ``KEEP_TOOL_ARGS`` rather than
    # resolved here, for the same reason the budget is: the writer resolves it inside
    # its own read-modify-write, so the scope written comes from the same read the
    # write is based on. Reading it on this side would hold a value read before a
    # concurrent PUT cleared it, and write that back -- restoring an egress scope
    # somebody just revoked.
    #
    # Validated rather than coerced, and a truthy stand-in is refused: this value
    # decides whether a new category of conversation content leaves the machine, so
    # ``"true"`` and ``1`` are 400s rather than silent yeses.
    tool_args = body.get("tool_args", consent.KEEP_TOOL_ARGS) if isinstance(body, dict) else False
    if tool_args is not consent.KEEP_TOOL_ARGS and not isinstance(tool_args, bool):
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
        return web.json_response(
            {"error": '"tool_args" must be true or false', "code": _CODE_INVALID_BODY},
            status=400,
        )

    # The third scope, on exactly the terms of the one above: absent is the
    # ``KEEP_COMPACTION`` sentinel resolved inside the writer's own lock, a truthy
    # stand-in is a 400 rather than a silent yes, and disabling clears it. A separate
    # field and not a wider reading of ``tool_args``, because the two were reviewed as
    # different things -- the arguments of one call, versus everything this session has
    # run.
    compaction = (
        body.get("compaction", consent.KEEP_COMPACTION) if isinstance(body, dict) else False
    )
    if compaction is not consent.KEEP_COMPACTION and not isinstance(compaction, bool):
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
        return web.json_response(
            {"error": '"compaction" must be true or false', "code": _CODE_INVALID_BODY},
            status=400,
        )

    # The recalled-memory scope, read and validated on exactly the terms above: the
    # sentinel is handed on rather than resolved here so the writer resolves it from
    # the same read its write is based on, and a truthy stand-in is a 400 rather than
    # a silent yes about a new egress category.
    memory_text = (
        body.get("memory_text", consent.KEEP_MEMORY_TEXT) if isinstance(body, dict) else False
    )
    if memory_text is not consent.KEEP_MEMORY_TEXT and not isinstance(memory_text, bool):
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="invalid_body")
        return web.json_response(
            {"error": '"memory_text" must be true or false', "code": _CODE_INVALID_BODY},
            status=400,
        )

    # Bound to the endpoint the owner REVIEWED, checked against the one the
    # config names now. Equal: consent is for the address on screen, and the one
    # the gate will hold the config to afterwards. Different: the config moved
    # under the owner's review, so refuse and let the card show the new address.
    endpoint = _gate.configured_endpoint()
    # ONE probe for this request, resolved before the branch so the refusal below
    # and the ``permits`` value in the response cannot disagree. Off the loop:
    # profile resolution may read from disk.
    from kiro_crew.decisions.capability import is_decisions_denied

    withdrawn = await asyncio.to_thread(is_decisions_denied)
    # What the fleet ceiling is held against is whether this write GRANTS something:
    # consent itself, or an egress scope. Only a grant is gated -- a disabling or
    # revoking PUT stays available so an owner can clear a record written before the pin
    # (the gate already reads it as off, so the write changes no authority, it only
    # tidies the record), and trapping them with a stale `"enabled": true` they cannot
    # clear would be worse than the record.
    #
    # A scope-only write is in scope for this: it carries ``KEEP_ENABLED`` and therefore
    # asserts nothing about consent, but turning a scope ON under a pin would still
    # record an egress permission the fleet has withdrawn.
    #
    # A NON-ZERO history ceiling grants too, on exactly that reading: the number bounds
    # how much prior conversation the seam may carry, so recording one under a pin books
    # an egress allowance for the moment the pin lifts -- and the pin is when it is
    # cheapest to record, because nothing sends while it holds. Tested by VALUE and not
    # against the recorded ceiling: reading the old number here would be the stale read
    # this route refuses to take (the writer resolves the sentinel under its own lock),
    # and no read is needed, because ``0`` is the only budget write that cannot widen
    # anything. So ``0`` stays available under a pin for the reason a revoke does -- it
    # is how prior turns are taken back -- and any other number waits for the pin to
    # lift. ``enabled is False`` is excluded because the writer forces the ceiling to 0
    # on a disabling write, so such a PUT records no allowance and must not be trapped.
    budget_grants = (
        budget is not consent.KEEP_HISTORY_BUDGET and budget > 0 and enabled is not False
    )
    grants = (
        enabled is True
        or tool_args is True
        or compaction is True
        or memory_text is True
        or budget_grants
    )
    # The same answer, carried to the audit verb below with the ABSENT case kept apart
    # from the false one: `budget_grants` is False both for a ceiling that widens nothing
    # and for a body that mentioned no ceiling at all, and those two must not audit alike
    # -- a PUT asserting nothing is `allowed`, not `revoked`.
    budget_asserts: object = (
        budget_grants if budget is not consent.KEEP_HISTORY_BUDGET else consent.KEEP_HISTORY_BUDGET
    )
    if grants and withdrawn:
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="capability_denied")
        return web.json_response(
            {
                "error": "the decision seam is withdrawn by governance policy",
                "code": _CODE_CAPABILITY_DENIED,
            },
            status=403,
        )
    # ``is True``, not truthiness: ``enabled`` may be the ``KEEP_ENABLED`` sentinel,
    # which is an object and therefore truthy. Only a write that actually turns consent
    # on has to echo the reviewed address -- a scope-only write is not consenting to an
    # address, it is leaving the recorded one exactly as it is.
    if enabled is True:
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
            consent.save_enabled,
            enabled,
            endpoint=endpoint,
            history_budget_chars=budget,
            tool_args=tool_args,
            compaction=compaction,
            memory_text=memory_text,
        )
    except consent.ConsentCorruptError as exc:
        await _audit(request, operation=OP_CONSENT_PUT, outcome="error", error="corrupt")
        return web.json_response(
            {"error": f"decisions_consent.json is unreadable: {exc}", "code": _CODE_CORRUPT},
            status=500,
        )
    except consent.ConsentEndpointMovedError:
        # The scope branch above checked that consent stands for the address config
        # names, then the writer found the keystone bound to a different one: a
        # re-bind landed between the two. Same 409 and the same code the pre-lock
        # checks answer with, because it is the same refusal -- the card re-reads and
        # shows the address in force -- and nothing was written.
        await _audit(request, operation=OP_CONSENT_PUT, outcome="denied", error="endpoint_changed")
        return web.json_response(
            {
                "error": "a scope needs consent in force for the endpoint config names",
                "code": _CODE_ENDPOINT_CHANGED,
                "configured_endpoint": endpoint,
            },
            status=409,
        )
    # Written and audited as the security decision it is: which way the switch
    # went is the one fact an auditor reconstructing "when did egress start" needs.
    # The endpoint travels in the row: "when did egress start, and to where" is
    # the pair an auditor needs.
    await _audit(
        request,
        operation=OP_CONSENT_PUT,
        # The verb names what THIS write ASSERTS, not the state it leaves behind. A
        # scope-only PUT that turns a scope off is a revocation even while consent stays
        # on, so a verb read off the recorded flag names it "granted" -- the opposite of
        # what the request asked for, in the one record an auditor reconstructs egress
        # from. Only fields the body actually carries count: a sentinel means the field is
        # absent, so it asserts nothing. A body that asserts nothing at all changes
        # nothing and is neither, so it audits as a plain allowed read-modify-write. A
        # mixed write that grants one thing and revokes another reads as "granted",
        # naming the widest thing it does; `resources` below carries every recorded value,
        # so the row says exactly what the file holds afterwards either way.
        #
        # The CEILING is judged on the same terms the governance check above judges it
        # on, and by reusing that same value rather than a second test of it: a bigger
        # number widens egress, so it is a grant, and `0` takes prior turns back, so it
        # is a revocation. One expression decides both what a pin refuses and what the
        # row says happened, which is the only way the two cannot drift into a refused
        # write the log calls something else.
        # A scope-only write is judged by the same rule rather than given a word of
        # its own: one word for every scope write collapses a GRANT and a REVOCATION
        # into the same row, and which way a scope moved is the fact an auditor
        # reconstructing when egress started actually needs.
        outcome=_consent_write_verb(enabled, tool_args, compaction, memory_text, budget_asserts),
        resources=(
            f"decisions_consent.json endpoint={endpoint} "
            f"history_budget_chars={consent.consented_history_budget(state)} "
            f"tool_args={consent.consented_tool_args(state)} "
            f"compaction={consent.consented_compaction(state)} "
            f"memory_text={consent.consented_memory_text(state)}"
        ),
    )
    return web.json_response(await asyncio.to_thread(_payload, state, denied=withdrawn))


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

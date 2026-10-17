"""Provider-write authority: ``/incident/action`` and the propose loop that approves one.

The autonomy gate is a chokepoint here, not a convention: ``_authorize`` is the only minter
of an ``_Authorized`` permit, and ``_execute_authorized`` — the only caller of
``ActionSink.execute`` — demands one. Both execution paths, a direct action and an approved
proposal, resolve the sink from the incident's own provider, refuse a verb the sink cannot
confirm, and schedule the post-action recheck the same way.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import rotation, store
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (
    AuditWriter,
    RegistryLookup,
    _json_body,
    _NotABool,
    _require_bool,
    _store_read_refusal,
    logger,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    DEFAULT_VERIFY_AFTER_SECS,
    EXPIRING_ACTIONS,
    VALID_ACTIONS,
    VERIFIABLE_ACTIONS,
    VERIFY_NOT_CHECKABLE,
    VERIFY_PENDING,
    Signal,
    resolve_silence_secs,
    utc_now_iso,
)

#: Cap on an operator-supplied note attached to an action.
_MAX_NOTE_LEN = 4000


async def _handle_action(
    request: web.Request,
    *,
    get_registry: RegistryLookup,
    _audit: AuditWriter,
    _safe_outbound: Callable[[str], str],
) -> web.StreamResponse:
    """Execute (or refuse) a provider action for an incident.

    The autonomy gate runs BEFORE the sink is touched: a sink does not police its
    own authority. A refusal returns 403 with the reason, which is what the UI
    renders as "needs a rule to do this".
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    incident_id = str(body.get("id", "")).strip()
    action = str(body.get("action", "")).strip()
    sink_id = str(body.get("sink", "")).strip()
    # Redact BEFORE the clip: truncating first could sever a token so the pattern no
    # longer matches, and clipping after masking only ever shortens a placeholder.
    note = _safe_outbound(str(body.get("note", "")))[:_MAX_NOTE_LEN]

    if action not in VALID_ACTIONS:
        return web.json_response(
            {"error": f"action must be one of {sorted(VALID_ACTIONS)}", "code": "invalid_action"},
            status=400,
        )
    incident = await asyncio.to_thread(store.get_incident, incident_id) if incident_id else None
    if incident is None:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )

    # `_authorize` is the gate AND the only minter of the permit `_execute_authorized`
    # demands, so the write below cannot happen without this line having allowed it. It also
    # runs the gate off the event loop — see its docstring for why that matters.
    permit, reason = await _authorize(incident.signal, action)
    if permit is None:
        return web.json_response(
            {"error": reason, "code": "not_authorized", "authorized": False}, status=403
        )

    registry = get_registry()
    # A caller-supplied sink must name THIS incident's provider, or nothing.
    #
    # `authorize_action` gates on `incident.signal`, and `AutonomyRule.matches` keys on
    # `signal.source` — so a rule only ever grants authority over the provider that
    # RAISED the signal. Honouring `sink_id` verbatim would let a grant on one provider
    # execute against another: a webhook signal carrying `dd_monitor_id`, a webhook-scoped
    # act-rule, and `sink="datadog"` would pass the webhook check and then silence an
    # unrelated Datadog monitor. The gate is correct, and the write has to land on the
    # thing the gate approved.
    #
    # Rejected rather than silently ignored. A caller that names the wrong sink has a
    # wrong model of what it is authorized to do, and quietly redirecting the write to the
    # right provider would confirm the wrong model while still performing a mutation.
    if sink_id and sink_id != incident.signal.source:
        return web.json_response(
            {
                "error": (
                    f"sink {sink_id!r} does not own this incident's signal "
                    f"({incident.signal.source!r}); authority is per provider"
                ),
                "code": "sink_not_owner",
                "authorized": False,
            },
            status=403,
        )
    # Default to the sink that owns this signal's provider, falling back to
    # observe-only so a proposal always has somewhere to land.
    sink = registry.action_sink(incident.signal.source) or registry.action_sink("noop")
    if sink is None:
        return web.json_response(
            {"error": "no action sink available", "code": "no_action_sink"}, status=503
        )

    refusal = _sink_refuses(sink, action)
    if refusal:
        # 422, not 403: authority is fine — the target simply cannot do this. A 403 would
        # send an operator to their autonomy rules, which are not the problem.
        return web.json_response({"error": refusal, "code": "action_unsupported"}, status=422)

    payload: dict[str, Any] = {"note": note}
    if action in EXPIRING_ACTIONS:
        # Clamped HERE, not in the adapter. A suppression with no expiry is the one
        # outcome the verb exists to prevent, so the bound is applied at the boundary
        # every sink goes through rather than trusted to each sink separately — an
        # adapter that forgot the check would silence a monitor forever.
        payload["duration_secs"] = resolve_silence_secs(body.get("duration_secs"))

    result = await _execute_authorized(sink, permit, payload)
    _audit(
        "incident_action",
        f"{incident_id} {action} via {sink.id}"
        + (f" for {payload['duration_secs']}s" if "duration_secs" in payload else ""),
        "success" if result.ok else "failed",
        error=result.error,
    )
    verification = ""
    verify_after = ""
    # A SIMULATED result schedules nothing. ``ok=True`` from the observe-only sink means "we
    # successfully did nothing", and the recheck cannot tell that from a real write: it read
    # the still-firing alarm as the action having failed and charged a ``miss_count`` to
    # every ledger entry the investigation cited. On a default install that is the ONLY
    # path, because `cloudwatch` and `webhook` register no ActionSink and every action falls
    # through to `noop` — so watching the proposal flow, which is exactly what an operator
    # is told to do before granting real authority, demoted their own proven knowledge for
    # a write nobody made. Verified before fixing: act mode plus one scoped cloudwatch rule
    # took a verified/high/2-use entry to `miss_count=1` and off the fast path.
    if result.ok and not result.simulated:
        # The SINK's reported suppression wins over the requested duration: an adapter
        # that aliases one verb onto a bounded mute (Datadog's `resolve`) is the only
        # party that knows the real window, and rechecking inside it manufactures a miss.
        verification, verify_after = await asyncio.to_thread(
            _schedule_verification,
            incident_id,
            action,
            result.suppressed_secs or payload.get("duration_secs"),
            _audit=_audit,
        )
    return web.json_response(
        {
            "ok": result.ok,
            "action": result.action,
            "detail": result.detail,
            "error": result.error,
            # Echoed so a caller can see the window actually applied, which may be
            # smaller than the one it asked for.
            "duration_secs": payload.get("duration_secs"),
            # What a 2xx from the provider DOES and does not mean, in the same response
            # as the result — a 2xx alone never means "applied". ``pending`` says a
            # recheck is scheduled; ``not_checkable`` says this app cannot observe this
            # verb's
            # outcome; ``""`` says the call failed so nothing was scheduled.
            "verification": verification,
            "verify_after": verify_after,
            # Present on both branches so a client reads ONE shape. `ok` already carries
            # the boolean; `code` is what a caller switches on, and the 502 branch is a
            # genuine error response the localized UI must not render as English prose.
            "code": "" if result.ok else "sink_execute_failed",
        },
        status=200 if result.ok else 502,
    )


def _schedule_verification(
    incident_id: str, action: str, duration_secs: Any, *, _audit: AuditWriter
) -> tuple[str, str]:
    """Record what was just done and when to re-read the signal. Returns (verdict, due).

    Two schedules, and the difference is the point ``ACTION_SILENCE``'s mandatory expiry
    buys. A suppression is rechecked at the END of its own window — which is the
    interesting moment, because a suppression that expires straight back into the same
    firing condition is positive evidence nothing was fixed. Everything else is rechecked
    after ``DEFAULT_VERIFY_AFTER_SECS``, long enough for a provider evaluating on a period
    to catch up.

    "A suppression", not "a ``silence``": the caller passes whatever window was actually
    ESTABLISHED, which the sink reports via ``ActionResult.suppressed_secs`` and which can
    be non-zero for a verb that is not ``silence`` at all. Datadog implements ``resolve``
    as an alias onto the same bounded mute, so keying this on the verb rechecked a 4-hour
    mute after 5 minutes and charged a false miss.

    An action outside ``VERIFIABLE_ACTIONS`` is stamped ``not_checkable`` with NO due
    date, so ``verify_pending_actions`` never picks it up. That is deliberate honesty
    rather than a gap left open: an ack leaves an alert firing by design, so a verdict
    derived from firing state would be a confident wrong answer about an unverifiable
    write. The board says "not checked" instead.

    Never raises: the provider write already happened and cannot be undone, so a failure
    to record the bookkeeping must not turn a completed action into a 500. It degrades to
    "no verification scheduled", which the response then reports honestly.
    """
    if action not in VERIFIABLE_ACTIONS:
        verdict, due = VERIFY_NOT_CHECKABLE, ""
    else:
        verdict = VERIFY_PENDING
        try:
            wait = int(duration_secs) if duration_secs else DEFAULT_VERIFY_AFTER_SECS
        except (TypeError, ValueError):
            wait = DEFAULT_VERIFY_AFTER_SECS
        due = (datetime.now(timezone.utc) + timedelta(seconds=max(1, wait))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    now = utc_now_iso()
    try:
        store.update_fields(
            incident_id,
            last_action=action,
            last_action_at=now,
            verify_after=due,
            verification=verdict,
            verification_detail="",
        )
    except json.JSONDecodeError:
        # Corruption gets its OWN handler here, but it must NOT raise. Both callers have
        # ALREADY performed the real external write by the time this runs (`result.ok and
        # not result.simulated`), so raising turns a successful action into a 500 and invites
        # the operator to retry it -- in `act` mode that is a second real write against their
        # production tooling. Swallowing it in the clause below (`JSONDecodeError`
        # subclasses `ValueError`) hides it, and re-raising misreports a completed action.
        # Neither remedy alone is right.
        #
        # So it is REPORTED rather than either swallowed or raised: its own clause, an
        # exception log, and a SEL audit entry recording that the action ran with no
        # verification scheduled. That is the same choice `_settings_write_or_refuse`
        # (`configuration.py`) makes for a partial ceiling apply -- the audit log is the
        # durable reader, and unlike a new
        # `verification` value it needs no vocabulary the dashboard cannot render. The
        # persistence argument still holds (corruption never self-heals), which is why it is
        # audited as a failure rather than folded into the tolerant arm.
        logger.exception(
            "ops-mission-control: index corrupt, verification not scheduled for %s", incident_id
        )
        _audit(
            "action_verification_unscheduled",
            incident_id,
            "failure",
            error="index corrupt; action executed with no recheck scheduled",
        )
        return "", ""
    except (KeyError, ValueError, OSError):
        logger.exception("ops-mission-control: could not schedule verification for %s", incident_id)
        return "", ""
    return verdict, due


def _sink_refuses(sink: Any, action: str) -> str:
    """Why ``sink`` cannot perform ``action``, or "" when it can.

    ``supported_actions()`` was declared on every adapter and enforced NOWHERE — a UI
    hint rather than a gate. So an action the autonomy rules authorized could still reach
    an adapter with no defined behaviour for it: GitHub Issues supports only
    ``{resolve, comment}``, and an authorized ``ack`` arriving there is an undefined call
    against a real repository, with the adapter free to do whatever its ``execute`` falls
    through to. Found in review.

    FAIL CLOSED. This gate fronts a real provider write, and every way the probe fails to
    return a set CONTAINING the action means the same thing: we could not positively confirm
    the adapter supports it. "I could not confirm" is not "it is supported" — an earlier
    version treated a missing method, a raising probe, and an EMPTY set as ALLOW, which is the
    exact undefined-`execute`-call this function exists to prevent, reached three other ways.
    `supported_actions` is part of the `ActionSink` protocol, so its absence is a broken
    adapter, not a legacy one; a raise is a broken probe; an empty set is an adapter that
    declares it can do nothing. In all three the safe answer against a production write is to
    refuse, and the ordinary autonomy gate having authorized the action does not change that,
    because authorization says "the operator permits this verb", not "this sink can perform
    it". A broken companion probe therefore degrades to "this action is refused", never to a
    crash and never to an unconfirmed write. Found in review (GPT 5.6).
    """
    probe = getattr(sink, "supported_actions", None)
    sink_id = getattr(sink, "id", "?")
    if not callable(probe):
        return (
            f"sink {sink_id!r} does not declare supported_actions(), so {action!r} cannot be "
            "confirmed as supported"
        )
    try:
        supported = probe()
    except Exception:  # noqa: BLE001 — a faulty probe refuses, it does not crash the path
        logger.exception("ops-mission-control: supported_actions() raised for sink %r", sink_id)
        return f"sink {sink_id!r} could not report its supported actions, so {action!r} is refused"
    if action in (supported or frozenset()):
        return ""
    return f"sink {sink_id!r} does not support {action!r} " f"(supports {sorted(supported or [])})"


class _Authorized:
    """Proof that the autonomy gate ran and allowed this exact (signal, action).

    Why a token rather than a comment. ``ActionSink.execute`` does not police its own
    authority — by design, spec §5.3 — and the gate lived at two independent call sites
    with the ordering held together by convention. Review named the shape: "autonomy
    enforcement is a convention, not a chokepoint. A third caller can silently skip the
    gate." Nothing in the code disagreed.

    So the permission is now a VALUE that only ``_authorize`` mints, and the only function
    that touches ``sink.execute`` demands one. A new caller cannot reach the write without
    holding a token, and cannot fabricate one without going through the gate that mints it
    — the mistake becomes a type error at authoring time instead of an unauthorized
    provider write in production.

    Deliberately not a general capability object. It carries the exact signal and action it
    was minted for, and ``_execute_authorized`` reads the write's target FROM the permit
    rather than from a parallel argument — so there is no way to hold a permit for
    ``comment`` and spend it on ``resolve``: the mismatch is not rejected, it is
    unrepresentable.
    """

    __slots__ = ("signal", "action", "reason")

    def __init__(self, signal: Signal, action: str, reason: str) -> None:
        self.signal = signal
        self.action = action
        self.reason = reason


async def _authorize(signal: Signal, action: str) -> tuple[_Authorized | None, str]:
    """Run the autonomy gate. Returns ``(token, reason)``; the token is ``None`` on deny.

    The ONLY place an ``_Authorized`` is created, which is what makes it proof.

    Off the event loop: ``rotation.authorize_action`` is synchronous by design, and its
    off-shift check reads the committed schedule and — with no ``schedule-file.github_login``
    configured, the documented default — resolves this instance's identity by spawning
    ``gh api user``, a blocking HTTPS round trip with a 10s timeout on the first call of a
    fresh gateway process. Run inline it freezes every other task on the loop, the user's
    chat turn and the liveness heartbeat included. The handlers that reach this gate never
    ``await registry.resolve_shift()`` first, so nothing has warmed the login cache
    off-loop by the time we get here.
    """
    allowed, reason = await asyncio.to_thread(rotation.authorize_action, signal, action)
    if not allowed:
        return None, reason
    return _Authorized(signal, action, reason), reason


async def _execute_authorized(sink: Any, permit: _Authorized, payload: dict[str, Any]) -> Any:
    """The ONLY path to ``ActionSink.execute``. Requires a gate-minted permit.

    The signal and action come FROM the permit, never from a separate argument — that is
    what makes "executed something other than what was authorized" unrepresentable rather
    than merely checked for.
    """
    return await sink.execute(permit.signal, permit.action, payload)


async def _execute_stored_proposal(
    incident: Any,
    proposal: dict[str, Any],
    permit: _Authorized,
    *,
    get_registry: RegistryLookup,
    _audit: AuditWriter,
    _safe_outbound: Callable[[str], str],
) -> dict[str, Any]:
    """Run an APPROVED proposal's stored terms through the ordinary sink path.

    Shares the per-provider ownership rule with ``_handle_action``: the sink must be the
    one that raised the signal. A proposal could otherwise name any sink at draft time
    and have it honoured at approve time, which would route around the very check that
    made caller-selected sinks safe.

    Only the payload is rebuilt, and only from stored fields — never from the approving
    request. The suppression window is re-clamped here because a stored value could have
    been written before the clamp existed.
    """
    registry = get_registry()
    sink_id = str(proposal.get("sink", ""))
    if sink_id and sink_id != incident.signal.source:
        return {
            "ok": False,
            "executed": False,
            "error": (
                f"proposal names sink {sink_id!r}, which does not own this incident's "
                f"signal ({incident.signal.source!r})"
            ),
            "code": "sink_not_owner",
        }
    sink = registry.action_sink(incident.signal.source) or registry.action_sink("noop")
    if sink is None:
        return {
            "ok": False,
            "executed": False,
            "error": "no action sink available",
            "code": "no_action_sink",
        }

    # From the PERMIT, not from the proposal dict. Both are derived from the same stored
    # field at the call site, so they agree today — but reading it twice is exactly the
    # coupling-by-convention this permit exists to remove: the capability check, the payload
    # shape and the write must all describe one action, and the permit is that one.
    action = permit.action
    refusal = _sink_refuses(sink, action)
    if refusal:
        return {"ok": False, "executed": False, "error": refusal, "code": "action_unsupported"}
    payload: dict[str, Any] = {"note": _safe_outbound(str(proposal.get("note", "")))}
    if action in EXPIRING_ACTIONS:
        payload["duration_secs"] = resolve_silence_secs(proposal.get("duration_secs"))

    result = await _execute_authorized(sink, permit, payload)
    _audit(
        "incident_action",
        f"{incident.incident_id} {action} via {sink.id} (approved proposal)",
        "success" if result.ok else "failed",
        error=result.error,
    )

    # Schedule the post-action recheck, exactly as `_handle_action` does. An approved
    # proposal executes the SAME real provider write as a direct action, so it must record
    # `last_action`/`last_action_at` and arm verification the same way — otherwise a resolve
    # or silence went out, `verify_pending_actions` never ran, `last_action` stayed empty,
    # and the incident record and postmortem showed a write that "never happened". The two
    # execution paths converge on `_execute_authorized`; this makes their FOLLOW-UP converge
    # too. `result.ok and not result.simulated` for the same reason: a `noop`-simulated write
    # changed nothing at the provider, so rechecking it would read the still-firing alarm as
    # a failure and charge a miss to the cited ledger entries. Found in review.
    # `result.suppressed_secs or payload.get("duration_secs")` — the SINK's reported window
    # first, identical to `_handle_action`. Datadog aliases `resolve` onto a bounded mute, and
    # only `EXPIRING_ACTIONS` (i.e. `silence`) gets a `duration_secs` from the route, so reading
    # the payload alone scheduled a five-minute recheck against a four-hour suppression and
    # charged a false miss to the cited ledger entries. The fix landed on the direct-action path
    # and not here, which is this PR's recurring lesson in miniature: the two paths converge on
    # `_execute_authorized` for the WRITE, so their follow-up has to converge too, and a fix
    # applied one layer up does not protect a path that does not pass through it. Found in
    # review (GPT 5.6).
    verification = verify_after = ""
    if result.ok and not result.simulated:
        verification, verify_after = await asyncio.to_thread(
            _schedule_verification,
            incident.incident_id,
            action,
            result.suppressed_secs or payload.get("duration_secs"),
            _audit=_audit,
        )
    return {
        "ok": result.ok,
        "executed": True,
        "action": result.action,
        "detail": result.detail,
        "error": result.error,
        "verification": verification,
        "verify_after": verify_after,
        "code": "" if result.ok else "sink_execute_failed",
    }


async def _handle_propose(
    request: web.Request, *, _audit: AuditWriter, _safe_outbound: Callable[[str], str]
) -> web.StreamResponse:
    """Record the exact action an agent would take, for a human to approve.

    Deliberately NOT gated on the autonomy mode. A proposal changes nothing in the
    operator's tooling — it is the safe half of the loop, and refusing to let an
    ``observe`` instance draft one would mean the mode below ``act`` still had nothing
    to show. The gate that matters runs on APPROVE, where the write happens.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"},
            status=400,
        )
    incident_id = str(body.get("id", "")).strip()
    action = str(body.get("action", "")).strip()
    sink = str(body.get("sink", "")).strip()
    # Redact BEFORE the clip: truncating first could sever a token so the pattern no
    # longer matches, and clipping after masking only ever shortens a placeholder.
    note = _safe_outbound(str(body.get("note", "")))[:_MAX_NOTE_LEN]
    if not incident_id:
        return web.json_response(
            {"error": "id is required", "code": "missing_required_field"}, status=400
        )
    if action not in VALID_ACTIONS:
        return web.json_response(
            {
                "error": f"action must be one of {sorted(VALID_ACTIONS)}",
                "code": "invalid_action",
            },
            status=400,
        )
    duration = (
        resolve_silence_secs(body.get("duration_secs")) if action in EXPIRING_ACTIONS else None
    )
    try:
        incident = await asyncio.to_thread(
            store.propose_action,
            incident_id,
            action=action,
            sink=sink or "",
            note=note,
            duration_secs=duration,
        )
    except KeyError:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    except (json.JSONDecodeError, OSError) as exc:
        # BEFORE the `ValueError` arm. `propose_action` reaches the strict reader through
        # `update_fields` -> `transition` -> `_read_index_for_update()`, so a corrupt index
        # raises `CorruptDocumentError`, which IS a `ValueError` -- and the arm below would
        # report it as `400 invalid_proposal`. That is worse than the bare 500 it replaces:
        # a 400 tells the operator their proposal was malformed, so they re-type the form
        # while the real fault sits on disk untouched. Found by auditing every
        # `ValueError`-family arm on the HTTP surface against the rule stated on
        # `_store_read_refusal`; this was the only one of ten that could reach a strict
        # reader and did not already have this clause.
        return _store_read_refusal(exc, code="dispatch_index")
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_proposal"}, status=400)

    _audit("incident_propose", f"{incident_id} {action} via {sink}", "success")
    return web.json_response({"incident": incident.to_dict()})


async def _handle_decide_proposal(
    request: web.Request,
    *,
    get_registry: RegistryLookup,
    _audit: AuditWriter,
    _safe_outbound: Callable[[str], str],
) -> web.StreamResponse:
    """Approve or reject a pending proposal, then EXECUTE the stored terms verbatim.

    Approving runs the action through the same ``authorize_action`` gate a direct call
    uses, so approval cannot launder a write past the autonomy ceiling: an operator who
    approves a proposal on an ``observe`` instance gets the decision recorded and the
    execution refused, which is the honest outcome rather than a silent upgrade.

    The executed terms come from the STORE, never from this request body. That is the
    point of the whole mechanism — a request that could supply its own note would let the
    text change between the operator reading it and the action firing.
    """
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"},
            status=400,
        )
    incident_id = str(body.get("id", "")).strip()
    try:
        # REQUIRED and strictly boolean: this field decides whether a production write happens,
        # so an ambiguous value must 400 rather than be guessed at in either direction.
        approve = _require_bool(body, "approve")
    except _NotABool:
        return web.json_response(
            {
                "error": "approve must be true or false (a JSON boolean, not a string)",
                "code": "invalid_field_type",
            },
            status=400,
        )
    if approve is None:
        return web.json_response(
            {"error": "approve is required", "code": "missing_required_field"}, status=400
        )
    digest = str(body.get("digest", "")).strip()
    if not incident_id:
        return web.json_response(
            {"error": "id is required", "code": "missing_required_field"}, status=400
        )
    try:
        decision = await asyncio.to_thread(
            store.decide_proposal, incident_id, approve=approve, digest=digest
        )
    except KeyError:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    except (json.JSONDecodeError, OSError) as exc:
        # `decide_proposal` is a locked read-modify-write that refuses rather than
        # publishing over a failed read, so its refusals need coded answers of their own
        # instead of a bare 500. The request is a human's approval of a production action,
        # so "did my approval land?" must not be ambiguous.
        logger.warning("ops-mission-control: proposal decision refused for %s", incident_id)
        _audit("incident_proposal_decide", incident_id, "failure", error="index unreadable")
        return _store_read_refusal(exc, code="dispatch_index")
    if not decision["ok"]:
        _audit("incident_proposal_decide", incident_id, "rejected", error=decision["reason"])
        return web.json_response(
            {"error": decision["reason"], "code": "proposal_conflict"}, status=409
        )

    proposal = decision["proposal"]
    _audit(
        "incident_proposal_decide",
        f"{incident_id} {proposal['action']} {proposal['state']}",
        "success",
    )
    if not approve:
        return web.json_response({"ok": True, "proposal": proposal, "executed": False})

    # Approved: execute the STORED terms through the normal gate.
    incident = await asyncio.to_thread(store.get_incident, incident_id)
    if incident is None:  # pragma: no cover — decided above, so it existed a moment ago
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    # Same gate, same minter as the direct-action path. The permit is PASSED to the executor
    # rather than the executor re-deriving authority, so the gate and the write cannot drift
    # apart the way two independent call sites can.
    permit, reason = await _authorize(incident.signal, str(proposal["action"]))
    if permit is None:
        return web.json_response(
            {
                "ok": False,
                "proposal": proposal,
                "executed": False,
                "error": reason,
                "code": "not_authorized",
                "authorized": False,
            },
            status=403,
        )
    result = await _execute_stored_proposal(
        incident,
        proposal,
        permit,
        get_registry=get_registry,
        _audit=_audit,
        _safe_outbound=_safe_outbound,
    )
    return web.json_response({"ok": result["ok"], "proposal": proposal, **result})


async def _handle_proposals(request: web.Request) -> web.StreamResponse:
    """The pending-proposal queue — the thing an operator could not see at all before."""
    pending = await asyncio.to_thread(store.pending_proposals)
    return web.json_response(
        {
            "proposals": [
                {
                    "incident_id": inc.incident_id,
                    "title": inc.signal.title,
                    "source": inc.signal.source,
                    "severity": inc.signal.severity,
                    **(inc.proposed_action or {}),
                }
                for inc in pending
            ],
            "total": len(pending),
        }
    )

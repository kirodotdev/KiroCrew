"""Incident lifecycle writes: ``/incident/transition``, ``/incident/claim``, ``/dispatch``.

The manual claim and the dispatch cycle are the two places a claim becomes durable, so both
attach the ledger's matches, broker the provider's evidence and publish to the pin board; a
transition is where an incident reaches ``needs_human`` and its Slack thread becomes
replyable. None of them writes to a provider: the autonomy-gated writes live in ``actions``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from aiohttp import web

from kiro_crew.apps.builtins.ops_mission_control.backend import (
    dispatch,
    notify_out,
    rotation,
    slack_out,
    store,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.http_routes._shared import (
    AuditWriter,
    RegistryLookup,
    _json_body,
    _slack_client,
    _store_read_refusal,
    logger,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    CLAIMED_BY_OPERATOR,
    STATE_FIRING,
    STATUS_NEEDS_HUMAN,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers import webhook as webhook_mod


async def _handle_transition(
    request: web.Request, *, _audit: AuditWriter, _safe_outbound: Callable[[str], str]
) -> web.StreamResponse:
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    incident_id = str(body.get("id", "")).strip()
    new_status = str(body.get("status", "")).strip()
    if not incident_id or not new_status:
        return web.json_response(
            {"error": "id and status are required", "code": "missing_required_field"}, status=400
        )

    updates: dict[str, Any] = {}
    for field_name in ("diagnosis", "resolution", "slot_key", "slack_thread_ts"):
        if field_name in body:
            value = str(body[field_name])
            # `diagnosis` and `resolution` are AGENT-AUTHORED free text that this app then
            # persists and renders — on the board, in the handover digest, and in the Slack
            # mirror. An investigating agent that pasted a provider token into its writeup
            # stored that token in the incident index and painted it on the dashboard. Same
            # shape as the action-note, Slack and ledger sinks, which were already covered
            # while this one was not. Found in review.
            #
            # `slot_key`/`slack_thread_ts` are machine ids, shape-checked downstream, and are
            # deliberately NOT run through the redactor: it would corrupt an id that happened
            # to match a token pattern.
            if field_name in ("diagnosis", "resolution"):
                value = _safe_outbound(value)
            updates[field_name] = value
    # Captured BEFORE the write, because the desktop notification below must fire on the
    # EDGE into ``needs_human`` and not on every later write while it sits there.
    # ``update_fields`` re-enters ``transition`` with the SAME status on an unrelated
    # field edit, so without this an incident parked on an approval would re-toast on
    # each one.
    previous = await asyncio.to_thread(store.get_incident, incident_id)
    previous_status = previous.status if previous is not None else ""
    try:
        incident = await asyncio.to_thread(store.transition, incident_id, new_status, **updates)
    except KeyError:
        return web.json_response(
            {"error": "unknown incident", "code": "unknown_incident"}, status=404
        )
    except (json.JSONDecodeError, OSError) as exc:
        # BEFORE the `ValueError` arm, and that order is the whole point: a corrupt index
        # was being reported as `409 illegal_transition` carrying the JSON parser's message
        # -- corruption misclassified as the operator making an illegal move, which sends
        # them to fix their own request instead of the file. Found in review (Opus 4.8),
        # and it is the same `JSONDecodeError`-under-`ValueError` accident this change
        # already closed at three other callers.
        return _store_read_refusal(exc, code="dispatch_index")
    except ValueError as exc:
        # An illegal transition is a client error, not a server fault.
        return web.json_response({"error": str(exc), "code": "illegal_transition"}, status=409)
    _audit("incident_transition", f"{incident_id}->{new_status}", "success")

    # Refresh the Slack pin board so its line tracks the new state, and put any
    # new diagnosis/resolution in the thread. Both are no-ops when Slack output is
    # off, and neither can fail the transition — the state change is already
    # durable at this point.
    client = _slack_client(request)
    await slack_out.publish(incident, client)
    detail = updates.get("resolution") or updates.get("diagnosis") or ""
    if detail:
        await slack_out.post_detail(incident, detail, client)

    # Make the board thread answerable. Done HERE rather than at claim time because the
    # investigation slot does not exist yet when the incident is claimed — the dispatch
    # SOP creates it immediately afterwards and reports the key on its first transition.
    # Re-linking an already-linked thread is idempotent.
    refreshed = await asyncio.to_thread(store.get_incident, incident_id)
    if refreshed is not None:
        incident = refreshed
    # ON THE LOOP, deliberately — NOT `asyncio.to_thread`. Both of these reach
    # loop-owned objects in `DashboardState`: the link path mutates the slot dicts and
    # the reverse Slack index, and the notify path ends in `_deliver_note` ->
    # `_broadcast`, which does `Queue.put_nowait` on every SSE client's queue and
    # `asyncio.Event.set()`. Those primitives are not thread-safe: `Event.set` resolves
    # waiter futures through `loop.call_soon`, which CPython documents as callable only
    # from the loop's own thread (`call_soon_threadsafe` is the cross-thread door). Off
    # the loop it happens to work — the waiter future is marked done synchronously and
    # the loop notices on its next poll — which is exactly what makes it the wrong kind
    # of correct: a latent race that passes every test.
    #
    # Running them here is also strictly BETTER for the blocking I/O, which is why
    # `to_thread` is not needed to protect the loop: `_deliver_note` already offloads its
    # own disk append via `run_in_executor`, but only when it can see a running loop.
    # Called from a worker thread it took the `RuntimeError` fallback and wrote to disk
    # INLINE in that thread — so the thread hop bought no I/O isolation while costing
    # thread-safety. Found in review (GPT 5.6); the review proposed deleting both calls,
    # which would have removed the replyable-thread link and the needs-human alert —
    # features, not incidental work — so they are marshalled instead.
    thread_linked = slack_out.link_thread_to_investigation(incident, request.app.get("state"))

    # The one state change worth interrupting for: an incident now waiting on a person.
    # Only on the EDGE — a transition that leaves the status where it already was is the
    # unchanged condition the noise rule forbids re-notifying for. After Slack and after
    # the write, so it can cost neither.
    if new_status == STATUS_NEEDS_HUMAN and previous_status != STATUS_NEEDS_HUMAN:
        notify_out.notify_needs_human(
            request.app.get("state"),
            incident.incident_id,
            incident.signal.title,
            incident.blocked_reason,
        )

    return web.json_response(
        {
            "incident": incident.to_dict(),
            # Reported so a caller can tell whether a reply into the Slack thread will
            # actually reach the investigation, instead of assuming it will.
            "slack_thread_replyable": thread_linked,
        }
    )


async def _handle_claim(
    request: web.Request, *, get_registry: RegistryLookup, _audit: AuditWriter
) -> web.StreamResponse:
    """Manually claim a signal the operator picked off the board."""
    body = await _json_body(request)
    if body is None:
        return web.json_response(
            {"error": "request body must be a JSON object", "code": "body_not_object"}, status=400
        )
    raw_signal = body.get("signal")
    if not isinstance(raw_signal, dict):
        return web.json_response(
            {"error": "signal object is required", "code": "missing_required_field"}, status=400
        )
    claimed_id = str((raw_signal or {}).get("id", "")).strip()
    if not claimed_id:
        return web.json_response(
            {"error": "signal must carry an id", "code": "missing_required_field"}, status=400
        )

    # RESOLVE THE SIGNAL SERVER-SIDE, by id, from a fresh poll — do NOT authorize against the
    # caller-supplied object.
    #
    # `resolve_mode` matches act-rules on `source`, `resource` and `labels`. A caller who
    # controls the whole Signal can pair a resource the operator's rule authorizes
    # (`resource="prod-db-1"`, matching `resource_glob="prod-*"`) with a DIFFERENT provider's
    # target in `labels` (`dd_monitor_id=<someone else's monitor>`) — the resource satisfies
    # the gate while a different field drives the downstream sink. The authorization then
    # describes a signal that does not exist. Found in review.
    #
    # The provider is the authority on what is firing, so we ask it: poll, and take the
    # server's OWN copy of the signal with this id. The claim is refused if the id is not
    # currently firing. The board already sends a signal it got from `/signals`, so this
    # rejects only a fabricated or stale one — exactly the case that must not authorize a
    # write.
    # `poll_all` returns EVERY state — firing, ok and suppressed — so the state filter is
    # required and was missing. The local was even named `firing`, which is what hid it: a
    # signal that recovered between the board's poll and this one came back as `ok`, matched
    # on id alone, and minted an incident for a fault that had already cleared. The two other
    # `poll_all` consumers (`dispatch.run_cycle`, `GET /signals`) both filter explicitly.
    # Found in review.
    #
    # `suppressed` is excluded by the same predicate and must be: somebody parked that signal
    # at the provider, so claiming it is precisely what they asked not to happen.
    signals, _errors = await get_registry().poll_all()
    signal = next((s for s in signals if s.id == claimed_id and s.state == STATE_FIRING), None)
    if signal is None:
        return web.json_response(
            {
                "error": (
                    "no firing signal with that id — a manual claim authorizes against the "
                    "provider's current signal, not a caller-supplied one"
                ),
                "code": "signal_not_firing",
            },
            status=409,
        )

    mode = rotation.resolve_mode(signal)
    # `operator`, not the heartbeat default: this route IS the board's manual claim,
    # and telling the two apart afterwards is the whole point of the field.
    try:
        incident = await asyncio.to_thread(
            store.claim, signal, operating_mode=mode, claimed_by=CLAIMED_BY_OPERATOR
        )
    except (json.JSONDecodeError, OSError) as exc:
        # `claim` raises on both failures by design -- a compare-and-set has no safe
        # degraded answer, since `None` already means "another instance owns this signal".
        # The board's manual claim therefore needs the same coded translation as its
        # siblings; unwrapped it answered a bare 500. Found in review (Opus 4.8).
        return _store_read_refusal(exc, code="dispatch_index")
    if incident is None:
        return web.json_response(
            {"error": "signal is already claimed", "code": "signal_already_claimed"}, status=409
        )

    # Acknowledge the push spool here too. `dispatch.run_cycle` acks what IT claims, and this
    # route is the second place a claim becomes durable — so without this a hand-claimed
    # webhook signal stays spooled forever, and on a full (200-entry) spool the next signed
    # delivery evicts the OLDEST unclaimed entry to make room for it: a real alert lost to a
    # duplicate nobody needed. `poll()` does not consume, so nothing else on this path acks.
    #
    # Cheap and unconditional: `ack` on an id that is not spooled removes nothing, so no
    # source check is needed and a future push provider gets the same treatment for free.
    await asyncio.to_thread(webhook_mod.ack, {signal.id})

    # Attach what the ledger already knows, exactly as the heartbeat does — a
    # manual claim from the board must not start colder than an automatic one.
    #
    # The claim is already durably on disk and the webhook is already acked, so a fault from
    # HERE ON must not be reported as a failed claim. `attach_ledger_matches` writes through
    # `store.update_fields` -> `transition` -> `_read_index_for_update()`, which this change
    # gave two new ways to raise -- and unguarded it answered a bare uncoded 500 while every
    # sibling mutating route got the coded translation. Found in review (Opus 4.8).
    #
    # Deliberately NOT the coded refusal that review suggested, for the same reason
    # `_schedule_verification` reports rather than raises: a 503 here says "the claim failed,
    # retry" about a claim that SUCCEEDED, and the retry answers `409 signal_already_claimed`.
    # It would also skip the audit entry, leaving a real claim unrecorded. The ledger
    # annotation is the deferrable half -- the dispatch cycle re-derives matches on its next
    # pass -- so the honest degraded answer is the claim without its matches, audited as such.
    try:
        claimed = await asyncio.to_thread(dispatch.attach_ledger_matches, incident)
        claim_note = ""
    except (json.JSONDecodeError, OSError):
        logger.exception(
            "ops-mission-control: claimed %s but could not attach ledger matches",
            incident.incident_id,
        )
        claimed = dispatch.ClaimedIncident(incident=incident)
        claim_note = "claimed without ledger matches; the index was unreadable"
    # Broker the provider evidence too, for the same reason: the agent that picks this
    # up has no AWS credentials, so the gateway is the only thing that can read the
    # alarm history and logs it needs to diagnose. Non-fatal.
    claimed.evidence = await dispatch.gather_evidence_safely(get_registry(), signal)
    # Onto the pin board, exactly as the heartbeat does — a hand-claimed incident
    # must not be invisible to the channel watching the board.
    await slack_out.publish(claimed.incident, _slack_client(request))
    _audit("incident_claim", incident.incident_id, "success", error=claim_note)
    return web.json_response({**claimed.to_dict(), "brief": dispatch.investigation_brief(claimed)})


async def _handle_dispatch(request: web.Request) -> web.StreamResponse:
    """Run one dispatch cycle: poll, claim, match the ledger, release stale work.

    This is what the dispatch cron calls. It returns ``changed: false`` when
    nothing happened, which is the cron's signal to stay completely silent.

    **Deliberately NOT shift-gated**, unlike ``authorize_action``. Audited after the
    off-shift write hole, since this has the same shape — a route reachable independently
    of the tier that pauses its cron. The difference is what it does: claiming a signal and
    reading evidence changes nothing in the operator's tooling, whereas
    ``rotation.authorize_action`` guards an actual provider write.

    Its two callers are the dispatch cron (paused off shift by the tier gate, so the
    automated path IS gated) and the dashboard's "Check now" button — a deliberate human
    action. Blocking the button off shift would stop an operator from proving a
    freshly-configured provider works, which is the one thing they most need right after
    setup; and claiming is idempotent across the team because ``store.claim`` is a
    compare-and-set, so a second instance finds nothing left to claim rather than
    duplicating work.

    The residual exposure is a duplicate *investigation session* if two instances both
    dispatch by hand at once. That is a wasted turn, not a production change — the same
    trade the claim design already accepts (see ``store.claim``).
    """
    result = await dispatch.run_cycle(
        slack_client=_slack_client(request),
        # Threaded in for the local notification bus, which lives on gateway state.
        # Same explicit-dependency rule as the Slack client: no global accessor.
        state=request.app.get("state"),
    )
    payload = result.to_dict()
    # Give the caller a ready-to-use brief per claim so the investigating agent
    # does not spend its first turn re-fetching context Python already has.
    payload["briefs"] = {
        c.incident.incident_id: dispatch.investigation_brief(c) for c in result.claimed
    }
    return web.json_response(payload)

"""Human-authored turns: ordinary messages, decision answers, and recovery.

A decision answer is claimed as a durable outbox row before it is dispatched,
under the directory turn lock, and a running slot refuses it rather than
queueing it: a queued answer can be discarded by Pause while the ledger would
still report it delivered. App tokens cannot author either kind of turn.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from ..decisions import (
    _CLAIM_ALIAS_CONFLICT,
    _CLAIM_FULL,
    _CLAIM_PENDING,
    _CLAIM_RECORDED,
    _CLAIM_TAKEN,
    _CLAIM_UNREADABLE,
    _CLAIM_WRITE_FAILED,
    _claim_decision,
    _current_decision,
    _pending_decisions,
)
from ..parsers import _clean_str, _decision_answer_prompt, _decision_fingerprint, _decision_key
from ..repository import _aload_index, _audit, _pin_legacy_slot_identity, _touch_spec
from ..runtime import _dispatch_turn, _ensure_worker_slot, _reserve_slot_turn
from .decision_outbox import _deliver_pending_decision, _replay_pending_decision
from .dispatch_claims import (
    _bind_pending_dispatch_to_turn,
    _pending_dispatch_is_current,
    _release_pending_dispatch_when_done,
    _reserve_pending_dispatch,
)
from .request_identity import _STALE_CLIENT_ERROR, _read_json, _require_interactive_user
from .turn_guard import (
    _alias_slots,
    _alias_turn_snapshot,
    _busy_alias,
    _final_alias_conflict,
    _turn_lock,
)


async def _handle_recover_decision(request: web.Request) -> web.Response:
    """POST crash-recovery relay; never dispatch from the detail GET."""
    if denied := _require_interactive_user(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    fresh = await _touch_spec(
        name,
        expect_spec_dir=claimed_dir or None,
        expect_slot_key=claimed_key or None,
    )
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": _STALE_CLIENT_ERROR},
            status=409,
        )
    state = request.app["state"]
    slot = await _ensure_worker_slot(state, name, fresh)
    if slot is None:
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    recovered = await _replay_pending_decision(state, slot, name, fresh)
    return web.json_response({"ok": recovered})


async def _handle_message(request: web.Request) -> web.Response:
    if denied := _require_interactive_user(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    if name not in index:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"code": "text_required", "error": "text required"}, status=400)
    state = request.app["state"]
    # Re-reading commit BEFORE dispatch: the body read above awaits, so a
    # concurrent DELETE can land in that window. Stamping through the mutator
    # both refuses to resurrect a deleted spec and hands back the FRESH entry to
    # scope the slot from, instead of the pre-await snapshot.
    # Identity-pinned against the CLIENT'S captured spec_dir, not against the
    # index we just read: comparing the index to itself always matches, so the
    # check was vacuous. The SPA sends the spec_dir it rendered (from the detail
    # payload), which is what makes a stale tab detectable -- if the spec was
    # deleted and recreated elsewhere under the same name, that value does not
    # match and the instruction must not reach the replacement's agent. A caller
    # that sends no spec_dir cannot be pinned; it is then treated as unpinned
    # rather than refused, so an older client keeps working.
    # The slot key rides along because a directory does NOT identify a creation:
    # delete leaves the documents on disk, so a re-import at the same name AND
    # path passes a spec_dir check while being a different spec with a different
    # conversation -- and this instruction would land in the replacement's chat.
    claimed_dir = str(body.get("spec_dir", "") or "").strip()
    claimed_key = str(body.get("slot_key", "") or "").strip()
    # Present only when this message is a decision card's answer.
    #
    # Both values go through _clean_str -- the SAME projection _normalize_spec_state
    # applies -- and through nothing else. Two reasons, and both were defects:
    #
    #  * the id becomes the ledger KEY, and the overlay matches it against the id
    #    the detail read serves. A different normalization here (a strip, a shorter
    #    cap) makes the two disagree for whitespace-bearing or long ids, and a
    #    disagreement is invisible: the answer is recorded, no card is ever locked,
    #    and the decision stays re-answerable.
    #  * the OPTION is what gets recorded and later rendered as the answer. The
    #    composed prompt ("Decision — <title>: <option>", localized) must not be:
    #    the card would show the whole sentence back instead of the choice.
    decision_id = _clean_str(body.get("decision_id"))
    decision_option = _clean_str(body.get("decision_option"))
    if decision_id and not decision_option:
        return web.json_response(
            {
                "code": "decision_option_required",
                "error": "decision_option required with decision_id",
            },
            status=400,
        )
    fresh = await _touch_spec(
        name, expect_spec_dir=claimed_dir or None, expect_slot_key=claimed_key or None
    )
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": _STALE_CLIENT_ERROR},
            status=409,
        )
    fresh = await _pin_legacy_slot_identity(name, fresh)
    if fresh is None:
        return web.json_response(
            {"code": "stale_client", "error": _STALE_CLIENT_ERROR},
            status=409,
        )
    slot = await _ensure_worker_slot(state, name, fresh)
    if slot is None:
        # Another app owns this slot key (see _ensure_worker_slot). Refuse rather
        # than dispatching a turn into a session we do not own.
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    # Pure lexical work on the loop. Alias discovery below reads the index off-loop,
    # but must happen only after this request owns the directory lock.
    dir_key = _decision_key(str(fresh.get("spec_dir", "")))
    current_decision: dict[str, Any] | None = None
    # The turn lock spans the running-check, the claim and the dispatch, so no other
    # handler can start a turn on this spec in between -- see _TURN_LOCKS. Acquired
    # BEFORE the re-pin so the last await before the dispatch is still a pinning one.
    async with _turn_lock(dir_key):
        expected_slot_key = str(fresh.get("slot_key", ""))
        dispatch_claim = _reserve_pending_dispatch(dir_key, expected_slot_key, name)
        if not dispatch_claim:
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        "another request is starting or stopping work on these files; "
                        "retry shortly"
                    ),
                },
                status=409,
            )
        _release_pending_dispatch_when_done(dispatch_claim)
        # Every OTHER name on this directory, read only after entering the lock. The
        # index is agent-writable, so an alias can be added while this request waits;
        # scanning before the wait would miss a newly-busy alias and admit a second
        # agent over the same files. The filesystem work stays off the event loop.
        aliases = await _alias_slots(
            dir_key,
            own_slot_key=expected_slot_key or str(getattr(slot, "key", "")),
        )
        # An alias mid-turn is a SECOND session over these documents, so its turn is a
        # concurrent editor no matter what this request carries -- a decision answer, an
        # ordinary message, anything. Refused for all of them.
        #
        # Our OWN slot is excluded from `aliases`, which is what preserves same-slot
        # queuing: a message to the session that is running is queued by _dispatch_turn
        # (the established behaviour), while a decision answer to it is refused below --
        # a queued answer may never be delivered, and the ledger would claim it was.
        if busy_under := _busy_alias(state, aliases):
            _audit("spec_busy_elsewhere", f"{name}: {busy_under}", outcome="denied")
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        f"another view of this spec ({busy_under}) has an agent working on "
                        "these files; wait for it to finish"
                    ),
                },
                status=409,
            )
        alias_snapshot = _alias_turn_snapshot(state, aliases)
        # Re-pin after slot acquisition. _ensure_worker_slot awaits (it revalidates the
        # working dir off the event loop), so a delete can start AND finish between the
        # check above and this line -- handing the turn to a slot whose spec is gone.
        #
        # BOTH pins come from `fresh` -- the entry this request already verified -- not
        # from the client body. `slot_key` is optional on the wire (an older client that
        # sends none is treated as unpinned rather than refused), so reusing the CLAIMED
        # value here meant a request without one had no creation pin on the second check:
        # a delete plus a same-path recreate passed it, because spec_dir still matched,
        # and the stale slot wrote into the replacement's files. The captured value is
        # server-side data, so pinning to it is strictly stronger AND still lets an older
        # client through the first check.
        if (
            await _touch_spec(
                name,
                expect_spec_dir=fresh.get("spec_dir"),
                expect_slot_key=str(fresh.get("slot_key", "")) or None,
            )
            is None
        ):
            return web.json_response(
                {
                    "code": "stale_client",
                    "error": _STALE_CLIENT_ERROR,
                },
                status=409,
            )
        # A decision answer is claimed before it is dispatched, and a decision that is
        # already recorded is refused outright -- the agent has that answer and is
        # acting on it, so a second one would silently reverse a settled decision. The
        # claim is atomic (see _claim_decision), so two concurrent clicks on the same
        # card resolve to exactly one dispatched answer rather than two turns.
        #
        # A RUNNING slot is refused rather than queued. _dispatch_turn queues into a turn
        # that is already in flight, and a Pause (or Stop, or Delete) clears that queue by
        # design -- ending a turn must not let the agent keep working. So a queued answer
        # is an answer that may never be delivered, while the ledger would go on claiming
        # it was.
        #
        # The check is trustworthy for every Spec Builder entry point because the turn
        # lock is held through delivery. The claim itself is pending until relay, so a
        # process exit in that window is replayed rather than treated as final.
        if decision_id and getattr(slot, "running", False):
            return web.json_response(
                {
                    "code": "decision_agent_busy",
                    "error": "the agent is working on this spec; answer the decision once it stops",
                    "decision_id": decision_id,
                },
                status=409,
            )
        turn_reservation: asyncio.Task[Any] | None = None
        if decision_id:
            # Publish the claim-in-progress through the slot's ordinary ``running``
            # surface before the first validation await. Dashboard chat can start the
            # same app-owned slot without this module's directory lock; it must queue
            # behind the answer rather than replace the question between validation
            # and the durable claim.
            turn_reservation = _reserve_slot_turn(state, slot)
            if turn_reservation is None:
                return web.json_response(
                    {
                        "code": "decision_agent_busy",
                        "error": (
                            "the agent is working on this spec; answer the decision once "
                            "it stops"
                        ),
                        "decision_id": decision_id,
                    },
                    status=409,
                )
            # A card is a snapshot. Validate it only after serialization and the
            # final identity/idle checks: while this request waited for the lock, the
            # preceding agent turn could replace or remove the question. Reading it
            # before the wait would claim and deliver an answer for stale state.
            current_decision, decision_state_usable = await asyncio.to_thread(
                _current_decision, Path(str(fresh.get("spec_dir", ""))), decision_id
            )
            if not decision_state_usable:
                _audit(
                    "spec_decision_state_unreadable",
                    f"{name}: {decision_id}",
                    outcome="denied",
                )
                return web.json_response(
                    {
                        "code": "decision_state_unreadable",
                        "error": "this decision could not be verified; reload and retry",
                        "decision_id": decision_id,
                    },
                    status=503,
                )
            if current_decision is None:
                _audit("spec_decision_not_found", f"{name}: {decision_id}", outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_not_found",
                        "error": "this decision is no longer present; reload before answering",
                        "decision_id": decision_id,
                    },
                    status=409,
                )
            offered_options = list(current_decision.get("options") or [])
            if offered_options and decision_option not in offered_options:
                _audit("spec_decision_option_stale", f"{name}: {decision_id}", outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_option_not_offered",
                        "error": (
                            "this decision's options have changed; reload and choose from "
                            "the current options"
                        ),
                        "decision_id": decision_id,
                    },
                    status=409,
                )
            fingerprint = _decision_fingerprint(current_decision or {})
            delivery_id = uuid.uuid4().hex
            outcome, held = await _claim_decision(
                name,
                decision_id,
                decision_option,
                expect_spec_dir=str(fresh.get("spec_dir", "")),
                expect_slot_key=str(fresh.get("slot_key", "")),
                fingerprint=fingerprint,
                message=_decision_answer_prompt(current_decision, decision_option),
                delivery_id=delivery_id,
            )
            if outcome == _CLAIM_TAKEN:
                return web.json_response(
                    {
                        "code": "decision_already_answered",
                        "error": "this decision was already sent to the agent and cannot be changed",
                        "decision_id": decision_id,
                        "answer": _clean_str(held),
                    },
                    status=409,
                )
            if outcome == _CLAIM_FULL:
                _audit("spec_decision_ledger_full", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_ledger_full",
                        "error": "too many recorded decisions for this spec",
                    },
                    status=409,
                )
            if outcome == _CLAIM_ALIAS_CONFLICT:
                _audit("spec_decision_directory_alias_conflict", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_directory_alias_conflict",
                        "error": "multiple spec names resolve to this directory; repair the spec index before continuing",
                    },
                    status=409,
                )
            if outcome == _CLAIM_UNREADABLE:
                # The record exists but could not be read. Writing would erase every
                # answer in it, so nothing is recorded and nothing is dispatched.
                _audit("spec_decision_record_unreadable", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_record_unreadable",
                        "error": "this spec's recorded decisions could not be read; retry shortly",
                    },
                    status=503,
                )
            if outcome == _CLAIM_WRITE_FAILED:
                # The record could not be written (a full or unwritable data home), so
                # nothing was recorded and nothing is dispatched.
                _audit("spec_decision_record_write_failed", name, outcome="denied")
                return web.json_response(
                    {
                        "code": "decision_record_write_failed",
                        "error": "this spec's recorded decisions could not be written; retry shortly",
                    },
                    status=503,
                )
            if outcome != _CLAIM_RECORDED:
                if outcome != _CLAIM_PENDING:
                    return web.json_response(
                        {
                            "code": "stale_client",
                            "error": _STALE_CLIENT_ERROR,
                        },
                        status=409,
                    )
            pending = next(
                (
                    entry
                    for entry in await _pending_decisions(str(fresh.get("spec_dir", "")))
                    if entry.get("decision_id") == decision_id
                    and entry.get("fingerprint") == fingerprint
                ),
                None,
            )
            active_delivery_id = (
                pending.get("delivery_id", "") if pending is not None else delivery_id
            )
            delivered = pending is not None and await _deliver_pending_decision(
                state,
                slot,
                str(fresh.get("spec_dir", "")),
                pending,
                turn_reservation=turn_reservation,
                initial_aliases=aliases,
                alias_snapshot=alias_snapshot,
                own_name=name,
                expected_slot_key=expected_slot_key,
                dispatch_claim=dispatch_claim,
            )
            if not delivered:
                exact_still_pending = any(
                    entry.get("decision_id") == decision_id
                    and entry.get("fingerprint") == fingerprint
                    and entry.get("delivery_id") == active_delivery_id
                    for entry in await _pending_decisions(str(fresh.get("spec_dir", "")))
                )
                if not exact_still_pending:
                    _audit(
                        "spec_decision_changed_before_delivery",
                        f"{name}: {decision_id}",
                        outcome="denied",
                    )
                    return web.json_response(
                        {
                            "code": "decision_changed_before_delivery",
                            "error": (
                                "this decision changed before the answer reached the "
                                "agent; reload and answer the current question"
                            ),
                            "decision_id": decision_id,
                        },
                        status=409,
                    )
                _audit(
                    "spec_decision_delivery_pending",
                    f"{name}: {decision_id}",
                    outcome="denied",
                )
                return web.json_response(
                    {
                        "code": "decision_delivery_pending",
                        "error": "the answer is saved and will be delivered when the agent is available",
                        "decision_id": decision_id,
                    },
                    status=503,
                )
            _audit("spec_decision_answered", f"{name}: {decision_id}")
        else:
            # The ordinary message path also awaited the identity re-pin above.
            # Dashboard chat can run a different alias during that hop, including
            # a complete turn whose task has already returned to None. Re-scan after
            # the last await and publish this task synchronously if still uncontested.
            if busy_under := await _final_alias_conflict(
                state,
                dir_key,
                expected_slot_key or str(getattr(slot, "key", "")),
                aliases,
                alias_snapshot,
                own_name=name,
            ):
                _audit("spec_busy_elsewhere", f"{name}: {busy_under}", outcome="denied")
                return web.json_response(
                    {
                        "code": "spec_busy_elsewhere",
                        "error": (
                            f"another view of this spec ({busy_under}) has an agent "
                            "working on these files; wait for it to finish"
                        ),
                    },
                    status=409,
                )
            if not _pending_dispatch_is_current(dispatch_claim):
                return web.json_response(
                    {
                        "code": "execution_stopped_during_start",
                        "error": "the message was stopped before it reached the agent",
                    },
                    status=409,
                )
            turn = _dispatch_turn(
                state,
                slot,
                text,
                directive_user_origin=True,
            )
            _bind_pending_dispatch_to_turn(dispatch_claim, slot, turn)
        _audit("spec_message", name)
        return web.json_response({"ok": True})

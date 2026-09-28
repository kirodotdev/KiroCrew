"""Execute and Stop: starting an autonomous build and halting it.

Execute claims the run before any side effect, arms its bounded nudge loop
through the shared authorization chokepoint, and dispatches only after a final
identity, claim and alias check; every refusal after the claim unwinds exactly
what that request created. Stop publishes its revocation before waiting for the
directory lock, so it wins against a handoff suspended mid-authorization.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from ..decisions import _CLAIM_TAKEN
from ..parsers import _decision_key
from ..repository import (
    _aload_index,
    _audit,
    _pin_legacy_slot_identity,
    _prepare_handoff,
    _slot_key,
    _touch_spec,
)
from ..runtime import _dispatch_turn, _ensure_worker_slot, _halt_active_turn, _teardown_worker_slot
from .dispatch_claims import (
    _bind_execution_claim_to_turn,
    _drop_execution_claim,
    _drop_execution_claim_if_owner,
    _execution_claim_is_current,
    _execution_stop_barrier,
    _reserve_execution_claim,
)
from .execution_state import (
    _CLAIM_OK,
    _EXEC_MAX_CYCLES,
    _autonudge_instance,
    _claim_execution,
    _exec_loop_id,
    _exec_prompt,
    _halt_execution,
    _remove_nudge_loop_for_slot,
    authorize_and_add_nudge,
)
from .request_identity import (
    _STALE_CLIENT_ERROR,
    _client_claim,
    _client_identity_mismatch,
    _require_auth,
)
from .turn_guard import (
    _alias_slots,
    _alias_turn_snapshot,
    _busy_alias,
    _final_alias_conflict,
    _turn_lock,
)

logger = logging.getLogger("kirocrew.app.spec-builder")


async def _handle_handoff(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    meta = await _pin_legacy_slot_identity(name, meta)
    if meta is None:
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    spec_dir = Path(meta["spec_dir"])
    working_dir = meta.get("working_dir", "")
    # Captured BEFORE the await below, so the reread can compare against the
    # identity this request started with rather than re-deriving one.
    started_slot_key = str(meta.get("slot_key", ""))
    # Parse and check the CLIENT's claim before the destructive call below, the
    # same ordering _handle_stop_execution documents. _prepare_handoff clears the
    # STOP sentinel, so a stale same-name execute that got this far would disarm a
    # replacement's Pause before any identity comparison had run.
    claimed = await _client_claim(request)
    if _client_identity_mismatch(claimed, spec_dir, started_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # One thread hop for every filesystem touch this handler needs: the identity
    # re-check, the tasks.md gate, clearing a stale STOP sentinel from a prior run
    # (symlink-safe), and resolving the sentinel path the autonudge arm requires.
    # name + started_slot_key make the CLEAR itself conditional on identity, which
    # is the half a claim comparison cannot cover for a claimless request.
    has_tasks, sentinel_path = await asyncio.to_thread(
        _prepare_handoff, spec_dir, name, started_slot_key
    )
    if not has_tasks:
        return web.json_response(
            {
                "code": "tasks_missing",
                "error": "tasks.md has no unchecked tasks yet — finish the Tasks phase first",
            },
            status=409,
        )
    # Reread AFTER the await as well: a delete+recreate can land during the thread
    # hop, and a stale request would then capture the REPLACEMENT's slot while its
    # own abort path -- correctly pinned to what it captured -- closed the new
    # session. This is what protects slot acquisition.
    current = await _aload_index()
    meta = current.get(name)
    # Pinned on the per-creation slot key as well as the directory. A delete +
    # re-import at the same name AND path leaves spec_dir identical, so the
    # directory alone cannot distinguish our spec from the replacement -- and the
    # slot_key check below only validates the CLIENT's claim, so a request that
    # carries no claim had no identity check at all.
    if (
        not meta
        or str(meta.get("spec_dir", "")) != str(spec_dir)
        or str(meta.get("slot_key", "")) != started_slot_key
    ):
        return web.json_response(
            {
                "code": "spec_changed_during_start",
                "error": "spec was deleted or recreated while starting; retry",
            },
            status=409,
        )
    working_dir = meta.get("working_dir", "")
    if _client_identity_mismatch(claimed, spec_dir, str(meta.get("slot_key", ""))):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    state = request.app["state"]
    # FAIL CLOSED. Falling through to a single turn would bypass the authorization
    # chokepoint, including slot ownership, message bounds, sentinel checks and SEL
    # audit. An unauthorized run is not a degraded run.
    svc = _autonudge_instance() if _autonudge_instance is not None else None
    if svc is None or authorize_and_add_nudge is None:
        _audit("spec_handoff_denied", f"{name}: autonudge unavailable", outcome="denied")
        return web.json_response(
            {
                "code": "autonudge_unavailable",
                "error": (
                    "autonomous execution is unavailable: the auto-nudge service is not "
                    "running, so the run cannot be authorized or bounded"
                ),
            },
            status=503,
        )

    # CLAIM the run before any side effect: one atomic compare-and-set that both
    # refuses a second handoff and records the execution state. Reading the status
    # here and committing it further down was not a guard at all -- two concurrent
    # requests both read "planning", both passed, and both dispatched, so Pause
    # cancelled one prompt while the other drained and kept editing the user's
    # files. The decision and the write are now the same index mutation.
    #
    # Recording BEFORE arming also matters on its own: the arm is shielded and
    # survives a restart, so arming first left a window where a shutdown persisted
    # a timer with no execution state -- and the restored timer ran something Pause
    # could not stop, because Pause keys off that state.
    captured_slot_key = str(meta.get("slot_key", ""))
    handoff_dir_key = _decision_key(str(spec_dir))
    execution_claim, reservation_refusal = _reserve_execution_claim(
        handoff_dir_key, captured_slot_key, name
    )
    if not execution_claim:
        stopping = reservation_refusal == "stopping"
        return web.json_response(
            {
                "code": "execution_stopping" if stopping else "already_executing",
                "error": (
                    "this spec is being stopped; wait for Stop to finish"
                    if stopping
                    else "this spec is already starting; wait for it to finish"
                ),
            },
            status=409,
        )

    # A cancelled HTTP request must not leave a process-owned claim behind. The
    # conditional drop cannot release a newer request's generation.
    handler_task = asyncio.current_task()
    if handler_task is not None:

        def _release_abandoned_claim(_done: asyncio.Task[Any]) -> None:
            _drop_execution_claim_if_owner(handoff_dir_key, execution_claim, _done)

        handler_task.add_done_callback(_release_abandoned_claim)

    # Serialize the durable claim itself with Stop. Stop publishes its barrier
    # before waiting for this lock, so a Stop that gets here first revokes the
    # token before any ``executing`` write. If this write gets here first, Stop
    # cannot report success until it has overwritten that exact state with
    # ``planning``. There is therefore no late claim write after a successful Stop.
    async with _turn_lock(handoff_dir_key):
        if not _execution_claim_is_current(handoff_dir_key, execution_claim):
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "execution was stopped before it started",
                },
                status=409,
            )
        live_slot = state.get_slot(_slot_key(name)) if state is not None else None
        try:
            claim, committed = await _claim_execution(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=captured_slot_key,
                live_running=bool(getattr(live_slot, "running", False)),
            )
        except Exception:
            # Nothing has been created yet, so there is nothing to unwind -- but the
            # run must not proceed on an unrecorded state, because Pause keys off it.
            _drop_execution_claim(handoff_dir_key, execution_claim)
            logger.warning("could not claim execution for %s", name, exc_info=True)
            return web.json_response(
                {
                    "code": "exec_state_write_failed",
                    "error": "could not record execution state; the run was not started",
                },
                status=500,
            )
        if claim == _CLAIM_TAKEN:
            _drop_execution_claim(handoff_dir_key, execution_claim)
            return web.json_response(
                {
                    "code": "already_executing",
                    "error": "this spec is already building; pause it before starting again",
                },
                status=409,
            )
        if claim != _CLAIM_OK:
            _drop_execution_claim(handoff_dir_key, execution_claim)
            return web.json_response(
                {
                    "code": "spec_changed_during_start",
                    "error": "spec was deleted or recreated while starting; retry",
                },
                status=409,
            )
        meta = committed or meta
    # Did the slot ALREADY exist? The unwind path below must only close a slot
    # this request created: a pre-existing one carries the user's conversation
    # (and possibly a running turn), and destroying it because a later index
    # write failed loses work the handoff never owned.
    slot_pre_existed = live_slot is not None
    # Tool calls are NOT auto-approved: the user approves (or clicks Trust) from
    # the embedded chat's approval card. The run is bounded by the STOP SENTINEL,
    # the Stop button, and a capped nudge cycle count.
    slot = await _ensure_worker_slot(state, name, meta)
    if slot is None:
        # Another app owns this slot key (see _ensure_worker_slot). Refuse rather
        # than dispatching a turn into a session we do not own -- and give the
        # claim back, or the spec stays marked executing with nothing running.
        if _execution_claim_is_current(handoff_dir_key, execution_claim):
            await _touch_spec(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=captured_slot_key or None,
                status="planning",
                exec_started_at=0.0,
                exec_arming_at=0.0,
            )
            _drop_execution_claim(handoff_dir_key, execution_claim)
        return web.json_response(
            {
                "code": "slot_owned_by_another_app",
                "error": "this spec's chat session is owned by another app",
            },
            status=409,
        )
    prompt = _exec_prompt(name, spec_dir, working_dir)
    # Arm the autonudge loop through the SHARED AUTHORIZATION CHOKEPOINT so this
    # app enforces the same slot-ownership checks, message limits, sensitive
    # stop_sentinel_path refusal and SEL audit as POST /api/autonudge. Calling
    # svc.add directly (as this did) bypassed all of it, and max_cycles=0 meant
    # an unbounded loop. Fails CLOSED: if authorization is refused we do not
    # dispatch the autonomous turn.

    async def _release(reason: str, *, loop_id: str | None = None) -> None:
        """Undo ONLY what this request created, in the reverse order it was created.

        Both the loop and the slot are looked up by name, so an unpinned abort
        would cancel the loop and destroy the slot of a same-name spec that
        replaced ours.
        """
        if loop_id:
            try:
                await _remove_nudge_loop_for_slot(
                    str(getattr(slot, "key", "")),
                    only_loop_id=loop_id,
                    stop_reason="spec_arm_aborted",
                )
            except Exception:
                # Best-effort HERE only: this is already an abort path, and the
                # reason that brought us here is the story worth surfacing. Logged
                # loudly because a surviving loop can still nudge.
                logger.warning(
                    "spec %s: could not remove the armed loop while unwinding",
                    name,
                    exc_info=True,
                )
        # Put the recorded state back only while this request still owns the
        # process claim. Stop revokes the token before waiting for this lock, and
        # a stale unwind must not overwrite Stop or tear down a newer request's slot.
        owned = _execution_claim_is_current(handoff_dir_key, execution_claim)
        if owned:
            try:
                await _touch_spec(
                    name,
                    expect_spec_dir=str(spec_dir),
                    expect_slot_key=captured_slot_key or None,
                    status="planning",
                    exec_started_at=0.0,
                    exec_arming_at=0.0,
                )
            except Exception:
                logger.warning(
                    "spec %s: could not clear the execution state while unwinding",
                    name,
                    exc_info=True,
                )
            owned = _drop_execution_claim(handoff_dir_key, execution_claim)
        if owned and not slot_pre_existed:
            await _teardown_worker_slot(state, name, only_slot=slot)
        _audit("spec_handoff_aborted", f"{name}: {reason}", outcome="denied")

    # The turn lock is acquired BEFORE the loop is armed, and held through the FINAL
    # freshness check and the dispatch. Arming first meant a 120s idle timer was already
    # running while this handler waited for the lock: a long wait let the loop dispatch
    # the build on its own, so a decision answer recorded under the lock queued behind a
    # turn nobody here started, and Pause could discard it.
    #
    # The busy check precedes arming so a refusal cannot leave a timer that later
    # dispatches the build it denied.
    async with _turn_lock(handoff_dir_key):
        # The execution claim is recorded before this lock is acquired. Stop takes
        # the same lock, but can get there first while this request is materializing
        # its slot: it then commits ``planning`` and reports success. Re-read both the
        # creation and the process-owned claim inside the lock, before arming
        # anything. The index is agent-writable, so its status and timestamps may
        # fail closed but can never authenticate ownership of this request.
        handoff_index = await _aload_index()
        handoff_meta = handoff_index.get(name) or {}
        same_creation = bool(
            handoff_meta
            and str(handoff_meta.get("spec_dir", "")) == str(spec_dir)
            and str(handoff_meta.get("slot_key", "")) == captured_slot_key
        )
        same_claim = bool(
            same_creation
            and str(handoff_meta.get("status", "")) == "executing"
            and _execution_claim_is_current(handoff_dir_key, execution_claim)
        )
        if not same_claim:
            stopped = not _execution_claim_is_current(handoff_dir_key, execution_claim)
            stopped = stopped or (
                same_creation and str(handoff_meta.get("status", "")) == "planning"
            )
            reason = "stopped before dispatch" if stopped else "execution claim changed"
            await _release(reason)
            return web.json_response(
                {
                    "code": (
                        "execution_stopped_during_start" if stopped else "spec_changed_during_start"
                    ),
                    "error": (
                        "execution was stopped before it started"
                        if stopped
                        else "spec or execution changed while starting; retry"
                    ),
                },
                status=409,
            )
        # The index is agent-writable, so discover aliases only after entering the
        # directory lock. A pre-lock snapshot can miss an alias added while this
        # request waits, after that alias has started work under the shared lock.
        handoff_aliases = await _alias_slots(
            handoff_dir_key,
            own_slot_key=captured_slot_key or str(getattr(slot, "key", "")),
        )
        # A handoff starts an autonomous build. Another name on this directory that is
        # mid-turn is a second agent already editing these files, so the build waits --
        # the same refusal an ordinary message gets, for the same reason.
        #
        # Nothing is armed yet, so this refusal has no loop_id to release.
        if busy_under := _busy_alias(state, handoff_aliases):
            await _release(f"busy under {busy_under}")
            _audit("spec_handoff_denied", f"{name}: busy under {busy_under}", outcome="denied")
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
        handoff_alias_snapshot = _alias_turn_snapshot(state, handoff_aliases)
        try:
            armed_loop, authz_err, _status = await authorize_and_add_nudge(
                svc=svc,
                state=state,
                slot_key=slot.key,
                message=prompt,
                idle_secs=120,
                max_cycles=_EXEC_MAX_CYCLES,
                stop_sentinel_path=sentinel_path,
                source="app:spec-builder",
                caller=str(request.get("user") or ""),
            )
        except Exception:
            logger.warning("autonudge arm raised for %s — refusing handoff", name, exc_info=True)
            await _release("authorization raised")
            _audit("spec_handoff_denied", f"{name}: authorization raised", outcome="denied")
            return web.json_response(
                {
                    "code": "authorization_failed",
                    "error": "could not authorize autonomous execution",
                },
                status=503,
            )
        if authz_err:
            # No trust to revoke (we never granted any), and revoking here would undo
            # a trust decision the user made themselves. The recorded execution state
            # IS ours to revoke, and _release does that.
            await _release(f"authorization refused: {authz_err}")
            _audit("spec_handoff_denied", f"{name}: {authz_err}", outcome="denied")
            return web.json_response(
                {
                    "code": "authorization_refused",
                    "error": f"could not start autonomous execution: {authz_err}",
                },
                status=403,
            )
        # Stop publishes its barrier before it waits for this directory lock, so it
        # can revoke a handoff while authorization is awaiting audit or persistence.
        # The armed loop is ours and must be removed, but Stop owns the durable
        # transition to ``planning`` once it has revoked this token.
        if not _execution_claim_is_current(handoff_dir_key, execution_claim):
            await _release(
                "stopped during authorization",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "execution was stopped before it started",
                },
                status=409,
            )
        # Authorization awaits outside the slot's own dispatch machinery. A channel
        # message can therefore start this same slot while the request is suspended,
        # even though Spec Builder handlers share the directory lock. Dispatching now
        # would QUEUE the build, and Pause clears that queue while this endpoint reports
        # success. Recheck the live slot after the await and unwind the loop we armed.
        if getattr(slot, "running", False):
            await _release(
                "the spec agent became busy during authorization",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "spec_agent_busy",
                    "error": "the spec agent started another turn; wait for it to finish",
                },
                status=409,
            )
        # The same turn lock the message and delete paths take, held across the FINAL
        # freshness check AND the dispatch. Two orderings depend on that span: a decision
        # answer must not be queued behind a build starting here (Pause would drop it),
        # and a DELETE must not slip between this check and the dispatch -- holding the
        # lock only for the dispatch left exactly that window, so the turn started on a
        # spec the delete had already removed.
        # Arming awaits too, so re-verify the creation once more. A DELETE landing in
        # that window tears down the slot and the loops it can see BY NAME -- ours
        # arrives after, and would be left nudging a spec the delete removed.
        # Committing before arming would catch this at the commit; arming last means
        # it has to be caught here.
        refreshed = await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=captured_slot_key or None,
            # The loop is armed: the reconciler can see it now, so the pre-arm
            # exemption must end here rather than expire on the grace window.
            exec_arming_at=0.0,
        )
        if (
            refreshed is None
            or str(refreshed.get("status", "")) != "executing"
            or not _execution_claim_is_current(handoff_dir_key, execution_claim)
        ):
            stopped = not _execution_claim_is_current(handoff_dir_key, execution_claim)
            await _release(
                (
                    "stopped during final execution check"
                    if stopped
                    else "deleted or recreated during authorization"
                ),
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": (
                        "execution_stopped_during_start" if stopped else "spec_changed_during_start"
                    ),
                    "error": (
                        "execution was stopped before it started"
                        if stopped
                        else "spec was deleted or recreated while execution was starting"
                    ),
                },
                status=409,
            )
        # Authorization and the freshness write await while dashboard chat can run
        # another alias without this directory lock. Re-scan after those waits and
        # compare its monotonic turn history; normal teardown clearing task=None must
        # not erase the evidence. An armed loop belongs to this refused handoff, so
        # unwind it along with the recorded execution claim.
        if busy_under := await _final_alias_conflict(
            state,
            handoff_dir_key,
            captured_slot_key or str(getattr(slot, "key", "")),
            handoff_aliases,
            handoff_alias_snapshot,
            own_name=name,
        ):
            await _release(
                f"alias became busy during authorization: {busy_under}",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "spec_busy_elsewhere",
                    "error": (
                        f"another view of this spec ({busy_under}) worked on these "
                        "files while execution was starting; retry after it finishes"
                    ),
                },
                status=409,
            )
        # This is synchronous with the same-slot busy check and dispatch below.
        # A Stop or another handoff may revoke the token during the alias await,
        # but nothing can replace it between this check and task publication.
        if not _execution_claim_is_current(handoff_dir_key, execution_claim):
            await _release(
                "stopped during the final alias check",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "execution_stopped_during_start",
                    "error": "execution was stopped before it started",
                },
                status=409,
            )
        # The alias scan above is the final await before dispatch. Channel traffic can
        # also start this same slot while that scan is off-loop. Refuse synchronously;
        # otherwise _dispatch_turn queues the build behind the channel turn and a
        # later Pause can discard it after this endpoint reported success.
        if getattr(slot, "running", False):
            await _release(
                "the spec agent became busy during the final freshness check",
                loop_id=getattr(armed_loop, "id", None),
            )
            return web.json_response(
                {
                    "code": "spec_agent_busy",
                    "error": "the spec agent started another turn; wait for it to finish",
                },
                status=409,
            )
        turn = _dispatch_turn(state, slot, prompt)
        _bind_execution_claim_to_turn(handoff_dir_key, execution_claim, slot, turn)
    _audit("spec_handoff", name)
    return web.json_response({"ok": True, "status": "executing"})


async def _handle_stop_execution(request: web.Request) -> web.Response:
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    # Parse the body FIRST. Reading it is an await, so doing it after the index
    # read reopened the very window the capture below is meant to close: a
    # delete+recreate landing while a slow request body arrived left the index
    # snapshot (and the identity check against it) describing the OLD spec while
    # the loop id and slot captured afterwards belonged to the REPLACEMENT, whose
    # run this request would then cancel.
    claimed = await _client_claim(request)
    index = await _aload_index()
    meta = index.get(name)
    if not meta:
        return web.json_response({"code": "not_found", "error": "not found"}, status=404)
    spec_dir = Path(meta["spec_dir"])
    # Stop is destructive, so it takes the SAME directory turn lock the message,
    # handoff and delete paths take. Without it Stop was the one way to interleave
    # with a decision answer: that path records the answer and dispatches it under
    # this lock, and an unserialized Stop landing between those two steps cancelled
    # the dispatched turn while the recorded answer stood -- leaving a card locked
    # to an answer the agent never received. The record is deliberately never
    # rewritten (a rewrite is how a decision gets reversed), so the fix is to stop
    # the interleaving rather than to undo the write: with the lock there are two
    # orderings instead of three, and both are honest. Answer then Stop cancels a
    # turn that really was dispatched; Stop then answer refuses at the busy check.
    dir_key = _decision_key(str(spec_dir))
    # Keep both identities. The raw key pins the mutable index row across awaits;
    # the monotonic resolver key identifies the live worker and is what detail gave
    # the client. They legitimately differ after an agent rewrites index.json.
    original_index_slot_key = str(meta.get("slot_key", ""))
    original_slot_key = _slot_key(name)
    # A stale tab must be refused before it can publish a Stop barrier. There is
    # no await between this check and entering the creation-scoped barrier below.
    if _client_identity_mismatch(claimed, spec_dir, original_slot_key):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    # Publish the Stop before waiting for the directory lock. A handoff may be
    # suspended inside authorization while holding that lock; revoking its
    # process-owned generation makes it unwind the loop instead of dispatching,
    # and the barrier refuses any restart for this creation until Stop commits.
    async with (
        _execution_stop_barrier(dir_key, original_slot_key, name) as claimed_slot_keys,
        _turn_lock(dir_key),
    ):
        # Re-read INSIDE the lock. Acquiring it is an await, so the snapshot above can
        # describe a spec that was replaced while this request waited, and the identity
        # check has to judge the spec actually about to be halted.
        index = await _aload_index()
        meta = index.get(name)
        if not meta:
            return web.json_response({"code": "not_found", "error": "not found"}, status=404)
        spec_dir = Path(meta["spec_dir"])
        if str(meta.get("slot_key", "")) != original_index_slot_key:
            # A different creation now holds this name. Halting would write a STOP
            # sentinel for, and cancel the run of, a spec this request never verified.
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        if _decision_key(str(spec_dir)) != dir_key:
            # Kept alongside the slot-key check because it answers a different question:
            # the index is agent-writable, so an entry can be repointed at another
            # directory WITHOUT a recreate, leaving the slot key intact while the lock
            # held is not the one guarding these documents.
            # now would serialize against nothing that matters and could cancel the
            # replacement's run. Refuse and let the client retry against what exists.
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        # From here to the capture there is NO await: the halt writes a sentinel,
        # removes the nudge loop and cancels the running turn, and all three are
        # looked up by name.
        if _client_identity_mismatch(claimed, spec_dir, _slot_key(name)):
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        state = request.app.get("state")
        # The creation this request verified, carried to the commit below.
        captured_slot_key = _slot_key(name)
        captured_loop_id = _exec_loop_id(name)
        captured_slot = state.get_slot(_slot_key(name)) if state is not None else None
        stop_slots = claimed_slot_keys.runtime_slots(state, captured_slot)
        primary_slot = stop_slots[0] if stop_slots else None
        try:
            await _halt_execution(
                state,
                name,
                spec_dir,
                reason="user stop",
                only_loop_id=captured_loop_id,
                only_slot=primary_slot,
                expect_slot_key=original_index_slot_key,
            )
            await claimed_slot_keys.remove_other_loops(
                captured_slot_key, captured_loop_id, stop_reason="spec_stopped"
            )
            for extra_slot in stop_slots[1:]:
                await _halt_active_turn(state, name, only_slot=extra_slot)
        except Exception:
            # A failed loop removal means the run can still nudge itself; saying
            # "stopped" would be false and the user would not retry.
            logger.warning("spec %s: halt failed", name, exc_info=True)
            _audit("spec_stop_failed", name, outcome="denied")
            return web.json_response(
                {
                    "code": "stop_failed",
                    "error": "could not stop the run; it may still be working — retry",
                },
                status=503,
            )
        # Re-reading commit: halting awaits, so a concurrent DELETE in that window
        # must not be undone by writing back the snapshot above. The halt itself is
        # idempotent, so nothing is lost by reporting the deletion instead.
        if (
            await _touch_spec(
                name,
                expect_spec_dir=str(spec_dir),
                expect_slot_key=original_index_slot_key or None,
                status="planning",
            )
            is None
        ):
            # Gone, or recreated elsewhere under the same name -- in which case the
            # STOP sentinel we just wrote belongs to the OLD spec and this request
            # must not mark the NEW one as stopped.
            return web.json_response({"code": "not_found", "error": "not found"}, status=404)
        claimed_slot_keys.commit()
    _audit("spec_stop_execution", name)
    return web.json_response({"ok": True, "status": "planning"})

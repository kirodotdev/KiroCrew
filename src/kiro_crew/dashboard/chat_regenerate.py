"""Regenerate, variant switch, and edit-resend endpoints."""

from __future__ import annotations

import asyncio
import copy
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history, save_slot_off_loop
from kiro_crew.dashboard.chat_runner import _run_chat, _start_next_queued_turn
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.remote_relay import remote_bound_refusal
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_MAX_VARIANTS = 20

# Longest replacement prompt edit-resend accepts, in characters. Named here
# rather than inlined so the endpoint's cap is greppable; the value matches the
# sibling boundaries (``chat_rewind``'s ``content`` and ``chat_fork``'s
# ``prompt``), which is the point -- one edit of the same message must not be
# accepted by one endpoint and refused by another.
_MAX_EDIT_CONTENT_CHARS = 32_768

# How many times the edit-resend cancellation path re-shields the in-flight
# history rewrite before giving up on learning its outcome. Each retry absorbs
# ONE further cancellation (a gateway shutdown landing on a handler already
# unwinding from a client disconnect), so this bounds a cancel storm rather than
# a duration -- the worker thread itself cannot be interrupted and always
# finishes. Giving up leaves the live slot untouched, which is the safe half of
# the desync: a stale-but-complete window rather than a committed edit nothing
# persisted.
_SAVE_DRAIN_ATTEMPTS = 8


async def api_chat_slot_regenerate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/regenerate — regenerate the last assistant reply."""
    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # A crew-bound slot has no local regenerate: it would truncate LOCAL history
    # and re-run the turn on this machine, diverging from the peer.
    refusal = remote_bound_refusal(slot)
    if refusal is not None:
        return refusal

    async with slot._lock:
        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )

        msgs = slot.messages
        ai_idx = -1
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "assistant":
                ai_idx = i
                break
        if ai_idx < 0:
            return web.json_response(
                {"error": "no assistant message to regenerate", "code": "no_assistant_message"},
                status=400,
            )
        u_idx = -1
        for i in range(ai_idx - 1, -1, -1):
            if msgs[i].get("role") == "user":
                u_idx = i
                break
        if u_idx < 0:
            return web.json_response(
                {"error": "no preceding user message", "code": "no_user_message"}, status=400
            )

        user_msg = msgs[u_idx].get("content", "")
        if not user_msg:
            return web.json_response(
                {"error": "empty user message", "code": "empty_user_message"}, status=400
            )

        ai_msg = msgs[ai_idx]
        _rv = ai_msg.get("variants")
        variants: list[dict] = list(_rv) if isinstance(_rv, list) else []  # type: ignore[arg-type]
        current_entry = {"content": ai_msg.get("content", ""), "ts": ai_msg.get("ts", "")}
        if not any(v.get("content") == current_entry["content"] for v in variants):
            variants.append(current_entry)
        if len(variants) > _MAX_VARIANTS:
            variants = variants[-_MAX_VARIANTS:]

        del slot.messages[u_idx + 1 :]
        slot.invalidate_source_links()
        slot._dirty = True
        slot._resumed_count = 0
        # Window was truncated → next save MUST be the archive-safe rewrite path.
        # If the inline save below fails, the flag keeps the flush loop on the
        # rewrite path so the dropped tail is still archived.
        slot._pending_rewrite = True
        slot._pending_variants = variants

        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("Regenerate: failed to rewrite session history", exc_info=True)

        sel().log_api_access(
            caller="dashboard",
            operation="chat.regenerate",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )

        hint = (
            "The user regenerated the previous response. Produce a fresh answer — "
            "vary phrasing, structure, or angle. Do not say you already answered or "
            "reference the prior reply."
        )
        task = asyncio.create_task(
            _run_chat(
                state,
                slot,
                user_msg,
                regenerate_hint=hint,
                _directive_user_origin=not bool(request.get("app", "")),
            )
        )
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _clear_pending_on_done(t: asyncio.Task) -> None:
            if slot._pending_variants:
                if not t.cancelled() and t.exception() is None:
                    logger.warning("Regenerate: pending variants not consumed by flush, discarding")
                slot._pending_variants = []

        task.add_done_callback(_clear_pending_on_done)
    state.push_slots_update()
    return web.json_response({"ok": True})


async def api_chat_slot_switch_variant(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/switch-variant — switch which regenerated variant is active."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    try:
        idx = int(body.get("index"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return web.json_response({"error": "invalid index", "code": "index_invalid"}, status=400)

    async with slot._lock:
        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )

        target = None
        for m in reversed(slot.messages):
            if m.get("role") == "assistant" and m.get("variants"):
                target = m
                break
        if target is None:
            return web.json_response({"error": "no variants", "code": "no_variants"}, status=400)
        raw_target_variants = target.get("variants")
        variants: list[dict] = (
            list(raw_target_variants)  # type: ignore[arg-type]
            if isinstance(raw_target_variants, list)
            else []
        )
        if idx < 0 or idx >= len(variants):
            return web.json_response(
                {"error": "index out of range", "code": "index_out_of_range"}, status=400
            )

        chosen = variants[idx]
        if not isinstance(chosen, dict):
            return web.json_response(
                {"error": "corrupt variant entry", "code": "variant_corrupt"}, status=400
            )
        target_dict: dict = target
        target_dict["content"] = chosen.get("content", "")
        slot.invalidate_source_links()
        target_dict["ts"] = chosen.get("ts", target_dict.get("ts", ""))
        target_dict["variant_idx"] = idx
        slot._dirty = True
        slot._resumed_count = 0
        try:
            msgs_snapshot = list(slot.messages)
            await asyncio.to_thread(_save_slot_to_history, state, slot, msgs_snapshot)
        except Exception:
            logger.warning("switch-variant: failed to persist", exc_info=True)
        sel().log_api_access(
            caller="dashboard",
            operation="chat.switch_variant",
            outcome="allowed",
            source="dashboard",
            resources=slot.key,
        )
        _bc, _ = redact_exfiltration_urls(target_dict["content"])
        _bc, _ = redact_credentials(_bc)
        state.broadcast_ws(
            "chat_variant_switch",
            {"slot": slot.key, "index": idx, "content": _bc},
        )
        return web.json_response({"ok": True, "index": idx})


async def api_chat_slot_edit_resend(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/edit-resend — edit a user message and resend."""
    # Local import, and it must STAY local: ``chat_handlers`` cannot be the first
    # module of the package to import (its own transitive
    # ``validation`` <-> ``artifacts`` cycle resolves only once something else
    # has pulled those in), so hoisting these two to module scope makes
    # ``import kiro_crew.dashboard.chat_regenerate`` fail on its own. Same reason
    # ``session_control`` and ``handlers/core`` reach it this way.
    from kiro_crew.dashboard.chat_handlers import (
        _check_slot_app_ownership,
        _reauthorize_after_await,
        _subagents_attached_response,
    )

    # Destructive: this truncates and PERSISTS history before the background
    # turn runs, so a failed turn cannot undo it. Unlike an ordinary send, the
    # readiness latch must be honored BEFORE the mutation.
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    request_app = request.get("app", "")
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App-ownership gate (App Kit §5.2). This endpoint discards the slot's
    # NATIVE ACP conversation below, so an app token reaching a slot it does not
    # own destroys a resume identity it has no claim on -- the same capability
    # every other app-reachable write authorizes first. Reuse the shared gate
    # rather than a second spelling of it: it authorizes all four keys
    # (``_app`` presence, ``_app`` match, the effective SESSION key, and the
    # TRANSCRIPT key), so a channel-linked slot -- whose effective session is a
    # conversation the app does not own -- and an UNBOUND channel-origin slot
    # are both already covered, with no separate link check to keep in sync.
    # Denials are 404, not 403: indistinguishable from a missing slot
    # (anti-enumeration, CWE-204); the true reason is logged via SEL inside.
    denied = _check_slot_app_ownership(slot, name, request_app, "chat.slot_edit_resend")
    if denied is not None:
        return denied

    # A crew-bound slot has no local edit-and-resend: it would truncate LOCAL
    # history and re-run the edited turn on this machine, diverging from the peer.
    # AFTER the app-ownership 404 above so a foreign app cannot tell a remote slot
    # apart from a missing one via the 409.
    refusal = remote_bound_refusal(slot)
    if refusal is not None:
        return refusal

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # A valid-JSON but non-object body (array/scalar) has no .get(), so
    # body.get("index") would raise AttributeError -> 500. Reject it as a 400,
    # matching the guard in api_chat_slot_switch_variant above.
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    index = body.get("index")
    ts = body.get("ts")
    # A PRESENT non-string ``content`` (``{"content": 123}``) has no ``.strip()``,
    # so it reached ``AttributeError`` -> 500 rather than a 400 the caller can
    # read. Missing/null stays ``content_required`` below, which is what an empty
    # composer sends. Both checks mirror the sibling ``rewind`` boundary, which
    # already type-checks and length-caps its own ``content``.
    raw_content = body.get("content")
    if raw_content is not None and not isinstance(raw_content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"}, status=400
        )
    content = (raw_content or "").strip()
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "content_required"}, status=400
        )
    if len(content) > _MAX_EDIT_CONTENT_CHARS:
        return web.json_response(
            {
                "error": f"content too long (max {_MAX_EDIT_CONTENT_CHARS} chars)",
                "code": "content_too_long",
            },
            status=400,
        )

    async with slot._lock:
        # Reading the body above was an await, and ``linked_session_key`` is
        # rebound on ALREADY-LIVE slots with no ``running`` gate (a cron
        # completion, a workflow injection), so a slow caller can be authorized
        # against its own session and land on somebody else's conversation.
        # Re-authorize before the first read of slot state, since ``running``
        # belongs to whichever conversation the slot now routes to.
        stale = _reauthorize_after_await(state, slot, name, request_app, "chat.slot_edit_resend")
        if stale is not None:
            return stale

        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )

        # The session whose native resume identity the discard below clears.
        # Resolved here because the two guards that follow are about THAT
        # session, not about this slot's own task.
        session_key = effective_session_key(slot)

        # ``slot.running`` is not the whole "is this session busy" question, and
        # ``discard_conversation`` is a full teardown. Both guards below are the
        # ones the sibling teardown route (``reset-conversation``) already
        # applies before the SAME call, in the same order and with the same
        # codes -- reused rather than respelled, so the two cannot drift.
        if slot._in_stage_execution:
            # An autopilot plan reads ``running`` False BETWEEN stages while it
            # is still mid-plan, so ``running`` alone would discard the
            # conversation the plan is writing into and cold-start its next
            # stage -- on top of truncating the history that plan is producing.
            return web.json_response(
                {"error": "slot is orchestrating", "code": "slot_orchestrating", "slot": name},
                status=409,
            )
        # The discard also releases the shared sub-agent runtime the parent's
        # children run on. ``slot.running`` is False while they keep going (the
        # parent turn ends first), so nothing above catches it and a child's
        # work would be destroyed by an edit it has no part in.
        attached = _subagents_attached_response(state, slot, session_key, "chat.slot_edit_resend")
        if attached is not None:
            return attached

        msgs = slot.messages

        if ts:
            index = next(
                (i for i, m in enumerate(msgs) if m.get("ts") == ts and m.get("role") == "user"),
                -1,
            )
            if not isinstance(index, int) or index < 0:
                return web.json_response(
                    {"error": "user message not found for ts", "code": "user_message_not_found"},
                    status=400,
                )
        elif isinstance(index, int) and 0 <= index < len(msgs):
            if msgs[index].get("role") != "user":
                return web.json_response(
                    {"error": "index is not a user message", "code": "index_not_user_message"},
                    status=400,
                )
        else:
            return web.json_response(
                {"error": "index or ts required", "code": "index_or_ts_required"}, status=400
            )

        # Capture routing state BEFORE any live mutation, exactly like rewind:
        # the truncation is a real conversation boundary, so the native ACP
        # conversation must be discarded and the truncated history durably saved
        # before the live slot adopts the edit or any replacement turn is
        # dispatched. ``expected_history_key`` is the transcript this edit was
        # authorized against (``session_key``, the session whose native resume
        # identity is cleared, was resolved with the busy guards above).
        expected_history_key = slot_history_key(slot)

        # Prepare the truncated+edited window on a COPY. The dirty-slot flush
        # can run while either durable boundary below is pending, so exposing a
        # truncated live window here could make a rejected edit permanent.
        _bc, _ = redact_exfiltration_urls(content)
        _bc, _ = redact_credentials(_bc)
        prospective_slot = copy.copy(slot)
        prospective_slot.messages = list(slot.messages[:index])
        # ``copy.copy`` is SHALLOW, so every mutable attribute still IS the live
        # slot's object. Reassigning ``messages`` alone is not enough, because
        # ``_ChatSlot.append`` below writes through four more of them: it
        # appends to ``_pending``, ``set()``s ``event``, filters
        # ``_question_pending``, and fires ``_on_question_retired``. On an
        # un-severed copy that publishes the edited row into the LIVE stream
        # reader's queue and announces the live question cards as retired
        # BEFORE any of the five rejection points below (failed discard, busy
        # session, failed flush, refused save, rebound slot) can refuse the edit
        # -- so a refused edit leaves a phantom row and card-less "needs input"
        # behind.
        # Sever all five; the commit re-adopts them, and only then. ``_queue``
        # is copied rather than emptied because, unlike rewind, edit-resend does
        # not discard queued sends -- the copy exists so no mutation on this
        # scratch slot can ever reach the live queue.
        prospective_slot._queue = list(slot._queue)
        prospective_slot._pending = list(slot._pending)
        prospective_slot._question_pending = dict(slot._question_pending)
        prospective_slot._on_question_retired = None
        prospective_slot.event = asyncio.Event()
        if prospective_slot._pending:
            prospective_slot.event.set()
        prospective_slot._dirty = True
        prospective_slot._resumed_count = 0
        prospective_slot.append("user", _bc, "msg msg-u")
        msgs_snapshot = list(prospective_slot.messages)
        # Which question cards the prospective append retired. Announced at
        # commit time instead, through the LIVE callback the copy was denied.
        retired_question_ids = [
            question_id
            for question_id in slot._question_pending
            if question_id not in prospective_slot._question_pending
        ]

        # The backing queue as it stood BEFORE the reservation below. An entry
        # arriving after it was diverted there by the reservation and has no
        # drain trigger of its own, so an abort must hand it off.
        pre_await_queue_ids = {item["id"] for item in slot._queue}

        # The live window and pending queue as they stand BEFORE the awaits. A
        # row absent from these arrived DURING them and belongs to the NEW
        # timeline, so the commit must carry it rather than replace it away --
        # the same rule rewind states for an entry queued during its own
        # boundary. Identity is the row OBJECT: a positional cut breaks the
        # moment ``append``'s own trim drops leading rows, and a restore-path row
        # carries no ``meta.mid`` to key on. An id cannot be recycled before the
        # commit because every pre-await row stays referenced throughout -- the
        # prefix by ``prospective_slot.messages``, the discarded suffix by the
        # live ``slot.messages``.
        pre_await_row_ids = {id(row) for row in slot.messages}
        pre_await_pending_ids = {id(row) for row in slot._pending}

        # Reserve the slot BEFORE the awaits below. ``slot.running`` derives
        # from ``slot.task``, and the send path is not serialized on
        # ``slot._lock``: without a live task, a send arriving while any of the
        # three durable boundaries below is pending observes an IDLE slot,
        # appends its row to ``slot.messages`` and dispatches a competing turn
        # -- which the commit below would then erase. Publishing the dispatch
        # task here (no await between the idle check above and this assignment)
        # makes such a send take the queue path instead; the entry is not in
        # ``pre_await_queue_ids``, so on abort the task hands it to the
        # canonical successor dispatch and it is never stranded. The turn itself
        # runs only once ``dispatch_commit`` is set, so the reservation never
        # dispatches an edit the boundaries refused.
        dispatch_ready = asyncio.Event()
        dispatch_commit = False

        async def _edit_resend_dispatch() -> None:
            await dispatch_ready.wait()
            if dispatch_commit:
                await _run_chat(
                    state,
                    slot,
                    _bc,
                    _directive_user_origin=not bool(request_app),
                )
                return
            # Edit rejected. A send diverted to the queue by this reservation
            # has no drain trigger of its own (no turn ran), so hand it to the
            # canonical successor dispatch, which re-validates holds before
            # starting anything. Entries queued BEFORE the reservation keep
            # waiting for their own trigger.
            if any(entry["id"] not in pre_await_queue_ids for entry in slot._queue):
                if await _start_next_queued_turn(state, slot):
                    return
            state.push_slots_update()

        task = asyncio.create_task(_edit_resend_dispatch())
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        def _on_done(t: asyncio.Task) -> None:
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    "edit-resend _run_chat failed for %s", slot.key, exc_info=t.exception()
                )

        task.add_done_callback(_on_done)

        try:
            # Durably clear the native conversation BEFORE the history rewrite,
            # mirroring rewind. A failure here leaves the original branch intact
            # and dispatches no replacement turn.
            if state.sessions is not None:
                try:
                    # ``skip_if_busy``: an inbound channel turn holds the session
                    # semaphore while ``slot.running`` reads False, so the idle
                    # check above cannot see it -- an unconditional discard would
                    # tear down its provider mid-reply.
                    discarded = await state.sessions.discard_conversation(
                        session_key, skip_if_busy=True
                    )
                except Exception:
                    logger.warning(
                        "edit-resend: failed to discard ACP conversation for %s",
                        session_key,
                        exc_info=True,
                    )
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not prepare edited conversation; retry the edit",
                            "code": "edit_resend_prepare_failed",
                        },
                        status=503,
                    )
                if not discarded:
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "the session is busy with another reply; retry the edit",
                            "code": "edit_resend_session_busy",
                        },
                        status=409,
                    )
                try:
                    # Force the durability point endpoint-side: the sid clear
                    # lands in the session map's debounced writer, and a gateway
                    # exit before that write would resurrect the discarded
                    # conversation on restart.
                    await state.sessions.aflush()
                except Exception:
                    logger.warning(
                        "edit-resend: failed to flush the cleared resume sid for %s",
                        session_key,
                        exc_info=True,
                    )
                    state.push_slots_update()
                    return web.json_response(
                        {
                            "error": "could not prepare edited conversation; retry the edit",
                            "code": "edit_resend_prepare_failed",
                        },
                        status=503,
                    )

            def _commit_target_intact() -> bool:
                """Whether the slot is still the one this edit was authorized against.

                Three axes can move across the boundary awaits, and each makes a
                commit land somewhere it was never authorized to. ONE predicate
                so the success path and the cancellation path cannot check
                different subsets -- the asymmetry that let a rebind through
                before.
                """
                if slot_history_key(slot) != expected_history_key:
                    # A cron or workflow injection re-linked the slot, hydrating
                    # it with ANOTHER conversation's state. The prospective copy
                    # froze the old routing, so the save-side
                    # ``expected_history_key`` guard cannot see the LIVE slot
                    # move -- this loop-side check is the one that can.
                    logger.warning(
                        "edit-resend: slot %s was rebound to another transcript during "
                        "persistence; refusing the commit",
                        slot.key,
                    )
                    return False
                if state._slots.get(name) is not slot:
                    # A close-and-recreate under the same name is a DIFFERENT
                    # conversation that a name-based check would wave through.
                    # Requires the same OBJECT, the same discipline
                    # ``_reauthorize_after_await`` applies to the body-read await.
                    logger.warning(
                        "edit-resend: slot %s was replaced during persistence; "
                        "refusing the commit",
                        slot.key,
                    )
                    return False
                if slot.task is not task:
                    # The reservation was displaced (a close cancelled it, or
                    # another dispatcher took the slot). Committing would leave
                    # this handler's turn running ALONGSIDE whatever now owns
                    # ``slot.task`` -- two concurrent turns writing one window.
                    logger.warning(
                        "edit-resend: the dispatch reservation for %s was displaced during "
                        "persistence; refusing the commit",
                        slot.key,
                    )
                    return False
                return True

            def _commit_live_state() -> None:
                """Adopt the prepared state on the live slot (synchronous).

                Shared by the normal success path and the cancellation path
                below: once the destructive rewrite has landed on disk, this is
                the only thing that keeps the live slot matching it. No await
                inside, so it is atomic on the event loop. Dispatching is the
                caller's separate ``dispatch_commit`` step, so a cancellation
                landing between the two cannot commit without dispatching.
                """
                # Carry the rows that landed on the LIVE slot while the
                # boundaries were pending. A workflow or cron completion appends
                # WITHOUT taking ``slot._lock`` (``workflow_inject`` calls
                # ``append_and_surface`` straight on the event loop), so a
                # wholesale replace drops the injected row -- and the rewrite
                # above cannot put it back, because a rewrite deliberately skips
                # the cross-process-append scan (``collect_foreign=not rewrite``
                # in ``chat_persistence``). Keeping it in the window is what
                # makes the next ORDINARY flush re-persist it. Appending them
                # AFTER the prospective window is the correct order and not just
                # a convenient one: ``monotonic_transcript_ts`` only ever moves a
                # row forward, so an arrived row can never be stamped EARLIER
                # than the edited one. It can be stamped IDENTICALLY -- on a
                # coarse clock (Windows ticks in ~15.6 ms steps) both appends read
                # the same instant -- and list order is what separates that tie,
                # which is why the merge order matters rather than a re-sort.
                arrived_rows = [row for row in slot.messages if id(row) not in pre_await_row_ids]
                arrived_pending = [
                    row for row in slot._pending if id(row) not in pre_await_pending_ids
                ]
                # A card retired by EITHER the edit or an arrived row stays
                # retired -- tightest-wins, so the commit can never resurrect one
                # whose answer channel is already gone -- and only a card still
                # live is announced.
                surviving_questions = {
                    cid: rec
                    for cid, rec in prospective_slot._question_pending.items()
                    if cid in slot._question_pending
                }
                announce_retired = [
                    question_id
                    for question_id in retired_question_ids
                    if question_id in slot._question_pending
                ]
                slot.messages = prospective_slot.messages + arrived_rows
                slot._pending = prospective_slot._pending + arrived_pending
                slot._question_pending = surviving_questions
                slot.invalidate_source_links()
                slot._dirty = True
                slot._resumed_count = 0
                # ``total_messages`` is a LIFETIME counter that survives
                # trimming, and the prospective ``append`` bumped only the COPY's
                # int -- so without this the edited row is invisible to every
                # reader of it: ``_get_active_workspace`` picks the max-counter
                # slot to resolve which workspace's lessons to load, and the
                # Slack mirror compares the counter against its own start value
                # to decide whether anything happened. Incremented by ONE here
                # rather than adopted from the copy, whose value predates the
                # arrived rows above (which bumped the live counter themselves).
                # Truncation deliberately does not decrement it.
                slot.total_messages += 1
                # Deliberately NOT copied from ``prospective_slot``: the
                # persistence witnesses (``_pending_rewrite``, ``_disk_*``,
                # ``_frozen_prefix_cache``). The save above ran on the LIVE slot
                # and stamped them with the post-rewrite truth; the prospective
                # copies are the PRE-save values, and restoring those would
                # re-arm a destructive rewrite on the next flush and move the
                # monotone ``_disk_tail_ts`` floor backwards.
                if slot._pending:
                    slot.event.set()
                else:
                    slot.event.clear()
                if announce_retired and callable(slot._on_question_retired):
                    try:
                        slot._on_question_retired(slot.key, announce_retired)  # type: ignore[operator]
                    except Exception:
                        logger.debug(
                            "edit-resend: question-retirement announcement failed for slot %s",
                            slot.key,
                            exc_info=True,
                        )

                sel().log_api_access(
                    caller=request_app or "dashboard",
                    operation="chat.edit_resend",
                    outcome="allowed",
                    source="dashboard",
                    resources=slot.key,
                )

            # Persist the truncated+edited history via the explicit-snapshot
            # rewrite path. Nothing was mutated on the live slot yet, so a
            # failure (exception OR a save refused by its own guards) means
            # nothing persisted: no live mutation and no dispatch.
            #
            # The worker thread cannot be interrupted: once the rewrite starts it
            # WILL finish, whether or not this handler is still alive. A client
            # disconnect cancels the handler task, and a bare await here would
            # then abandon a completed destructive rewrite -- persisted history
            # truncated, the live slot still holding the full original window,
            # and the next periodic dirty-slot flush re-serializing that stale
            # window back over the truncated file. That is exactly the "live
            # window desynchronized from disk" failure this change set out to
            # close, so shield the save; on cancellation, wait for the worker's
            # real outcome and, if the rewrite landed on the transcript we
            # authorized, commit the live slot to match disk and let the reserved
            # dispatch run the edited prompt before propagating the cancellation.
            # Through ``save_slot_off_loop``, not a bare ``to_thread``, and the
            # difference is load-bearing rather than stylistic. Because the live
            # slot keeps the FULL window until the commit, the periodic
            # dirty-slot flush can snapshot that stale window, block behind this
            # rewrite on the per-session history lock, and then write the
            # snapshot back on top -- restoring every message the rewrite just
            # discarded. The helper bumps ``slot._metadata_persist_inflight``
            # around the write, which is exactly the flag ``flush_slot_now``
            # already honours to keep "this unpinned periodic writer" off a slot
            # with a guarded write pending, and it decrements in a ``finally``.
            # Shielding the wrapper (rather than the inner future) is what keeps
            # that exclusion held for the whole write: a cancellation reaching
            # the shield leaves the coroutine running, so its ``finally`` does
            # not release the flag early. ``best_effort=False`` so a failure
            # propagates to the 503 below instead of being swallowed and
            # re-armed as a dirty retry.
            save_task = asyncio.ensure_future(
                save_slot_off_loop(
                    state,
                    slot,
                    msgs_snapshot,
                    best_effort=False,
                    expected_history_key=expected_history_key,
                )
            )
            try:
                saved = await asyncio.shield(save_task)
            except asyncio.CancelledError:
                # Drain the worker's real outcome, shielded and RETRIED. A
                # SECOND cancellation -- a gateway shutdown arriving while this
                # handler is already unwinding from a client disconnect -- lands
                # on whatever await sits here, and ``CancelledError`` is a
                # BaseException, so an ``except Exception`` cannot absorb it. A
                # bare ``await save_task`` therefore abandons a rewrite the
                # worker thread finishes anyway: disk truncated, live slot still
                # holding the discarded suffix, the next dirty-slot flush pushing
                # that stale window back over the truncated file -- exactly the
                # desync this boundary exists to close. Shielding each attempt
                # keeps the worker's future alive across those cancellations, and
                # the outcome is read off the settled task rather than awaited,
                # so it cannot be lost to a cancel landing between the two.
                # Bounded: a cancel storm must not spin here.
                landed = False
                for _ in range(_SAVE_DRAIN_ATTEMPTS):
                    if save_task.done():
                        break
                    try:
                        await asyncio.shield(save_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if save_task.done() and not save_task.cancelled():
                    save_exc = save_task.exception()
                    landed = save_exc is None and bool(save_task.result())
                elif not save_task.done():
                    logger.warning(
                        "edit-resend: the history rewrite for %s did not settle within "
                        "%d cancellation(s); leaving the live slot untouched",
                        slot.key,
                        _SAVE_DRAIN_ATTEMPTS,
                    )
                if landed and _commit_target_intact():
                    _commit_live_state()
                    dispatch_commit = True
                    logger.info(
                        "edit-resend: request cancelled after the rewrite landed for %s; "
                        "committed live state and dispatching the edited prompt",
                        slot.key,
                    )
                raise
            except Exception:
                logger.warning("edit-resend: failed to persist", exc_info=True)
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "edit_resend_save_failed",
                    },
                    status=503,
                )
            if not saved:
                # The save's own guards refused the write (the session was
                # permanently deleted, or the slot was rebound to another
                # transcript, while the write awaited its lock). Nothing was
                # persisted, so dispatching a turn now would run from state that
                # exists only in memory.
                logger.warning(
                    "edit-resend: history save refused for %s (concurrent delete or rebind)",
                    slot.key,
                )
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "could not save edited conversation; retry the edit",
                        "code": "edit_resend_save_failed",
                    },
                    status=503,
                )

            # Both irreversible boundaries succeeded. Before adopting the
            # prepared state, confirm the slot is still the one this edit was
            # authorized against, on all three axes that can move across the
            # awaits above. No await between these checks and the mutations
            # below, so the decision cannot go stale.
            if not _commit_target_intact():
                state.push_slots_update()
                return web.json_response(
                    {
                        "error": "the conversation changed while saving; retry the edit",
                        "code": "edit_resend_slot_rebound",
                    },
                    status=503,
                )

            # Both boundaries committed. Adopt the prepared state on the LIVE
            # slot, then release the reserved dispatch. No await between the save
            # above and these mutations, so they are atomic on the event loop.
            #
            # And NOTHING may await between here and ``dispatch_ready.set()`` in
            # the ``finally`` below, because ``dispatch_commit`` is already True by
            # then: an await there lets a cron completion rebind the slot, and the
            # released dispatch would run this handler's prompt against ANOTHER
            # conversation. That rules out a second guarded save for a row the
            # commit carried -- the commit sets ``_dirty``, so the merged window
            # reaches disk on the next periodic flush like any ordinary append,
            # and re-checking the commit target after such an await could not
            # rescue it either: refusing once the live slot has adopted the
            # truncated window would leave a truncation with no turn.
            _commit_live_state()
            dispatch_commit = True
        finally:
            # Wake the reserved dispatch task on every exit: it runs the
            # replacement turn on commit and the queue handoff on abort.
            dispatch_ready.set()

    state.push_slots_update()
    return web.json_response({"ok": True})

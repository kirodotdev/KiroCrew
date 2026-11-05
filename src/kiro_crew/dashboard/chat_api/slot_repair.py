"""The two routes that rebuild a slot's session in place: the image repair
(``api_chat_slot_repair_images``) and the fresh-conversation reset
(``api_chat_slot_reset_conversation``).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _SESSION_RELOAD_NOTICE,
        DashboardState,
        SlotContinueRefusal,
        _app_cancel_denied,
        _ChatSlot,
        _coerce_bool,
        _slot_replaced_while_queued,
        _subagents_attached_response,
        _switch_target_busy,
        continue_slot_turn,
        effective_session_key,
        logger,
        read_bounded_json,
        reload_slot_session,
        sel,
        slot_not_found,
    )


async def api_chat_slot_repair_images(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/repair-images — recover a many-image-wedged turn.

    The remedy the ``image_repair`` error-card kind points at. When a turn is
    refused because an image already in the native history exceeds the provider's
    2000px many-image cap (the ``image_dimension_wedge`` tag), Resume replays the
    identical history and fails the same way. This route downscales the oversized
    images in place in the transcript and re-issues the interrupted turn, so no
    turns are lost -- unlike reset-conversation, which starts over.

    One mechanism, in order: (1) tear the slot's kiro-cli process down through
    the shared :func:`reload_slot_session` path, which carries every busy /
    children / rebind guard the reload and reset routes use; (2) WHILE that
    process is down and both locks are held -- the only moment
    ``session_image_repair`` will touch the transcript, since it refuses a file a
    live kiro-cli holds -- scan and, if anything is over the cap, rewrite the
    transcript in place with its ``.pre-image-repair.bak`` sidecar; (3) let the
    reload path re-arm the resume spawn, which ``session/load``s the repaired
    transcript; (4) re-issue the interrupted turn with ``continue_slot_turn``.

    A transcript that turns out to need no repair (every image already within the
    cap) is left untouched and the turn is still re-issued: the wedge may have
    been cleared by hand, and re-sending a now-compliant history is the recovery
    either way.

    Authorization and the guard callbacks are the reload route's own, so an app
    token must own both the slot and the session, and every refusal maps the
    same way. A non-persistent (Incognito / Temporary) session is refused with a
    409 ``image_repair_not_persistent`` before any teardown, because that
    teardown deletes its native history. The repair step's own refusals (a
    transcript that moved under it, an existing sidecar) surface as a 409; a
    process still live after teardown is the reload path's ``turn_in_flight``.
    """
    from kiro_crew import session_image_repair
    from kiro_crew.config.paths import kiro_sessions_dir

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    def _still_ours(_after_reset: bool) -> bool:
        return not _slot_replaced_while_queued(
            state, slot, name, request, "chat.slot_repair_images"
        )

    live_slot: _ChatSlot = slot

    def _denied(session_key: str) -> "web.Response | None":
        refusal = _app_cancel_denied(request, live_slot, "chat.slot_repair_images", session_key)
        if refusal is not None:
            return refusal
        # A non-persistent (Incognito / Temporary) session's teardown deletes
        # its native history (the provider's shutdown runs cleanup_session), so
        # repairing it would destroy the very transcript the repair rewrites,
        # with no backup. Refuse before the teardown; the card then offers the
        # fresh-session way out.
        live = state.sessions.get_provider(session_key)
        modes = (getattr(live, "memory_mode", None), getattr(live_slot, "memory_mode", None))
        if any(isinstance(m, str) and m and m != "persistent" for m in modes):
            return web.json_response(
                {
                    "error": "image repair needs a persistent session; start a fresh session instead",
                    "code": "image_repair_not_persistent",
                    "slot": name,
                },
                status=409,
            )
        return None

    def _busy(session_key: str) -> bool:
        provider = state.sessions.get_provider(session_key)
        if provider is not None and provider.has_active_turn():
            return True
        return _switch_target_busy(state, slot, session_key, provider)  # type: ignore[arg-type]

    # Records whether the in-lock repair actually REWROTE a transcript. The
    # route reports ``repaired`` from this, not unconditionally: a session with
    # no transcript, or one already within the cap, is left untouched and must
    # not claim a repair. A real repair FAILURE (sidecar exists, transcript
    # moved, OSError) raises out of the hook instead -- which propagates through
    # ``reload_slot_session`` and SKIPS the respawn, so the turn is never
    # re-issued onto an unrepaired transcript.
    repair_state: dict[str, object] = {"rewrote": False}

    class _RepairFailed(Exception):
        def __init__(self, message: str, code: str = "image_repair_failed") -> None:
            super().__init__(message)
            self.message = message
            # Distinguishes the one PERMANENT refusal (a prior backup sidecar
            # blocks a second repair -> image_repair_backup_exists, a dead end
            # the frontend answers with "start a fresh session") from the other
            # causes (moved transcript, unverifiable scan, OSError) that map to
            # the generic image_repair_failed and stay retryable.
            self.code = code

    async def _repair_transcript(session_key: str) -> None:
        # The process is down and both locks are held: the one window
        # session_image_repair will act in. The transcript file is
        # ``kiro_sessions_dir()/<sid>.jsonl`` for the resumable sid the reload
        # path preserved; with no sid there is no accumulated conversation to
        # repair, so the repair is simply skipped (rewrote stays False).
        sessions_dir = kiro_sessions_dir()

        def _run() -> tuple[bool, str | None, str]:
            # Returns (rewrote, error). ``error`` is a real repair failure, which
            # the caller turns into a raise so the respawn is skipped. The whole
            # body runs off the event loop (``asyncio.to_thread`` below): both
            # the resumable-sid lookup (a persisted-state FILE CHECK) and the
            # transcript-path probe (a ``stat`` per suffix on the sessions
            # mount) land here rather than stalling the loop if that mount is
            # slow to answer.
            sid = state.sessions.resumable_sid(session_key) or ""
            if not sid:
                # No resumable session: nothing accumulated to repair, skip.
                return (False, None, "")
            # ``session_image_repair`` operates on the kiro-cli ``<sid>.jsonl``
            # transcript. (The ``<sid>.json`` file kiro-cli also keeps is resume
            # metadata, not the conversation, so it is deliberately not a
            # fallback here.)
            transcript_path = sessions_dir / f"{sid}.jsonl"
            if not transcript_path.exists():
                # No accumulated conversation to repair; skip, not a failure.
                return (False, None, "")
            report, lines = session_image_repair.scan_file(transcript_path)
            if report.error:
                return (
                    False,
                    f"could not read the transcript: {report.error}",
                    "image_repair_failed",
                )
            if report.unanchored_images or report.walk_truncated:
                # The scan's tripwire fired: the generic walk saw image-shaped
                # blocks the anchored traversal did not, or the walk hit its
                # depth ceiling. Either way scan_file's "nothing over the cap"
                # cannot be trusted, so treating lines=None as "re-issue as-is"
                # would silently replay the same wedge. Refuse instead -- a real
                # failure the route maps to a 409 -- rather than respawn onto a
                # transcript whose images the scan could not fully see.
                return (
                    False,
                    "the transcript scan could not verify every image "
                    "(the conversation shape may have drifted); not re-issued",
                    "image_repair_failed",
                )
            if lines is None:
                # Nothing over the cap; leave the file untouched. Not a failure
                # -- the wedge may have cleared by hand -- so the turn is still
                # re-issued, but repaired stays False.
                return (False, None, "")
            try:
                session_image_repair.apply_repair(
                    transcript_path,
                    lines,
                    backup=True,
                    expect=report.source_stat,
                )
            except session_image_repair.BackupExistsError as exc:
                # A prior repair on this session already wrote the
                # ``.pre-image-repair.bak`` sidecar. That sidecar is the single
                # recovery copy of the original bytes, and the repair module
                # refuses to overwrite it ON PURPOSE (its "No --no-backup flag"
                # decision), so a second wedge is refused rather than rewritten
                # without a backup. This is the one PERMANENT dead end (only a
                # manual file move clears it), so it gets its own code and the
                # frontend offers a fresh session rather than a pointless retry.
                return (False, str(exc), "image_repair_backup_exists")
            except session_image_repair.TranscriptMovedError as exc:
                return (False, str(exc), "image_repair_failed")
            except session_image_repair.TranscriptLiveError as exc:
                # Should not happen -- the process is down -- but surface it
                # rather than respawn onto an unrepaired transcript.
                return (False, str(exc), "image_repair_failed")
            except OSError as exc:
                return (False, f"could not write the repair: {exc}", "image_repair_failed")
            return (True, None, "")

        rewrote, err, code = await asyncio.to_thread(_run)
        if err is not None:
            # Raise so reload_slot_session's after_reset hook propagates and the
            # respawn is SKIPPED -- the turn is never re-issued onto a transcript
            # the repair could not fix.
            raise _RepairFailed(err, code)
        repair_state["rewrote"] = rewrote

    try:
        resp = await reload_slot_session(
            state,
            slot,
            name,
            still_ours=_still_ours,
            denied=_denied,
            busy=_busy,
            notice=_SESSION_RELOAD_NOTICE,
            after_reset_in_lock=_repair_transcript,
        )
    except _RepairFailed as exc:
        # The teardown ran and the repair was refused, so the respawn did not
        # happen and no turn was re-issued. The slot is idle and resumable. The
        # code distinguishes the permanent backup-exists dead end (the frontend
        # offers a fresh session) from the other causes (retryable).
        return web.json_response(
            {"error": exc.message, "code": exc.code, "slot": name},
            status=409,
        )
    # A non-2xx reload response (a 404/409 from its guards) is returned as-is:
    # nothing was re-issued because the teardown itself was refused.
    if resp.status >= 300:
        return resp
    repaired = bool(repair_state["rewrote"])
    # Re-issue the interrupted turn against the (possibly repaired) reloaded
    # conversation. Outside the reload locks: continue_slot_turn takes slot._lock
    # itself.
    try:
        await continue_slot_turn(
            state,
            slot,
            directive_user_origin=not bool(request.get("app", "")),
            # Same-name registration can change while the lock or child probe
            # waits, exactly as it can for the plain Continue endpoint.
            replaced=lambda: _slot_replaced_while_queued(
                state, slot, name, request, "chat.slot_repair_images"
            ),
            via="repair_images",
        )
    except SlotContinueRefusal as exc:
        # The slot is reloaded (and repaired, if anything was over the cap); the
        # turn was not re-issued (e.g. a turn started racing). Report
        # ``resumed: False`` so the frontend keeps a Resume affordance instead
        # of claiming the turn runs. ``exc`` is logged, not returned: the
        # frontend branches on ``resumed`` alone.
        logger.info("Slot %s repair re-issue refused: %s (%s)", name, exc.message, exc.code)
        return web.json_response(
            {
                "slot": name,
                "repaired": repaired,
                "resumed": False,
            },
            status=200,
        )
    return web.json_response({"slot": name, "repaired": repaired, "resumed": True})


async def api_chat_slot_reset_conversation(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reset-conversation — a fresh conversation, same slot.

    Drops the slot's resume pointer, so its next turn cold-starts a new native
    conversation instead of ``session/load``-ing the accumulated one. Everything
    else survives: the slot stays open, its transcript stays on disk, and the
    session-map ENTRY keeps its channel linkage.

    This capability existed internally with no way to ask for it. Resume is
    key-driven — ``resume_sid = self._session_map.get(key)`` — and a slot key is
    stable by design, so reopening one continues where it left off. That is the
    point for a tab the user closed and came back to. It is NOT what a caller
    wants after a long-lived conversation has drifted, filled up, or outlived the
    thing it was about; and until now the only way to break the link was to
    DELETE the session from history, which destroys the record to reset the
    pointer. This separates the two.

    ``discard_conversation``, not ``destroy``: the entry also carries the Slack
    thread/channel linkage and the reverse index built from it, so dropping the
    row would silently unlink a mirrored session. The dropped value is stashed as
    ``discarded_sid``, so this is diagnosable and reversible by hand.

    It is nonetheless a FULL teardown — it shuts the provider down and releases
    the shared sub-agent runtime — so it takes the same guards the sibling
    teardown route does, through the same shared helpers rather than a third
    policy of its own: authorization on the SESSION (not merely the slot),
    ``provider.has_active_turn()``, ``running``, a sibling alias's turn on the
    same session (``_switch_target_busy``), and the sub-agent gate. Each of the
    four protects work the caller cannot see from the outside: a turn running on
    the session with no dashboard task behind it (an inbound channel message), a
    turn mid-write, another alias's turn still cold-starting before any provider
    is registered, and children still running after their parent's turn ended.

    The ``has_active_turn()`` check is a best-effort fast path; the
    authoritative guard is the discard's ``skip_if_busy``, which probes the
    per-session SEMAPHORE atomically with the session pop (see
    :meth:`SessionManager.discard_conversation`). The fast path has a known
    edge — a turn holding the semaphore but not yet having a prompt in flight
    is invisible to it — and the atomic guard is what closes it, the same
    contract the sibling reload route rests on, so the two teardowns keep one
    notion of "busy". Of the refusal paths, only the atomic guard's decline is
    SEL-recorded (``outcome="denied"``): it is the one refusal that happens
    after the route has committed to the teardown, while the fast-path 409s
    are pre-checks and stay unlogged, as they are on the sibling.

    The transcript is deliberately left in place, which means the tab still shows
    the earlier messages while the model does not remember them. That is the
    honest rendering of what happened — the record is the user's, the context was
    the conversation's — and it is why this is a deliberate action rather than
    something the gateway does on its own.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    # Resolved before authorization, because what has to be authorized is the
    # SESSION this will clear, not the slot it was reached through.
    key = effective_session_key(slot)

    # Slot ownership does not imply ownership of that session:
    # ``get_or_create_slot`` resolves ``linked_session_key`` from the session map
    # for a name shaped like a channel stem, so an app that names a live channel
    # thread ends up owning a slot bound to a conversation it has no claim on.
    # ``_app_cancel_denied`` is the shared policy for exactly that, and it tests
    # the key the caller will actually act on. Answers an indistinguishable 404,
    # and runs BEFORE the 409s below so a refusal cannot confirm the slot exists.
    denied = _app_cancel_denied(request, slot, "slot_reset_conversation", key)
    if denied is not None:
        return denied

    # Read the body HERE — after authorization, before the busy guards. Reading it
    # is an await the CLIENT controls the duration of, and every guard below
    # protects work that can START during a suspension: a turn admitted after
    # ``has_active_turn()`` answered False is torn down mid-write by the discard.
    # Parsing after the guards would widen that window from one event-loop hop to
    # however long a slow body takes to arrive. The guards must be the last thing
    # that happens before the teardown.
    #
    # An absent body is not an error: this route took no body before, so
    # refusing one would break every existing caller for a parameter they do
    # not send. A present-but-malformed body IS refused — "sent nothing" and
    # "sent garbage" are different facts, and only the first can be defaulted.
    replay = True
    body, body_err = await read_bounded_json(request, allow_absent=True)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if "replay" in body:
        # A spelled "false"/"0"/"no"/"off" means False; any other value keeps
        # the truthiness it has always been read with.
        raw_replay = body.get("replay")
        replay = _coerce_bool(raw_replay, bool(raw_replay))

    # A turn in flight on the SESSION, which ``slot.running`` cannot see: that
    # flag tracks this slot's own task, while an inbound channel message runs a
    # turn on the linked session with no dashboard task at all. Tearing the
    # provider down under it loses that turn's output. Same probe, same order as
    # the sibling reload route — one policy for one teardown.
    provider = state.sessions.get_provider(key)
    if provider is not None and provider.has_active_turn():
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight", "slot": name},
            status=409,
        )

    if slot.running:
        return web.json_response(
            {
                "error": "a turn is running on this slot",
                "code": "turn_in_flight",
                "slot": name,
            },
            status=409,
        )
    # ``discard_conversation`` is a full teardown: it also releases the shared
    # sub-agent runtime the parent's children run on. ``slot.running`` is False
    # while they keep going — the parent turn ends first — so nothing above
    # catches it, and the same guard the reload route uses is what does.
    attached = await _subagents_attached_response(state, slot, key, "slot_reset_conversation")
    if attached is not None:
        return attached

    # A SIBLING alias cold-starting on this same session, which the two checks
    # above cannot see: two slots can drive one session (a channel-linked slot
    # and its dashboard twin), and while the other one's first turn is still in
    # ``provider.start()`` this slot's ``running`` is False and no provider is
    # registered yet. Discarding the resume pointer then reports a reset while
    # the starting turn brings the old conversation back. ``_switch_target_busy``
    # is the predicate the switch handlers refuse that window with. It runs
    # here, after the children probe, because that probe is an await a turn can
    # begin in, and nothing but the discard follows it. The discard's
    # ``skip_if_busy`` stays the authority for a registered session; it cannot
    # see an unregistered one, which is why this read must be the last.
    if _switch_target_busy(state, slot, key, state.sessions.get_provider(key)):
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight", "slot": name},
            status=409,
        )

    # ``skip_if_busy``: the fast paths above cannot see a turn that holds the
    # per-session semaphore but has not yet put a prompt in flight (an inbound
    # channel message between the lease and its first stream event). The discard
    # probes the semaphore atomically with the session pop, so a turn admitted
    # after the guards above answered False is refused here instead of being
    # torn down mid-lease.
    discarded = await state.sessions.discard_conversation(key, replay=replay, skip_if_busy=True)
    if not discarded:
        sel().log_api_access(
            caller=request.get("app", "") or "dashboard",
            operation="slot_reset_conversation",
            outcome="denied",
            resources=f"slot={name} replay={replay}",
        )
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight", "slot": name},
            status=409,
        )
    # The fresh conversation will advertise its own model list, so the previous
    # one's withhold verdict does not describe this slot. Only on a performed
    # discard: a refusal above leaves the old conversation (and its verdict) in
    # place.
    slot.forget_session_model_state()
    sel().log_api_access(
        caller=request.get("app", "") or "dashboard",
        operation="slot_reset_conversation",
        outcome="completed",
        resources=f"slot={name} replay={replay}",
    )
    return web.json_response({"slot": name, "reset": True, "replay": replay})

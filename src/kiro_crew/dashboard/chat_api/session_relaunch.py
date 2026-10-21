"""The relaunch transactions: reload (``reload_slot_session``), the workspace change and
the project change.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        _SESSION_RELOAD_NOTICE,
        _TEARDOWN_INCOMPLETE_WARNING,
        SESSION_RELOAD_KIND,
        AcpProvider,
        DashboardState,
        LLMProvider,
        _app_cancel_denied,
        _append_session_switch_notice,
        _ChatSlot,
        _CommitToken,
        _reset_slot_session,
        _reset_slot_session_or_warn,
        _save_recent_project,
        _slot_not_found,
        _slot_replaced_while_queued,
        _slot_switch_session_lock,
        _subagents_attached_response,
        _switch_target_busy,
        _test_interleave,
        default_project_dir,
        deny_app_slot_access,
        effective_session_key,
        is_sensitive_path,
        logger,
        owner_start_priority,
        read_bounded_json,
        relay_archive_refusal,
        schedule_eager_spawn,
        sel,
        slot_not_found,
        voice_runtime_workspace_conflict,
    )


async def api_chat_slot_reload(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reload -- relaunch the slot's agent process.

    A live agent process mounts its MCP servers and builds its tool table once,
    at session-init time; config that changes afterwards (a newly added MCP
    server, an env or agent-spec fix) never reaches it. Reload is the in-place
    remedy: tear the process down exactly like the agent/workspace switch
    handlers do, then eagerly re-arm the resume spawn, so the relaunched
    process re-reads its agent spec and environment and re-initializes MCP
    servers via session/load -- with the conversation preserved.

    The teardown itself is :func:`reload_slot_session`, shared with the
    session-control ``session_reload`` verb; this handler supplies the
    dashboard route's own authorization (slot identity and app isolation).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()

    def _still_ours(_after_reset: bool) -> bool:
        # Re-authorize after every await: ``name`` can be recreated for a
        # DIFFERENT app while this request queued on a lock (slot removal +
        # re-registration under the same name is how a client reconnects), and
        # the stale ``slot`` object's app-isolation check would then authorize
        # this teardown against the NEW slot's session -- the same
        # cross-slot-identity gap the tags/folders/regenerate handlers close
        # with this exact re-check (e.g. chat_tags.py's ``is not slot`` guard).
        return not _slot_replaced_while_queued(state, slot, name, request, "chat.slot_reload")

    def _denied(session_key: str) -> web.Response | None:
        # App isolation, same policy as the cancel routes: reload is a
        # teardown, so an app token must own both the slot and the session the
        # teardown lands on, and a denial is indistinguishable from a missing
        # slot.
        return _app_cancel_denied(request, slot, "chat.slot_reload", session_key)

    def _busy(session_key: str) -> bool:
        # The registered provider's own view first, as this route always read
        # it (any provider object, not only an ``LLMProvider``). Then
        # ``_switch_target_busy``, the predicate the switch handlers and the
        # ``session_reload`` verb refuse with: it adds this slot's own
        # ``running`` reservation and every SIBLING alias's turn on the same
        # session. Both cover a first turn still in ``provider.start()``, which
        # has no registered provider to ask -- reloading then finds no session,
        # reports success and eagerly spawns a second start beside the one in
        # flight. The menu item is already disabled while this slot runs; this
        # makes the route agree with it. A fast path: the reset's
        # ``skip_if_busy`` stays the authority.
        provider = state.sessions.get_provider(session_key)
        if provider is not None and provider.has_active_turn():
            return True
        return _switch_target_busy(state, slot, session_key, provider)  # type: ignore[arg-type]

    return await reload_slot_session(
        state,
        slot,
        name,
        still_ours=_still_ours,
        denied=_denied,
        busy=_busy,
        notice=_SESSION_RELOAD_NOTICE,
    )


async def reload_slot_session(
    state: DashboardState,
    slot: _ChatSlot,
    name: str,
    *,
    still_ours: Callable[[bool], bool],
    denied: Callable[[str], web.Response | None],
    busy: Callable[[str], bool],
    notice: str,
    after_reset_in_lock: Callable[[str], Awaitable[None]] | None = None,
) -> web.Response:
    """Tear down *slot*'s agent process and re-arm its resume spawn.

    The one reload teardown, shared by ``api_chat_slot_reload`` (the tab menu)
    and ``session_control.reload_target`` (the ``session_reload`` MCP verb), so
    the lock order and the re-checks below cannot drift between the two. The
    transcript is not touched: only the agent session is reset, and one notice
    row (*notice*, tagged ``SESSION_RELOAD_KIND``) is appended afterwards.

    The caller supplies its own authorization as three callbacks:

    * ``still_ours(after_reset)`` -- re-run after every await; False answers
      the byte-identical ``slot_not_found`` 404. ``after_reset`` is True only
      for the re-check that follows ``_reset_slot_session``, so a caller can
      tell "refused, nothing was torn down" from "the process is already
      gone" without inferring the phase from call order. It may also RAISE
      (the session-control verb re-runs its gate here and lets a pre-reset
      refusal propagate); the lock stack still unwinds.
    * ``denied(session_key)`` -- the teardown-target check, run once the
      session key is resolved inside both locks; a response it returns is
      returned as-is.
    * ``busy(session_key)`` -- the fast-path busy probe, refusing with
      ``turn_in_flight``.

    Refused with 409 while a turn is in flight (killing an in-flight ACP
    process orphans the streaming prompt: resume refusals, empty responses)
    and while sub-agent children are attached (their shared runtime is torn
    down with the parent session -- see ``SessionManager.reset`` -- so a
    reload under a working child silently discards its work). The ``busy``
    check is a best-effort fast path; the authoritative guard is the reset's
    skip_if_busy, which evaluates busyness atomically with the session pop
    (see _reset_slot_session for why the unblock half of the chokepoint is
    safe even when the guard declines).
    """
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. The four commit-before-reset switch
    # handlers hold both across their commit-then-reset span, and reload joins
    # them so its probe-then-teardown is serialized against that span:
    # reload holds no setting to commit, but it tears the session down
    # the same way, and holding neither lock let a reload land inside a
    # switch's span -- the switch could report success on a session reload had
    # already replaced, or vice versa. An ExitStack because the session lock's
    # KEY is only known after the in-lock read below, and locking on any
    # earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above. A mismatch here is
        # indistinguishable from a missing slot.
        if not still_ours(False):
            return slot_not_found()
        # The session the reload will tear down. ``effective_session_key``,
        # never ``_history_key_for``: a channel- or cron-born slot runs its
        # turns under its linked key, and the dashboard-prefixed spelling
        # names a session that never existed -- the reset would "succeed"
        # against nothing while the live process kept its stale config.
        # Resolved INSIDE slot._lock, not before it: a channel/cron rebind can
        # land while this request queues on the lock, so keying the session
        # lock on an earlier read would guard the wrong session (see
        # _slot_switch_session_lock).
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot's switch on this same
        # session, keyed on the value resolved just above -- the same one the
        # probe and reset below use.
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Re-authorize again: the session-lock wait above is a SECOND await
        # point (real contention when a switch on the same session holds it),
        # and a slot removal + re-registration under this name can land while
        # this request queued on THAT lock just as easily as on slot._lock
        # above. Without this, the 7396 check would guard only the first
        # await and leave the exact gap it exists to close open on the
        # second.
        if not still_ours(False):
            return slot_not_found()
        # Re-derive rather than trust the captured session_key: it names a
        # MUTABLE attribute (slot.linked_session_key), so a cron/channel
        # rebind landing on the SAME slot object during the session-lock wait
        # changes what effective_session_key(slot) resolves to without
        # tripping the identity check above. Mirrors the switch handlers'
        # own post-lock ``effective_session_key(slot) != session_key`` guard.
        if effective_session_key(slot) != session_key:
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        refusal = denied(session_key)
        if refusal is not None:
            return refusal
        # Children guard, shared with api_chat_slot_continue: RUNNING children
        # die with the parent runtime, and _subagents_attached_response
        # documents why queued children and in-flight deliveries count too.
        denied_409 = await _subagents_attached_response(state, slot, session_key, "reload")
        if denied_409 is not None:
            return denied_409
        if _test_interleave is not None:
            # Reload now holds slot._lock and _slot_switch_session_lock across
            # its probe-then-teardown, so its teardown is serialized against a
            # switch's commit-then-reset span. Suspending here holds that
            # span open across another actor's transaction so a test
            # can observe that the other actor now blocks on the session lock
            # instead of interleaving.
            await _test_interleave("reload:pre_reset")
        # Re-authorize once more before the teardown: the children probe (and
        # the test seam) above are awaits too, and a channel link, mirror or
        # slot replacement landing during them would otherwise reach
        # _reset_slot_session on authorization read before that await. This
        # is the last pre-reset check, so a refusal here still means nothing
        # was torn down.
        if not still_ours(False):
            return slot_not_found()
        # The busy probe goes LAST, after every await above, for the same
        # reason the switch handlers re-probe right before their reset: message
        # dispatch takes neither lock held here, so a turn -- this slot's own
        # or a sibling alias's cold start, neither of which has a provider to
        # ask yet -- can begin during the children probe. Read before it, that
        # turn would be invisible to this check and to the reset's
        # ``skip_if_busy`` alike, which sees only a registered session.
        if busy(session_key):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        teardown_incomplete = False

        async def _reset() -> bool:
            # SessionManager.reset pops the session before its shutdown can
            # raise, so a raise after the pop means the old process is gone and
            # the reload has happened with a degraded teardown. Propagating it
            # would answer 500 with no notice and no respawn for a completed
            # teardown. Same identity probe as _reset_slot_session_or_warn: the
            # SAME provider still registered means the raise came before the
            # pop, nothing was torn down, and the raise propagates.
            nonlocal teardown_incomplete
            prior_provider = state.sessions.get_provider(session_key)
            try:
                return await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
            except Exception:
                if (
                    prior_provider is not None
                    and state.sessions.get_provider(session_key) is prior_provider
                ):
                    raise
                logger.exception("Slot %s reload: old session teardown incomplete", name)
                teardown_incomplete = True
                return True

        reloaded = await _reset()
        if not reloaded:
            provider = state.sessions.get_provider(session_key)
            if provider is not None and provider.has_active_turn():
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                )
            if provider is not None:
                # A turn slipped into the guard window and already FINISHED:
                # the declined reset left a live idle session untouched, and
                # falling through would report success while the stale process
                # survives -- the silent failure this endpoint exists to
                # prevent. Retry once; a second decline means another turn is
                # genuinely racing, which is the turn-in-flight case.
                reloaded = await _reset()
                if not reloaded:
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"},
                        status=409,
                    )
        # Re-check once more, still inside both locks: _reset_slot_session
        # (and its one retry above) is itself an await, and _bind_cron_slot
        # writes slot.linked_session_key with NO lock of its own, so a rebind
        # can land during that specific await just as it can during the
        # earlier lock-acquisition waits. Skipping this would report success
        # while the now-current session was never touched -- the exact silent
        # stale-session failure the two earlier checks exist to prevent, just
        # moved one await later.
        #
        # Identity first, same as the 7399/7422 checks above: registry
        # mutation (slot removal + same-name re-registration for a DIFFERENT
        # app) takes no lock of its own, so it can land during this same
        # await exactly as it can during the two earlier lock-acquisition
        # waits those checks guard. A key-only re-check would still pass for
        # a stale ``slot`` object recreated under app B's name whenever B's
        # session happens to resolve to the same key, and the notice/
        # broadcast below would then fire under B's identity -- the same
        # cross-slot-identity gap the two earlier checks close, just moved to
        # this last await. Same response as those checks: a mismatch here is
        # indistinguishable from a missing slot.
        if not still_ours(True):
            return slot_not_found()
        if effective_session_key(slot) != session_key:
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        # Optional work that must run while the agent process is DOWN and both
        # locks are held -- the image-repair route rewrites the on-disk
        # transcript here, which ``session_image_repair`` refuses to touch while
        # a kiro-cli holds it. It runs AFTER the teardown (the process is gone,
        # the transcript lock released) and BEFORE the respawn below re-reads
        # that transcript via ``session/load``. A failure propagates so the
        # route maps it to a response instead of respawning onto an unrepaired
        # transcript; the teardown has already happened, so the next message
        # cold-starts and loads the conversation either way.
        if after_reset_in_lock is not None:
            await after_reset_in_lock(session_key)
            # ``after_reset_in_lock`` can itself await (the image-repair hook
            # rewrites the transcript off the event loop via
            # ``asyncio.to_thread``), and the lock-free slot swap above can land
            # during THAT await just as it can during the earlier ones. Re-check
            # once more so the notice and respawn below fire under this slot's
            # identity, not a same-name slot recreated for a different app
            # mid-hook. A mismatch is indistinguishable from a missing slot.
            if not still_ours(True):
                return slot_not_found()
            if effective_session_key(slot) != session_key:
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
    logger.info("Slot %s session reloaded (had_live_session=%s)", name, reloaded)
    # Feed notice: the visible confirmation (and the durable record) that the
    # relaunch happened. Tagged so the last-real-message scans skip it on both
    # sides (is_system_notice here, isSystemNoticeKind on the frontend).
    # append() itself broadcasts the row -- with the per-row ``mid`` identity
    # clients dedupe on -- so an explicit broadcast here would deliver the
    # notice twice.
    slot.append(
        "assistant",
        notice,
        "msg msg-a",
        meta={"kind": SESSION_RELOAD_KIND},
    )
    # Respawn + session/load now rather than on the next message, so the fresh
    # process (and its rebuilt toolset) is ready when the user comes back.
    schedule_eager_spawn(state, slot, allow_resume=True)
    state.push_slots_update()
    if teardown_incomplete:
        return web.json_response({"ok": True, "warning": _TEARDOWN_INCOMPLETE_WARNING})
    return web.json_response({"ok": True})


async def api_chat_slot_workspace(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/workspace — set workspace for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_workspace")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ws_name = body.get("workspace", "default")
    if not isinstance(ws_name, str):
        # The sibling project handler's guard: with the message-count refusal
        # lifted, this value reaches `default_project_dir` and the slot header
        # on a live conversation, so a non-string is rejected at the boundary.
        return web.json_response(
            {"error": "workspace must be a string", "code": "invalid_workspace"}, status=400
        )
    refusal = relay_archive_refusal(slot)
    if refusal is not None:
        return refusal
    # Same serialization as the agent switch: the reset await yields the event
    # loop, so the mutate-then-reset section runs under the slot's lock — an
    # unlocked write here would interleave with the agent handler's locked
    # compare-and-set on the same workspace/project fields, and two racing
    # workspace switches could each reset against the other's half-applied
    # state. Commit-before-reset ordering per the agent-handler template: a
    # send landing while the reset await is in flight cold-starts a session
    # from the slot's CURRENT bindings, so the new pair must already be
    # visible.
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. An ExitStack because the session
    # lock's KEY is only known after the in-lock read below, and locking on
    # any earlier read could leave this holding the wrong session lock.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        # Re-authorize after the await above (see _slot_replaced_while_queued):
        # ``name`` can be recreated for a different app while this request
        # queued, and every read of ``slot`` below would be of the stale one.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_workspace"):
            return slot_not_found()
        # The session the reset tears down — effective_session_key, never
        # _history_key_for (see api_chat_slot_model), resolved INSIDE the lock
        # so a binding that lands while this request waits on it is what the
        # reset addresses — with the same session-level app isolation the
        # model handler applies.
        session_key = effective_session_key(slot)
        # Now serialize against every OTHER alias slot on this same session.
        # slot._lock is created per _ChatSlot and so is DISJOINT across
        # aliases. Keyed on the value resolved just above -- the same one the
        # probe and reset below use -- so the lock provably guards them even if
        # a binding landed while this request waited on slot._lock (see
        # _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name
        # recreate lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_workspace"):
            return slot_not_found()
        denied = _app_cancel_denied(request, slot, "chat.slot_workspace", session_key)
        if denied is not None:
            return denied
        # A started conversation is NOT refused. Such a refusal protects
        # nothing the sibling handlers protect: the transcript and its
        # session key are workspace-independent (the name is a metadata
        # field inside the same history file, never part of its path), so a
        # switch costs the LIVE agent context and nothing persisted -- and
        # `api_chat_slot_agent` already re-points
        # ``slot.workspace`` mid-conversation, with no message-count guard,
        # whenever the picked agent carries different bindings. The restart
        # is marked in the transcript at the commit point below, by the same
        # helper every sibling reset site uses.
        #
        # Same-value re-pick: nothing to switch, so nothing to tear down.
        # Checked INSIDE the locks (the model handler's ordering): a serialized
        # predecessor targeting this workspace may have committed while this
        # request waited, and resetting again would kill the session that
        # predecessor just set up. Answers the same 200 a real switch does,
        # since the slot IS on the requested workspace.
        if slot.workspace == ws_name:
            return web.json_response({"ok": True, "workspace": ws_name})
        # Children attached to this session run on the runtime the reset
        # below tears down, so refuse rather than discard their work -- the
        # same probe every sibling switch (agent, model, effort, reload)
        # applies. Before the commit, so no rollback is needed.
        children_409 = await _subagents_attached_response(
            state, slot, session_key, "slot_workspace"
        )
        if children_409 is not None:
            return children_409
        # Never tear down an in-flight turn: the model handler's early
        # refusal, shared through _switch_target_busy. The reset
        # below calls _unblock_pending_waits BEFORE SessionManager.reset's
        # atomic busy decline, so without this check a turn parked on a
        # pending approval has that approval rejected and only then gets a
        # 409 -- the turn is altered despite the refusal; and without the
        # helper's third signal a switch through this slot while a sibling
        # alias cold-starts would report success while that turn runs on the
        # old project. The atomic skip_if_busy decline stays as the backstop
        # for a turn that starts after this read.
        pre_provider = state.sessions.get_provider(session_key)
        if _switch_target_busy(state, slot, session_key, pre_provider):
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        prior_workspace = slot.workspace
        prior_project = slot.project
        # Commit as identity tokens (the agent handler's _CommitToken
        # precedent): ``slot.project`` has lock-free writers -- the in-turn
        # set_project directive lands during the reset await -- so a rollback
        # must unwind only the value THIS request wrote, never a concurrent
        # write of a different (or even the same) text.
        committed_workspace = _CommitToken(ws_name)
        committed_project = _CommitToken(default_project_dir(ws_name))
        slot.workspace = committed_workspace
        slot.project = committed_project
        logger.info("Slot %s workspace switched to %r, resetting session", name, ws_name)

        def _rollback() -> None:
            """Unwind this request's commit on every 409 path.

            Identity-scoped per field (see the tokens above), then re-marked
            dirty: the periodic flush runs unlocked every few seconds and may
            already have written the provisional bindings to disk during the
            reset await (a started slot is dirty whenever a turn is active),
            so without the re-mark a rejected switch would survive a restart.
            The flush rebuilds the metadata line from the live fields, so the
            re-mark reconverges disk to whatever the rollback left.
            """
            if slot.workspace is committed_workspace:  # type: ignore[union-attr]
                slot.workspace = prior_workspace  # type: ignore[union-attr]
            if slot.project is committed_project:  # type: ignore[union-attr]
                slot.project = prior_project  # type: ignore[union-attr]
            slot._dirty = True  # type: ignore[union-attr]

        # skip_if_busy: message dispatch does not take slot._lock, so a send
        # can land while this request holds it. SessionManager.reset evaluates busyness
        # atomically with the session pop (the authoritative backstop
        # api_chat_slot_reload documents), so the slipped-in turn is declined
        # here instead of torn down mid-stream.
        # Set when the old session's teardown RAISED after the pop: the
        # switch is committed, the response carries an advisory warning
        # (agent-handler precedent via _reset_slot_session_or_warn).
        teardown_incomplete = False
        reset_ok = await _reset_slot_session_or_warn(
            state, slot, session_key, switch_kind="workspace"
        )
        if reset_ok is None:
            # Teardown raised after the session pop: the switch is COMMITTED
            # (see _reset_slot_session_or_warn), so the answer is the
            # committed state with an advisory warning — never a 500 that
            # strands clients on the old bindings. Deliberately no rollback
            # of slot.workspace/slot.project: rollback is only for the
            # decline/409 paths, where nothing was torn down. NOT an early
            # return: the rebind guard below must still run, so a slot
            # rebound during the raising await answers the same rollback +
            # 409 as any other rebind.
            teardown_incomplete = True
        elif not reset_ok:
            # Disambiguate FAIL-CLOSED, same as the model handler: dispatch
            # captures the slot bindings at its call site but registers the
            # session only after a multi-second provider.start(), so no
            # identity or registration-time reasoning can prove which
            # bindings a live session carries. A false 409 is retryable; a
            # false success strands a live session on the old bindings.
            busy_provider = state.sessions.get_provider(session_key)
            live_serves_target = False
            if isinstance(busy_provider, AcpProvider) and slot.project:
                # Truth-based check, mirroring the model handler: when the
                # live session's actual working directory already equals the
                # COMMITTED project, the session cold-started on the new
                # bindings (a first send captured them after the commit) —
                # rolling back would advertise the old workspace while the
                # live process runs the new one. Success without teardown is
                # the truthful answer.
                live_serves_target = busy_provider.cwd == slot.project
                if live_serves_target:
                    logger.info(
                        "Slot %s workspace switch: live session already runs under %r; "
                        "declined reset left it in place",
                        name,
                        slot.project,
                    )
            if not live_serves_target and isinstance(busy_provider, LLMProvider):
                if busy_provider.has_active_turn():
                    # Roll back the commit (commit-before-reset means the new
                    # pair is already visible) and answer the same 409 the
                    # guard gives.
                    _rollback()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )
                # A live IDLE session declined the reset. Tearing down an idle
                # session is always safe, so retry once
                # (api_chat_slot_reload's template); a second decline means
                # another turn is genuinely racing.
                reset_ok = await _reset_slot_session_or_warn(
                    state, slot, session_key, switch_kind="workspace"
                )
                if reset_ok is None:
                    # Retry teardown raised: same committed-switch answer as
                    # the first attempt, and same fall-through to the rebind
                    # guard below.
                    teardown_incomplete = True
                elif not reset_ok:
                    _rollback()
                    return web.json_response(
                        {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
                    )
            # No live provider: no registered session to tear down — the next
            # message cold-starts under the new bindings.
        if effective_session_key(slot) != session_key:
            # The slot was bound to a different session during the reset
            # await(s): the session torn down is not the slot's, so the
            # committed bindings would describe a session that never saw the
            # switch. Roll back and answer 409; the retry resolves the
            # current binding.
            _rollback()
            return web.json_response(
                {"error": "slot session was rebound during the switch", "code": "session_rebound"},
                status=409,
            )
        # Mark for the periodic flush: the flush writes a slot's metadata
        # line only while ``_dirty`` is set, and nothing else on this path
        # sets it. The switch is allowed on a started conversation, so
        # without the mark a gateway crash before the next message restores
        # the OLD workspace/project over a switch the user saw succeed. The
        # same-value no-op changes nothing, so it does not need this.
        slot._dirty = True
        # The transcript boundary for the reset, still inside both locks so
        # the same-value check above and this append cannot interleave.
        await _append_session_switch_notice(slot, "workspace", ws_name)
    state.push_slots_update()
    ws_resp: dict = {"ok": True, "workspace": ws_name}
    if teardown_incomplete:
        # Advisory only — the switch itself succeeded and the response
        # carries the committed state (agent-handler precedent).
        ws_resp["warning"] = _TEARDOWN_INCOMPLETE_WARNING
    return web.json_response(ws_resp)


async def api_chat_slot_project(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/project — set project directory for file search scoping."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_project")
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
        return _slot_not_found()
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    project = body.get("project", "")
    if not isinstance(project, str):
        return web.json_response({"error": "project must be a string"}, status=400)
    project = project.strip()
    # Session-level app isolation BEFORE any filesystem probing: the
    # isdir / sensitive-path / voice-runtime checks below answer differently
    # for existing vs missing paths, so running them ahead of the denial
    # would hand an app caller that owns a linked slot an unauthorized
    # filesystem existence oracle. Best-effort read outside the lock — the
    # locked re-check below stays authoritative for a binding that moves
    # while this request waits on the lock.
    denied = _app_cancel_denied(request, slot, "chat.slot_project", effective_session_key(slot))
    if denied is not None:
        return denied
    if project:
        project = os.path.realpath(os.path.expanduser(project))
        if not os.path.isdir(project):
            return web.json_response({"error": "Not a directory"}, status=400)
        if is_sensitive_path(project):
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="chat_slot_project",
                outcome="denied",
                resources=f"slot={name} project={project}",
                error="sensitive path",
            )
            return web.json_response({"error": "Access denied"}, status=403)
        # Pre-flight the voice-runtime workspace guard: a
        # workspace that contains (or sits inside) the Kiro Crew data home is
        # refused at agent spawn anyway, but only after the session exists and
        # with a spawn-time stack trace. Reject it here, at the moment of
        # choice, with the same actionable message. Off-loop: the check primes
        # the runtime path cache (mkdir/realpath) on first use.
        conflict = await asyncio.to_thread(voice_runtime_workspace_conflict, project)
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
            return _slot_not_found()
        if conflict is not None:
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="chat_slot_project",
                outcome="denied",
                resources=f"slot={name} project={project}",
                error="voice runtime overlap",
            )
            return web.json_response(
                {"error": conflict, "code": "workspace_overlaps_data_home"},
                status=400,
            )
    # Same serialization as the agent/model/workspace switch handlers: this is
    # the one remaining live HTTP mutator of slot.project (the MCP set_project
    # directive in session_directive_apply also writes it, but from inside the
    # slot's own running turn, where this lock is not an option), and unlocked
    # it could interleave with a locked switch's mutate-then-reset section —
    # the workspace handler's rollback would then erase a project pick that
    # landed during its reset await. The lock only serializes the write; the
    # reset stays DEFERRED via the flag (the killpg constraint below), so no
    # reset is awaited while holding the lock beyond what the other switch
    # handlers already hold.
    #
    # Two locks, in the order documented at _slot_switch_session_lock:
    # slot._lock, then the session lock. The session lock is the one a turn's
    # dispatch in chat_runner._run_chat holds while it captures its binding
    # (slot.project included) and registers its session. Without it a pick
    # made in the first moments of a reply lands inside that window, and the
    # turn's _require_current_binding check fails the message just sent with
    # "conversation binding changed during preparation". Under it the pick
    # waits until the turn has registered (the dispatch releases the lock
    # there, never holding it across the turn), so the running reply keeps
    # its captured project and the pick applies from the next turn via the
    # deferred reset below.
    async with contextlib.AsyncExitStack() as _stack:
        await _stack.enter_async_context(slot._lock)
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
            return _slot_not_found()
        # The session the deferred reset will address — ``effective_session_key``,
        # never ``_history_key_for`` (see api_chat_slot_model): a channel- or
        # cron-born slot runs its turns under its linked key, and the
        # dashboard-prefixed spelling names a session that never existed, so
        # the deferred reset would "succeed" against nothing while the live
        # process kept the old CWD. Resolved INSIDE the lock (a binding can
        # land while this request waits on it), and the flag below carries
        # THIS key — the one the app gate authorized — so authorization and
        # action cannot disagree. session_directive_apply's set_project path
        # already resolves the flag the same way.
        session_key = effective_session_key(slot)
        # Keyed on the value resolved just above, so the lock provably guards
        # the session this request acts on (see _slot_switch_session_lock).
        await _stack.enter_async_context(_slot_switch_session_lock(session_key))
        # Second lock-acquisition await, second re-check: a same-name recreate
        # lands during this wait just as easily as during the first.
        if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
            return _slot_not_found()
        # App isolation on the SESSION, not just the slot (the cancel routes'
        # policy): slot ownership does not imply ownership of a linked
        # channel session, so an app caller may not repoint the project a
        # channel thread runs under. Denied as an indistinguishable 404.
        denied = _app_cancel_denied(request, slot, "chat.slot_project", session_key)
        if denied is not None:
            return denied
        old_project = slot.project
        # _CommitToken (identity-gated rollback), the agent handler's pattern:
        # slot.project has unlocked writers (the in-turn set_project directive
        # writes this field without the lock, and may legitimately write the
        # very project this handler sets). A value compare-and-set rollback
        # cannot tell such a same-text write from this handler's own commit and
        # would erase it; a per-request identity token can.
        committed_project = _CommitToken(project)
        slot.project = committed_project
        logger.info("Slot %s project set to %r", name, project)
        sel().log_api_access(
            caller=request.get("user", "dashboard"),
            operation="chat_slot_project",
            outcome="allowed",
            resources=f"slot={name} project={project}",
        )
        # Track recent projects
        if project:
            try:
                await asyncio.to_thread(_save_recent_project, project)
            except Exception:
                logger.warning("Failed to save recent project", exc_info=True)
            # A detached slot cannot arm a reset for its same-name successor.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_project"):
                if slot.project is committed_project:
                    slot.project = old_project
                return _slot_not_found()
        # Reset the session so the next message cold-starts with the new CWD and
        # picks up project-level .kiro/steering/**/*.md (mirrors api_chat_slot_agent).
        # Only on an actual change — avoids a needless cold start on a no-op set.
        #
        # Deferred via a flag because this endpoint is reachable over loopback HTTP
        # from inside the kiro-cli process group (the set_project MCP tool); an
        # inline reset would killpg() the caller. Consumed in chat_runner.
        if project != old_project:
            if effective_session_key(slot) != session_key:
                # The slot was bound to a different session while the
                # recent-project save awaited: arming the flag with the key
                # this request resolved would have the consumer tear down a
                # session nobody is on while the slot's ACTUAL session keeps
                # the old CWD — the exact stale-binding class this handler
                # was converted to remove. Re-resolving here instead is not
                # an option either: it would arm a key the app gate above
                # never authorized. Roll back the commit (identity-gated on
                # the _CommitToken — the in-turn set_project directive writes
                # this field without the lock, and a same-value write must not
                # be mistaken for this handler's own commit) and answer the
                # same 409 the sibling switch handlers use.
                if slot.project is committed_project:
                    slot.project = old_project
                return web.json_response(
                    {
                        "error": "slot session was rebound during the switch",
                        "code": "session_rebound",
                    },
                    status=409,
                )
            # Determine whether a start that captured the OLD cwd is already
            # in flight BEFORE scheduling the replacement eager spawn, because
            # that scheduling cancels any prior eager task and roots a new one
            # at the NEW project. A start carries the old cwd when:
            #   * a session is already registered under the key, or
            #   * a turn is in flight (running, or its admission reserved) whose
            #     own get_or_create captured slot.project BEFORE this change and
            #     will register a session rooted at the old cwd, or
            #   * a cold start is already reserved under the key — a
            #     channel-linked turn (Slack/Discord) resuming on
            #     effective_session_key(slot) passes no cwd and resolves the
            #     stored old cwd, yet nothing is in the session map and this
            #     slot's own ``running`` is False. session_keys() includes
            #     allocation reservations, so it catches that window that both
            #     get_provider and slot.running miss. It also subsumes the
            #     registered case above, kept explicit for readability, or
            #   * no session is live or reserved but the key still has a
            #     resumable entry — an idle-reaped session, or one lost in a
            #     restart, whose resume sid and old cwd remain on disk. The
            #     next channel turn resumes it in the old project and later
            #     dashboard turns reuse that live session, so the reset must
            #     still be owed. resumable_hint() answers exactly that.
            arm_reset = (
                state.sessions.get_provider(session_key) is not None
                or slot.running
                or session_key in state.sessions.session_keys()
                or state.sessions.resumable_hint(session_key)
            )
            # Speculatively re-create the session rooted at the NEW project so the
            # cwd change is paid during think-time. This cancels any prior eager
            # task and the fresh one roots at the new project, so a newly
            # scheduled spawn never carries the old cwd — it must NOT by itself
            # arm the reset, or the first turn's correctly rooted session is torn
            # down at turn end for nothing (the exact waste this guard avoids,
            # where the teardown costs a full tool_search replay). The eager task
            # consumes the deferred reset itself, but only when no turn is
            # running — the same killpg constraint that deferred the reset
            # applies to it.
            schedule_eager_spawn(state, slot, start_priority=owner_start_priority(request))
            # _consume_pending_reset only spends the flag on a real teardown, so
            # an arm that races a cold start stays queued for the next consume
            # rather than clobbering a correct session.
            if arm_reset:
                slot._pending_reset_history_key = session_key
    state.push_slots_update()
    return web.json_response({"ok": True, "project": project})

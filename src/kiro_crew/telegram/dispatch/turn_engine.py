"""The turn engine: one model turn for a message ``handle_message`` admitted.

``_run_turn`` acquires the session, builds the prompt, drives the shared ``TurnDriver``
under the PreToolUse tool gate and the conversation's turn ceiling, books the outcome,
classifies a failure, finalizes the renderer and releases the session, and then drains
what queued behind the turn. ``_persist_turn`` is its durable transcript write and
``_maybe_notice`` its soft-threshold context notice, both post-turn steps.

The dispatcher state these read is ``client``, ``approval_mode``, ``sessions``,
``ctx_builder``, ``conv_log``, ``dashboard_state``, ``_model_pref`` and ``_conv``; they
write ``_active_renderers`` (the running turn's renderer, which a mid-turn steer hands
its text to) and ``_title_tasks``. Every other step is a dispatcher call through
``self``. The user-facing failure wording, ``_user_safe_failure_reason``, stays in
``transport_dispatch.py`` as the registered "Telegram failure reason" redaction sink
and is read through the facade, like every name tests rebind there.

Repository guards read the tool gate, the turn ceiling and its mute-aware refusal, the
crew-log opener, the memory-store seam, the persist-then-pin order and the failure
charge in this file.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew import runtime_death
from kiro_crew.context import session_store_for_turn
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import mint_row_mid
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, hook_gate_kwargs
from kiro_crew.messaging import auto_title, privacy_mode, turn_ceiling
from kiro_crew.messaging.attachments import append_attachment_context
from kiro_crew.messaging.commands import (
    compact_unsupported_backend,
    context_recycle_warning,
    recycle_backend,
    recycle_warning_should_send,
)
from kiro_crew.messaging.dispatch import (
    build_auto_approve,
    build_directive_consumer,
    charge_turn_failure,
    consume_reinjection,
    delivery_is_muted,
    driver_turn_landed,
    open_turn_crew_log,
    predecessor_sid,
    rearm_reinjection,
    requested_model_sid,
    rollback_skill_bodies,
    slot_workspace,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.inbound_spool import InboundRoute, spool_refused_turn
from kiro_crew.messaging.link import DM_SCOPE_UNIFIED, channel_namespace_of
from kiro_crew.messaging.renderer import SilentRenderer
from kiro_crew.messaging.session_resume import persisted_session_agent
from kiro_crew.messaging.session_trust import is_session_trusted
from kiro_crew.messaging.turn_ceiling import TurnCeilingExceeded
from kiro_crew.safety_override import safety_override
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.start_priority import person_priority
from kiro_crew.telegram.attachments import process_telegram_attachments
from kiro_crew.telegram.renderer import TelegramApprovalDecider as _APPROVAL_REGISTRY
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

if TYPE_CHECKING:
    from kiro_crew.messaging.transport import InboundMessage
    from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

#: The dispatcher's one logger, named for the facade module operators filter on.
logger = logging.getLogger("kiro_crew.telegram.transport_dispatch")


async def _run_turn(
    self: TelegramDispatcher,
    msg: InboundMessage,
    text: str,
    *,
    session_key: str,
    resumed_key: str | None,
    route: tuple[str, str],
    user_id: int,
    chat_id: int,
    thread: str | None,
    reply_thread: int | None,
    interpret_commands: bool,
    drain: bool,
) -> None:
    """Run one model turn for a message ``handle_message`` admitted, then drain.

    By here the message has passed governance, admission and activation, is
    not a command and did not land mid-turn, and *session_key* is the key the
    turn runs under, with any privacy request already applied to it. This is
    the turn itself: acquiring the session, building the prompt, driving
    ``TurnDriver``, the post-turn bookkeeping, classifying a failure,
    finalizing the renderer and releasing the session, and then draining the
    queue that built up behind the turn. Repository guards read its constructs
    in this file: the tool gate, the turn ceiling and its mute-aware refusal,
    the crew-log opener, the persist-then-pin order and the failure charge.

    *thread* is the message's own Topic id, which the voice leg answers in;
    *reply_thread* is the Topic the route resolved for every other reply.
    """
    from kiro_crew.telegram import transport_dispatch as facade

    assert self.client is not None, "TelegramDispatcher.client must be set"
    channel_id = f"telegram:{user_id}"
    agent = self._resolve_agent(route)
    if resumed_key is not None:
        persisted = await asyncio.to_thread(persisted_session_agent, self.conv_log, resumed_key)
        if persisted:
            agent = persisted

    decider = (
        facade.TelegramApprovalDecider(session_key=session_key)
        if self.approval_mode == APPROVAL_INTERACTIVE
        else None
    )
    renderer = facade.TelegramRenderer(
        self.client,
        chat_id,
        TELEGRAM_CAPABILITIES,
        session_key=session_key,
        message_thread_id=reply_thread,
        show_thinking=bool(self._live_cfg().telegram.show_thinking),
        uploads_allowed=not await self._uploads_restricted(session_key),
        reply_to_message_id=self._reply_target(msg, interpret_commands=interpret_commands),
    )
    # Same gate as Discord, for the same reason: Telegram also runs its own
    # copy of the turn loop rather than going through ``drive_turn``, so a
    # disconnected conversation would otherwise keep answering.
    muted = delivery_is_muted(self.sessions, session_key, facade.TelegramRenderer.channel_type)
    # Handed to the driver AND closed in the finally. Not a reassignment of
    # ``renderer`` because the concrete ``close`` is not inert (it finalizes the
    # "🤔" placeholder and can surface an error), and a muted turn must leave
    # nothing behind in the conversation. Typed as a union rather than the base
    # ``Renderer`` because this channel WIDENS close to take ``failure_reason``.
    out_renderer: facade.TelegramRenderer | SilentRenderer = (
        SilentRenderer(TELEGRAM_CAPABILITIES, facade.TelegramRenderer.channel_type)
        if muted
        else renderer
    )
    # Expose this turn's renderer so a concurrent mid-turn steer (a separate
    # _handle_busy task) can hand it the user's typed steer text for the
    # inline "↪️ steered: …" chip. Popped in finally.
    # Not published when muted: the steer path calls the channel-specific
    # ``note_steer`` and already skips cleanly on absence, so this both
    # silences the chip in a disconnected conversation and keeps that
    # channel-local API off the shared substitute.
    if not muted:
        self._active_renderers[session_key] = renderer

    # Everything acquire-dependent runs INSIDE the try so the finally always
    # finalizes the placeholder (renderer.close -> no perma-"🤔 …"), even if
    # get_or_create itself raises on a cold-start failure. release() is gated
    # on _acquired so we never release a semaphore we didn't hold. Mirrors
    # slack/transport_dispatch.py.
    _acquired = False
    # The provider THIS turn acquired, for the failure handler's attribution
    # question. Bound before the try so every handler can read it -- an
    # attribution flag read on a path its assignment cannot reach is an
    # UnboundLocalError inside an except arm, not a guard. Stays None when
    # get_or_create never returned, and an unattributable death charges as
    # before.
    _turn_provider: object | None = None
    failure_reason: str | None = None
    attachment_temp_paths: list[str] = []
    # Post-compaction re-injection bookkeeping for the finally: whether this
    # turn consumed the one-shot flag, and whether it landed (recorded success).
    _needs_reinjection = False
    _turn_landed = False
    # The turn's driver, for the finally: it records whether the backend
    # compacted the session, on every exit path.
    driver: facade.TurnDriver | None = None
    try:
        # Ack placeholder first (before the potentially slow cold-start);
        # on_turn_start is idempotent so the driver's later call no-ops.
        # Skipped when muted, as in the Discord twin.
        if not muted:
            await renderer.on_turn_start()
        _memory_store = await session_store_for_turn(self.ctx_builder, session_key)
        provider, is_new, resumed = await self.sessions.get_or_create(
            session_key,
            start_priority=person_priority(msg.person_origin),
            agent=agent,
            channel_id=channel_id,
            # "" is the Auto row's stored value; collapse it to None so Auto
            # means "as if never picked". get_or_create gates its own model
            # resolution on `model is None`, so passing "" would skip that
            # and land on the provider factory's narrower fallback instead.
            #
            # Scoped to a NATIVE turn. ``_model_pref`` is this Telegram route's
            # own choice, and a resumed dashboard session already has a model of
            # its own; handing the route's preference to a cold start would run
            # that conversation under a model its owner never picked, silently
            # and for every later turn. A resumed session therefore passes None
            # and lets its own persisted model resolve.
            model=(None if resumed_key is not None else self._model_pref.get(route) or None),
        )
        _acquired = True
        # Hold the provider this turn obtained, for the failure handler's
        # attribution question. Captured HERE rather than looked up when a
        # failure is handled: the recovery paths replace a dead session, so a
        # lookup at failure time answers for the replacement and the question
        # silently becomes "was the NEW runtime shared".
        _turn_provider = provider
        if resumed_key is None or channel_namespace_of(session_key):
            # The session's crew log, opened the moment the allocation lands
            # and before ANY further await: the work ledger appends every write
            # to the acting session's log and rolls back one it cannot record,
            # so a DM admitted as a conductor needs its log to exist before its
            # first ledger call -- and a turn that bails between the allocation
            # and a later opener (a failed attachment fetch, a renderer error)
            # would leave a live session whose log is first created on the NEXT
            # turn, by then a warm reuse, so the previous edge would never be
            # written. The predecessor is the allocation boundary's own capture,
            # taken inside its critical section and consumed here after the
            # claim -- no read of the mapping around the call, which a
            # concurrent turn's allocate-and-recycle could stale while this turn
            # waited inside the allocation. Every CHANNEL session this dispatcher
            # runs is opened here, including a same-DM native history picked
            # through ``/sessions`` (``resumed_key`` naming a channel key): no
            # dashboard runner ever handles its turns, so this is its only
            # opener. Only a resumed DASHBOARD session is left alone -- its
            # opener is the dashboard's, which alone holds its lineage. The
            # workspace is read off the dashboard slot this conversation is
            # surfaced under, the source a tab on it states the same fact from,
            # so the two writers of this log agree. Never raises, never suspends.
            open_turn_crew_log(
                provider,
                session_key=session_key,
                agent=agent,
                resumed=resumed,
                ctx_builder=self.ctx_builder,
                previous_sid=predecessor_sid(self.sessions, session_key),
                model_requested=requested_model_sid(self.sessions, session_key),
                workspace=slot_workspace(self.dashboard_state, session_key),
            )
        # Extraction's approved root is the provider's OWN resolved cwd, so a
        # path lexically outside it is refused before any metadata probe.
        # Read defensively: an absent cwd must mean "no uploads", never a dead
        # turn — this capability may not add a failure mode to the path that
        # answers the user's message.
        # The runtime's own `$KIROCREW_SCRATCH` goes beside it, read the same
        # defensive way: that is where the agent is told to stage screenshots.
        renderer.authorize_upload_root(
            getattr(provider, "cwd", ""),
            scratch_root=str(getattr(provider, "work_scratch_dir", None) or ""),
        )
        # Feeds the turn footer's context gauge. Read off the provider rather
        # than passed at construction, because the provider does not exist
        # until the session is acquired.
        renderer.attach_context_client(getattr(provider, "client", None))
        is_new_own_session = is_new and resumed_key is None
        if is_new_own_session:
            await self.sessions.set_channel(session_key, channel_id)
        if resumed_key is None:
            # A resumed dashboard session already owns its surface and binding.
            # Reassert only Telegram's native conversation mirror -- and record
            # that conversation as the session's ORIGIN, the in-memory fact the
            # dashboard reads for unattended output about the session and for
            # session control's owner-DM check (a DM whose mirror IS its own
            # conversation is one audience; a mirror aimed anywhere else is not,
            # and only the recorded origin can tell the two apart). Discord's
            # dispatcher writes both on the same turn for the same reasons.
            # The same key-based unified guard ``bind_origin_mirror`` applies:
            # a ``unified:{agent}`` bucket collapses every user's DMs into one
            # session, so it has no single origin to record.
            setter = getattr(self.sessions, "set_origin_link", None)
            if setter is not None and channel_namespace_of(session_key) != DM_SCOPE_UNIFIED:
                setter(session_key, self._origin_mirror_link(route, chat_id))
            self._bind_origin_mirror(session_key, route, chat_id)
        # ── Attachment ingestion (mirrors Discord) ──
        if msg.attachments:
            attachment_result = await process_telegram_attachments(self.client, msg.attachments)
            attachment_temp_paths = list(attachment_result.temp_paths)
            text = append_attachment_context(text, attachment_result)
        if not text:
            return
        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
        await facade.publish_turn_identity(self.sessions, session_key)
        # This conversation's own silo, from the session's RECORDED binding and
        # never from ``agent``: that value is a kiro agent name, a namespace
        # disjoint from ``cfg.agents``, so a store derived from it resolves to
        # ``default`` for exactly the crew that configured otherwise. Private
        # memory was prepared before provider acquisition and fails closed.
        # A compaction drops session-start context. Read-and-clear the
        # one-shot flag so this turn re-injects that context exactly once;
        # the finally re-arms it if this turn never lands.
        _needs_reinjection = consume_reinjection(self.sessions, session_key)
        # Off-loop: build_message embeds the episodic query (blocking urllib).
        full_message, _ = await run_in_embed_pool(
            self.ctx_builder.build_message,
            text,
            is_new,
            session_key,
            channel_id=channel_id,
            agent=agent,
            memory_store=_memory_store,
            resumed=resumed,
            needs_reinjection=_needs_reinjection,
            runtime_source="telegram",
            context_provider=provider,
            # Temporary mode reads NO memory, which is the half the transcript
            # gate cannot cover: refusing to WRITE still leaves yesterday's
            # memories and lessons in today's prompt. Incognito deliberately
            # still reads — that is the documented difference between the two.
            # Resolved dashboard-aware, because a RESUMED session carries its
            # mode on the slot rather than in this channel's tracker.
            blocks_reads=await self._blocks_memory_reads(session_key),
            # Who is speaking. Slack has always passed this; without it the
            # model cannot address the user or tell two participants of a
            # forum Topic apart. Already narrowed to Telegram's own username
            # grammar by ``prompt_safe_handle``, and empty for an account with
            # no @handle, which ``build_message`` treats as "omit the line".
            user_display_name=getattr(msg, "username", "") or None,
        )

        # PreToolUse security gate (channel-neutral, off ctx_builder.hooks):
        # sensitive-path keystone + governance ceiling + deny-list. Returns
        # "deny" (un-overridable), "auto_approve", or "" (passthrough).
        def _tool_gate(event: Any) -> str:
            result = self.ctx_builder.hooks.on_tool_call(
                getattr(event, "title", "") or "",
                session_key=session_key,
                agent=agent,
                **hook_gate_kwargs(event),
            )
            if result.action == TOOL_DENY:
                return "deny"
            if result.action == TOOL_AUTO_APPROVE:
                return "auto_approve"
            return ""

        driver = facade.TurnDriver(
            provider,
            out_renderer,
            approval_mode=self.approval_mode,
            decider=decider,
            # Preserve the auto_approve_subagent_spawn hook for spawn_run.
            # The shared builder keys on canonical event identity
            # (tool_name/is_shell), never the model-authored title.
            auto_approve_tool=build_auto_approve(self.ctx_builder),
            # /yolo: read the grant per request, not once at boot, so turning
            # it on (or letting it expire) takes effect on the very next tool
            # instead of after a gateway restart. TurnDriver runs the
            # PreToolUse gate BEFORE this, so a hard deny still wins.
            # Global YOLO OR this conversation's own Trust grant. Read per
            # request, not once at boot, so a Trust press or a YOLO expiry
            # takes effect on the very next tool. TurnDriver runs the
            # PreToolUse gate BEFORE this, so a hard deny still wins.
            auto_approve_session=lambda: (
                safety_override().is_active() or is_session_trusted(session_key)
            ),
            tool_gate=_tool_gate,
            # Session-directive consumer: monitor_start / autonudge_stop /
            # ... return a marker the driver decodes; apply it against THIS
            # turn's session key (dashboard-only directives stay refused
            # for channel sessions).
            directive_consumer=build_directive_consumer(
                session_key=session_key, sessions=self.sessions, dispatcher=self
            ),
            audit_session_key=session_key,
            audit_agent=agent or "kirocrew",
            closing_gate=turn_ceiling.gate(
                session_key, lambda: self.sessions.begin_turn(session_key)
            ),
            undelivered_watch_key=session_key,
        )
        accumulated = await driver.run(full_message)

        # ── Post-turn bookkeeping (each guarded so a failure here can't
        # fall through to the except and re-record the successful turn). ──
        _unfinished = getattr(driver, "unfinished_turn", False) is True
        if _unfinished:
            # The handle ended a turn the backend never finished; the driver
            # already followed the partial text with the timeout notice. The
            # runtime still reads alive, so book a failure and reset the
            # session (keeping the conversation), as the shared channel
            # dispatcher does, or the next message waits on it again.
            logger.warning(
                "Telegram transport_dispatch: the turn for %s ended unfinished; "
                "resetting the session",
                session_key,
            )
            # At its threshold the breaker resets the session inside
            # record_failure and answers True. A same-key message can claim a
            # successor while that reset is awaited, so a second reset here
            # would end the successor mid-turn: reset only when it did not.
            breaker_reset = await self.sessions.record_failure(session_key)
            if breaker_reset is not True:
                try:
                    await self.sessions.reset(session_key)
                except Exception:
                    logger.warning(
                        "Telegram transport_dispatch: session reset after an "
                        "unfinished turn failed session=%s",
                        session_key,
                        exc_info=True,
                    )
        else:
            self.sessions.record_success(session_key)
            # Beside the counter it stands in for: a landed turn clears the
            # shared-death streak exactly as it clears the consecutive-failure
            # count, so the streak stays a consecutive run rather than a lifetime
            # total whose bound is permanently tripped.
            runtime_death.clear_shared_deaths(session_key)
        # The prompt (with any re-injected context) reached the model and
        # the turn completed, so the finally must NOT restore the flag --
        # unless the user cancelled it, which discards that prompt.
        _turn_landed = driver_turn_landed(driver)
        if _unfinished:
            facade.Stats().inc_message_failed()
        else:
            facade.Stats().inc_message_success()
        if accumulated and not muted and self._voice_enabled(route):
            # Its own bookkeeping step, and last-effort by design: the text
            # answer has already landed, so a TTS failure must not reach the
            # except below and re-record a successful turn as a failure.
            # ``muted`` conversations get nothing outbound at all, voice
            # included — a disconnected conversation that started talking would
            # be the loudest possible version of the bug the mute gate exists
            # to prevent.
            await self._speak_reply(route, chat_id, accumulated, int(thread) if thread else None)
        # Decided BEFORE the persist below, and used by BOTH it and the
        # auto-title dispatch, so the two cannot disagree. That write mints
        # this conversation's deterministic fallback name, and auto-title's
        # guard refuses a record that already carries a name -- so a fallback
        # written here would be the conversation's name for good, with the
        # claim retained and no later exchange retrying. The suppression is
        # passed explicitly rather than inferred from claim timing, because the
        # claim is taken AFTER this write: the pin below reads a record this
        # turn has already persisted.
        #
        # ``accumulated`` is required: a turn that produced no text has
        # nothing to name, and titling it would spend a background turn to be
        # told SKIP. A restricted session persists nothing to title, and a
        # resumed session is not this dispatcher's to name.
        _will_auto_title = bool(
            resumed_key is None
            and accumulated
            and not privacy_mode.is_restricted(session_key)
            # Cheap synchronous peek: ``try_claim`` below tests this very
            # membership, so without it the pin's thread hop would be paid
            # and then thrown away on every later message of every
            # already-named conversation.
            and not auto_title.is_titled(session_key)
        )
        try:
            # Circular import: the dashboard package imports the channel
            # transports on its boot path, so this edge only exists at call
            # time. Same reason as the ``surface_dispatcher_session`` import
            # below.
            from kiro_crew.dashboard.channel_slots import project_channel_turn_live

            # Decided HERE, on the loop, because a resumed dashboard session's
            # restriction lives on its live slot (or, with the tab closed, in
            # the persisted transcript) — neither is reachable from the worker
            # thread the write runs in. BOTH paths below must be gated: the
            # projection appends to a dashboard slot and marks it dirty, so a
            # later slot flush would persist those rows even if this direct
            # writer were skipped.
            dashboard_restricted = await self._session_restricted(session_key)
            if not dashboard_restricted:
                mirror_mids = project_channel_turn_live(
                    self.dashboard_state,
                    session_key,
                    text,
                    accumulated,
                    broadcast_user=True,
                )
                await asyncio.to_thread(
                    self._persist_turn,
                    session_key,
                    text,
                    accumulated,
                    is_new_own_session,
                    agent=agent,
                    mirror_mids=mirror_mids,
                    auto_title_pending=_will_auto_title,
                )
        except Exception:
            logger.warning("Telegram: persist_turn failed session=%s", session_key, exc_info=True)
        # Auto-title, fire-and-forget. Without it a conversation's name is
        # frozen at the first forty characters of the first message forever —
        # the deterministic fallback ``_persist_turn`` writes — and the only
        # correction is a manual /title. Claim-and-spawn, never awaited: the
        # answer has already been delivered, so the user waits on nothing.
        #
        # Placed AFTER the persist above, because the record the pin reads is
        # the one ``_persist_turn`` mints: pinning ahead of it would read
        # ABSENT on a conversation's first exchange, which the guard refuses,
        # so a new thread would lose its generated name and pay a second
        # background turn for it on the next exchange. Both Slack dispatchers
        # are ordered the same way -- they persist their turn before they pin
        # -- so an ABSENT pin means the record is genuinely gone rather than
        # not yet written. It stays OUTSIDE the ``dashboard_restricted``
        # branch above so titling keeps the conditions it has here and gains
        # none from that gate.
        #
        # Isolated like every other bookkeeping step here, so failing even to
        # SPAWN the task never re-records this successful turn as a failure.
        try:
            if _will_auto_title:
                # Pin BEFORE claiming, and both before scheduling. The pin read
                # suspends on a thread, so claiming first would hold the claim
                # across that await with nothing scheduled yet to release it,
                # and a cancellation there would strand it -- the claim is
                # process-wide, so this key could not be named again until the
                # gateway restarts. The pin still precedes ``create_task``,
                # which is what closes the scheduling-tick window: read inside
                # the task, one tick is enough for a delete plus a re-message
                # on this thread to pin the replacement.
                _title_pin = await auto_title.pin_record(self.conv_log, session_key)
                if auto_title.try_claim(session_key):
                    _title_task = asyncio.create_task(
                        auto_title.maybe_auto_title(
                            self.sessions,
                            self.conv_log,
                            session_key,
                            text,
                            accumulated,
                            pin=_title_pin,
                            source="telegram",
                        )
                    )
                    self._title_tasks.add(_title_task)
                    _title_task.add_done_callback(self._title_tasks.discard)
        except Exception:
            logger.warning(
                "Telegram: auto-title dispatch failed session=%s",
                session_key,
                exc_info=True,
            )
        if is_new_own_session:
            try:
                # Circular import: dashboard boot imports channel packages.
                from kiro_crew.dashboard.channel_slots import (
                    surface_dispatcher_session,
                )

                await surface_dispatcher_session(self)
            except Exception:
                logger.warning(
                    "Telegram: immediate dashboard session surface failed session=%s",
                    session_key,
                    exc_info=True,
                )
        try:
            await self._maybe_notice(chat_id, route, session_key, provider)
        except Exception:
            logger.warning("Telegram: maybe_notice failed session=%s", session_key, exc_info=True)
        try:
            facade.sel().log_api_access(
                caller=f"telegram:{user_id}",
                operation="transport_dispatch.handle",
                outcome="success",
                source="telegram",
                resources=f"session={session_key}",
            )
        except Exception:
            logger.debug("Telegram: success audit failed", exc_info=True)
    except TurnCeilingExceeded as exc:
        # At the conversation's turn ceiling, so no turn opened. Unlike the
        # shutdown branch below this is NOT spooled -- the spool replays a
        # message our restart dropped, and this one was refused on purpose --
        # and it is not charged to the circuit breaker. The notice is
        # rendered so the placeholder finalizes as the pause message rather
        # than a perma-"thinking".
        #
        # Through ``out_renderer``, the same one the driver streams to, NOT
        # the concrete one: a muted conversation substitutes ``SilentRenderer``
        # because the dashboard disconnected it, and posting there would put a
        # message into a chat that is supposed to hear nothing -- once per
        # inbound message, since the latch does not clear on its own.
        logger.warning("Telegram turn ceiling reached for %s -- conversation paused", session_key)
        await turn_ceiling.render_refusal(out_renderer, exc)
    except SessionClosingError:
        # Shutdown began between the claim and the dispatch, so no turn ever
        # opened. Caught ahead of the generic handler so a restart is not
        # charged to the circuit breaker via `record_failure` nor counted as
        # a failed message — neither is true of a session that never
        # misbehaved. `failure_reason` is left as it was, so the `finally`
        # finalizes the placeholder with this channel's usual notice instead
        # of leaving a perma-"🤔 …".
        logger.info(
            "Telegram: aborting dispatch for %s — gateway is shutting down",
            session_key,
        )
        # Durable inbound spool. Written HERE and nowhere else:
        # this is the one point where the payload is still in memory AND the
        # turn is provably unopened, so a replay on the next start cannot
        # double-answer a turn that actually ran. Telegram cannot recover this
        # from its own offset either — ``_persistable_offset`` bounds duplicate
        # replay, not loss, because the next long poll server-confirms the
        # batch it just dispatched.
        #
        # NOT for a restricted session. ``/incognito`` and ``/temporary`` are a
        # promise that this conversation persists nothing, and the spool is a
        # durable file holding the message verbatim. The same predicate that
        # gates the durable-history write gates this one; a refused restricted
        # message degrades to the pre-feature loss, which is what the user asked
        # for by choosing the mode.
        if not await self._session_restricted(session_key):
            await spool_refused_turn(
                channel_type="telegram",
                route=InboundRoute(
                    conversation_id=str(chat_id),
                    # ``msg.text``, NOT the local ``text``: by here the latter
                    # has attachment context appended, whose inlined temp paths
                    # are gone after a restart, and may have had a mid-turn
                    # override prefix stripped. The spool wants what the user
                    # typed.
                    text=msg.text,
                    user_id=str(user_id),
                    thread_id=str(reply_thread) if reply_thread else "",
                    message_id=str(getattr(msg, "message_id", "") or ""),
                    attachments_dropped=len(getattr(msg, "attachments", None) or ()),
                ),
            )
    except Exception as exc:
        logger.exception("Telegram transport_dispatch: error handling message")
        # Permanent, user-actionable failures (e.g. model entitlement)
        # surface their own bounded reason instead of the misleading
        # generic retry text; everything else stays generic (None).
        failure_reason = facade._user_safe_failure_reason(exc)
        if _acquired:
            # A dying runtime reaches this generic handler as one more
            # exception, so without the attribution question every tenant of
            # one process charges its own breaker for a single process event.
            # ``_turn_provider`` is the one THIS turn acquired, never a lookup
            # made while handling the failure.
            await charge_turn_failure(
                self.sessions,
                session_key,
                exc=exc,
                provider=_turn_provider,
                channel_type="telegram",
            )
            facade.Stats().inc_message_failed()
    finally:
        # An approval window the driver never awaited -- the prompt went out
        # and the turn then ended before the decider -- has no wait of its own
        # to close it, so it would outlive this turn with its nonce still
        # armed and authorizing a press.
        #
        # ``_APPROVAL_REGISTRY`` is ``TelegramApprovalDecider`` under a second
        # name. Reservations are class state, so the sweep has to reach the
        # class holding them, and the construction name above is a seam
        # callers and tests substitute to observe the decider a turn builds.
        # Sweeping through that name aims at the substitute: it raises on a
        # plain function, and on a stand-in class it clears an empty registry
        # and leaves the real window armed past the end of its turn.
        _APPROVAL_REGISTRY.discard_session(session_key)
        # A turn that consumed the post-compaction flag but never landed
        # discarded the prompt carrying the re-injected context, and a backend
        # that compacted the session during the turn dropped it; either way
        # the flag is set so the next turn re-injects it.
        rearm_reinjection(
            self.sessions,
            session_key,
            consumed=_needs_reinjection,
            landed=_turn_landed,
            compacted=getattr(driver, "compaction_completed", False) is True,
        )
        rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
        # Always finalize the placeholder (no perma-"🤔 …"), even if
        # get_or_create raised before the semaphore was held. Only release
        # the semaphore if we actually acquired it.
        #
        # ``close()`` is best-effort and must NEVER prevent the three steps
        # after it. A renderer that fails to finalize -- a malformed
        # Telegram response, a socket dropped mid-edit -- would otherwise
        # skip ALL of them: the session semaphore is never given back (and
        # because it is keyed by SESSION, every later message in that
        # conversation blocks forever and the queue never drains), the
        # ``_active_renderers`` entry leaks, and the attachment temp files
        # stay on disk. Discord and the shared pipeline guard this the same
        # way.
        try:
            await out_renderer.close(failure_reason=failure_reason)
        except Exception:
            logger.warning(
                "Telegram: renderer.close failed session=%s",
                session_key,
                exc_info=True,
            )
        self._active_renderers.pop(session_key, None)
        if _acquired:
            self.sessions.release(session_key)
        await asyncio.to_thread(facade.cleanup_attachments, attachment_temp_paths)

    # Now that the turn is released, run anything that queued during it
    # (queue_mode == "queue"). ``drain`` is False for drained turns so the
    # loop stays iterative at one level (no recursion); ``limit`` bounds it.
    #
    # Deliberately handed NOTHING about this turn but its session key: the
    # replay envelope comes from each queued entry's own recorded origin, and
    # under ``dm_scope = "unified"`` the person who opened this turn is not
    # necessarily the person who queued during it.
    if drain:
        await self._drain_queue(session_key)


def _persist_turn(
    self: TelegramDispatcher,
    session_key: str,
    user_text: str,
    reply_text: str,
    is_new: bool,
    agent: str | None = None,
    mirror_mids: tuple[str, str] | None = None,
    auto_title_pending: bool = False,
) -> None:
    """Persist one atomic turn, deduplicating rows already projected live.

    ``auto_title_pending`` says a generated name is on its way for this
    conversation, so the deterministic fallback below is skipped: auto-title's
    guard refuses a record that already carries a name, so writing one here
    would make the first forty characters of the first message the permanent
    name and retain the claim, leaving nothing to retry. The caller decides it
    once and uses the same value for its own dispatch, so the two cannot
    disagree. It defaults to False, so a caller that does not dispatch
    auto-title writes the fallback unconditionally.

    ``privacy_mode.is_restricted`` is this channel's OWN privacy gate and is
    checked here, at the only writer, so it covers the turn, the drained queue
    and the steered continuation alike.

    It is NOT the whole ceiling for a RESUMED dashboard session. That
    predicate reads a process-local tracker which only an inbound CHANNEL
    message populates, so it answers ``False`` for an incognito dashboard slot
    and fails open. That rung is decided by the CALLER, through
    :meth:`_session_restricted`, which skips this call entirely — it needs the
    live slot registry and, failing that, an await on the persisted transcript,
    neither reachable from the worker thread this runs in. A second caller must
    make the same check before calling.
    """
    if self.conv_log is None or privacy_mode.is_restricted(session_key):
        return
    with self.conv_log.atomic_appends(session_key):
        if mirror_mids is not None:
            user_mid, assistant_mid = mirror_mids
            self.conv_log.append_if_absent(
                session_key,
                "user",
                user_text,
                agent=agent,
                mid=user_mid,
            )
            if reply_text:
                self.conv_log.append_if_absent(
                    session_key,
                    "assistant",
                    reply_text,
                    agent=agent,
                    mid=assistant_mid,
                )
        else:
            self.conv_log.append(
                session_key,
                "user",
                user_text,
                agent=agent,
                mid=mint_row_mid(),
            )
            if reply_text:
                self.conv_log.append(
                    session_key,
                    "assistant",
                    reply_text,
                    agent=agent,
                    mid=mint_row_mid(),
                )
        if is_new and not auto_title_pending and not auto_title.is_titled(session_key):
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Telegram"
            self.conv_log.set_title(session_key, title)


async def _maybe_notice(
    self: TelegramDispatcher, chat_id: int, route: tuple[str, str], session_key: str, provider: Any
) -> None:
    """Soft-threshold context warning as a SEPARATE message (not persisted).

    Kept out of the streamed answer buffer so it is never persisted into the
    assistant turn and replayed next turn as though the assistant said it.
    The hard-compaction backstop is the backend autocompactor
    (``session.autocompact_pct``).
    """
    pct = self.sessions.check_context_usage(session_key, provider)
    soft_pct = self._soft_threshold()
    if recycle_backend(provider):
        # Crew restarts this backend's session at the threshold; warn once
        # before it, offering a fresh start instead of a compaction.
        if recycle_warning_should_send(self.sessions, self._conv, route, session_key, pct):
            assert self.client is not None
            await self._reply(chat_id, context_recycle_warning(), thread=self._route_thread(route))
        return
    if pct >= soft_pct and compact_unsupported_backend(provider):
        # Capability gate: the nudge advises /compact, which this
        # backend refuses — it compacts on its own as context fills, so
        # there is nothing for the user to act on.
        return
    if pct >= soft_pct and not self._conv.is_awaiting(route):
        self._conv.set_awaiting(route)
        assert self.client is not None
        await self._reply(
            chat_id,
            "⚠️ Context is getting long. Use /compact to compress or " "/new to start fresh.",
            thread=self._route_thread(route),
        )

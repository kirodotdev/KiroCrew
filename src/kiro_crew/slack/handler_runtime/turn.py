"""One native Slack turn: the state its phases share, and its body.

``_SlackTurn`` is built by ``handle_message`` once it has admitted a message. It carries
the call's inputs and every value one phase hands the next, and it owns the session
permit and the turn's single verdict (``release_permit``, ``book_success``,
``book_failure``). An attribute is declared on the class and set where the turn first
binds the value, so reading one before then raises ``UnboundLocalError``, as reading the
unbound local did; it is a ``NameError``, so a handler of either still catches it. The
declarations are strings: the class body evaluates nothing, so a test that rebinds a
name on the facade still reaches every reader.

``_run_turn`` is the turn body: it opens with read-only aliases of the inputs it reads
and ends by copying onto the turn exactly what ``_deliver_answer`` reads. Its stream loop
hands each tool call and permission request to ``permission_ladder.py``.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _CIRCUIT_BREAKER_THRESHOLD,
        _COMPACTION_FAILED_RETRIES,
        _COMPACTION_RETRY_NOTICE,
        _STATUS_WORKING,
        _TIMEOUT_REPLY,
        _UNFINISHED_STOP_REASONS,
        CONTEXT_EVENT_COMPACTION,
        EVENT_COMPLETE,
        EVENT_PERMISSION_REQUEST,
        EVENT_TEXT_CHUNK,
        EVENT_THINKING_CHUNK,
        EVENT_TOOL_CALL,
        SESSION_NOT_FOUND_GIVE_UP_TEXT,
        SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
        SESSION_NOT_FOUND_RETRY_NOTICE,
        STOP_REASON_CANCELLED,
        STOP_REASON_COMPACTION_FAILED,
        STOP_REASON_END_TURN,
        AcpError,
        AcpProcessDied,
        AcpPromptBusy,
        AcpTimeoutError,
        ContextBuilder,
        ConversationLog,
        CronService,
        DashboardState,
        HistoryConsolidator,
        KiroCrewConfig,
        LLMProvider,
        SessionClosingError,
        SessionManager,
        SlackClientOps,
        StartPriority,
        Stats,
        StatusReactionController,
        SubagentManager,
        Task,
        TaskRunner,
        ThreadReplies,
        TurnCeilingExceeded,
        UnknownMemoryStore,
        _answer_permission_request,
        _AnswerStream,
        _CompactionReplay,
        _get_default_agent,
        _hydrate_conv_flags,
        _hydrate_conv_header,
        _hydrate_thread_overrides,
        _screen_tool_call,
        _thread_agents,
        _thread_context,
        _thread_meta_fallback,
        _TurnScopeReset,
        acp_error_is_session_not_found,
        acp_error_needs_fresh_credentials,
        build_cancelled_turn_preamble,
        build_session_replay,
        build_working_blocks,
        canonical_key,
        consume_reinjection,
        context_template,
        credential_give_up_text,
        enter_turn,
        exit_turn,
        expire_slack_options,
        get_dashboard_state,
        handle_message,
        is_slack_born,
        is_thread_temporary,
        logger,
        note_turn,
        publish_turn_identity,
        rearm_reinjection,
        record_interaction_event,
        redact,
        redact_local_paths,
        resolve_channel_crew,
        rollback_skill_bodies,
        run_in_embed_pool,
        runtime_death,
        session_stop_generation,
        session_store_for_turn,
        stop_reason_landed,
        turn_ceiling,
        window_for_provider_client,
    )


class _SlackTurn:
    """One native Slack turn's inputs and the state its phases hand on; see the module."""

    # ``handle_message``'s inputs. ``text`` is rebound by ``_dispatch_before_turn`` (the
    # privacy modifiers) and by ``_run_turn`` (the cancelled-turn preamble).
    slack: "SlackClientOps"
    sessions: "SessionManager"
    channel: "str"
    text: "str"
    thread_ts: "str | None"
    msg_ts: "str"
    user_id: "str"
    team_id: "str"
    approval_mode: "str"
    context_builder: "ContextBuilder | None"
    cron_service: "CronService | None"
    conversation_log: "ConversationLog | None"
    consolidator: "HistoryConsolidator | None"
    subagent_manager: "SubagentManager | None"
    task_runner: "TaskRunner | None"
    channel_agent: "str | None"
    user_display_name: "str | None"
    action_context: "str | None"
    target_slot_name: "str | None"
    route_pinned: "bool"
    asker_key: "str | None"
    from_trusted_bot: "bool"
    channel_activation: "str | None"
    had_voice_input: "bool"
    spool_text: "str | None"
    spool_attachments: "int"
    _compaction_replay: "_CompactionReplay | None"
    start_priority: "StartPriority"

    # Bound by ``handle_message`` before the first phase.
    _t0: "float"
    reply_ts: "str"
    session_key: "str"

    # Copied by ``_dispatch_before_turn`` (the agent it resolves) and ``_run_turn``: for
    # the permission ladder before the prompt opens, and for ``_deliver_answer`` once
    # the turn body has ended. ``routed_link_key`` is the turn body's
    # ``linked_session_key``, named apart because an attribute of that name is a
    # session's channel link (``test_crew_log_class_recorder`` reads it as one).
    _acquired: "bool"
    _agent: "str | None"
    answer: "_AnswerStream"
    client: "LLMProvider | None"
    _had_error: "bool"
    _replayed: "bool"
    routed_link_key: "str | None"
    _stop_reason: "str"
    task: "Task"
    _turn_completed_ok: "bool"
    _working_ts: "str | None"
    status_ctrl: "StatusReactionController"
    # Set by ``_deliver_answer`` once it has taken down the working indicators, so
    # a later cancel does not clear a review-mode status it chose to keep.
    _indicators_cleared: "bool"
    # Opened by ``_run_turn`` (native Stop's write fence); ``close_turn_scope`` closes it
    # from ``handle_message``'s ``finally``, after delivery.
    _turn_scope: "_TurnScopeReset | None"

    # Set by ``_deliver_answer`` before its first await; ``book_success`` and
    # ``book_failure`` read and set it, so the turn books exactly one verdict.
    _verdict_booked: "bool"

    def __init__(
        self,
        *,
        slack: "SlackClientOps",
        sessions: "SessionManager",
        channel: "str",
        text: "str",
        thread_ts: "str | None",
        msg_ts: "str",
        user_id: "str",
        team_id: "str",
        approval_mode: "str",
        context_builder: "ContextBuilder | None",
        cron_service: "CronService | None",
        conversation_log: "ConversationLog | None",
        consolidator: "HistoryConsolidator | None",
        subagent_manager: "SubagentManager | None",
        task_runner: "TaskRunner | None",
        channel_agent: "str | None",
        user_display_name: "str | None",
        action_context: "str | None",
        target_slot_name: "str | None",
        route_pinned: "bool",
        asker_key: "str | None",
        from_trusted_bot: "bool",
        channel_activation: "str | None",
        had_voice_input: "bool",
        spool_text: "str | None",
        spool_attachments: "int",
        _compaction_replay: "_CompactionReplay | None",
        start_priority: "StartPriority",
        _t0: "float",
        reply_ts: "str",
        session_key: "str",
    ) -> None:
        self.slack = slack
        self.sessions = sessions
        self.channel = channel
        self.text = text
        self.thread_ts = thread_ts
        self.msg_ts = msg_ts
        self.user_id = user_id
        self.team_id = team_id
        self.approval_mode = approval_mode
        self.context_builder = context_builder
        self.cron_service = cron_service
        self.conversation_log = conversation_log
        self.consolidator = consolidator
        self.subagent_manager = subagent_manager
        self.task_runner = task_runner
        self.channel_agent = channel_agent
        self.user_display_name = user_display_name
        self.action_context = action_context
        self.target_slot_name = target_slot_name
        self.route_pinned = route_pinned
        self.asker_key = asker_key
        self.from_trusted_bot = from_trusted_bot
        self.channel_activation = channel_activation
        self.had_voice_input = had_voice_input
        self.spool_text = spool_text
        self.spool_attachments = spool_attachments
        self._compaction_replay = _compaction_replay
        self.start_priority = start_priority
        self._turn_scope = None
        self._t0 = _t0
        self.reply_ts = reply_ts
        self.session_key = session_key
        # Bound up front: ``handle_message``'s cancel arm reads both whichever
        # phase the cancel lands in.
        self._working_ts = None
        self._indicators_cleared = False

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> object:
            # Only reached for an attribute that was never set. A declared one is a
            # value the turn has not bound yet: raise what reading that local raised.
            if name in _SlackTurn.__annotations__:
                raise UnboundLocalError(
                    f"cannot access local variable {name!r} where it is not associated with a value"
                )
            raise AttributeError(name)

    # Release the retained permit once delivery and accounting have run. Called
    # explicitly right after accounting so a queued turn can proceed while the
    # post-answer decorations run, and again from ``_deliver_answer``'s structural
    # ``finally`` so that ANY exit from the delivery/accounting region — a return, or a
    # raise from a Slack send or a task cancellation — still releases. Idempotent:
    # guarded on ``_acquired`` so the second call is a no-op.
    def release_permit(self) -> None:
        if self._acquired:
            self.sessions.release(self.session_key)
            self._acquired = False
            # The gap opened for a replay is held for as long as the permit is,
            # so the deferred-release path closes it here, right after the
            # release, and ``_run_turn``'s ``finally`` closes it on every other exit.
            _close_gap = getattr(self.sessions, "close_replay_gap", None)
            if callable(_close_gap):
                _close_gap(self.session_key)

    # The one place that takes down the "…is working" thread status and the inline
    # Stop block: ``_deliver_answer`` calls it on its exits, ``_run_turn`` on each
    # exit before the stream opens, a compaction replay before its nested turn
    # starts, and ``handle_message``'s cancel arm on a cancel. A no-op once the indicators are cleared or
    # released, so a cancel after delivery adds no second clear. ``keep_status``
    # deletes only the Stop block and leaves the turn owning the status (a review
    # turn keeps it until its draft lands). Each Slack call is guarded on its own,
    # so one failure does not skip the other.
    async def clear_working_indicators(self, *, keep_status: bool = False) -> None:
        if self._indicators_cleared:
            return
        if not keep_status:
            try:
                await self.slack.set_thread_status(self.channel, self.reply_ts, "")
            except Exception:
                logger.debug("Failed to clear the thread status", exc_info=True)
        if self._working_ts:
            try:
                await self.slack.delete_message(self.channel, self._working_ts)
            except Exception:
                logger.debug("Failed to delete the Stop block", exc_info=True)
            self._working_ts = None
        if not keep_status:
            self._indicators_cleared = True

    # Hand the thread status to its new owner: a posted review draft owns
    # "Awaiting review…". A later cancel of this turn then leaves the status alone.
    def release_working_indicators(self) -> None:
        self._indicators_cleared = True

    # Close native Stop's write fence, if ``_run_turn`` opened it. ``handle_message``
    # calls this from its ``finally``, so the fence covers delivery and closes on every
    # exit. Idempotent: the scope is dropped once it is closed.
    def close_turn_scope(self) -> None:
        scope, self._turn_scope = self._turn_scope, None
        if scope is not None:
            exit_turn(scope)

    def book_success(self) -> None:
        if self._verdict_booked:
            return
        self._verdict_booked = True
        self.sessions.record_success(self.session_key)
        # Reset with the counter it substitutes for: record_success clears
        # consecutive_failures, so a completed turn must clear the shared-death
        # streak too. Otherwise the streak is a LIFETIME total and the bound
        # stays permanently tripped, silently ending the exemption.
        runtime_death.clear_shared_deaths(self.session_key)
        Stats().inc_message_success()
        if self.client is not None:
            record_interaction_event(self.client, self.session_key, "slack")

    async def book_failure(self) -> None:
        if self._verdict_booked:
            return
        self._verdict_booked = True
        self._had_error = True
        # The turn body finalized the reaction before delivery; a reply that then
        # failed still owes the user the error reaction.
        self.status_ctrl.finalize(error=True)
        await self.sessions.record_failure(self.session_key)
        Stats().inc_message_failed()


async def _run_turn(turn: _SlackTurn) -> bool:
    """Hold the session and stream the answer: True when ``_deliver_answer`` runs next.

    The turn body of ``handle_message``, from the queued reaction to the ``finally`` that
    releases or keeps the session permit: the Working block and the answer stream, the
    route to the thread's owner and the session claim, the prompt and its context, the
    gates before the prompt opens (a cancelled message, a stopped replay, the turn
    ceiling, a shutdown), the stream loop, the stop reasons, the compaction and lost-
    session replays, and the except arms that book a failed turn. ``False`` means the
    turn ended before an answer was due; a raise propagates as it did.
    """
    slack = turn.slack
    sessions = turn.sessions
    channel = turn.channel
    text = turn.text
    thread_ts = turn.thread_ts
    msg_ts = turn.msg_ts
    user_id = turn.user_id
    team_id = turn.team_id
    approval_mode = turn.approval_mode
    context_builder = turn.context_builder
    cron_service = turn.cron_service
    conversation_log = turn.conversation_log
    consolidator = turn.consolidator
    subagent_manager = turn.subagent_manager
    task_runner = turn.task_runner
    channel_agent = turn.channel_agent
    user_display_name = turn.user_display_name
    action_context = turn.action_context
    target_slot_name = turn.target_slot_name
    route_pinned = turn.route_pinned
    asker_key = turn.asker_key
    from_trusted_bot = turn.from_trusted_bot
    channel_activation = turn.channel_activation
    had_voice_input = turn.had_voice_input
    _compaction_replay = turn._compaction_replay
    start_priority = turn.start_priority
    reply_ts = turn.reply_ts
    session_key = turn.session_key
    _agent = turn._agent

    status_ctrl = StatusReactionController(
        slack,
        channel,
        msg_ts,
        enabled=KiroCrewConfig.load().slack.reactions_enabled,
    )
    _had_error = False
    _stop_reason = ""
    # Whether the stream delivered an EVENT_COMPLETE; ``_stop_reason`` alone
    # cannot say (it is "" both before any completion and for one that carries
    # no reason), and the re-injection bookkeeping needs the difference.
    _completion_observed = False
    # Set at clean model completion; success accounting is booked only after the
    # answer-carrying delivery (``_deliver_answer``) actually posts, so this records "the model
    # finished" separately from "the reader received the answer".
    _turn_completed_ok = False
    _replayed = False  # set when a transient-compaction replay took over this message
    # Set as the last statement of the turn body. A raise that skips it -- a
    # cancellation landing in the post-compaction reset, after the model already
    # completed -- never reaches the delivery region, so the ``finally`` must not
    # defer the release to a ``turn.release_permit`` that will never run.
    _body_completed = False

    # Set assistant thread status while we wait for the LLM to respond.
    # Defer start_stream until the first text chunk arrives so the user
    # sees the status indicator instead of a blank bot message. Where Slack
    # shows that native status (an assistant thread, or an agent session) it is
    # the progress signal; elsewhere the message gets the 👀 ack instead
    # (``StatusReactionController.start``).
    turn._turn_scope = enter_turn(channel, reply_ts)  # native Stop's write fence
    _native_status = await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)
    status_ctrl.start(native_status=bool(_native_status))

    # Post inline stop button (only in threaded conversations to avoid breaking tests)
    _working_ts: str | None = None
    if thread_ts:
        # Same gate the timing footer applies at turn end (threaded, thread not
        # already linked, dashboard present), evaluated NOW so the "Link to
        # Dashboard" control exists for the whole turn rather than appearing
        # only once the turn completes — on a long turn the footer arrives far
        # too late to be the first chance to link. A click imports and links the
        # thread via the existing command path; the next turn then mirrors
        # through the normal pre-committed-link mirror in ``_deliver_answer``.
        _early_link = (
            not sessions.get_session_for_thread(reply_ts) and get_dashboard_state() is not None
        )
        # Best-effort: a failed stop-button post must not drop the user's message.
        try:
            _working_ts = await slack.post_blocks(
                channel,
                build_working_blocks(session_key, include_dashboard_link=_early_link),
                "Working…",
                reply_ts,
            )
        except Exception:
            logger.warning("Failed to post inline stop button", exc_info=True)
    # Published now, not only at the end of this phase: a cancel at any later
    # await skips that copy, and ``handle_message``'s cancel arm reads it here.
    turn._working_ts = _working_ts

    # The Slack wire this turn's answer streams to, and every flag the end of the turn
    # reads off it (``handler_runtime/stream.py``).
    answer = _AnswerStream(
        slack,
        channel,
        reply_ts,
        team_id=team_id,
        user_id=user_id,
        channel_activation=channel_activation,
        show_thinking=KiroCrewConfig.load().slack.show_thinking,
    )

    task = Task(id=msg_ts)
    _acquired = False

    # ── Bidirectional sync: check if this Slack thread is linked to a dashboard session ──
    # The thread index is keyed by the bare Slack thread_ts (reply_ts), NOT the
    # namespaced session key. A self-linked Slack thread resolves to our own
    # canonical key (no-op rewrite); a dashboard-linked thread resolves to its
    # ``dashboard:chat-N`` key.
    # Keep the thread's owner truthful. Three separate decisions
    # below consume it -- whether to re-route this turn, whether to CLAIM the
    # thread, and whether to mirror into a dashboard slot -- and a pinned answer
    # needs a different answer for each. Falsifying this single value to steer all
    # three is what made the pin land wrong three times running.
    thread_owner_key = sessions.get_session_for_thread(reply_ts)
    # Mirror/footer value: a pinned answer belongs to the conversation that ASKED,
    # not to whoever owns the thread now, so it mirrors nowhere. (A pinned asker
    # that *does* hold a slot never reaches here -- maybe_route_linked_thread
    # already delivered the turn into that slot and returned.)
    linked_session_key = None if route_pinned else thread_owner_key
    if route_pinned:
        # A pinned answer names its own conversation, so the thread's CURRENT
        # owner has no say -- rewriting the key here is what let a pinned answer
        # land in whoever took the thread over in the meantime.
        #
        # Suppressing that rewrite is only half of it. A pinned asker that holds no
        # slot -- a cron or native conversation -- would otherwise be left running
        # under the bare Slack thread key, which for a cron asker is a DIFFERENT
        # conversation: the answer would open a new session and take the thread
        # mapping with it. So the asker becomes the session key outright.
        if asker_key:
            session_key = asker_key
            # Same reason as the linked-thread reroute below: overrides are keyed
            # BY SESSION and the hydration at entry ran for the PREVIOUS key, so
            # without this the agent re-resolution reads a key nobody hydrated and
            # falls through to the channel or default agent -- discarding a binding
            # the asker's own metadata records correctly. The pinned path needs it
            # exactly as much: `asker_key` is a different conversation, which is
            # the whole reason it is substituted here.
            await _hydrate_thread_overrides(session_key, conversation_log)

    client: LLMProvider | None = None
    # Post-compaction re-injection bookkeeping for the finally: whether this
    # turn consumed the one-shot flag, whether it landed (recorded success), and
    # whether the backend reported that it compacted the session.
    _needs_reinjection = False
    _turn_landed = False
    _turn_compacted = False
    # This turn's thread-replies read; its watermark moves in the finally.
    _thread_replies: ThreadReplies | None = None

    async def _replay_abandoned(notice: str, replay: _CompactionReplay) -> None:
        """Re-run this message on the successor of the session just reset.

        The caller opened the replay gap and reset the session; this closes the
        gap once the replay has settled. The replay is a nested ``handle_message``
        call with every argument unchanged, which is what makes it Slack's
        own replay: it resolves the same session, keeps the same activation and
        pinning, runs while this task still owns the attachment temp files, and
        needs no queue drain from whoever dispatched the original -- the
        interaction paths dispatch ``handle_message`` without one.
        """
        nonlocal _acquired, _replayed, _needs_reinjection, _turn_compacted, _working_ts
        # This attempt's permit died with the session the reset popped; the
        # successor's belongs to the replay, whose own ``finally`` releases it,
        # so this frame must not release again.
        _acquired = False
        _replayed = True
        # The reset popped the session, so the replay cold-starts a NEW one whose
        # first prompt carries the full session-start context anyway; re-arming
        # the one-shot flag for this abandoned prompt, or for a compaction the
        # popped session reported, would only make the turn after the replay
        # inject it twice.
        _needs_reinjection = False
        _turn_compacted = False
        # This attempt is over: take its ack reaction down now (idempotent, so
        # the ``finally`` re-call is a no-op), and take down what
        # it posted -- the Working block and a reasoning placeholder that a
        # thinking-only attempt left above where the answer would have gone. The
        # nested call posts its own.
        status_ctrl.finalize(error=False)
        for _ts in (_working_ts, answer.thinking_ts):
            if _ts:
                try:
                    await slack.delete_message(channel, _ts)
                except Exception:
                    pass
        _working_ts = None
        turn._working_ts = None
        answer.thinking_ts = None
        # Visible, not persisted: the abandoned attempt records nothing, so the
        # conversation log carries this message exactly once, with the reply the
        # replay produces.
        try:
            await slack.post_message(channel, notice, reply_ts)
        except Exception:
            logger.debug("Failed to post the replay notice", exc_info=True)
        # Clear this attempt's status before the nested turn starts, so a cancel
        # that lands before the nested turn owns a status has nothing left over.
        # From here the nested turn sets and clears its own status, and a cancel
        # that unwinds this frame must not clear it (e.g. its "Awaiting review…"
        # for a draft it already posted): the clear marks this turn's indicators
        # done, so a later clear here is a no-op.
        await turn.clear_working_indicators()
        try:
            await handle_message(
                slack,
                sessions,
                channel,
                _user_text,
                thread_ts,
                msg_ts,
                user_id,
                team_id=team_id,
                approval_mode=approval_mode,
                context_builder=context_builder,
                cron_service=cron_service,
                conversation_log=conversation_log,
                consolidator=consolidator,
                subagent_manager=subagent_manager,
                task_runner=task_runner,
                channel_agent=channel_agent,
                user_display_name=user_display_name,
                action_context=action_context,
                target_slot_name=target_slot_name,
                route_pinned=route_pinned,
                asker_key=asker_key,
                from_trusted_bot=from_trusted_bot,
                channel_activation=channel_activation,
                had_voice_input=had_voice_input,
                spool_text=turn.spool_text,
                spool_attachments=turn.spool_attachments,
                _compaction_replay=replay,
                start_priority=start_priority,
            )
        finally:
            # The replay has settled and released its permit (or never got
            # there); a waiter admitted now finds an idle session.
            sessions.close_replay_gap(session_key)

    try:
        task.start()
        while True:
            candidate_key = session_key
            if not route_pinned:
                thread_owner_key = sessions.get_session_for_thread(reply_ts)
                candidate_key = thread_owner_key or canonical_key(reply_ts)
                if candidate_key != session_key:
                    await _hydrate_thread_overrides(candidate_key, conversation_log)
                    await _hydrate_conv_header(sessions, candidate_key)
                    if sessions.get_session_for_thread(reply_ts) != thread_owner_key:
                        continue
            # Both private identity hydration and store resolution can yield to
            # a link/unlink. Commit the route only after those reads agree with
            # the current owner; a pinned answer always keeps its asker instead.
            memory_error = None
            try:
                _memory_store = await session_store_for_turn(context_builder, candidate_key)
            except UnknownMemoryStore as exc:
                memory_error = exc
            if not route_pinned:
                if sessions.get_session_for_thread(reply_ts) != thread_owner_key:
                    continue
                if candidate_key != session_key:
                    logger.info(
                        "🔗 Slack thread %s linked to dashboard session %s — routing there",
                        session_key,
                        candidate_key,
                    )
                session_key = candidate_key
                linked_session_key = thread_owner_key
                _hydrate_conv_flags(sessions, session_key)
            if memory_error is not None:
                raise memory_error
            break
        # Re-resolve _agent against (possibly linked) session_key for the main
        # LLM path — linked dashboard sessions may carry a different thread agent.
        _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
        # A channel bound to a Crew agent RECORD runs as that record (mode,
        # crew identity, workspace cwd) -- see slack/channel_crew.py. Same rule
        # as the messaging transport: only when the selected agent IS the
        # channel's binding (a thread value equal to it is that binding restored
        # from the transcript header) and the session is Slack's own.
        _channel_crew = (
            await asyncio.to_thread(resolve_channel_crew, channel_agent)
            if channel_agent and _agent == channel_agent and is_slack_born(session_key)
            else None
        )
        # The context builder takes the TEMPLATE the turn runs, never a record's
        # name, including on a dashboard-linked session (``context_template``).
        _context_agent = await asyncio.to_thread(context_template, _agent, _channel_crew)
        _alloc: dict[str, Any] = (
            _channel_crew.allocation_kwargs() if _channel_crew else {"agent": _agent}
        )
        client, is_new, resumed = await sessions.get_or_create(
            session_key, channel_id=channel, start_priority=start_priority, **_alloc
        )
        _acquired = True
        if _compaction_replay is not None:
            # The gap the outer attempt opened stays open until this replay has
            # settled and released its permit (the outer's ``finally`` closes it):
            # a message admitted now would park on this session's semaphore,
            # which a further retry's reset would pop from under it.
            _stop_gen_at_entry = _compaction_replay.stop_gen_at_entry
        else:
            # The user's Stop count for this key at turn start; a replay of
            # this message re-reads it before opening its prompt, so a Stop
            # issued anywhere in between -- on any surface -- keeps the
            # abandoned message dropped.
            _stop_gen_at_entry = session_stop_generation(sessions, session_key)
        # Expire AGAIN now the turn is serialized — see the same call in
        # transport_dispatch. The pass in ``_dispatch_before_turn`` runs before
        # `get_or_create` waits its turn, so two messages arriving together both
        # clear the OLD control and neither clears the NEW one the first turn
        # posts on its way out, leaving live buttons for a superseded question.
        await expire_slack_options(
            cast("DashboardState | None", get_dashboard_state()),
            sessions.get_session_for_thread(reply_ts) or session_key,
        )
        if is_new:
            await sessions.set_channel(session_key, channel)
        if thread_owner_key is None and not route_pinned:
            # Self-link: thread index maps the bare Slack thread_ts to this
            # session's canonical key. reply_ts (not session_key) is the true
            # Slack timestamp — storing the namespaced key as slack_thread_ts
            # would corrupt reply routing.
            #
            # A PINNED answer never claims the thread, however empty the index
            # looks. Pinning exists so an accepted click cannot mutate thread
            # routing: a cron or native asker claiming the thread here would
            # evict its real owner, and every later human reply would land in
            # the cron conversation instead.
            sessions.set_slack_link(session_key, reply_ts, channel)
        logger.info(
            "🔍 session state: key=%s is_new=%s resumed=%s",
            session_key,
            is_new,
            resumed,
        )

        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
        await publish_turn_identity(sessions, session_key)

        # Build message with context injection
        compressed: str | None = None
        # Scale the injected-context budget to the live model's context window
        # (200K model ⇒ one-fifth the memory/lessons/history chars of a 1M
        # model, same window share). Derived from the resolved session client;
        # Auto/unknown ⇒ None ⇒ the 1M reference (unchanged default).
        _model_window = window_for_provider_client(client)
        # is_new = new kiro-cli/dashboard process, NOT new conversation.
        # The Slack thread persists across processes, so we replay its history
        # to bootstrap the fresh session. Same lossless tail-first replay the
        # dashboard uses: a process death is not a context overflow, so code
        # and tool output must come back verbatim, not as an LLM summary.
        if is_new and not resumed and context_builder and context_builder.conversation_log:
            compressed = await asyncio.to_thread(
                build_session_replay,
                context_builder.conversation_log,
                session_key,
                model_window=_model_window,
            )

        # The user's message as it arrived. The block below may fold a
        # cancelled-turn preamble into ``text`` for the model; a
        # transient-compaction replay must re-run THIS value, so the nested call
        # derives its own preamble (its own gate, its own one-shot flag) and
        # persists what the user actually typed, not a preamble a previous
        # attempt prepended.
        _user_text: str = text
        # After a soft-cancel, kiro-cli drops the cancelled turn from its
        # conversation log — but the user+assistant text is persisted to our
        # local conversation_log. Re-inject just the cancelled turn as a
        # preamble so the LLM remembers what was interrupted. Flag lives on
        # the session (set by SessionManager.stop_turn), consumed one-shot.
        # Use getattr for prev_turn_cancelled so test doubles (AsyncMock)
        # don't raise AttributeError on coroutine-returning mock chains.
        _user_text_range = (0, len(text))
        _session = getattr(sessions, "_sessions", {}).get(session_key)
        if (
            _session is not None
            and getattr(_session, "prev_turn_cancelled", False)
            and context_builder
            and context_builder.conversation_log
        ):
            _session.prev_turn_cancelled = False
            _preamble = await asyncio.to_thread(
                build_cancelled_turn_preamble, context_builder.conversation_log, session_key
            )
            if _preamble:
                offset = len(_preamble) + 2
                _user_text_range = (offset, offset + len(text))
                text = _preamble + "\n\n" + text

        # The thread's first message for a fresh session, and the replies since this
        # conversation's last turn (``handler_runtime/turn_context.py``).
        thread_parent_text, _thread_replies = await _thread_context(
            slack,
            channel,
            thread_ts,
            msg_ts,
            session_key,
            is_new=is_new,
            resumed=resumed,
            compressed=compressed,
            context_builder=context_builder,
            conversation_log=conversation_log,
            agent=_agent,
        )

        if context_builder:
            # Thread-scoped temporary mode: blocks memory reads.
            _slack_blocks_reads = is_thread_temporary(session_key)

            _thread_meta = await _thread_meta_fallback(
                slack,
                channel,
                thread_ts,
                is_new=is_new,
                resumed=resumed,
                thread_parent_text=thread_parent_text,
                compressed=compressed,
                context_builder=context_builder,
            )

            # This conversation's own silo, resolved from the session's RECORDED
            # binding and never from ``_agent`` -- on Slack that value is a kiro
            # agent name, a namespace disjoint from ``cfg.agents``, so deriving a
            # store from it answers ``default`` for exactly the crew that
            # configured otherwise. A thread taken over from a crew-bound
            # dashboard session carries that crew's key here, which is what stops
            # the takeover from reading the operator's own memory instead.
            #
            # The private tier was prepared before provider acquisition. Missing
            # or unreadable member memory refuses the turn with its own error.
            # A compaction drops session-start context. Read-and-clear the
            # one-shot flag so this turn re-injects that context exactly once;
            # the finally re-arms it if this turn never lands.
            _needs_reinjection = consume_reinjection(sessions, session_key)
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                context_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel,
                thread_ts=thread_ts or msg_ts,
                agent=_context_agent,
                memory_store=_memory_store,
                resumed=resumed,
                needs_reinjection=_needs_reinjection,
                user_display_name=user_display_name,
                compressed_history=compressed,
                action_context=action_context,
                thread_parent_text=thread_parent_text,
                thread_meta=_thread_meta,
                thread_replies_text=_thread_replies.text if _thread_replies else None,
                blocks_reads=_slack_blocks_reads,
                model_window=_model_window,
                runtime_source="slack",
                user_text_range=_user_text_range,
                context_provider=client,
            )
        else:
            full_message = text

        # ── Early cancellation check: bail before expensive LLM call ──
        if sessions.is_cancelled(session_key, msg_ts):
            logger.info("Message %s cancelled before LLM call — skipping", msg_ts)
            await turn.clear_working_indicators()
            return False
        # A replay must not run a message the user has stopped since its first
        # attempt began. Same shape and same placement as the check above: the
        # last look before the prompt opens.
        if (
            _compaction_replay is not None
            and session_stop_generation(sessions, session_key) != _stop_gen_at_entry
        ):
            logger.info("Message %s stopped before its compaction replay — skipping", msg_ts)
            await turn.clear_working_indicators()
            return False

        # The permission ladder reads the routed conversation, its agent and this
        # turn's presentation off the turn (``_answer_permission_request``); none of
        # them is rebound past this point.
        turn.session_key = session_key
        turn._agent = _agent
        turn.answer = answer
        turn.task = task

        # Lease-dispatch race gate: the session lease was taken by
        # get_or_create above, but the turn only opens on the first stream
        # iteration below. If a gateway restart moved the SessionManager into the
        # closing state during the async prep between, dispatching now would open
        # a turn ABSENT from the shutdown drain snapshot → killed mid-turn with
        # its native lock held (empty-response bug). Re-check SYNCHRONOUSLY here
        # (no await between this check and the async-for) so the _closing read
        # and the stream's turn registration are one atomic span, strictly
        # ordered w.r.t. close_all's _closing set. Abort if closing (the outer
        # finally releases the lease).
        try:
            turn_ceiling.gate(session_key, lambda: sessions.begin_turn(session_key))()
        except TurnCeilingExceeded as exc:
            # At the conversation's turn ceiling, so no turn opened. This route
            # streams without a TurnDriver, so the notice is posted directly
            # rather than rendered; without it the pause would be the same
            # silence the per-message echo guard already leaves.
            #
            # The "Working…" block is deleted on the way out, as on every early
            # exit here: it carries a live Stop button for a turn that never
            # opened, and this route returns before the normal path that would
            # clean it up. The latch does not clear on its own, so leaving it
            # would strand one per refused message.
            logger.warning("Slack turn ceiling reached for %s -- conversation paused", session_key)
            if exc.announce:
                try:
                    await slack.post_message(channel, str(exc), reply_ts or None)
                except Exception:
                    logger.debug("turn-ceiling notice post failed", exc_info=True)
            await turn.clear_working_indicators()
            return False
        except SessionClosingError:
            logger.info("Aborting Slack dispatch for %s — gateway shutting down", session_key)
            await turn.clear_working_indicators()
            return False

        async for event in client.stream(full_message):
            if event.kind == EVENT_TEXT_CHUNK:
                await answer.on_text(event)

            elif event.kind == EVENT_THINKING_CHUNK:
                await answer.on_thinking(event)

            elif event.kind == EVENT_TOOL_CALL:
                await _screen_tool_call(turn, event)

            elif event.kind == EVENT_PERMISSION_REQUEST:
                if await _answer_permission_request(turn, client, event):
                    break

            elif event.kind == CONTEXT_EVENT_COMPACTION:
                # The backend compacted the session on its own: the session-start
                # context is gone from its window, and the finally arms the flag.
                if event.text == "completed":
                    _turn_compacted = True

            elif event.kind == EVENT_COMPLETE:
                # The turn ended: text carried across a final ``wait`` is answer.
                answer.take_carried()
                _stop_reason = event.stop_reason
                _completion_observed = True
                if (
                    _stop_reason
                    and _stop_reason != STOP_REASON_END_TURN
                    and _stop_reason != STOP_REASON_CANCELLED
                    # Expected terminal state after a failed auto-compaction;
                    # handled below with a session reset, so not "unexpected".
                    and _stop_reason != STOP_REASON_COMPACTION_FAILED
                    and _stop_reason not in _UNFINISHED_STOP_REASONS
                ):
                    logger.warning(
                        "Unexpected stop_reason %r for %s — treating as normal completion",
                        _stop_reason,
                        session_key,
                    )
                break

        if _stop_reason == STOP_REASON_CANCELLED:
            logger.info("Turn cancelled by user for %s", session_key)
            task.complete()
        elif _stop_reason in _UNFINISHED_STOP_REASONS:
            # Booked like a raised AcpTimeoutError (failed task, a failure
            # toward the breaker), with the partial text kept and the timeout
            # notice after it, so the thread does not read a half answer as the
            # reply. The reset replaces the silent runtime and keeps the
            # conversation, as the COMPACTION_FAILED branch below does.
            logger.warning("Turn for %s ended unfinished (%s)", session_key, _stop_reason)
            _had_error = True
            answer.accumulated = (
                f"{answer.accumulated}\n\n{_TIMEOUT_REPLY}"
                if answer.accumulated
                else _TIMEOUT_REPLY
            )
            task.fail("timeout")
            # At its threshold the breaker resets the session inside
            # record_failure and answers True. A same-key message can claim a
            # successor while that reset is awaited, so a second reset here would
            # end the successor mid-turn: reset only when it did not.
            _breaker_reset = await sessions.record_failure(session_key)
            Stats().inc_timeout()
            Stats().inc_message_failed()
            if _breaker_reset is not True:
                try:
                    await sessions.reset(session_key)
                except Exception:
                    logger.debug(
                        "Failed to reset session %s after an unfinished turn",
                        session_key,
                        exc_info=True,
                    )
        else:
            task.complete()
            # Success accounting is deferred to after the answer-carrying delivery
            # in ``_deliver_answer`` (past the ``finally``), not recorded here. On the no-stream
            # paths the answer is not sent yet at this point, so booking a success
            # here would credit a turn whose only message can still fail to post.
            # This flag marks a clean model completion; ``_deliver_answer``
            # decides success vs failure once the answer is actually out.
            _turn_completed_ok = True
            # Re-injection is restored only when the observed completion did not
            # land. Account success separately after confirmed answer delivery.
            _turn_landed = stop_reason_landed(
                (_stop_reason or "") if _completion_observed else None
            )

        if _stop_reason == STOP_REASON_COMPACTION_FAILED:
            # The completion was synthetic — the backend abandoned the turn
            # after a failed auto-compaction and never sent end_turn, so it
            # still counts the prompt as in progress. Reset now (mirrors the
            # dashboard runner's needs_session_reset) or the NEXT message
            # collides with "prompt already in progress" and burns the busy
            # recovery path. The context-usage probe is skipped — compaction
            # just failed and the session was torn down.
            #
            # Opened BEFORE the reset pops the session: from that pop until the
            # replay acquires its successor, a Stop would find no session and
            # go unrecorded -- the window the replay's pre-prompt check exists
            # for -- and a newer message for this key would claim the successor
            # first and run ahead of the replay; the open gap makes any other
            # task's claim wait. Closed when the replay has settled (the
            # ``finally`` around the nested call), or right below when no
            # replay is attempted.
            sessions.open_replay_gap(session_key)
            _reset_ok = True
            try:
                await sessions.reset(session_key)
            except Exception:
                _reset_ok = False
                logger.debug(
                    "Failed to reset session %s after compaction failure",
                    session_key,
                    exc_info=True,
                )
            # Whether the abandoned message is replayed depends on WHY
            # compaction failed, which is the verdict the ACP layer records. A
            # compaction that overflowed the window fails again identically, so
            # replaying it only burns the budget — the case the unconditional
            # give-up was written for. A throttled or 5xx'd summarization call
            # has nothing wrong with it, and dropping the message for it ends
            # the turn on a backend hiccup the next attempt would clear.
            _attempt = _compaction_replay.attempt if _compaction_replay is not None else 0
            if (
                _reset_ok
                # Compared against True rather than read for truthiness: the
                # retry must require a real verdict, so a provider that never
                # set the attribute (or exposes an auto-created stand-in for
                # it) cannot be read as "transient" by accident.
                and getattr(client, "last_compaction_transient", False) is True
                # Verbatim replay is only safe before anything landed in the
                # thread — text or a tool card. Once output or a tool call has
                # landed, re-sending could repeat a side effect, so an emitted
                # turn keeps the give-up behaviour.
                and not answer.accumulated
                and answer.task_counter == 0
                and _attempt < _COMPACTION_FAILED_RETRIES
            ):
                logger.info(
                    "Transient compaction failure in %s (attempt %d/%d) — "
                    "replaying the abandoned message",
                    session_key,
                    _attempt + 1,
                    _COMPACTION_FAILED_RETRIES,
                )
                await _replay_abandoned(
                    _COMPACTION_RETRY_NOTICE,
                    _CompactionReplay(
                        attempt=_attempt + 1,
                        stop_gen_at_entry=_stop_gen_at_entry,
                        session_reloaded=(
                            _compaction_replay is not None and _compaction_replay.session_reloaded
                        ),
                    ),
                )
            else:
                sessions.close_replay_gap(session_key)
        elif _stop_reason not in _UNFINISHED_STOP_REASONS:
            # Check context usage — fires background compaction at configured
            # threshold, never blocks. This runs AFTER ``_turn_completed_ok`` is
            # set (the model already finished cleanly), so a raise here must NOT
            # reach the turn-failure ``except`` chain below: that chain books a
            # raw ``record_failure`` while ``_turn_completed_ok`` stays True, so
            # the delivery-verdict block would then book a SECOND time (the
            # ``_verdict_booked = not _turn_completed_ok`` guard is defeated) —
            # double-booking the turn on an ordinary transient probe error. The
            # probe is advisory, so swallow its failure here and let the completed
            # turn proceed to its single delivery-time verdict.
            try:
                sessions.check_context_usage(session_key, client)
            except Exception:
                logger.warning(
                    "check_context_usage failed for %s — skipping (advisory)",
                    session_key,
                    exc_info=True,
                )
        _body_completed = True

    except AcpTimeoutError as e:
        _had_error = True
        # The per-process client raises at its prompt deadline and leaves its
        # process running, so this ending is handled like the stop-reason one:
        # the text that streamed (or the partial output the error carries) is
        # kept with the notice after it, and the session this turn held is reset.
        _partial = e.partial_output or answer.accumulated
        answer.accumulated = f"{_partial}\n\n{_TIMEOUT_REPLY}" if _partial else _TIMEOUT_REPLY
        task.fail("timeout")
        # As in the unfinished-turn branch: the breaker that trips here has reset
        # the session already, and a second reset could end a successor.
        _breaker_reset = await sessions.record_failure(session_key)
        Stats().inc_timeout()
        Stats().inc_message_failed()
        if _acquired and _breaker_reset is not True:
            try:
                await sessions.reset(session_key)
            except Exception:
                logger.debug(
                    "Failed to reset session %s after a timed-out turn", session_key, exc_info=True
                )
    except AcpProcessDied:
        _had_error = True
        answer.accumulated = answer.accumulated or "💀 Agent process died. Please try again."
        task.fail("process_died")
        # The circuit breaker counts a session's OWN consecutive failures, and
        # trips into a reset. A process this session was sharing dying is not
        # this session's failure, and counting it there is how N co-tenants each
        # marched their own breaker toward tripping over one process event. The
        # death was classified once where it was detected; this reads that record.
        # A single-tenant runtime is charged exactly as before.
        if runtime_death.caused_by_this_session(client):
            await sessions.record_failure(session_key)
        else:
            # Bounded, like every other exemption: the breaker is what resets a
            # session whose runtime keeps dying, so an unbounded skip would leave
            # a session on a permanently dying shared process never recovering.
            # The streak is counted against that runtime rather than the session.
            #
            # At the limit the substitute bound PERFORMS the actuator rather than
            # adding one charge to the counter it stood in for. Charging instead
            # would deliver twice the bound it claims: the exemption spends the
            # first `_CIRCUIT_BREAKER_THRESHOLD` deaths, and a counter still at
            # zero then needs that many charges again, so a session on a
            # permanently dying shared runtime would lose about twice as many
            # turns as one that was never exempted. `record_failure` trips into
            # exactly this reset, so calling it here is the same recovery at the
            # limit the breaker would have reached -- and it leaves the session's
            # own failure count untouched, which is the whole point: the session
            # never misbehaved.
            _shared_streak = runtime_death.note_shared_death(session_key)
            if _shared_streak >= _CIRCUIT_BREAKER_THRESHOLD:
                logger.warning(
                    "session %s: the runtime it shares has died %d times running — "
                    "resetting it now, the same recovery the breaker performs",
                    session_key,
                    _shared_streak,
                )
                try:
                    await sessions.reset(session_key)
                    # The reset IS the hand-over, so the streak is spent: clear it
                    # or the next shared death hands over again and every death
                    # from here on performs the actuator, which is the unexempted
                    # behaviour the bound exists to replace. Cleared only once the
                    # reset has returned -- a reset that raised transferred
                    # nothing, and keeping the streak is what makes the next death
                    # retry it.
                    runtime_death.clear_shared_deaths(session_key)
                except Exception:
                    logger.warning(
                        "session %s: reset after a shared runtime's deaths failed",
                        session_key,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "session %s lost a turn to a SHARED runtime's death (%d running) — "
                    "not counting it toward the circuit breaker",
                    session_key,
                    _shared_streak,
                )
        Stats().inc_message_failed()
    except AcpPromptBusy as e:
        _had_error = True
        # Session is wedged mid-prompt — reset the provider so the next
        # message cold-starts cleanly instead of hitting the same wall.
        try:
            await sessions.reset(session_key)
        except Exception:
            logger.debug("Failed to reset session %s after prompt-busy", session_key, exc_info=True)
        answer.accumulated = f"❌ {e}"
        task.fail(str(e))
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except AcpError as e:
        _had_error = True
        # Only once this attempt holds a claim: the binding to reset is the one
        # it claimed, and the Stop count the replay checks was read with it.
        _session_lost = acp_error_is_session_not_found(e) and _acquired
        _reset_ok = False
        if _session_lost:
            # A live backend holds no session under the mapped id, so every later
            # message on this binding would draw the same answer. The reset keeps
            # the session-map entry: the next claim spawns a fresh runtime that
            # session/loads the SAME id. The gap is opened first for the same
            # reason as the compaction replay above.
            sessions.open_replay_gap(session_key)
            _reset_ok = True
            try:
                await sessions.reset(session_key)
            except Exception:
                _reset_ok = False
                logger.debug(
                    "Failed to reset session %s after the backend lost it",
                    session_key,
                    exc_info=True,
                )
        _already_reloaded = _compaction_replay is not None and _compaction_replay.session_reloaded
        if (
            _session_lost
            and _reset_ok
            and not _already_reloaded
            # Verbatim replay only before anything landed in the thread, the
            # same bar the compaction replay holds: a tool may have run.
            and not answer.accumulated
            and answer.task_counter == 0
        ):
            logger.warning(
                "Backend lost the session for %s — re-loading it and replaying the message once",
                session_key,
            )
            _had_error = False
            task.complete()
            await _replay_abandoned(
                SESSION_NOT_FOUND_RETRY_NOTICE,
                _CompactionReplay(
                    attempt=_compaction_replay.attempt if _compaction_replay is not None else 0,
                    stop_gen_at_entry=_stop_gen_at_entry,
                    session_reloaded=True,
                ),
            )
        else:
            if _session_lost:
                sessions.close_replay_gap(session_key)
            if _session_lost and (answer.accumulated or answer.task_counter):
                answer.accumulated = f"⟳ {SESSION_NOT_FOUND_NOT_REPLAYED_TEXT}"
            elif _session_lost and acp_error_needs_fresh_credentials(e):
                # A broken helper fails the fresh process too: a new thread
                # would fail alike, so the provider's remedy is the message.
                answer.accumulated = (
                    f"❌ {credential_give_up_text(e)} Then send your message again."
                )
            elif _session_lost:
                answer.accumulated = (
                    f"❌ {SESSION_NOT_FOUND_GIVE_UP_TEXT} Send your message again, "
                    "or start a new thread if this keeps happening."
                )
            else:
                answer.accumulated = f"❌ {e}"
            task.fail(str(e))
            await sessions.record_failure(session_key)
            Stats().inc_message_failed()
    except UnknownMemoryStore as exc:
        _had_error = True
        answer.accumulated = redact_local_paths(redact(str(exc)))[0][:1000]
        task.fail("memory_unavailable")
        Stats().inc_message_failed()
    except Exception:
        _had_error = True
        logger.exception("Unexpected error handling message")
        answer.accumulated = answer.accumulated or "🔧 Something went wrong. Please try again."
        task.fail("unexpected")
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    finally:
        # A turn that consumed the post-compaction flag but never landed (an
        # error arm, a cancel) discarded the prompt carrying the re-injected
        # context, and a backend that compacted the session during the turn
        # dropped it; either way the flag is set so the next turn re-injects it.
        rearm_reinjection(
            sessions,
            session_key,
            consumed=_needs_reinjection,
            landed=_turn_landed,
            compacted=_turn_compacted,
        )
        # The replies watermark moves only past a turn that landed after a good
        # read; a cancelled or failed turn discarded the prompt that carried them.
        if _turn_landed and _thread_replies is not None and _thread_replies.read_ok:
            note_turn(session_key, thread_ts or msg_ts, msg_ts)
        rollback_skill_bodies(context_builder, session_key, landed=_turn_landed)
        # The permit is held past this ``finally`` when the turn reached a clean
        # model completion, because success/failure accounting is booked only
        # after ``_deliver_answer``'s answer-carrying delivery and mutates per-session breaker
        # state (``consecutive_failures``). Releasing here would open a window in
        # which the next queued turn for the same folded key acquires the permit
        # and mutates that same state while this turn is still deciding its own
        # verdict, corrupting the breaker. On every error path accounting already
        # ran inside the ``except`` blocks above, so the permit is released now.
        # A raise that left the body after the model completed (``_body_completed``
        # still False) is released now too: ``_deliver_answer`` never runs.
        _release_deferred = _acquired and _turn_completed_ok and _body_completed
        if _acquired and not _release_deferred:
            sessions.release(session_key)
            _acquired = False
        # A replay gap this attempt opened must not outlive it: a cancellation
        # landing in the reset (``!stop`` cancels the handler task) skips every
        # close inside the try, and a gap left open makes every later claim on
        # this key wait forever. Idempotent, and ordered after the release so a
        # waiter admitted now finds an idle session; when the release is
        # deferred past this ``finally`` the gap is deferred with it and
        # ``turn.release_permit`` closes both. Probed with ``getattr`` because this
        # line runs on EVERY turn, including the many focused session-manager
        # doubles across the suite that predate the method.
        if not _release_deferred:
            _close_gap = getattr(sessions, "close_replay_gap", None)
            if callable(_close_gap):
                _close_gap(session_key)
        status_ctrl.finalize(error=_had_error)

    # What ``_deliver_answer`` reads off the turn, as this phase left it.
    turn.status_ctrl = status_ctrl
    turn._acquired = _acquired
    turn._agent = _agent
    turn.answer = answer
    turn.client = client
    turn._had_error = _had_error
    turn.routed_link_key = linked_session_key
    turn._replayed = _replayed
    turn.session_key = session_key
    turn._stop_reason = _stop_reason
    turn.text = text
    turn._turn_completed_ok = _turn_completed_ok
    turn._working_ts = _working_ts
    return True

"""Deliver a sub-agent completion into its parent.

The dashboard tab that shows the parent (a turn of its own, a queued card, or the
bell once the tab has closed), a channel or session parent with no tab (its ACP
session takes the turn and only the reply reaches the conversation), a cron
parent, and the bell. A queued or dispatched hand-off owes its delivery marks to
the turn that consumes it.

Orchestrator fields read: ``_cfg``, ``_cron_injecting``, ``_owner_id``, ``conv_log``,
``ctx_builder``, ``dashboard_state``, ``sessions``, ``slack``, ``subagent_mgr``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from kiro_crew.slack.gateway import (
        INJECTION_TIMEOUT,
        SLACK_NAMESPACE,
        SUBAGENT_COMPLETION_KIND,
        SUBAGENT_COMPLETION_META_KEY,
        Any,
        GatewayOrchestrator,
        SubagentDelivery,
        SubagentInfo,
        _arm_queued_delivery_settlement,
        _get_agent_for_session,
        _run_chat,
        _subagent_queued_count,
        bounded_chat_turn,
        build_options_blocks,
        build_timing_footer,
        channel_namespace_of,
        extract_options,
        is_legacy_slack_key,
        is_thread_incognito,
        is_thread_temporary,
        logger,
        mint_options_token,
        redact_credentials,
        redact_exfiltration_urls,
        render_for_slack,
        run_in_embed_pool,
        save_conversation_turn_off_loop,
        session_store_for_turn,
    )


def _injection_slot_busy(slot: Any) -> bool:
    """True when *slot* already owns a turn a new injection must wait behind.

    ``slot.running`` alone is not enough. A just-dispatched injection parks in
    ``bounded_chat_turn``'s off-loop timeout resolution before ``_run_chat``
    starts, and only the live ``slot.task`` — assigned synchronously at
    dispatch — records that claim. A slot whose ``running`` is not derived
    from ``task`` (test doubles, duck-typed slots) reads such a window as
    idle, so a later digest chunk takes the idle branch: it appends in
    whichever order the dispatch hops resolve (not FIFO under CPU load) and
    assigns ``slot.task`` over the earlier chunk's still-pending task instead
    of awaiting it. Consulting the claim directly keeps chunk delivery FIFO
    regardless of how ``running`` is implemented or when the hop resolves.
    """
    task = slot.task
    return bool(slot.running) or (task is not None and not task.done())


# Max attempts, the first one included, to inject a subagent result into a
# parent session.
_MAX_INJECT_ATTEMPTS = 2


def _defer_queued_delivery(
    slot: Any, announce: str, info: SubagentInfo, *, flush_only: bool
) -> None:
    """Owe a queued completion's delivery tombstones to the queue drain.

    The retention window for ``result.txt`` (``agent.subagent_result_ttl_secs``)
    exists so the parent can read the full transcript AFTER the completion
    event reaches it. Writing the ``delivered`` tombstone when the announce is
    merely QUEUED starts that clock while the event is still waiting for a
    turn, so a long-running turn ahead of it lets the reaper prune every file
    the queued announce points at.

    So the ids are handed to the slot keyed on the announce ITSELF,
    ``_delivery_queued`` tells the run loop to skip its own ``mark_delivered``,
    and the drain (``chat_runner._start_next_queued_turn``) settles them once a
    turn has consumed that announce -- including a retry, because a failure
    before the model consumed the prompt re-queues the same text under a newly
    minted queue id, which a debt keyed on the original id could never match. A
    wave digest carries its held members' snapshots too:
    ``_digest_settle_deliveries`` is
    transferred (not copied), so the run loop's ``_settle_digest_holds`` becomes
    a no-op rather than a second writer.

    Best-effort in one direction only: if the slot cannot take the ids (a
    stubbed slot in tests), nothing is transferred and the previous
    immediate-tombstone behaviour stands — better a short window than a
    folder no one ever tombstones.
    """
    # A flush-only record is synthetic (no run, no folder of its own). Only a
    # COMPLETED member owes a delivered mark: ``info.outcome`` is the codebase's
    # canonical three-way classification precisely because the ``error``-
    # nullability idiom reports a user-stopped agent as completed, and a
    # stopped or failed run already carries its own tombstone whose 7-day
    # post-mortem window a "delivered" write would shorten to the result TTL.
    # A memory-wait expiry has no folder either, but the store owes its report
    # until the announce is consumed, so its debt is carried the same way.
    owed: list[SubagentDelivery] = []
    if not flush_only and info.outcome == "completed":
        owed.append(SubagentDelivery(info.id, info.elapsed, info.credits))
    elif not flush_only and getattr(info, "_report_owed", False) is True:
        owed.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
    held = getattr(info, "_digest_settle_deliveries", None)
    if isinstance(held, list):
        owed.extend(held)
    try:
        slot.note_pending_subagent_delivery(announce, owed)
    except Exception:
        logger.debug("Subagent %s: could not defer queued delivery marks", info.id, exc_info=True)
        return
    info._delivery_queued = True
    if isinstance(held, list):
        info._digest_settle_deliveries = []


async def _route_subagent_to_tab(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    sub_meta: dict[str, Any],
    _flush_only: bool,
    _injection_slot: Any,
    _injection_slot_name: str,
    parent_key: str,
) -> None:
    """Deliver a completion into the dashboard tab that shows its parent.

    An idle tab gets a ``_run_chat`` turn and a busy one a queued card; a result
    the blocking spawn tool already returned is skipped; a tab that closed gets the
    bell. Every exit hands the completion off or settles it.
    """
    assert self.dashboard_state is not None
    _slot_name = _injection_slot_name
    # Route the result through _run_chat for full streaming, tool
    # call visibility, and proper lifecycle. A channel-born tab
    # runs on the channel's own session, so the turn's mirror
    # carries the reply back to the thread — the raw-injection path
    # below is for parents with no tab to stream into.
    # Redact LLM-generated output before any external surface
    announce, _ = redact_exfiltration_urls(announce)
    announce, _ = redact_credentials(announce)
    body, _ = redact_exfiltration_urls(body)
    body, _ = redact_credentials(body)

    if _injection_slot:

        # ── Fix 2 (B1): arm a one-shot post-fan-out synthesis turn ──
        # When this is the LAST outstanding sub-agent for the parent,
        # flag the slot so that once every completion has been
        # processed and the queue drains, _run_chat fires ONE
        # dedicated synthesis turn (see chat_runner drain/idle branch).
        # The fire gate drops it when one completion turn carried the
        # whole batch (slot._synthesis_completion_turns == 1).
        # Ordering guarantees running_agents_for == [] here on the last
        # agent (info.done set + _running_count decremented first).
        # IN MEMORY ONLY, cheapest first, and with no await: the arm
        # sits on the delivery path, so a store read here would queue
        # every completion behind the writer, and an await would let
        # the tab close or a sibling register under it. The store
        # half (a sibling the gate still holds) is the fire gate's
        # (chat_utils.synthesis_fire_verdict), which refuses while
        # anything is queued; the other completions re-arm.
        try:
            _mgr = self.subagent_mgr
            _arm_synthesis = (
                _mgr is not None
                and not _injection_slot._pending_synthesis
                and not _flush_only
                and info.id not in _injection_slot._subagents_inline_collected
                and _mgr.running_agents_for(parent_key) == []
                and not _mgr.has_in_memory_pending_work_for(parent_key, exclude_id=info.id)
            )
        except Exception:
            _arm_synthesis = False  # error → don't arm (fail safe)
        if _arm_synthesis:
            _injection_slot._pending_synthesis = True

        # ── Skip injection for blocking-tool-collected results ──
        # spawn_sub_agents (blocking MCP tool) already delivered
        # this result inline as a tool-call return value. Injecting
        # it again would trigger a redundant _run_chat turn whose
        # assistant response shadows any [OPTIONS:] buttons from the
        # synthesis message. Mark delivered and return.
        # NOTE: This check is placed BEFORE the inflight counter and
        # busy-wait because at this point the blocking tool's
        # mark-collected POST has already landed (the tool returns
        # before its turn ends, and _subagent_done fires only after
        # the agent's terminal report, which is after the tool has
        # finished). However, if the slot is busy (turn still
        # running) we must wait first, then re-check — see the
        # second check after the busy-wait below.
        if info.id in _injection_slot._subagents_inline_collected:
            _injection_slot._subagents_inline_collected.discard(info.id)
            # Disarm synthesis — the blocking tool already delivered
            # all results and the model synthesized inline.
            if not _injection_slot._subagents_inline_collected:
                _injection_slot._pending_synthesis = False
            logger.info(
                "Subagent %s: skipping injection (already collected inline by spawn_sub_agents)",
                info.id,
            )
            # The blocking tool's return value carried it.
            info._report_delivered = True
            return

        # Fix 2 (B1) race guard: count this completion as an
        # in-flight delivery from entry until it is handed off (turn
        # launched or queued). The synthesis fire-gate in chat_runner
        # requires this count to be zero, so a concurrently-finishing
        # sibling that is still awaiting the current turn (busy path)
        # can't let an earlier turn fire synthesis before this result
        # is delivered. try/finally so a CancelledError can't leak it.
        _injection_slot._subagent_deliveries_inflight += 1
        try:
            if _injection_slot_busy(_injection_slot):
                # Slot is busy (or an injection is dispatched but
                # not yet started) — wait for that task to finish,
                # then inject. No visible queue card.
                _current = _injection_slot.task
                if _current is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(_current),
                            timeout=INJECTION_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        pass  # Timed out waiting — slot still busy, will be queued below
                    except asyncio.CancelledError:
                        raise  # Don't swallow cancellation of this coroutine
                    except Exception:
                        pass  # Task failed — slot is now idle

                # Re-check: another injection may have claimed the slot
                # during the await above.
                if _injection_slot_busy(_injection_slot):
                    # Check inline-collected before queuing — if the
                    # blocking tool already handled this result, don't
                    # queue it for a later redundant turn.
                    if info.id in _injection_slot._subagents_inline_collected:
                        _injection_slot._subagents_inline_collected.discard(info.id)
                        if not _injection_slot._subagents_inline_collected:
                            _injection_slot._pending_synthesis = False
                        logger.info(
                            "Subagent %s: skipping queue " "(already collected inline)",
                            info.id,
                        )
                        info._report_delivered = True
                        return
                    logger.info(
                        "Subagent %s: slot %s claimed by another injection, queuing",
                        info.id,
                        _slot_name,
                    )
                    # Each queued completion drains as its own turn
                    # (a system injection never merges), so it is
                    # one turn for the synthesis fire gate's count.
                    _injection_slot._synthesis_completion_turns += 1
                    # Bounded by the configured turn ceiling
                    # (chat_turn_timeout_secs, 14400s default):
                    # _run_chat's finally block drains slot._queue
                    # on any exit path.
                    # Carry the structured completion facts so the
                    # drained row is a card without re-parsing the
                    # prose; _start_next_queued_turn reads them.
                    _injection_slot.queue_append(
                        announce,
                        kind=SUBAGENT_COMPLETION_KIND,
                        meta={SUBAGENT_COMPLETION_META_KEY: sub_meta},
                    )
                    # Queuing is not delivery. The announce promises
                    # result paths the parent can read on demand, but
                    # it will not be in the parent's context until a
                    # turn drains it — a wait bounded only by the turn
                    # ceiling, so longer than the retention TTL. Owe
                    # the delivery tombstones to that drain instead of
                    # writing them now, or the reaper prunes
                    # result.txt while the promise is still queued
                    # and the parent is handed dead paths.
                    self._defer_queued_delivery(
                        _injection_slot, announce, info, flush_only=_flush_only
                    )
                    self.dashboard_state.push_slots_update()
                    logger.info("Subagent %s → queued in %s", info.id, _slot_name)
                    return

            # Slot is idle — re-check inline-collected (the
            # blocking tool's mark-collected POST has now landed,
            # since the tool returns before its owning turn ends).
            if info.id in _injection_slot._subagents_inline_collected:
                _injection_slot._subagents_inline_collected.discard(info.id)
                if not _injection_slot._subagents_inline_collected:
                    _injection_slot._pending_synthesis = False
                logger.info(
                    "Subagent %s: skipping injection after wait "
                    "(already collected inline by spawn_sub_agents)",
                    info.id,
                )
                info._report_delivered = True
                return

            # Slot is idle — start _run_chat.
            #
            # This branch hands the digest off ASYNCHRONOUSLY: the
            # turn is a task, and `_on_done` returns to
            # `_report_terminal` while it is still pending — so a
            # bare return here is a local routing success, not
            # evidence the parent received anything. Owe
            # the delivery bookkeeping to the turn's CONSUMPTION
            # instead, through the same `_defer_queued_delivery`
            # the queue branch uses: it records the debt (the
            # completed member's own tombstone AND any held wave
            # siblings) in the slot's content-keyed ledger, keyed
            # on this announce, and flags `_delivery_queued` — so
            # the run loop's `mark_delivered` and its digest-hold
            # settle both become no-ops for this route, and the
            # two settle paths cannot both fire.
            #
            # The task's own OUTCOME is deliberately not the
            # signal: `_run_chat` returns NORMALLY on a signed-out
            # CLI, a dead provider, exhausted retries and a first
            # empty response — several of them after re-queueing
            # the announce itself — so "the task finished cleanly"
            # says nothing about delivery. Consumption does, and a
            # failure before it re-queues the announce, whose drain
            # claims this same content-keyed debt on the replay. An
            # unconfirmed hand-off leaves the debt parked on
            # purpose: a duplicate announce after a restart is
            # visible to the parent and recoverable, a lost result
            # is neither.
            #
            # Computed BEFORE the transfer (which detaches the held
            # ids); stays False when there is nothing to owe — a
            # failed or stopped solo member settles through its own
            # failure tombstone, not this ledger. A memory-wait
            # expiry is the exception: it has no tombstone, and the
            # store owes its report until this turn consumes it.
            _owes_delivery = bool(info._digest_settle_deliveries) or (
                not _flush_only
                and (info.outcome == "completed" or getattr(info, "_report_owed", False) is True)
            )
            self._defer_queued_delivery(_injection_slot, announce, info, flush_only=_flush_only)
            _consumed: list[bool] = [False]

            def _note_consumed(consumed: bool = True) -> None:
                # False is a retraction: the first empty response
                # re-queues this exact announce verbatim, so the
                # delivery that counts has not happened yet.
                _consumed[0] = consumed

            _run_kwargs: dict[str, Any] = {}
            if _owes_delivery:
                _run_kwargs["_on_consumed"] = _note_consumed
            # One completion turn for the synthesis fire gate's
            # count, as in the queued branch above.
            _injection_slot._synthesis_completion_turns += 1
            _task = asyncio.create_task(
                bounded_chat_turn(
                    _run_chat(
                        self.dashboard_state,
                        _injection_slot,
                        announce,
                        _directive_user_origin=False,
                        # Structural provenance for the session
                        # crew log: the queued twin above carries
                        # SUBAGENT_COMPLETION_KIND, and this branch
                        # is the same injector dispatching directly.
                        _turn_actor="subagent",
                        **_run_kwargs,
                    ),
                    state=self.dashboard_state,
                    slot=_injection_slot,
                )
            )
            _injection_slot.task = _task
            self.dashboard_state._background_tasks.add(_task)
            _task.add_done_callback(self.dashboard_state._background_tasks.discard)

            def _on_inject_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                if _injection_slot.task is t:
                    _injection_slot.task = None
                if not t.cancelled() and t.exception():
                    logger.error("Subagent injection _run_chat failed: %s", t.exception())
                    if self.subagent_mgr:
                        _reason = str(t.exception())
                        _reason, _ = redact_exfiltration_urls(_reason)
                        _reason, _ = redact_credentials(_reason)
                        self.subagent_mgr.notify_injection_failed(
                            info,
                            reason=_reason,
                        )

            _task.add_done_callback(_on_inject_done)
            if _owes_delivery:
                # Settle the owed tombstones only once the model has
                # consumed this turn's prompt — the drain's own
                # settlement path, reused verbatim. If the transfer
                # above fell back (stubbed slot), the ledger holds
                # no debt and the claim inside is an empty no-op.
                _arm_queued_delivery_settlement(
                    self.dashboard_state,
                    _injection_slot,
                    _task,
                    [announce],
                    _consumed,
                )
            self.dashboard_state.push_slots_update()
            logger.info("Subagent %s → _run_chat in %s", info.id, _slot_name)
        finally:
            _injection_slot._subagent_deliveries_inflight -= 1
    else:
        logger.info(
            "Subagent %s: parent slot %s gone, notification only",
            info.id,
            _slot_name,
        )
        # Only notify when slot is gone — active slots already show
        # results in the Activity panel and chat.
        self.dashboard_state.notify(
            "subagent",
            title,
            body,
            meta=self._notif_meta(parent_key),
        )
        # The notification is this route's whole delivery.
        info._report_delivered = True
    return


async def _inject_subagent_into_channel_parent(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    parent_key: str,
) -> None:
    """Inject a completion into a channel or session parent that has no tab.

    The parent's ACP session takes the turn; only its synthesized reply goes to the
    conversation, through Slack or the channel's transport, then the bell rings.
    """
    # Parent with no tab to stream into — inject silently into the
    # parent's ACP session, then deliver only the synthesized reply
    # to the conversation. Make up to _MAX_INJECT_ATTEMPTS attempts;
    # only a timeout starts the next one (_inject_with_retry has its
    # own ACP-error retry inside each attempt). Slack keeps its
    # dedicated rich posting; every other channel namespace
    # (Telegram, Discord, …) delivers through the governed transport
    # ladder — its stored channel value is not a Slack channel id,
    # so posting it through the Slack client can never reach the
    # user. A parent with neither a channel namespace nor a Slack
    # thread (a taskrunner: session, say) is still injected, but its
    # reply goes to no channel.
    assert self.sessions is not None
    _namespace = channel_namespace_of(parent_key)
    _via_transport = bool(_namespace) and _namespace != SLACK_NAMESPACE
    _slack_thread = self.sessions.get_thread(parent_key)
    if _slack_thread is None and is_legacy_slack_key(parent_key):
        _slack_thread = parent_key
    _via_slack = _namespace == SLACK_NAMESPACE or bool(_slack_thread)
    if _via_transport:
        _inject_label = _namespace
    elif _via_slack:
        _inject_label = "Slack"
    else:
        _inject_label = "session"
    # Snapshot the transport reply link BEFORE the injection retry
    # loop: the timeout path's sessions.reset() evicts the session's
    # in-memory origin link, the first rung _channel_reply_link
    # tries, so resolving after a retry could miss the
    # conversation's own thread/forum target.
    _reply_link = self._channel_reply_link(parent_key) if _via_transport else None
    _injected = False
    _inject_failure_reasons: list[str] = []
    _sleep_before_retry = False
    for _attempt in range(1, _MAX_INJECT_ATTEMPTS + 1):
        if _sleep_before_retry:
            await asyncio.sleep(2)
            _sleep_before_retry = False
        _acquired = False
        _footer_client = None
        try:
            logger.debug(
                "Subagent %s: %s injection attempt %d/%d into %s",
                info.id,
                _inject_label,
                _attempt,
                _MAX_INJECT_ATTEMPTS,
                parent_key,
            )
            _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
            client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
            _acquired = True
            _footer_client = client
            _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
            if self.ctx_builder:
                # The completion is injected into the PARENT's
                # conversation, so it reads the parent session's silo,
                # resolved from that session's recorded binding. The
                # child's own store is not the answer here: this turn
                # continues the parent, and the reply it produces is
                # consolidated into the parent's store.
                msg, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    announce,
                    is_new,
                    parent_key,
                    memory_store=_memory_store,
                    provider_type=_provider,
                    context_provider=client,
                    resumed=_resumed,
                )
            else:
                msg = announce
            response = await asyncio.wait_for(
                self._inject_with_retry(info, client, msg, parent_key, _inject_label),
                timeout=INJECTION_TIMEOUT,
            )
            _injected = True  # LLM processed result; channel posting is best-effort

            # Persisted at the per-attempt level, NOT inside the
            # Slack branch below: a channel parent (Discord,
            # Telegram) delivers via the transport ladder and skips
            # that branch entirely, so persisting there dropped
            # every non-Slack subagent turn from replay. It still
            # runs BEFORE the Slack control is posted, so the
            # control's staleness token names this turn rather than
            # the one before it.
            # Persist BEFORE the control below invites
            # an answer to this turn: the token names
            # this session's last written row, so an
            # unwritten turn stamps the control with the
            # PREVIOUS turn's position and the first
            # click reads as already superseded.
            if self.conv_log and not (
                is_thread_temporary(parent_key) or is_thread_incognito(parent_key)
            ):
                try:
                    # Defense-in-depth: `announce` is composed from
                    # already-redacted parts plus identifiers such as
                    # `info.agent`; we re-redact before persisting to the
                    # dashboard replay (an external surface), mirroring the
                    # dashboard branch. `response` is fresh LLM output from
                    # stream_and_collect and is NOT yet redacted, so its
                    # redaction here is strictly required.
                    safe_announce, _ = redact_exfiltration_urls(announce)
                    safe_announce, _ = redact_credentials(safe_announce)
                    safe_response, _ = redact_exfiltration_urls(response or "")
                    safe_response, _ = redact_credentials(safe_response)
                    await save_conversation_turn_off_loop(
                        self.conv_log,
                        parent_key,
                        safe_announce,
                        safe_response,
                        source_thread=parent_key,
                        source_user="subagent",
                        agent=_get_agent_for_session(parent_key),
                    )
                except Exception:
                    logger.warning(
                        "Failed to persist subagent turn for %s",
                        parent_key,
                        exc_info=True,
                    )
            # Deliver only the LLM's synthesized response to the
            # parent's own conversation. Non-Slack channels go
            # through the governed transport ladder; on failure
            # the dashboard notification below still fires.
            if _via_transport and response:
                await self._deliver_channel_reply(parent_key, response, resolved_link=_reply_link)
            # Post only the LLM's synthesized response to Slack
            try:
                if response and _via_slack and self.slack and self._owner_id:
                    channel = (
                        self.sessions.get_channel(parent_key) if self.sessions else None
                    ) or await self.slack.open_dm(self._owner_id)
                    if channel:
                        # OPTIONS off the RAW text, then render: the
                        # tag is plain text, so extracting it after
                        # conversion made the controls hostage to
                        # to_slack_mrkdwn's 39,000-char truncation.
                        reply_text, options = extract_options(response)
                        for part in render_for_slack(reply_text):
                            await self.slack.post_message(channel, part, _slack_thread)
                        try:
                            elapsed = (
                                info.elapsed
                                if info.elapsed > 0
                                else (time.monotonic() - info.started)
                            )
                            footer_blocks, footer_text = build_timing_footer(
                                elapsed,
                                _footer_client,
                            )
                            if options:
                                _sub_token = await asyncio.to_thread(
                                    mint_options_token,
                                    self.dashboard_state,
                                    parent_key,
                                )
                                footer_blocks.extend(
                                    build_options_blocks(options, staleness_token=_sub_token)
                                )
                            _footer_ts = await self.slack.post_blocks(
                                channel,
                                footer_blocks,
                                footer_text,
                                _slack_thread,
                            )
                            self._remember_options(
                                parent_key,
                                channel,
                                _footer_ts,
                                options,
                                footer_blocks,
                                footer_text,
                            )
                        except Exception:
                            logger.debug(
                                "Failed to post timing footer for %s",
                                parent_key,
                                exc_info=True,
                            )
            except Exception:
                logger.exception(
                    "Subagent %s: Slack posting failed (injection succeeded)",
                    info.id,
                )

            logger.info("Subagent %s → %s session %s", info.id, _inject_label, parent_key)
            break
        except asyncio.TimeoutError:
            _inject_failure_reasons.append(
                f"attempt {_attempt} timed out after {int(INJECTION_TIMEOUT)}s"
            )
            logger.warning(
                "Subagent %s: %s injection attempt %d/%d timed out after %.0fs",
                info.id,
                _inject_label,
                _attempt,
                _MAX_INJECT_ATTEMPTS,
                INJECTION_TIMEOUT,
            )
            if _acquired:
                try:
                    await self.sessions.reset(parent_key)
                except Exception:
                    logger.debug(
                        "Failed to reset %s after channel injection timeout",
                        parent_key,
                        exc_info=True,
                    )
            if _attempt < _MAX_INJECT_ATTEMPTS:
                _sleep_before_retry = True
        except Exception as exc:
            _inject_failure_reasons.append(f"attempt {_attempt} failed: {exc}")
            logger.exception("Subagent %s %s injection failed", info.id, _inject_label)
            break
        finally:
            if _acquired:
                try:
                    # Only this injection's own prompt: after the timeout
                    # arm's reset the key may hold a successor whose live
                    # turn (the parent's next message) is not ours to cancel.
                    await self.sessions.cancel_current(
                        parent_key, owner_task=asyncio.current_task()
                    )
                except Exception:
                    logger.debug(
                        "Failed to cancel parent prompt for %s",
                        info.id,
                        exc_info=True,
                    )
                try:
                    self.sessions.release(parent_key)
                except Exception:
                    logger.exception("Failed to release session %s", parent_key)

    if not _injected:
        _last_failure_reason = "; ".join(_inject_failure_reasons)
        _last_failure_reason, _ = redact_exfiltration_urls(_last_failure_reason)
        _last_failure_reason, _ = redact_credentials(_last_failure_reason)
        logger.error(
            "Subagent %s: all %d %s injection attempts failed: %s",
            info.id,
            _MAX_INJECT_ATTEMPTS,
            _inject_label,
            _last_failure_reason,
        )
        if self.subagent_mgr:
            self.subagent_mgr.notify_injection_failed(
                info,
                reason=_last_failure_reason,
            )
    # Dashboard notification
    if self.dashboard_state:
        self.dashboard_state.notify(
            "subagent",
            title,
            body,
            meta=self._notif_meta(parent_key),
        )
    return


async def _finish_subagent_cron_or_notify(
    self: GatewayOrchestrator,
    info: SubagentInfo,
    *,
    announce: str,
    body: str,
    title: str,
    parent_key: str,
) -> None:
    """Inject a completion into its cron parent, then ring the bell for any parent.

    A parent with no conversation to inject into is delivered by the bell alone.
    """
    # Cron parent — inject result back into the cron session.
    # Track pending injections to avoid resetting the session while
    # other subagents are queued behind the per-session semaphore.
    if parent_key.startswith("cron:"):
        self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 0) + 1
        assert self.sessions is not None
        acquired = False
        cron_response: str | None = None
        try:
            _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
            client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
            acquired = True
            _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
            if self.ctx_builder:
                # Same rule as the interactive injection above: the turn
                # continues the PARENT conversation, so it reads the parent
                # session's silo from that session's recorded binding.
                msg, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    announce,
                    is_new,
                    parent_key,
                    memory_store=_memory_store,
                    provider_type=_provider,
                    context_provider=client,
                    resumed=_resumed,
                )
            else:
                msg = announce
            cron_response = await asyncio.wait_for(
                self._inject_with_retry(info, client, msg, parent_key, "cron"),
                timeout=INJECTION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Subagent %s: cron injection timed out after %.0fs",
                info.id,
                INJECTION_TIMEOUT,
            )
            try:
                await self.sessions.reset(parent_key)
            except Exception:
                logger.debug(
                    "Failed to reset %s after cron injection timeout",
                    parent_key,
                    exc_info=True,
                )
            if self.subagent_mgr:
                self.subagent_mgr.notify_injection_failed(
                    info,
                    reason=f"injection timed out after {int(INJECTION_TIMEOUT)}s",
                )
        except Exception:
            logger.exception("Subagent %s cron injection failed", info.id)
            # Swallowed with no failure notice, so the record is the only
            # place that says the parent was never told.
            info._report_undelivered = True
        finally:
            if acquired:
                try:
                    # Same fence as the channel route: never the live turn
                    # of a successor that claimed the key after a reset.
                    await self.sessions.cancel_current(
                        parent_key, owner_task=asyncio.current_task()
                    )
                except Exception:
                    logger.debug(
                        "Failed to cancel parent prompt for cron %s", info.id, exc_info=True
                    )
                try:
                    self.sessions.release(parent_key)
                except Exception:
                    logger.exception("Failed to release session %s", parent_key)
            self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
            if self._cron_injecting[parent_key] <= 0:
                self._cron_injecting.pop(parent_key, None)
        if cron_response:
            cron_response, _ = redact_exfiltration_urls(cron_response)
            cron_response, _ = redact_credentials(cron_response)
            body = f"{body}\n\n{cron_response}"
            logger.info("Subagent %s → cron session %s", info.id, parent_key)
            # also deliver the synthesized response to the job's own
            # surfaces. honor the parent cron job's silent flag too:
            # info.silent is never set from the cron's silent setting for
            # spawn_run sub-agents, so a silent cron would otherwise still
            # post every subagent-completion turn.
            try:
                await self._deliver_cron_response(
                    parent_key,
                    cron_response,
                    silent=info.silent or self._cron_job_is_silent(parent_key),
                )
            except Exception:
                logger.exception(
                    "Subagent %s: failed to deliver cron response",
                    info.id,
                )
        # Reset only when no subagents running or QUEUED AND no
        # injections pending. Queued spawns (behind the concurrency /
        # stagger gate) have no SubagentInfo in `running` yet — a
        # sibling completing while the rest of the wave is still
        # queued must not reset the parent out from under them.
        still_running = self.subagent_mgr and (
            any(
                a.parent_session_key == parent_key and a.id != info.id
                for a in self.subagent_mgr.running
            )
            or await _subagent_queued_count(self.subagent_mgr, parent_key) > 0
        )
        still_injecting = self._cron_injecting.get(parent_key, 0) > 0
        if not still_running and not still_injecting:
            try:
                await self.sessions.reset(parent_key)
                logger.info("Cron session %s: last subagent done, session reset", parent_key)
                # reset succeeded → the reaper does not need the
                # registered ephemeral key. Clear inside try so a failed
                # reset leaves the key registered (ephemeral session may
                # still be alive — reaper must be able to target it).
                # parent_key is "cron:{job_id}" (persistent) or
                # "cron:{job_id}:{run_id}" (ephemeral); job_id is the
                # second colon-separated segment in both cases. Exact-key
                # tracking means an older deferred reset removes only its
                # own key even after a newer run registers another one,
                # without stripping the reaper/ownership fence for either.
                cron_svc = getattr(self, "cron_svc", None)
                if cron_svc is not None:
                    parts = parent_key.split(":", 2)
                    if len(parts) >= 2:
                        cron_svc.clear_active_session_key(parts[1], parent_key)
            except Exception:
                logger.exception("Cron session %s: reset failed after last subagent", parent_key)

    # Dashboard notification
    if self.dashboard_state and not info.silent:
        self.dashboard_state.notify(
            "subagent",
            title,
            body,
            meta=self._notif_meta(parent_key),
        )
    if not parent_key.startswith("cron:"):
        logger.info("Subagent %s → notification only (parent=%s)", info.id, parent_key)
        # No conversation to inject into: the notification is this
        # route's whole delivery. A cron parent acknowledges in
        # ``_inject_with_retry`` instead, and only when it lands.
        info._report_delivered = True

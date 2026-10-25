"""Fire one nudge or monitor wake into a dashboard chat slot.

The slot turn itself, the work-ledger wake steered into a running turn, and the
fire-time refusals for the slot's mode and the crew boundary.

Orchestrator fields read: ``_session_tasks``, ``autonudge_svc``, ``dashboard_state``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.gateway import (
        MODEL_REFUSED_REASON,
        STRUCTURAL_TERMINAL_REASON,
        Any,
        GatewayOrchestrator,
        MonitorCompletionHook,
        MonitorDispatchResult,
        NudgeLoop,
        _delivery_result,
        autonudge_selfarm,
        compose_nudge_body,
        load_window_off_loop,
        logger,
        nudge_cycle_header,
        rehydrate_slot_from_history_async,
        render_nudge_message,
        sel,
        spawn_guarded_turn,
    )


async def _fire_dashboard_nudge(
    self: GatewayOrchestrator, loop: NudgeLoop, wake_message: str | None = None
) -> bool | MonitorDispatchResult:
    """Drive one nudge turn in a dashboard chat slot.

    Sibling of :meth:`_fire_slack_nudge` / :meth:`_fire_discord_nudge`; a
    named method rather than an inline branch so the slot-resolution
    contract below is directly testable.

    Returns True if the nudge was dispatched, False if skipped (dashboard
    not ready, session genuinely gone, or a turn still active). The service
    only counts dispatched cycles toward ``max_cycles``.
    """
    # Guard (not assert): stripped under -O; also _init_autonudge() can
    # run before _init_dashboard(), and _init_dashboard is skipped
    # entirely in --no-dashboard mode. Mirrors _observer's guard.
    if self.dashboard_state is None:
        logger.warning("AutoNudge: dashboard not ready — skipping fire for loop %s", loop.id)
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    # Slot resolution mirrors the cron→origin delivery contract in
    # dashboard/handlers/messaging.py: get_slot() is the hot path, and a
    # miss falls back to restoring the session from its persisted history
    # rather than assuming it is gone. A miss is NOT evidence of a dead
    # session — the in-memory registry is empty for any tab the user has
    # navigated away from, and it is empty for EVERY slot immediately
    # after a gateway restart (AutoNudgeService.start() re-arms timers
    # before the dashboard has restored its slots). Removing the loop on a
    # cold cache would silently abandon a live babysit loop.
    #
    # Rehydration deliberately does NOT resurrect a session the user
    # dismissed with ✕ — that is the documented "respect the close" rule.
    # It is enforced where the user acts: api_chat_slot_delete removes
    # this loop as part of the close, so a loop that is still armed was
    # never user-dismissed. Only a genuinely unreachable session retires
    # the loop.
    #
    # Hence adopt_closed=True. ``closed`` in the metadata is written
    # by TWO producers, and only one of them is the user: idle archival
    # (POST /api/chat/slots/cleanup, default 3 days) also marks a slot
    # closed. An unattended worker is idle by nature between cycles, so
    # archival would make it unreachable here and the loop would be
    # REMOVED below — terminally, with no way back. api_chat_slots_cleanup
    # exempts loop-owning slots; adopting the closed session is the
    # backstop for an older archive or any other automatic closer.
    slot = self.dashboard_state.get_slot(loop.slot_key)
    if slot is None:
        # Rehydration reads the session's persisted transcript and replays
        # its window. Real sessions reach tens of MB, so the reads must not
        # run on the event loop: the gateway serves every request, turn and
        # the stall-watchdog heartbeat on one thread, and a fire here is a
        # timer callback. The async form hoists ONLY the reads to a worker
        # thread and builds the slot back on the loop, because slot
        # construction broadcasts through asyncio primitives that are not
        # thread-safe.
        slot = await rehydrate_slot_from_history_async(
            self.dashboard_state, loop.slot_key, adopt_closed=True
        )
        if slot is None:
            logger.warning(
                "AutoNudge: session %s unreachable (no history or deleted) " "— removing loop %s",
                loop.slot_key,
                loop.id,
            )
            if wake_message is None:
                await self.autonudge_svc.remove(  # type: ignore[union-attr]
                    loop.id, stop_reason="session_unreachable"
                )
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
        logger.info(
            "AutoNudge: rehydrated session %s from history for loop %s",
            loop.slot_key,
            loop.id,
        )
    elif getattr(slot, "window_pending", False) is True:
        # A tab restored as a sidebar row (a paused loop's tab can be one)
        # loads its window before the nudge appends to it, with the read off
        # the loop for the reason above. The same tab must still be live
        # after the await; a tab closed meanwhile takes no nudge.
        await load_window_off_loop(slot)
        if self.dashboard_state.get_slot(loop.slot_key) is not slot:
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
    # STRUCTURAL-TERMINAL GUARD (message loops only). If the slot's LAST
    # delivered turn ended on a malformed-request rejection, the backend
    # refused the payload's SHAPE, deterministically -- re-injecting the same
    # nudge context can only reproduce it. Firing again would spend cycle
    # after cycle on an identical doomed turn, so STOP the loop instead. The verdict is scoped to the loop id
    # (``_last_turn_structural_terminal_loop_id``) AND applied under an ATOMIC
    # (id, generation) fence in AutoNudgeService.update(expected_generation=):
    # the loop's config generation captured at fire time
    # (``_last_turn_structural_terminal_loop_gen``) must still match under the
    # service lock, or the completion is a STALE result of an OLD instruction
    # (the A->B->A race) and the stop is refused there with no TOCTOU window.
    # The loop stays INACTIVE when stopped; the stop is REPLACEABLE
    # (STRUCTURAL_TERMINAL_REASON), which does not re-arm on its own -- it
    # only PERMITS a later directive to re-arm the loop, and any such re-arm
    # advances the generation so this verdict cannot follow it. The slot's
    # verdict is cleared at the start of every genuine new turn (chat_runner).
    # A structured monitor WAKE (``wake_message is not None``) carries its own
    # actionable context and is out of scope here. Tested with ``is True``
    # (not truthiness) so a bare MagicMock slot's truthy attribute cannot
    # trip it; getattr keeps minimal slot doubles safe.
    if (
        wake_message is None
        and getattr(slot, "_last_turn_structural_terminal", False) is True
        and getattr(slot, "_last_turn_structural_terminal_loop_id", "") == loop.id
        and self.autonudge_svc is not None
    ):
        _expected_gen = int(getattr(slot, "_last_turn_structural_terminal_loop_gen", 0) or 0)
        stopped_loop = await self.autonudge_svc.update(  # type: ignore[union-attr]
            loop.id,
            active=False,
            stopped_reason=STRUCTURAL_TERMINAL_REASON,
            expected_generation=_expected_gen,
        )
        # The fence answers three cases, and only ONE may dispatch:
        #   * inactive loop  -> stopped (verdict applied): return UNAVAILABLE.
        #   * None            -> update refused the mutation because the loop
        #     is quiescing/removed under maintenance (``_acquire_mutation_lock``
        #     returns None), NOT a live target: return UNAVAILABLE, matching
        #     the sibling ``_stop_message_loop_if_structural_terminal`` seam,
        #     which treats None as not-a-live-loop.
        #   * still-active loop -> the fence refused because the config
        #     generation advanced under the turn (stale completion): fall
        #     through and dispatch the reconfigured loop.
        # So dispatch happens ONLY for a non-None ACTIVE loop; a None must not
        # be conflated with "refused, still firing".
        if stopped_loop is None or not stopped_loop.active:
            logger.warning(
                "AutoNudge: loop %s on slot %s not dispatched — its last "
                "delivered turn was rejected as structurally malformed and "
                "the loop is stopped or is not a live target; re-firing "
                "the same context cannot help, and a later directive (after "
                "a fresh conversation) may re-arm it",
                loop.id,
                loop.slot_key,
            )
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
        logger.info(
            "AutoNudge: loop %s structural stop skipped — config generation "
            "advanced under the fired turn, so the verdict is stale",
            loop.id,
        )
    # MODEL-REFUSED GUARD (message loops only). If this loop's own last
    # delivered cycle ended on a content refusal that nothing retried, the
    # next cycle would send the same prompt to the same model and be declined
    # the same way, cycle after cycle, with nobody told. Stop the loop with
    # ``model_refused`` and notify the owner once instead. Same scoping as the
    # structural guard above: the verdict names the loop id, and the stop is
    # applied under the (id, generation) fence, so a revised instruction is
    # not stopped by a refusal of the old one. The slot's verdict is cleared at
    # the start of every genuine new turn (a person's message), which lets the
    # loop fire again once the context changes. Compared as a ``str`` so a
    # bare MagicMock slot's attribute cannot match.
    _refused_loop_id = getattr(slot, "_last_turn_model_refused_loop_id", "")
    if (
        wake_message is None
        and isinstance(_refused_loop_id, str)
        and _refused_loop_id
        and _refused_loop_id == loop.id
        and self.autonudge_svc is not None
    ):
        _refused_gen = getattr(slot, "_last_turn_model_refused_loop_gen", 0)
        stopped_loop = await self.autonudge_svc.update(  # type: ignore[union-attr]
            loop.id,
            active=False,
            stopped_reason=MODEL_REFUSED_REASON,
            expected_generation=_refused_gen if isinstance(_refused_gen, int) else 0,
        )
        # Same three answers as the structural fence: inactive means stopped,
        # None means not a live target, and an active loop means the
        # generation moved under the refused turn (a stale verdict).
        if stopped_loop is None or not stopped_loop.active:
            if stopped_loop is not None and stopped_loop.stopped_reason == MODEL_REFUSED_REASON:
                logger.warning(
                    "AutoNudge: loop %s on slot %s stopped -- its last cycle "
                    "was declined by the model's content filter and no "
                    "fallback retried it, so the next cycle would be declined "
                    "the same way",
                    loop.id,
                    loop.slot_key,
                )
                self._notify_nudge_expired(stopped_loop)
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
        logger.info(
            "AutoNudge: loop %s refusal stop skipped -- config generation "
            "advanced under the refused turn, so the verdict is stale",
            loop.id,
        )
    # This cycle's work-ledger news: which items moved, and where to read the
    # rest. Built ONCE, here, because both ways of delivering it are below -- the
    # prefix onto the nudge prompt a step down, and the steer into a turn already
    # running further down still -- and both must carry the same text. Empty for
    # every other loop shape and for a cycle with nothing owed, which is what
    # keeps those paths byte-identical to what shipped.
    _wake_text = (
        await self._work_ledger_wake_text(loop)
        if self._work_ledger_wake_loop(loop, wake_message)
        else ""
    )
    if wake_message is None:
        # Snapshot message, sentinel AND config generation TOGETHER, before
        # the compose_nudge_body() await: a concurrent PATCH during that
        # suspension could otherwise pair the OLD message with the NEW
        # generation, so the malformed verdict would match the reconfigured
        # loop's generation and wrongly stop it. The generation recorded with
        # the verdict must be the one that goes with the message the turn
        # actually runs.
        _fired_message = loop.message
        _fired_sentinel = loop.stop_sentinel_path
        _fired_generation = loop.config_generation
        msg = await compose_nudge_body(_fired_message, _fired_sentinel, loop.slot_key)
        tagged = f"{nudge_cycle_header(loop)}\n{msg}"
        if _wake_text:
            # The news FIRST, then the standing patrol instruction. Without this
            # the delivered turn carried the cycle header and the loop's own
            # message alone: the probe's briefs died with the verdict the gate
            # read, so the conductor was not told which item had moved and spent
            # a ``work_ledger_read`` finding out.
            tagged = f"{_wake_text}\n\n{tagged}"
    else:
        tagged = wake_message
        # Capture the generation on THIS arm too. A monitor wake runs the
        # loop's own cycle just as a plain nudge does, so the directive
        # consumers (the structural-terminal verdict and the failed-cycle
        # charge) must scope to the generation this turn fired under here as
        # well -- otherwise the read below is unbound on this path.
        _fired_generation = loop.config_generation
    # ONE STRING, TWO CONSUMERS, and only an opt-in ``banner`` splits them.
    # ``tagged`` is the PROMPT and is never shortened — re-delivering the
    # whole instruction every cycle is the guarantee the nudge exists to
    # provide. ``visible`` is the transcript row, which a reader consults
    # only to learn that a cycle happened. Without a banner it IS ``tagged``,
    # so an existing loop's row is byte-identical to today's.
    #
    # A banner deliberately skips ``compose_nudge_body``: that composer
    # prefixes the SESSION ledger snapshot (``session_ledger.render_snapshot``,
    # not the work ledger, whose wake carries a read pointer instead), which the
    # model wants and a display line does not. ``render_nudge_message`` still applies, so
    # ``{{STOP_FILE}}`` resolves in a banner as it does in a message.
    #
    # A banner is a MESSAGE-loop concept: a monitor wake (``wake_message``)
    # shows its own actionable-wake row, so the banner only splits the row
    # on the ``wake_message is None`` arm.
    #
    # ``isinstance`` rather than a bare falsiness test: ``banner: str`` is a
    # plain dataclass annotation, unenforced at runtime, and ``_load`` builds
    # a loop straight from parsed JSON — so a store carrying ``"banner": 5``
    # yields ``loop.banner == 5`` and ``.strip()`` on it would raise
    # ``AttributeError``, killing the fire and (since the service re-arms an
    # undelivered cycle) rearming the loop forever. A whitespace-only banner
    # is truthy too and its blank row is worse than the verbose one, so both
    # fall through to ``tagged``.
    banner = loop.banner.strip() if isinstance(loop.banner, str) else ""
    if banner and wake_message is None:
        # Credential redaction lives at the banner's single owner — the
        # authorized write paths (incl. /goal via ``normalize_banner``) and
        # ``_load`` for a hand-edited store — so ``loop.banner`` is already
        # scrubbed here and every egress (this row, ``GET /api/autonudge``,
        # the WS broadcast) serves the same scrubbed value. No per-fire,
        # per-field scrub at this sink.
        shown = render_nudge_message(banner, loop.stop_sentinel_path)
        visible = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{shown}"
    else:
        visible = tagged
    from kiro_crew.dashboard.chat import (
        _run_chat,  # circular import: gateway -> dashboard.chat -> gateway (chat dispatch references GatewayOrchestrator)
    )

    if slot.running:
        # Turn still active. A work-ledger wake goes INTO that turn rather than
        # waiting it out: its payload is a line naming the item that moved plus a
        # line saying where to read it, and a conductor told a worker finished
        # three minutes after the fact has lost the whole point of an
        # event-driven watch. Everything else still declines -- queueing a
        # multi-kilobyte instruction payload behind a turn would stack identical
        # copies and blow out the context window.
        if _wake_text and await self._steer_work_ledger_wake(loop, slot, _wake_text):
            # DISPATCHED, because the news reached a model turn: that is what the
            # fire exists to do, and the loop must not re-owe a wake it has
            # delivered. It also charges the cycle, which is the honest direction
            # for a budget -- a steered wake buys the conductor's attention just
            # as a fired turn does, so counting it can only leave the loop's
            # remaining cycles understated, never overspent.
            return _delivery_result(wake_message, MonitorDispatchResult.DISPATCHED)
        # Next idle-timer tick will schedule again once the turn ends.
        # Returning False keeps cycle_count accurate (only delivered
        # nudges count toward max_cycles).
        logger.info(
            "AutoNudge skip: slot %s is running (loop %s cycle %d)",
            slot.key,
            loop.id,
            loop.cycle_count,
        )
        return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
    # Crew/member slot boundary, for EVERY dashboard loop -- prompt loops
    # included, not only the structured/gated ones the completion hook
    # covers below. Such a slot accepts a wake only from a loop its own
    # turn armed, proven by the persisted bit AND the keystone-gated trust
    # record together (see ``_dashboard_mode_admits``). The hook path
    # re-checks right before provider entry for the TOCTOU window; this is
    # the gate that applies when there is no hook at all.
    if not await self._dashboard_mode_admits(loop, slot):
        await self._audit_fire_refused(loop, slot)
        return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
    # Show nudge as a distinct "nudge" role message in the slot history.
    # The structured meta lets the dashboard render a compact cycle chip
    # instead of echoing the whole instruction payload as a chat bubble.
    # The tag stays in ``content`` because that is what the model reads,
    # and the body is deliberately NOT duplicated into meta — the client
    # derives it from content, so a multi-KB payload is stored and
    # broadcast once rather than twice. ``visible`` rather than ``tagged``
    # in the appended row: identical unless the loop opted into a ``banner``,
    # in which case this transcript row is the only thing shortened while the
    # full ``tagged`` prompt still reaches ``_run_chat``.
    # The row's role and ``meta.nudge`` come from the wake's origin
    # (``session_deliver.origin_row``), the same mapping a steered wake uses.
    # Deferred import for the cycle the steer path documents.
    from kiro_crew.dashboard import session_deliver

    wake_origin = session_deliver.automation(loop.cycle_count + 1, loop.id)
    monitor_meta: dict[str, Any] = {}
    if wake_message is not None and loop.monitor is not None:
        monitor_meta["monitor"] = {
            "id": loop.id,
            "fingerprint": loop.monitor.last_wake_fingerprint,
            "classification": (
                loop.monitor.last_decision.value
                if loop.monitor.last_decision is not None
                else "actionable"
            ),
        }
    completion_hook = self._monitor_completion_hook(loop)
    if wake_message is not None and completion_hook is None:
        return MonitorDispatchResult.UNAVAILABLE
    dashboard_state = self.dashboard_state
    turn_slot = slot
    assert dashboard_state is not None and turn_slot is not None

    def _append_nudge() -> None:
        session_deliver.write_origin_row(turn_slot, visible, wake_origin, extra_meta=monitor_meta)

    if completion_hook is None:
        _append_nudge()
    # An unattended app-owned nudge turn runs under the background
    # concurrency cap. This is the fleet's hot path — N armed loops fire
    # independently and would otherwise put N turns on the runtime at once.
    # An attended slot (any user session with a monitor loop) is passed
    # straight through, so babysit loops on human sessions are unaffected.
    admission: asyncio.Future[MonitorDispatchResult] | None = None
    if completion_hook is not None:
        admission = asyncio.get_running_loop().create_future()

    def _settle_admission(result: MonitorDispatchResult) -> None:
        if admission is not None and not admission.done():
            admission.set_result(result)

    if completion_hook is not None:
        base_hook = completion_hook

        async def _authorize_dashboard_turn(monitor_id: str, fingerprint: str) -> bool:
            current_slot = dashboard_state.get_slot(loop.slot_key)
            # A crew/member slot refuses a wake armed from OUTSIDE the
            # session; a loop the slot's OWN turn armed is the member keeping
            # itself awake and must fire. Same rule as ``autonudge_authz``.
            # TWO sources must agree, because the loop store is agent-
            # writable and this is the one bit that relaxes a session
            # boundary: the persisted ``self_armed`` must be the boolean True
            # (``is True`` -- a forged string is truthy; ``_load`` normalises
            # too) AND the keystone-gated trust record the authorizer wrote
            # at arm time (``autonudge_selfarm``, which agent file tools
            # cannot reach) must name this loop on this slot. A forged
            # boolean in the store has no trust entry and refuses.
            mode_refused = not await self._dashboard_mode_admits(loop, turn_slot)
            if mode_refused:
                await self._audit_fire_refused(loop, turn_slot)
            if (
                current_slot is not turn_slot
                or bool(getattr(turn_slot, "is_closing", False))
                or mode_refused
                or str(getattr(turn_slot, "memory_mode", "persistent")) != "persistent"
            ):
                _settle_admission(MonitorDispatchResult.UNAVAILABLE)
                return False
            callback = base_hook.authorization_callback
            authorized = True if callback is None else await callback(monitor_id, fingerprint)
            if not authorized:
                _settle_admission(MonitorDispatchResult.UNAVAILABLE)
            return authorized

        def _accept_dashboard_turn() -> None:
            callback = base_hook.acceptance_callback
            if callback is not None:
                callback()
            _append_nudge()
            # The row now names the turn's opener. Its frame was queued on
            # every socket before this push's, and a socket sends in order,
            # so no client sees the mark drop before the row arrives.
            turn_slot._wake_starting_task = None
            dashboard_state.push_slots_update()
            _settle_admission(MonitorDispatchResult.DISPATCHED)

        completion_hook = MonitorCompletionHook(
            base_hook.monitor_id,
            base_hook.fingerprint,
            base_hook.callback,
            authorization_callback=_authorize_dashboard_turn,
            acceptance_callback=_accept_dashboard_turn,
        )

    run_kwargs: dict[str, Any] = {}
    # This turn IS the loop's delivered wake on its own slot. The directive
    # consumer treats that as self-arm provenance alongside a human-started
    # turn, so a member re-arming or revising its loop from inside a cycle
    # is admitted; a cron, app or sub-agent turn on the same slot never
    # carries this mark. On a crew/member slot the wake only exists because
    # ``_dashboard_mode_admits`` already proved the loop self-armed.
    run_kwargs["_directive_self_wake"] = True
    # Scope the structural-terminal verdict this turn may record to THIS loop
    # and the CONFIG GENERATION it fires under (the snapshot captured with the
    # message above, before compose_nudge_body's await), so the stop is
    # applied via an atomic (id, generation) fence and a stale completion
    # cannot deactivate a loop whose config advanced under the turn.
    run_kwargs["_directive_loop_id"] = loop.id
    # Pass the generation captured when THIS turn fired, for both fire
    # shapes. A plain nudge and a monitor wake_message both snapshot
    # ``loop.config_generation`` into ``_fired_generation`` above, before the
    # compose await, so a real generation is always available -- passing 0
    # for the wake_message shape would leave the fence comparing against a
    # generation no revised loop ever holds, so a stale completion of a
    # since-revised loop (A->B->A) could still match and wrongly stop it, and
    # a legitimately-revised loop fired this way would never match at all.
    # The default of 0 on the runner parameter is the floor for a turn that
    # is NOT a self-wake (no loop, so the consumers are guarded off anyway);
    # every self-wake fire passes its true generation here.
    run_kwargs["_directive_loop_gen"] = _fired_generation
    if completion_hook is not None:
        run_kwargs["monitor_completion"] = completion_hook
        # Structured monitor turns own a single durable budgeted turn.
        # Nested depth disables dashboard recovery paths that would enqueue
        # an additional provider turn outside that accounting boundary.
        run_kwargs["_prompt_depth"] = 1

    async def _run_dashboard_turn() -> None:
        # Unattended slots can wait behind the background-turn semaphore.
        # Reject a revoked structured claim before entering ``_run_chat``:
        # prompt-submit hooks run during its setup and must not observe a
        # monitor the user stopped while this turn waited for admission.
        # The runner retains its own final recheck immediately before
        # provider entry to cover revocation during that setup.
        if completion_hook is not None and not await completion_hook.authorize():
            return
        # NO AUDIENCE FENCE for the work-ledger wake, by construction: the
        # wake carries ``wake_brief`` lines
        # and a ``work_ledger_read`` pointer, so there is no worker-authored
        # ledger prose in it for a fence to withhold. The conductor's own read,
        # which is what the pointer sends it to, goes through
        # ``_caller_key`` -- which judges the containment and records the
        # admission itself, inside this very turn. The gate sits at the one
        # place that reads the board.
        await _run_chat(
            dashboard_state,
            turn_slot,
            tagged,
            _directive_user_origin=False,
            **run_kwargs,
        )

    async def _run_background_turn() -> None:
        try:
            await dashboard_state.run_background_turn(turn_slot, _run_dashboard_turn())
        except TimeoutError:
            _settle_admission(MonitorDispatchResult.BUSY)
        except asyncio.CancelledError:
            _settle_admission(MonitorDispatchResult.BUSY)
            raise
        except Exception:
            _settle_admission(MonitorDispatchResult.UNAVAILABLE)
            raise

    if admission is not None:
        turn_coro = _run_background_turn()
    else:
        turn_coro = dashboard_state.run_background_turn(
            turn_slot,
            _run_dashboard_turn(),
        )
    task = spawn_guarded_turn(dashboard_state, turn_slot, turn_coro)
    # Mirror dashboard /api/chat/send path so slot.running == True and sidebar
    # shows the "turn active" three-dots indicator immediately.
    slot.task = task
    if completion_hook is not None:
        # A structured wake writes its row only once the runner accepts the
        # turn, so the push below shows the slot running while the newest
        # opener is still an older row. Mark the turn as a starting wake
        # first: that same frame then says who is opening it.
        slot._wake_starting_task = task
    self._session_tasks[slot.key] = task
    self.dashboard_state.push_slots_update()
    if admission is not None:

        def _settle_unstarted_admission(_task: asyncio.Task[Any]) -> None:
            _settle_admission(MonitorDispatchResult.BUSY)

        task.add_done_callback(_settle_unstarted_admission)
        return _delivery_result(wake_message, await admission)
    return _delivery_result(wake_message, MonitorDispatchResult.DISPATCHED)


def _work_ledger_wake_loop(
    self: GatewayOrchestrator, loop: NudgeLoop, wake_message: str | None
) -> bool:
    """Whether *loop* is a GATED work-ledger watch delivering its own wake.

    The one loop shape the two paths below are for. A structured monitor
    (``wake_message is not None``) composes its own envelope through its
    controller and needs neither; a plain prompt loop has no probe and so no
    news of its own to carry; a gated watch on some other subject gets its text
    from that subject's probe and is not this module's to reshape.
    """
    if wake_message is not None:
        return False
    monitor = getattr(loop, "monitor", None)
    if monitor is None or not getattr(loop, "gate", False):
        return False
    from kiro_crew import probes

    return str(getattr(monitor, "kind", "")) == probes.WORK_LEDGER


async def _work_ledger_wake_text(self: GatewayOrchestrator, loop: NudgeLoop) -> str:
    """The text a work-ledger wake delivers for *loop*, or ``""``.

    Which items moved, as the kernel's briefs named them, and then the pointer
    telling the conductor to read the ledger for itself.
    ``ledger_wake.wake_turn_text`` owns the shape; this method owns where the
    briefs come from.

    The briefs come from the loop's OWED wake claim, which is the only holder of
    them: the kernel composed them into the verdict the gate read, and a gated
    loop's fire path has no envelope to carry a verdict. Read rather than taken,
    so a fire this text is refused by leaves it owed to the retry.

    A POINTER, NOT THE BOARD, and that is a cost argument. Inlining the compact
    board makes one item moving cost a payload proportional to the whole goal:
    tens of kilobytes of mostly-closed rows on a twenty-item ledger, re-sent per
    wake to save one tool call. It also makes the text ledger prose, which may
    enter only a turn whose cross-surface publication is fenced for that turn's
    whole life -- so a board cannot ride the steer, and the two delivery routes
    then carry different text. The pointer is constant in size, discloses
    nothing, and leaves the board to the conductor's own ``work_ledger_read``:
    identity-gated, audited and containment-judged at the moment of the read, by
    the route that already owns all three.

    Empty when there is nothing owed, which leaves the plain nudge untouched: a
    quiet-streak floor tick, a fallback, or a retry whose claim was released
    observed no news, so there is no item to point AT. It also must not give the
    steer below something to inject -- such a tick would interrupt a turn the
    user is driving to say nothing, and charge a cycle for it.

    The claim is the only input, so there is no dashboard check: nothing here
    reads the dashboard, and the caller returns before this on a gateway without
    one.
    """
    service = self.autonudge_svc
    if service is None:
        return ""
    reader = getattr(service, "pending_wake_briefs", None)
    briefs = str(reader(loop.id)) if callable(reader) else ""
    if not briefs:
        return ""
    from kiro_crew import ledger_wake

    return ledger_wake.wake_turn_text(briefs)


async def _steer_work_ledger_wake(
    self: GatewayOrchestrator, loop: NudgeLoop, slot: Any, text: str
) -> bool:
    """Inject *text*, *loop*'s pending wake, into the turn *slot* is running.

    THE FIX for a wake that lands mid-turn. The fire below declines a running
    slot, and declining is right for a plain nudge -- a multi-kilobyte
    instruction payload queued behind a turn would stack identical copies and
    blow out the context window. A work-ledger wake is the opposite shape: a
    line naming the item that moved plus a line saying where to read it, which
    is exactly what a steer is for. So rather than hold it until the turn ends,
    the news goes INTO the turn that is running.

    Authorization is the fire's own, unchanged. ``_dashboard_mode_admits`` is
    checked here because the ordinary path checks it only past the running-slot
    return, and a steer must not become the way around it. Nothing else is
    needed: the loop is already authorized to start a WHOLE TURN on this slot
    with this text, and a steer delivers strictly less -- same session, smaller
    payload, no new turn -- so it cannot reach anywhere the fire could not.

    The containment snapshot is recorded for the REQUEUE. A turn that ends while
    the steer RPC is suspended degrades the text to a queue card, and the drain
    re-checks that card against the containment that held when it was admitted;
    an entry carrying no stamp falls to the drain's fail-closed floor instead.
    ``user_origin=False``, because nobody typed this into the session's own
    surface, so it must not inherit a human's exemption from the drain's linked
    drop.

    IT CARRIES THE SAME TEXT THE FIRE DOES, and the wake being a pointer is what
    makes that safe. Worker-authored ledger prose may enter only a turn whose
    cross-surface publication this gateway has fenced for that turn's WHOLE life,
    and a turn already running stops being such a turn the moment its text is
    requeued -- the teardown clears the fence and the drain starts a successor
    nothing re-fences. A steer can always be requeued, which is what this
    method's own ``STEER_REQUEUED`` arm exists for, so a board could not ride it
    and this delivery would have to be the smaller one. It is not: the text is
    ``ledger_wake.wake_brief`` lines plus a ``work_ledger_read`` pointer, the
    same text ``irq`` is already allowed to persist outside the store's
    identity-gated read path, so the two routes cannot diverge.

    *text* is passed in rather than built here because the caller has already
    built it for the other delivery, and both must deliver the same string.

    ``False`` for every refusal -- a mode that declines, a slot with no
    steer-capable client, a backend that queued the text instead of consuming it
    -- and the caller then declines the fire exactly as it did before, leaving
    the wake owed and the turn-end retry to deliver it.
    """
    state = self.dashboard_state
    if state is None or not text or bool(getattr(slot, "is_closing", False)):
        return False
    if not await self._dashboard_mode_admits(loop, slot):
        await self._audit_fire_refused(loop, slot)
        return False
    # Deferred import for the cycle the sibling fire path documents: gateway ->
    # dashboard.chat_delivery -> gateway.
    from kiro_crew.dashboard import session_deliver
    from kiro_crew.dashboard.session_control import containment_meta

    try:
        admission = containment_meta(state, slot)
    except Exception:
        # A containment this cannot read is not one to steer past. Declining
        # costs the wake its place in THIS turn; the turn-end retry still
        # delivers it, so the cheap failure is to fall back to the fire.
        logger.debug(
            "AutoNudge: could not read containment for loop %s -- not steering",
            loop.id,
            exc_info=True,
        )
        return False
    # NO AUDIENCE FENCE HERE, and its absence is the point rather than an
    # omission: this delivery carries structural briefs and a read pointer only,
    # so there is nothing a fence would withhold. A fence recorded on the running
    # turn would also be the wrong promise -- the teardown clears it, and a
    # requeued steer's successor would run without it.
    try:
        # The wake names itself as this loop's automation; the session turns
        # that into the steer and the nudge row. The cycle is the one this
        # delivery charges.
        outcome = await session_deliver.deliver(
            state,
            slot,
            text,
            session_deliver.automation(loop.cycle_count + 1, loop.id),
            admission=admission,
        )
    except Exception:
        logger.warning(
            "AutoNudge: steering loop %s's work-ledger wake raised -- " "leaving the wake owed",
            loop.id,
            exc_info=True,
        )
        return False
    if outcome == session_deliver.DELIVERED_REQUEUED:
        # The turn ended while the RPC was suspended, so the teardown degraded
        # the text to a queue card at the HEAD of the slot queue. It runs, as
        # its own turn, and the drain's own notice covers the one case it does
        # not. So the news is delivered and the claim must be discharged:
        # reporting a refusal here would re-owe these briefs, the turn-end retry
        # would deliver them a second time on another charged turn, and on the
        # possibly-delivered path ``chat_delivery`` forbids resending the text.
        logger.info(
            "AutoNudge: loop %s's work-ledger wake was requeued as its own turn "
            "on slot %s; the wake is discharged",
            loop.id,
            loop.slot_key,
        )
        return True
    if outcome != session_deliver.DELIVERED_STEERED:
        logger.info(
            "AutoNudge: loop %s could not steer its work-ledger wake into the "
            "running turn (%s); the wake stays owed",
            loop.id,
            outcome,
        )
        return False
    logger.info(
        "AutoNudge: loop %s steered its work-ledger wake into the turn already "
        "running on slot %s",
        loop.id,
        loop.slot_key,
    )
    return True


async def _dashboard_mode_admits(loop: NudgeLoop, slot: Any) -> bool:
    """Whether *slot*'s mode admits a wake from *loop*.

    Any mode but crew/member admits. A crew/member slot refuses a wake armed
    from OUTSIDE the session; a loop the slot's OWN turn armed is the
    member keeping itself awake and must fire. Same rule as
    ``autonudge_authz``. TWO sources must agree, because the loop store is
    agent-writable and this is the one bit that relaxes a session boundary:
    the persisted ``self_armed`` must be the boolean True (``is True`` -- a
    forged string is truthy; ``_load`` normalises too) AND the
    keystone-gated trust record the authorizer wrote at arm time
    (``autonudge_selfarm``, unreachable by agent file tools) must name this
    loop on this slot. A forged boolean in the store has no trust entry and
    refuses. The record read is file IO, so it is offloaded.
    """
    if str(getattr(slot, "mode", "")) not in {"crew", "member"}:
        return True
    if getattr(loop, "self_armed", False) is not True:
        return False
    return bool(
        await asyncio.to_thread(autonudge_selfarm.is_recorded_self_arm, loop.id, loop.slot_key)
    )


async def _audit_fire_refused(loop: NudgeLoop, slot: Any) -> None:
    """SEL-record a fire-time refusal at the crew/member boundary.

    A permission decision that keeps an unattended turn OUT of a session is
    as audit-worthy as the arm that let one in (backend-security-controls):
    without it an operator reading the trail sees a loop armed and never
    fired, with nothing saying why. Best-effort and offloaded -- the
    refusal stands whether or not the write lands.
    """
    mode = str(getattr(slot, "mode", ""))
    try:
        await asyncio.to_thread(
            lambda: sel().log_tool_invocation(
                session_key=loop.slot_key,
                source="autonudge",
                tool_name="monitor_fire",
                outcome="denied",
                error=f"{mode}-mode session refuses a wake it did not arm itself",
                metadata={
                    "loop_id": loop.id,
                    "self_armed_bit": getattr(loop, "self_armed", False) is True,
                },
            )
        )
    except Exception:  # noqa: BLE001 - auditing must never break the fire path
        logger.warning("fire-refusal SEL audit failed for loop %s", loop.id, exc_info=True)

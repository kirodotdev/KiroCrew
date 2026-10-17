"""Build the sub-agent manager and the callbacks it reports through.

``_init_subagents`` snapshots the recovery ladder and wires the manager's
callbacks: the status and event frames, spawn approval, the orphan notices, and
``_subagent_done``, the one consumer every terminal report goes through. That
consumer resolves the parent's tab, composes the report, peeks at the wave, and
hands the completion to the wave digest and then to its delivery route. This
module also owns the manager reads made off the loop, the scale coalescer, and
which slot governs a workflow step's spawn.

Orchestrator fields read: ``_background_tasks``, ``_batch_progress``, ``_cfg``,
``_owner_id``, ``ctx_builder``, ``dashboard_state``, ``sessions``, ``slack``.
Orchestrator fields written: ``_subagent_coalescer_inst``, ``subagent_mgr``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio

    from kiro_crew.slack.gateway import (
        ORPHAN_NOTICE_OWED,
        OUTCOME_FAILED,
        SUBAGENT_COMPLETION_META_KEY,
        SUBAGENT_COMPLETION_PREFIX,
        Any,
        GatewayOrchestrator,
        LLMEvent,
        SubagentDelivery,
        SubagentEventCoalescer,
        SubagentInfo,
        SubagentManager,
        _ChatSlot,
        _compose_subagent_completion,
        _injection_notice_outcome,
        _queued_depth_off_loop,
        bounded_chat_turn,
        dashboard_slot_key,
        deliver_spawn_approval,
        drain_idle_parent_queue_for_stall,
        format_subagent_usage,
        load_window_off_loop,
        logger,
        redact_and_truncate,
        redact_credentials,
        redact_exfiltration_urls,
        resolve_max_subagents,
        safety_override,
        single_completion_meta,
        subagent_event_slot,
    )


async def _subagent_work_pending(manager: Any, parent_session_key: str) -> bool:
    """Whether *parent_session_key* still has sub-agents running or QUEUED.

    Asked through ``SubagentManager.has_pending_work_for_async``, whose store
    ``count_pending`` runs on the task store's writer thread: the synchronous
    entry takes the SQLite connection on this loop, and a wait there freezes
    every session's turn behind it. A manager double without the async sibling
    is asked synchronously -- the pre-queue behaviour those doubles model, and
    the same probe ``dashboard.handlers.messaging._spawn_on_loop`` makes for
    ``spawn_async``.
    """
    import inspect

    entry = getattr(manager, "has_pending_work_for_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(parent_session_key))
    return bool(manager.has_pending_work_for(parent_session_key))


async def _subagent_queued_count(manager: Any, parent_session_key: str) -> int:
    """This parent's QUEUED spawn count, read off-loop like
    :func:`_subagent_work_pending` (which also counts the running ones)."""
    return await _queued_depth_off_loop(manager, parent_session_key)


async def _subagent_batch_pending(manager: Any, batch_id: str) -> bool:
    """Whether any member of *batch_id* is still outstanding, read off-loop for
    :func:`_subagent_work_pending`'s reason: the store half of
    ``batch_members_pending`` is a ``fetch_pending_by_batch`` on the connection
    this loop would otherwise block on."""
    import inspect

    entry = getattr(manager, "batch_members_pending_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(batch_id))
    return bool(manager.batch_members_pending(batch_id))


#: Session-key prefixes of a workflow RUN's step sessions, each followed by
#: ``<run_id>:``: per-call (``wf:``), pooled worker, unpooled call, and the
#: memory-scoped worker key.
_WORKFLOW_STEP_PREFIXES = ("wf:", "wf-pool:", "wf-unpooled:", "wf-worker:")


def _spawn_parent_slot(parent_key: str, workflow_service: Any) -> str:
    """The dashboard slot whose Trust governs a spawn parented on *parent_key*.

    A workflow step runs in its own ``<prefix>:<run_id>:...`` session, which no
    tab displays. Its spawns answer to the chat that STARTED the run: the registry
    records that origin ``session_key`` only after the run route proved the
    caller is that session. An unknown run or a blank origin yields ``""``, so
    the spawn prompts exactly as before.
    """
    if not parent_key:
        return ""
    if not parent_key.startswith(_WORKFLOW_STEP_PREFIXES):
        return subagent_event_slot(parent_key)
    run_id = parent_key.split(":", 2)[1]
    registry = getattr(workflow_service, "registry", None)
    try:
        handle = registry.get(run_id) if run_id and registry is not None else None
    except Exception:
        logger.warning("spawn slot: workflow run %s lookup failed", run_id, exc_info=True)
        return ""
    origin = str(getattr(handle, "session_key", "") or "")
    return subagent_event_slot(origin) if origin else ""


def _subagent_coalescer(self: GatewayOrchestrator) -> "SubagentEventCoalescer":
    """Lazily construct the scale coalescer (needs dashboard_state +
    subagent_mgr, both wired after __init__)."""
    if self._subagent_coalescer_inst is None:
        from kiro_crew.subagent_scale import SubagentEventCoalescer

        _state = self.dashboard_state

        def _bcast_all(t: str, d: dict) -> None:
            if _state:
                _state.broadcast_ws(t, d)

        def _bcast_subs(t: str, d: dict) -> None:
            if _state:
                _state.broadcast_ws_subagent_subscribers(t, d)

        self._subagent_coalescer_inst = SubagentEventCoalescer(
            _bcast_all,
            _bcast_subs,
            lambda: self.subagent_mgr.running_count if self.subagent_mgr else 0,
        )
    return self._subagent_coalescer_inst


def _init_subagents(self: GatewayOrchestrator) -> None:
    """Initialize the subagent manager."""
    from kiro_crew.recovery.ladder import configure_default_ladder

    # The process recovery ladder is snapshotted from config HERE because
    # this step is unconditional and runs before the task runner, the
    # dashboard and any chat slot — i.e. before any layer can decide a
    # retry, and outside the event-loop failure branch that would otherwise
    # be the first to want a delay. `agent.recovery_backoff_*` are
    # restart=True, so one snapshot is the whole contract.
    configure_default_ladder(self._cfg)

    # Per-slot WS events route by EXACT slot-key match in the frontend —
    # `subagent_event_slot` maps a parent session key to the tab that
    # displays it (cron-born tabs are `cron-<id>`, channel-born tabs their
    # transcript stem), falling back to the legacy prefix-strip when no
    # tab is open. A raw `removeprefix("dashboard:")` here left the
    # Subagents panel permanently empty for cron/channel-born sessions.
    _event_slot = subagent_event_slot

    async def _broadcast_subagent_status(
        info: SubagentInfo,
        event: str,
        selected_slot_name: str = "",
    ) -> None:
        """Broadcast subagent status change via WS for per-slot tracking."""
        if not self.dashboard_state:
            return
        try:
            slot = selected_slot_name or _event_slot(info.parent_session_key)
            agents = (
                self.subagent_mgr.running_agents_for(info.parent_session_key)
                if self.subagent_mgr
                else []
            )
            running = len(agents)
            payload = {
                "running": running,
                "id": info.id,
                "event": event,
                "slot": slot,
                "agents": agents,
            }
            logger.info(
                "📡 subagent_status WS: event=%s slot=%s running=%d agents=%d",
                event,
                slot,
                running,
                len(agents),
            )
            self.dashboard_state.broadcast_ws("subagent_status", payload)
        except Exception:
            logger.info("Failed to broadcast subagent %s status", info.id, exc_info=True)

    def _retrigger_recovery(slot: "_ChatSlot", parent_key: str) -> None:
        """Drain queued failures into a new recovery _run_chat turn.

        Called from _on_done callbacks after resetting the guard, so
        failures that arrived while the previous recovery was running
        get processed without waiting for user input.
        """
        if slot._recovery_chat_triggered or not slot._pending_subagent_failures:
            return
        if not self.dashboard_state:
            return
        _max_retrigger = 3
        if slot.recovery_retrigger_count >= _max_retrigger:
            logger.warning(
                "Recovery retrigger cap (%d) reached for %s, dropping %d queued failures",
                _max_retrigger,
                parent_key,
                len(slot._pending_subagent_failures),
            )
            slot._pending_subagent_failures.clear()
            return
        slot.recovery_retrigger_count += 1
        slot._recovery_chat_triggered = True
        # Bound here rather than at module scope: this reads ``_run_chat`` from
        # ``dashboard.chat``, a different module than the top-level
        # ``chat_runner`` import, and resolving it per call is what lets a test
        # patch ``dashboard.chat._run_chat`` and have this path observe it.
        from kiro_crew.dashboard.chat import _run_chat

        failures = slot._pending_subagent_failures[:]
        slot._pending_subagent_failures.clear()
        msg = "\n\n".join(failures)
        msg, _ = redact_exfiltration_urls(msg)
        msg, _ = redact_credentials(msg)
        slot.append("user", msg, "msg msg-u auto-go")
        logger.info(
            "Re-triggering recovery _run_chat for %s (%d queued failures)",
            parent_key,
            len(failures),
        )

        def _done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
            if t.cancelled():
                logger.warning("Re-triggered recovery cancelled for %s", parent_key)
                slot._recovery_chat_triggered = False
                return
            elif t.exception():
                logger.error(
                    "Re-triggered recovery failed for %s",
                    parent_key,
                    exc_info=t.exception(),
                )
            slot._recovery_chat_triggered = False
            if slot._pending_subagent_failures:
                _retrigger_recovery(slot, parent_key)

        _task = asyncio.create_task(
            bounded_chat_turn(
                _run_chat(
                    self.dashboard_state,
                    slot,
                    msg,
                    _directive_user_origin=False,
                    # The text is the re-injected sub-agent completion or
                    # failure this recovery drains, so the sub-agent is what
                    # caused the turn.
                    _turn_actor="subagent",
                ),
                state=self.dashboard_state,
                slot=slot,
            ),
        )
        slot.task = _task
        self._background_tasks.add(_task)
        _task.add_done_callback(self._background_tasks.discard)
        _task.add_done_callback(_done)

    async def _subagent_done(info: SubagentInfo) -> None:
        # A synthetic flush-only record is NOT a wave member (see
        # SubagentManager.force_digest_flush): it exists only to force the
        # wave's pending digest chunk out when its hold deadline expired.
        # Every per-member side effect below must be skipped for it — a
        # terminal WS event, orchestration accounting or a done/ok counter
        # bump would invent an agent that never ran.
        _flush_only = getattr(info, "_digest_flush_only", False) is True
        parent_key = info.parent_session_key
        _parent_slot_name = dashboard_slot_key(parent_key)

        _injection_slot = None
        if self.dashboard_state and _parent_slot_name:
            from kiro_crew.dashboard.handlers.messaging import _slot_for_parent

            _injection_slot = _slot_for_parent(self.dashboard_state, parent_key)
            if _injection_slot is None:
                _injection_slot = self.dashboard_state.get_slot(_parent_slot_name)
            if getattr(_injection_slot, "window_pending", False) is True:
                # A parent tab restored as a sidebar row loads its window
                # before the completion is delivered into it, off the loop.
                await load_window_off_loop(_injection_slot)
                _injection_slot = _slot_for_parent(
                    self.dashboard_state, parent_key
                ) or self.dashboard_state.get_slot(_parent_slot_name)
        _completion_key = getattr(_injection_slot, "key", "")
        _injection_slot_name = (
            _completion_key
            if isinstance(_completion_key, str) and _completion_key
            else _event_slot(parent_key)
        )

        if not _flush_only:
            await _broadcast_subagent_status(info, "done", _injection_slot_name)
        report = _compose_subagent_completion(info, _flush_only=_flush_only)

        # ── Batch accounting + wave digest (scale plumbing) ──
        # Every batch member is accounted here (the single completion
        # consumer for all terminal paths). Waves larger than the digest
        # threshold deliver ONE consolidated injection turn when the wave
        # finishes, instead of N per-agent turns — at 60-100 agents the
        # per-agent turns are the parent-context flood (N full LLM turns)
        # and bury the 2 failures among 58 successes.
        # (Type guards: test doubles pass MagicMock infos whose attrs are
        # truthy mocks — only real str/int batch identity participates.)
        _batch_id = getattr(info, "batch_id", "")
        _batch_total = getattr(info, "batch_total", 0)
        if not isinstance(_batch_id, str):
            _batch_id = ""
        if not isinstance(_batch_total, int):
            _batch_total = 0
        if _flush_only and not _batch_id:
            # A flush-only record without wave identity has nothing to
            # release and MUST NOT fall through to the per-agent routing
            # below — that would inject a completion turn for an agent that
            # never ran.
            return
        # The one await on the wave-accounting path runs BEFORE this
        # member touches the wave record. Everything from reading
        # ``_batch_progress`` to the hold-or-flush decision below is then a
        # single synchronous block, so a sibling completion can never close
        # the wave between this member writing its result line and deciding
        # how that line is delivered: the closer's settle set always carries
        # every member its digest names. While this member awaits, its own
        # report hold is still armed, so no sibling closes the wave on it.
        # The lookup is skipped when this member's count alone closes the
        # wave; nothing can interleave between the peek and the block.
        _no_batch_pending = False
        if _batch_id and not _flush_only and self.subagent_mgr:
            _peek = self._batch_progress.get(_batch_id)
            _peek_done = _peek["done"] if _peek is not None else 0
            if not (_batch_total > 0 and _peek_done + 1 >= _batch_total):
                try:
                    _members = await _subagent_batch_pending(self.subagent_mgr, _batch_id)
                    _no_batch_pending = not _members
                except Exception:
                    _no_batch_pending = False
        wave = self._subagent_wave_digest(
            info,
            report=report,
            _flush_only=_flush_only,
            _batch_id=_batch_id,
            _batch_total=_batch_total,
            _no_batch_pending=_no_batch_pending,
            _injection_slot_name=_injection_slot_name,
        )
        if wave is None:
            return

        # ── Route completion back to the originating session ──
        # Tab open        → that tab (a channel-born tab mirrors on to its channel)
        # Channel, no tab → channel thread + dashboard notification
        # Cron/no parent  → dashboard notification only

        if _parent_slot_name and self.dashboard_state:
            await self._route_subagent_to_tab(
                info,
                announce=wave.announce,
                body=report.body,
                title=report.title,
                sub_meta=wave.sub_meta,
                _flush_only=_flush_only,
                _injection_slot=_injection_slot,
                _injection_slot_name=_injection_slot_name,
                parent_key=parent_key,
            )
            return

        if parent_key and not parent_key.startswith(("cron:", "subagent:")):
            await self._inject_subagent_into_channel_parent(
                info,
                announce=wave.announce,
                body=report.body,
                title=report.title,
                parent_key=parent_key,
            )
            return

        await self._finish_subagent_cron_or_notify(
            info,
            announce=wave.announce,
            body=report.body,
            title=report.title,
            parent_key=parent_key,
        )

    assert self.sessions is not None
    assert self.ctx_builder is not None

    def _is_yolo() -> bool:
        return safety_override().is_active()

    def _spawn_slot_resolver(request_id: str) -> str:
        """Resolve slot from spawn request_id (spawn:{agent_id})."""
        agent_id = request_id.removeprefix("spawn:")
        info = self.subagent_mgr.get(agent_id) if self.subagent_mgr is not None else None
        # A workflow step's spawn resolves to the run's origin chat.
        slot = _spawn_parent_slot(
            info.parent_session_key if info else "",
            getattr(self.dashboard_state, "workflow_service", None),
        )
        logger.info(
            "_spawn_slot_resolver: rid=%s agent_id=%s info=%s slot=%s",
            request_id,
            agent_id,
            info is not None,
            slot,
        )
        return slot

    _approve_subagent = self._interactive_approval("subagent", slot_resolver=_spawn_slot_resolver)
    # A SECOND instance of the same callback, differing only in the
    # unreachable behaviour. It is a separate closure because the one above is
    # also wired as ``on_tool_approval``: a mid-run tool prompt has no
    # terminal path that could report the refusal, so raising there would turn
    # a recoverable park into a lost turn.
    _approve_spawn_gate = self._interactive_approval(
        "subagent", slot_resolver=_spawn_slot_resolver, raise_when_unreachable=True
    )

    async def _spawn_approve(
        request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        # Channel-side delivery FIRST. A spawn parented on
        # a live channel conversation (Telegram, …) is best answered where the
        # human already is, with that channel's own Approve/Deny/Trust
        # keyboard. The seam returns True/False when the channel surfaced the
        # prompt and got a press; None means no channel hook owns this session,
        # or the hook could not surface it here — either way, fall through to
        # the unchanged Slack-DM/dashboard gate below (which still raises
        # SpawnApprovalUnreachable when no surface is attached).
        channel_decision = await deliver_spawn_approval(request_id, description, parent_session_key)
        if channel_decision is not None:
            return channel_decision
        event = LLMEvent(kind="permission_request", request_id=request_id, title=description)
        return await _approve_spawn_gate(event, parent_session_key)

    # Debounced slots push: keep slots[].subagents_running and
    # slots[].subagents_queued live for every
    # SSE consumer (composer busy affordance, Board "working" lane, and
    # external readers of the slots stream). Without this, the field is
    # only fresh on a full GET — serialize_slots() computes it at call
    # time but nothing pushed on sub-agent lifecycle transitions. The
    # 0.2s coalesce window collapses batch spawns into one push. Covers
    # the reaper too: _force_reap fires subagent_done through the same
    # on_event path.
    _slots_push_pending = False

    def _flush_slots_push() -> None:
        nonlocal _slots_push_pending
        _slots_push_pending = False
        if self.dashboard_state:
            self.dashboard_state.push_slots_update()

    def _schedule_slots_push() -> None:
        nonlocal _slots_push_pending
        if _slots_push_pending:
            return
        _slots_push_pending = True
        asyncio.get_running_loop().call_later(0.2, _flush_slots_push)

    def _tab_queued_depth(parent_key: str, slot_name: str, extra: dict) -> object:
        """The queued depth a ``subagent_queued`` frame carries: its tab's sum.

        Several parents can route to one tab (a cron tab's ``cron:<job>:<run>``
        and ``cron:<job>:<agent>`` runs). The client reducer sets the tab's
        count from each frame, and the slot list sums the published depths by
        tab, so a frame carrying only its own parent's depth would disagree
        with the next slots push and the count would flip between the two.
        The frame therefore carries this parent's depth plus every other
        parent's published depth on the same tab. A non-int depth is passed
        through unchanged for the client to ignore.
        """
        own = extra.get("queued")
        if not isinstance(own, int) or isinstance(own, bool) or not self.subagent_mgr:
            return own
        total = max(own, 0)
        for other, depth in self.subagent_mgr.published_queued_depths().items():
            if other != parent_key and _event_slot(other) == slot_name:
                total += depth
        return total

    async def _subagent_event(etype: str, info: SubagentInfo, extra: dict) -> None:
        if not self.dashboard_state:
            return
        slot_name = _event_slot(info.parent_session_key)
        base = {"id": info.id, "slot": slot_name}
        # Batch identity rides every frame when present so the UI can
        # group/aggregate a wave without a lookup table. (Type guard:
        # test doubles pass MagicMock infos.)
        _ebid = getattr(info, "batch_id", "")
        if isinstance(_ebid, str) and _ebid:
            base["batch_id"] = _ebid
        if etype == "subagent_queued":
            extra = {
                **extra,
                "queued": _tab_queued_depth(info.parent_session_key, slot_name, extra),
            }
        if etype == "subagent_injection_failed":
            # Show error in UI + queue for LLM context on next turn.
            slot = self.dashboard_state.get_slot(slot_name)
            if slot is not None and getattr(slot, "window_pending", False) is True:
                # A parent tab restored as a sidebar row loads its window
                # before the card is appended, with the read off the loop.
                await load_window_off_loop(slot)
                slot = self.dashboard_state.get_slot(slot_name)
            if slot:
                task_preview = redact_and_truncate(info.task or "", 100)
                error_text, _ = redact_exfiltration_urls(extra.get("error", "timed out"))
                error_text, _ = redact_credentials(error_text)
                # The visible transcript card must state the run's real
                # outcome, same as the queued LLM copy: this event fires for
                # every terminal state whose report could not be injected,
                # not only successful completions.
                outcome_line = _injection_notice_outcome(info)
                usage = format_subagent_usage(info.credits, info.elapsed)
                slot.append(
                    "assistant",
                    f"{SUBAGENT_COMPLETION_PREFIX}\n"
                    f"Agent `{info.id}` ❌\n"
                    f"Task: {task_preview}\n\n"
                    f"Usage: {usage}\n\n"
                    f"Error: {error_text}\n"
                    f"⚠️ Result delivery failed — {outcome_line}",
                    "msg msg-a",
                    meta={
                        SUBAGENT_COMPLETION_META_KEY: single_completion_meta(
                            agent_id=info.id,
                            outcome=OUTCOME_FAILED,
                            agent_name=info.agent or "",
                            task=task_preview,
                            requested_model=info.requested_model or info.model or "",
                            resolved_model=info.resolved_model or "",
                        )
                    },
                )
                # Queue failure for LLM context drain
                failure_msg = extra.get("failure_msg", "")
                if failure_msg:
                    failure_msg, _ = redact_exfiltration_urls(failure_msg)
                    failure_msg, _ = redact_credentials(failure_msg)
                    slot._pending_subagent_failures.append(failure_msg)
                    # A completed run keeps no delivered tombstone while its
                    # report is undelivered, and the drain that hands this
                    # notice to the parent writes it, keyed on the text.
                    if info.outcome == "completed":
                        try:
                            slot.note_pending_subagent_delivery(
                                failure_msg,
                                [SubagentDelivery(info.id, info.elapsed, info.credits)],
                            )
                        except Exception:
                            logger.debug(
                                "Subagent %s: could not record the notice's delivery",
                                info.id,
                                exc_info=True,
                            )
                self.dashboard_state.push_slots_update()
                logger.warning(
                    "Injected timeout error for subagent %s into slot %s", info.id, slot_name
                )
            self.dashboard_state.broadcast_ws(etype, {**base, **extra})
        elif etype == "subagent_chunk":
            # Heavy data — only to subscribed clients. At scale (>threshold
            # active agents) the coalescer absorbs it into the ~1s
            # subagent_batch_chunks frame instead of a per-event frame.
            if self._subagent_coalescer().handle(etype, {**base, **extra}):
                return
            self.dashboard_state.broadcast_ws_subagent_subscribers(etype, {**base, **extra})
        else:
            # Lightweight status events — broadcast to all. High-frequency
            # deltas (tool/stalled/retrying) coalesce at scale into ONE
            # subagent_batch_update frame per tick; lifecycle events
            # (spawn/done/recovering/batch_*) always pass through.
            #
            # A child the reaper flagged stalled does not hold the parent's
            # user messages (``subagents_hold_user_messages``), yet nothing
            # else starts that drain: a message queued BEFORE the flag is set
            # stays queued until the next send or the wall-clock reap. Kick
            # the parent slot's drain here — before the coalescer can absorb
            # the UI frame — so the parked message starts now. The drain
            # re-reads the hold under the slot lock, so a live sibling keeps
            # the queue parked and a running slot is left alone.
            if etype == "subagent_stalled" and extra.get("stalled"):
                _drain = asyncio.ensure_future(
                    drain_idle_parent_queue_for_stall(self.dashboard_state, info.parent_session_key)
                )
                self.dashboard_state._background_tasks.add(_drain)
                _drain.add_done_callback(self.dashboard_state._background_tasks.discard)
            if self._subagent_coalescer().handle(etype, {**base, **extra}):
                return
            self.dashboard_state.broadcast_ws(etype, {**base, **extra})
            # subagents_running flips truth value exactly at spawn/done, and
            # subagents_queued changes with every queued-depth frame — push
            # (debounced) so slots-stream consumers stay live, and a client
            # that missed a depth frame is corrected by the next push.
            if etype in ("subagent_spawn", "subagent_done", "subagent_queued"):
                _schedule_slots_push()

    async def _orphan_notify(
        parent_session: str,
        msg: str,
        meta: dict | None = None,
        delivery: SubagentDelivery | None = None,
    ) -> bool | str:
        """Inject an orphan notification into the parent dashboard slot.

        Mirrors the subagent_injection_failed delivery: visible transcript
        card + queue into ``slot._pending_subagent_failures`` so the LLM
        drains it (as a digest with any other pending failures) on its next
        turn. Returns False when the slot is gone, so the manager
        falls through to the owner-DM path. ``msg`` is redacted by the
        manager before delivery; re-redact defensively anyway.

        ``meta`` carries the structured completion facts so the
        orphan row renders as a card without re-parsing its prose header.
        *delivery* is the tombstone the notice owes. It is recorded on the
        slot's ledger under the queued text, for the drain of that text to
        write, and ``ORPHAN_NOTICE_OWED`` tells the manager it was taken.
        """
        # Deliver to whichever tab shows the parent conversation, including a
        # channel-born one; only a parent with no tab wants the owner DM.
        slot_name = dashboard_slot_key(parent_session)
        if not self.dashboard_state or not slot_name:
            return False
        slot = self.dashboard_state.get_slot(slot_name)
        if slot is not None and getattr(slot, "window_pending", False) is True:
            # A parent tab restored as a sidebar row loads its window before
            # the notice is appended, with the read off the loop.
            await load_window_off_loop(slot)
            slot = self.dashboard_state.get_slot(slot_name)
        if not slot:
            return False
        safe_msg, _ = redact_exfiltration_urls(msg)
        safe_msg, _ = redact_credentials(safe_msg)
        slot.append(
            "assistant",
            safe_msg,
            "msg msg-a",
            meta={SUBAGENT_COMPLETION_META_KEY: meta} if meta else None,
        )
        slot._pending_subagent_failures.append(safe_msg)
        owed = False
        if delivery is not None:
            try:
                slot.note_pending_subagent_delivery(safe_msg, [delivery])
                owed = True
            except Exception:
                logger.debug(
                    "Could not record orphan %s's notice delivery",
                    delivery.agent_id,
                    exc_info=True,
                )
        self.dashboard_state.push_slots_update()
        logger.info("Orphan notification injected into slot %s", slot_name)
        return ORPHAN_NOTICE_OWED if owed else True

    async def _orphan_dm(msg: str) -> bool:
        """Owner-DM fallback for orphan notifications (bell + Slack DM)."""
        safe_msg, _ = redact_exfiltration_urls(msg)
        safe_msg, _ = redact_credentials(safe_msg)
        delivered = False
        if self.dashboard_state:
            try:
                self.dashboard_state.notify("subagent", "Sub-agent orphaned by restart", safe_msg)
                delivered = True
            except Exception:
                logger.debug("Orphan bell notification failed", exc_info=True)
        try:
            if self.slack and self._owner_id:
                ch = await self.slack.open_dm(self._owner_id)
                if ch:
                    await self.slack.post_message(ch, safe_msg)
                    delivered = True
        except Exception as exc:
            logger.warning("Failed to send orphan notification to Slack DM: %s", exc)
        return delivered

    self.subagent_mgr = SubagentManager(
        sessions=self.sessions,
        ctx_builder=self.ctx_builder,
        on_done=_subagent_done,
        max_concurrent=resolve_max_subagents(self._cfg),
        default_turn_limit=self._cfg.agent.subagent_max_turns,
        default_timeout=self._cfg.agent.subagent_timeout_secs,
        stall_idle_secs=self._cfg.agent.subagent_stall_idle_secs,
        on_tool_approval=_approve_subagent,
        on_spawn_approval=_spawn_approve,
        is_yolo=_is_yolo,
        on_event=_subagent_event,
        on_orphan_notify=_orphan_notify,
        on_orphan_dm=_orphan_dm,
        completion_keep=self._cfg.agent.completion_keep,
        completion_keep_chars=self._cfg.agent.completion_keep_chars,
        # Rows that survived the restart are claimed by the manager's pump,
        # which the reaper start below kicks as soon as the loop yields --
        # that yield is ``run()``'s memory barrier. Hold the pump until
        # ``_start_subagent_dispatch_after_memory_ready`` opens it.
        defer_queue_dispatch=True,
    )
    # A parent that ends takes its children with it, on every backend. The
    # session lifecycle owns the boundary and drives both halves at each of its
    # parent-end paths, so no surface that closes, resets, discards or retires a
    # conversation needs a cancel call of its own. The manager is passed whole
    # rather than as two bound methods because the halves have to agree about
    # which runs they are talking about.
    self.sessions.set_child_teardown_handler(self.subagent_mgr)
    self.subagent_mgr.start_reaper()

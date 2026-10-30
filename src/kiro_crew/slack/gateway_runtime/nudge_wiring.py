"""Build and start the AutoNudge service.

The fire and monitor dispatchers, the observer that persists and publishes loop
transitions, the wake judge's evidence readers, and the notices that tell the user a
loop is held or stopped at a terminal bound.

Orchestrator fields read: ``_background_tasks``, ``dashboard_state``.
Orchestrator fields written: ``autonudge_svc``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import time

    from kiro_crew.slack.gateway import (
        APPROVAL_STALL_REASON,
        CONSECUTIVE_FAILURE_REASON,
        DEFAULT_MONITOR_STALL_MIN_SECS,
        DEFAULT_MONITOR_STALL_TICKS,
        FINISHED_LOOP_REASONS,
        MODEL_REFUSED_REASON,
        MONITOR_CHANNEL,
        MONITOR_STOP_APPROVAL_STALL,
        MONITOR_STOP_COMPLETION_UNAVAILABLE,
        MONITOR_STOP_INVALID_RECORD,
        MONITOR_STOP_SESSION_UNAVAILABLE,
        MONITOR_STOP_UNSUPPORTED_VERSION,
        MONITOR_STOP_VERDICT_STALL,
        MONITOR_TERMINAL_REASON,
        Any,
        AutoNudgeService,
        GatewayOrchestrator,
        MonitorDispatchResult,
        MonitorOutcome,
        NudgeLoop,
        _redact_monitor_value,
        autonudge_enabled,
        data_home,
        is_channel_key,
        is_structured_monitor_loop,
        logger,
        monitor_state_public_dict,
        reason_in,
        redact_credentials,
        redact_exfiltration_urls,
        runtime_budget_exceeded,
        terminal_notification_delivery_matches,
    )


async def _init_autonudge(self: GatewayOrchestrator) -> None:
    """Initialize and start the auto-nudge service (feature-flagged)."""
    if not autonudge_enabled():
        logger.info("AutoNudge disabled via feature flag")
        return

    # Keep the disabled gateway boot path free of controller and provider
    # imports. Provider adapters load credentials and client dependencies
    # that a gateway with automation disabled never uses.
    from kiro_crew.monitoring.controller import MonitorController

    async def _fire(loop: NudgeLoop) -> bool:
        """Inject nudge message into the bound session.

        Routes by binding-key namespace: ``slack:``/``discord:`` keys run
        an unattended turn in the channel session; bare keys are dashboard
        chat slots (original path).

        Returns True if the nudge was actually dispatched, False if skipped
        (slot missing, dashboard not ready, or turn still active). The
        service uses this to avoid counting skipped cycles toward
        max_cycles.
        """
        if is_channel_key(loop.slot_key):
            if loop.slot_key.startswith("slack:"):
                result = await self._fire_slack_nudge(loop)
                assert isinstance(result, bool)
                return result
            if loop.slot_key.startswith("discord:"):
                result = await self._fire_discord_nudge(loop)
                assert isinstance(result, bool)
                return result
            if loop.slot_key.startswith("webex:"):
                return await self._fire_webex_nudge(loop)
            logger.warning(
                "AutoNudge: unsupported channel key %s — removing loop %s",
                loop.slot_key,
                loop.id,
            )
            await self.autonudge_svc.remove(  # type: ignore[union-attr]
                loop.id, stop_reason="unsupported_channel"
            )
            return False
        result = await self._fire_dashboard_nudge(loop)
        assert isinstance(result, bool)
        return result

    async def _fire_monitor(loop: NudgeLoop, envelope: str) -> MonitorDispatchResult:
        """Route one controller-owned envelope without legacy decoration."""
        if loop.slot_key.startswith("slack:"):
            result = await self._fire_slack_nudge(loop, envelope)
            if not isinstance(result, MonitorDispatchResult):
                logger.error("Slack monitor dispatcher returned an untyped result")
                return MonitorDispatchResult.UNAVAILABLE
            return result
        if loop.slot_key.startswith("discord:"):
            result = await self._fire_discord_nudge(loop, envelope)
            if not isinstance(result, MonitorDispatchResult):
                logger.error("Discord monitor dispatcher returned an untyped result")
                return MonitorDispatchResult.UNAVAILABLE
            return result
        if is_channel_key(loop.slot_key):
            return MonitorDispatchResult.UNAVAILABLE
        result = await self._fire_dashboard_nudge(loop, envelope)
        if not isinstance(result, MonitorDispatchResult):
            logger.error("Dashboard monitor dispatcher returned an untyped result")
            return MonitorDispatchResult.UNAVAILABLE
        return result

    controller: MonitorController | None = None
    notified_monitor_terminals: set[tuple[str, MonitorOutcome, float]] = set()

    async def _record_terminal_notification_delivery(
        monitor_id: str,
        outcome: MonitorOutcome,
        stopped_at: float,
    ) -> None:
        try:
            assert self.autonudge_svc is not None
            await self.autonudge_svc.mark_terminal_notification_delivered(
                monitor_id,
                outcome,
                stopped_at,
            )
        except Exception:
            # Delivery already happened. Leaving the marker unset makes a
            # later restart retry the notice instead of losing it forever.
            logger.warning(
                "AutoNudge: could not persist terminal notification delivery for %s",
                monitor_id,
                exc_info=True,
            )

    async def _deliver_terminal_notification(
        loop: NudgeLoop,
        terminal_key: tuple[str, MonitorOutcome, float],
    ) -> None:
        notification_persisted = False
        try:
            if not self._notify_nudge_expired(loop):
                return
            assert self.dashboard_state is not None
            # notify() synchronously installs the append future before it
            # returns. Capture that exact future before yielding so another
            # notification cannot replace the state-wide pointer underneath
            # this terminal generation.
            persisted = self.dashboard_state.last_notification_persist
            if persisted is not None and not await persisted:
                logger.warning(
                    "AutoNudge: terminal notification persistence failed for %s",
                    terminal_key[0],
                )
                return
            notification_persisted = True
        except Exception:
            logger.warning(
                "AutoNudge: terminal notification persistence raised for %s",
                terminal_key[0],
                exc_info=True,
            )
            return
        finally:
            # The observer may emit the same terminal generation again.
            # Release its in-process claim until a durable notice exists.
            if not notification_persisted:
                notified_monitor_terminals.discard(terminal_key)
        await _record_terminal_notification_delivery(*terminal_key)

    def _schedule_terminal_notification(
        loop: NudgeLoop,
        terminal_key: tuple[str, MonitorOutcome, float],
    ) -> None:
        task = asyncio.create_task(_deliver_terminal_notification(loop, terminal_key))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _monitor_tick(loop: NudgeLoop) -> None:
        if controller is not None:
            await controller.tick(loop, now=time.time())

    def _observer(event: str, loop: NudgeLoop | None) -> None:
        if event == "expired" and loop is not None:
            self._notify_nudge_expired(loop)
        elif event == "held" and loop is not None:
            self._notify_nudge_held(loop)
        elif (
            loop is not None
            and is_structured_monitor_loop(loop)
            and loop.monitor is not None
            and not loop.active
        ):
            state = loop.monitor
            if state.outcome in {
                MonitorOutcome.SUCCESS,
                MonitorOutcome.BLOCKED,
                MonitorOutcome.BUDGET,
                MonitorOutcome.TARGET_UNAVAILABLE,
            }:
                terminal_key = (loop.id, state.outcome, state.stopped_at)
                if terminal_key not in notified_monitor_terminals:
                    notified_monitor_terminals.add(terminal_key)
                    _schedule_terminal_notification(loop, terminal_key)
        if self.dashboard_state and loop is not None:
            loop_payload: dict[str, Any] = {
                "id": loop.id,
                "slot_key": loop.slot_key,
                "message": loop.message,
                "idle_secs": loop.idle_secs,
                "max_cycles": loop.max_cycles,
                "max_runtime_secs": loop.max_runtime_secs,
                "created_ts": loop.created_ts,
                "cycle_count": loop.cycle_count,
                "active": loop.active,
                "last_fire_ts": loop.last_fire_ts,
                # On EVERY loop, not only a structured monitor's: the goal
                # popover words a paused loop by ``stopped_reason`` and reads
                # its countdown off ``next_due_ts``, and the dashboard caches
                # the frame over the REST read, so a frame without them blanks
                # both the moment it lands. Both are public on the ungated
                # REST list already.
                "next_due_ts": loop.next_due_ts,
                "stopped_reason": loop.stopped_reason,
                # The settled outcome of the loop's own watch ("success" for a
                # merged subject or an accepted work ledger, "blocked" for one
                # closed without merging or rejected), "" while none has settled,
                # and the KIND of subject it watched ("gh-pr", "work-ledger"; ""
                # with no watch). On every loop for the reason the two above are:
                # a GATED prompt loop whose subject finished reads Done in the
                # popover, worded by these two, and this frame withholds its
                # ``monitor`` record (the frame is broadcast ungated, and the
                # record names the subject) -- so the two scalars the wording
                # needs ride alone. They say in what state the automation is and
                # what class of thing it watched, never which one. The outcome
                # is an enum's value; the kind is a stored string, and this
                # frame goes to every dashboard socket, so it passes through
                # the same redaction as the structured record below -- every
                # writer checks a kind against the registry, but a state file
                # edited by hand is read back as written.
                "monitor_outcome": (
                    loop.monitor.outcome.value
                    if loop.monitor is not None and loop.monitor.outcome is not None
                    else ""
                ),
                "monitor_kind": (
                    _redact_monitor_value(loop.monitor.kind) if loop.monitor is not None else ""
                ),
                # An ACTIVE loop holding for an unanswered approval; the
                # popover words it as paused.
                "approval_stalled": bool(loop.approval_stalled),
                # An ACTIVE work-ledger watch holding because every open item
                # waits on a person; the popover says it is waiting on you.
                "waiting_on_person": bool(loop.waiting_on_person),
            }
            if is_structured_monitor_loop(loop):
                assert loop.monitor is not None
                loop_payload["monitor"] = _redact_monitor_value(
                    monitor_state_public_dict(loop.monitor)
                )
            broadcast = (
                self.dashboard_state.broadcast_ws_owners
                if is_structured_monitor_loop(loop)
                else self.dashboard_state.broadcast_ws
            )
            _frame = {
                "event": event,
                "slot": loop.slot_key,
                "loop": loop_payload,
            }

            def _publish(frame: dict = _frame) -> None:
                broadcast("autonudge_state", frame)

            # Per-member event log: a member's DM-slot patrol started or
            # stopped. The log -- not the loop -- is what the Crew Members
            # drawer reads for a patrol's stop REASON across a restart:
            # `reconcile_members_at_startup` closes a log that still reads
            # `armed` with no live loop as reason='interrupted', so a
            # transition published BEFORE its append landed lets a crash in
            # that window replace the real reason (`runtime_budget`, say)
            # permanently, with nothing able to recover it. The append
            # therefore runs FIRST and the publish follows it, which is what
            # `persist-before-you-publish` requires of a state a record owns.
            #
            # Still queued on the ordered executor whether or not this is the
            # serving thread: one queue is what orders a rapid start/stop, and
            # an inline write from a non-loop caller could land ahead of an
            # append already queued. So the publish is scheduled back onto the
            # loop from that one worker, because `broadcast_ws` is loop-affine
            # and a synchronous durability barrier on the observer would stall
            # every concurrent session.
            _publish_deferred = False
            # Whether a DURABLE transition applies to this event at all. It is
            # not the same question as `_publish_deferred`, and conflating the
            # two is what published unpersisted state: a false
            # `_publish_deferred` means "no worker will publish", which covers
            # both "there was nothing to persist" (this path may publish) and
            # "the append was REFUSED" (it must not).
            _ledger_owns_frame = False
            try:

                from kiro_crew import eventlog_hooks
                from kiro_crew.eventlog.types import PATROL_STARTED, PATROL_STOPPED

                _pslug = eventlog_hooks.member_slug_for_slot(loop.slot_key)
                _etype2: str | None = None
                _edata: dict = {}
                if event == "added":
                    _etype2, _edata = PATROL_STARTED, {"slot_key": loop.slot_key}
                elif event in ("removed", "expired") or (
                    # A loop FINISHED by its stop file is kept and deactivated,
                    # so it arrives as ``updated``; the patrol is over all the
                    # same, and a log left reading ``armed`` would never close --
                    # the boot closer closes only a log whose row is gone. A
                    # plain pause stays a pause: not a stop, not recorded.
                    event == "updated"
                    and not loop.active
                    and reason_in(loop.stopped_reason, FINISHED_LOOP_REASONS)
                ):
                    _reason = getattr(loop, "stopped_reason", None) or event
                    _etype2, _edata = (
                        PATROL_STOPPED,
                        {"slot_key": loop.slot_key, "reason": _reason},
                    )
                if _pslug is not None and _etype2 is not None:
                    _ledger_owns_frame = True
                    _pslug_s: str = _pslug
                    _etype_s: str = _etype2
                    try:
                        _loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
                    except RuntimeError:
                        # A non-loop caller: it was already publishing from
                        # its own thread before this, so keep that shape
                        # rather than drop the append to preserve it.
                        _loop = None

                    def _emit_patrol(
                        slug: str = _pslug_s, etype: str = _etype_s, data: dict = _edata
                    ) -> None:
                        landed = False
                        try:
                            landed = bool(eventlog_hooks.emit(slug, None, etype, data))
                        except Exception:
                            logger.debug("patrol event-log emit failed", exc_info=True)
                        if not landed:
                            # NOT published. The frame would assert a transition
                            # the ledger does not hold, and the ledger is what
                            # the drawer reads for the stop reason after a
                            # restart -- so publishing here is the
                            # report-success-on-a-failed-write shape that
                            # `persist-before-you-publish` forbids. The refusal
                            # is not silent either: the log still reads `armed`,
                            # and `reconcile_members_at_startup` closes exactly
                            # that state with an explicitly terminal
                            # `interrupted`, which is the state the rule asks a
                            # failed transition to leave behind.
                            logger.debug(
                                "patrol %s for %s was not persisted; frame withheld",
                                etype,
                                slug,
                            )
                            return
                        # Published only AFTER the append landed, and from the
                        # loop, because `broadcast_ws` is loop-affine.
                        try:
                            if _loop is not None:
                                _loop.call_soon_threadsafe(_publish)
                            else:
                                _publish()
                        except Exception:
                            logger.debug("patrol publish failed", exc_info=True)

                    _publish_deferred = eventlog_hooks.submit(_emit_patrol)
            except Exception:
                logger.debug("patrol event-log hook failed", exc_info=True)
            if not _publish_deferred and not _ledger_owns_frame:
                # Publish directly ONLY when this event has no durable meaning --
                # not a member slot, or a type the log does not record -- so there
                # is no ledger state for the frame to contradict.
                #
                # A refused queue deliberately falls through here without
                # publishing. `_publish_deferred` alone cannot tell a refusal
                # from "nothing to persist", and publishing on refusal asserts a
                # transition the ledger never received, which is what
                # `persist-before-you-publish` forbids. The state is not lost:
                # the log still reads `armed` and `reconcile_members_at_startup`
                # closes exactly that with an explicitly terminal `interrupted`.
                _publish()

    async def _collect_judge_evidence(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
        """The wake judge's evidence for one tick: new worker rows, plus the probe.

        A closure rather than a method on the service, for the reason ``_fire`` and
        ``_monitor_owner_session_id`` are: authorizing a transcript read needs
        ``dashboard_state``, which ``AutoNudgeService`` does not hold.

        Creator-only by REUSE, not by a second check. ``read_messages`` calls
        ``authorize_target`` before it returns a row, so a target this loop's owner
        may not read raises and is counted as dropped. Nothing here decides who may
        read what.

        ``since`` is the loop's own per-target cursor, so each tick sees only what
        arrived after the last one, and the cursor advances only on a read that
        actually returned -- a refusal leaves it where it was rather than skipping
        the rows it would have served.
        """
        from kiro_crew import autonudge_judge as _judge
        from kiro_crew.dashboard import session_control as _sc

        state = self.dashboard_state
        if state is None:
            # No cursors either, and an empty map is the truthful third value: with
            # no dashboard state nothing was read, so nothing advanced.
            return [], 0, {}

        async def _read_session(target: str, since: int) -> tuple[list[dict], int, int | None]:
            # Off the loop: this authorizes, may write a SEL row, and reads slot
            # state. Raises on refusal, which the collector counts as a drop.
            #
            # The limit MUST match what the collector retains. ``next_since``
            # follows the returned window, and ``session_evidence`` keeps only the
            # last ``MAX_ROWS_PER_TARGET`` rows, so a wider page advances the
            # cursor across rows that are then discarded and never read again: a
            # 20-row burst loses its oldest 8, which is where an actionable line
            # sits when a worker posted several since the last tick. Reading
            # exactly what is retained turns that loss into a later page: the
            # collector reads again from ``next_since`` while it is short of
            # ``total``, which is why ``total`` is returned too.
            payload = await asyncio.to_thread(
                _sc.read_messages,
                state,
                caller_session_key=loop.slot_key,
                target=target,
                limit=_judge.MAX_ROWS_PER_TARGET,
                since=since or None,
            )
            rows = payload.get("messages") or []
            cursor = payload.get("next_since")
            total = payload.get("total")
            return (
                list(rows),
                int(cursor) if isinstance(cursor, int) else since,
                int(total) if isinstance(total, int) else None,
            )

        async def _read_pr(target: str) -> dict | None:
            # The reading the fetcher ALREADY made this tick, never a fresh fetch:
            # re-asking the forge would spend a subprocess to learn what the
            # monitor record already holds, and the judge's job is the owner's own
            # prose criterion read against those facts.
            monitor = loop.monitor
            observed = getattr(monitor, "last_observation", None) if monitor else None
            if monitor is None or not isinstance(observed, dict):
                return None
            if _judge.pr_target_is_unread(observed):
                # No reading, or one whose own status says it is short. A partial
                # reading counts as unread: a quiet drawn from the half that was
                # read would be a quiet about the wrong half.
                logger.debug(
                    "AutoNudge: no whole pull-request reading for loop %s -- counting "
                    "the target as unread",
                    loop.id,
                )
                return None
            # A loop holds ONE monitor, so this returns the same observation for
            # every subject it is asked about. The brief's targets are the owner's
            # strings and may name a DIFFERENT pull request, which would label the
            # row with that name while carrying the watched subject's state.
            if not _judge.pr_observation_is_about(
                target,
                monitor_kind=str(getattr(monitor, "kind", "") or ""),
                monitor_target=str(getattr(monitor, "target", "") or ""),
                observation=observed,
            ):
                logger.debug(
                    "AutoNudge: a judge brief named a pull request this loop does not watch"
                )
                return None
            # Bodies are merged into a COPY. They live in memory for this tick
            # only, and the record the copy is taken from carries who said
            # something and when, never what -- so filling them in place is
            # exactly how review prose would reach the disk.
            stashed, stash_dropped = _judge.take_pr_bodies(loop.id)
            payload = _judge.payload_for_judge(observed, stashed, stash_dropped)
            if payload is None:
                logger.debug(
                    "AutoNudge: loop %s lost its stashed remark bodies -- counting "
                    "the target as unread rather than judging prose the judge never got",
                    loop.id,
                )
                return None
            # ``last_observed_at`` is a SIBLING field of the fact object, not a key
            # inside it, so the collector cannot age the reading without being
            # handed it.
            at = getattr(monitor, "last_observed_at", 0.0)
            if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0:
                payload["observed_at"] = float(at)
            return payload

        targets = _judge.parse_targets(
            _judge.spec_of(loop), loop.message, watched=_judge.watched_pr_subject(loop)
        )
        # Pruned to the targets this tick actually reads, not merely copied. The
        # collector only ever ADDS a key, the targets come from ``loop.message``, and
        # ``asdict`` persists whatever the map holds, so without this a retarget
        # leaves the departed target's cursor in the store for the life of the loop.
        #
        # Pruning at the write is what bounds retention in the process doing the
        # writing. The load cap is not that bound: it keeps an arbitrary 16, so it
        # can discard the cursor of a target still being read, and a lost cursor
        # replays rows the judge already screened. With the population pruned to the
        # current targets, the two bounds that disagree -- 16 cursors against 8
        # targets -- collapse into the smaller one and the cap never binds.
        wanted = set(targets)
        cursors = {t: c for t, c in loop.judge_cursors.items() if t in wanted}
        evidence, dropped = await _judge.collect_evidence(
            targets,
            read_session=_read_session,
            read_pr=_read_pr,
            cursors=cursors,
        )
        # RETURNED, not assigned onto the loop. The advanced positions are a
        # consequence of a reading that has not been judged yet, and the judge await
        # that follows is cancellable -- a user typing cancels exactly that task --
        # so publishing here moves the cursors for a verdict that never commits. The
        # next tick then reads nothing new, answers quiet, and the wake the skipped
        # row had earned is gone. The caller owns the one point where a verdict is
        # committed, so the caller publishes them.
        return evidence, dropped, cursors

    async def _emit_judge_notice(loop: NudgeLoop, line: str) -> None:
        """Write ONE ``notice`` row on the owning session for a judge verdict.

        The same surface a refused arm uses (``_surface_arm_refusal``): a row of
        its own, because that is what reaches whoever is watching the session
        without costing a turn. A quiet verdict is exactly the case that needs
        it: without a row, a loop that judged and stayed quiet looks identical
        to a loop that died.

        The row is scrubbed before it is persisted or broadcast, like every
        other transcript egress. It carries probabilities and counts, never
        evidence text: the state stays in the request, and the transcript gets
        the verdict.

        Notice rows are NOT evidence. The session collector admits assistant
        rows only, so a judge can never read its own previous notice back as
        new evidence about the session it is watching.
        """
        state = self.dashboard_state
        if state is None:
            return
        # ``get_slot`` is the accessor; ``state.sessions`` is the SessionManager and
        # holds sessions rather than chat slots. A channel-bound loop has no slot
        # window at all, which is why this returns rather than inventing one: the
        # verdict is still on the loop record and in the decisions log.
        slot = state.get_slot(loop.slot_key)
        if slot is None:
            return
        from kiro_crew.dashboard.state import append_and_surface

        text, _ = redact_exfiltration_urls(line)
        text, _ = redact_credentials(text)
        await asyncio.to_thread(append_and_surface, state, slot, "notice", text, "msg msg-info")

    def _worker_slot_running(session_key: str) -> bool:
        """Bind the wake gate's liveness question to this gateway's slot table.

        The logic is ``ledger_wake.worker_running``, not a copy of it here: a
        closure inside this constructor is unreachable from a test, and the one
        hop the probe cannot make for itself is exactly the hop that should not
        be the untested one. This keeps only the binding, which is what a
        gateway is for.

        Imported HERE rather than at module scope, matching
        ``_monitor_owner_session_id`` below. ``ledger_wake`` reaches the
        work-ledger store, and every gateway boots whether or not any conductor
        has ever opened a ledger, so a module-level import would put an optional
        subsystem on the startup path of every install to serve a callable that
        only runs once a work-ledger watch ticks.
        """
        from kiro_crew import ledger_wake

        return ledger_wake.worker_running(self.dashboard_state, session_key)

    def _worker_slot_closed(session_key: str) -> bool:
        """Bind the wake gate's "is this worker gone" question to the slot table.

        A second binding rather than a second return value from the one above,
        because the two answers are read at different strengths: a running worker
        suppresses a stall wake, a closed one causes it without waiting out the
        window. The logic is ``ledger_wake.worker_closed``, for the reason its
        sibling gives -- a closure in this constructor is unreachable from a test.
        """
        from kiro_crew import ledger_wake

        return ledger_wake.worker_closed(self.dashboard_state, session_key)

    self.autonudge_svc = AutoNudgeService(
        base_dir=data_home(),
        on_fire=_fire,
        on_monitor_tick=_monitor_tick,
        collect_judge_evidence=_collect_judge_evidence,
        emit_judge_notice=_emit_judge_notice,
        worker_running=_worker_slot_running,
        worker_closed=_worker_slot_closed,
    )

    def _monitor_owner_session_id(loop: NudgeLoop) -> str:
        """The crew log unit the loop's owner slot is serving on, or ``""``.

        The same resolver the session ledger uses: an exact registry read plus
        an attribute read, no disk and no session state mutated by asking. A
        slot with no live session answers ``""`` and the controller records
        nothing for it -- a probe runs without a model turn and must not start
        a session to file its result.
        """
        if self.dashboard_state is None:
            return ""
        from kiro_crew.crew_log.resolve import unit_for_session_key

        return unit_for_session_key(self.dashboard_state.sessions, loop.slot_key)

    controller = MonitorController(
        self.autonudge_svc,
        _fire_monitor,
        owner_session_id=_monitor_owner_session_id,
    )
    # Timers can complete while start() awaits store repair. Install the
    # observer first so that transition cannot fall between startup and
    # terminal replay.
    self.autonudge_svc.subscribe(_observer)
    await self.autonudge_svc.start()
    # A persisted terminal transition and its notification are separate
    # durable steps. Replay any notice not proven delivered; marking only
    # after delivery gives this boundary at-least-once crash semantics.
    for loop in self.autonudge_svc.list_all():
        if (
            is_structured_monitor_loop(loop)
            and loop.monitor is not None
            and not loop.active
            and loop.monitor.outcome
            in {
                MonitorOutcome.SUCCESS,
                MonitorOutcome.BLOCKED,
                MonitorOutcome.BUDGET,
                MonitorOutcome.TARGET_UNAVAILABLE,
            }
        ):
            terminal_key = (loop.id, loop.monitor.outcome, loop.monitor.stopped_at)
            if terminal_key not in notified_monitor_terminals:
                notified_monitor_terminals.add(terminal_key)
                if not terminal_notification_delivery_matches(
                    loop,
                    loop.monitor.outcome,
                    loop.monitor.stopped_at,
                ):
                    _schedule_terminal_notification(loop, terminal_key)


def _notify_nudge_held(self: GatewayOrchestrator, loop: NudgeLoop) -> bool:
    """Tell the user once that a loop is paused for an unanswered approval.

    Sent when the hold is recorded, never per tick: a held loop fires
    nothing, so this is the one place a person away from the session learns
    that it is waiting for them, and what resumes it. Same meta and channel
    rules as :meth:`_notify_nudge_expired`, and best-effort for the same
    reason (it runs inside ``_emit``'s observer loop).
    """
    if not self.dashboard_state:
        return False
    try:
        key = loop.slot_key
        meta = None if is_channel_key(key) else self._notif_meta(f"dashboard:{key}")
        title = "Monitoring loop paused — a tool approval went unanswered"
        body = (
            f"The loop is paused at cycle {loop.cycle_count} and spends no "
            "cycles or time budget while it waits. Press Play in its goal "
            "popover on the dashboard to resume it."
        )
        if not is_channel_key(key):
            body += " Sending any message in that chat resumes it too."
        self.dashboard_state.notify("agent", title, body, meta=meta, channel=MONITOR_CHANNEL)
        return True
    except Exception:
        logger.debug("AutoNudge hold notification failed", exc_info=True)
        return False


def _notify_nudge_expired(self: GatewayOrchestrator, loop: NudgeLoop) -> bool:
    """Notify the user that a monitoring loop stopped at a terminal bound.

    Reaching ``max_cycles`` or spending ``max_runtime_secs`` is a runaway
    backstop, not a finish line: the loop stopped with its goal possibly
    unmet. Without this the only signals were a log line and an
    ``active=False`` state change that looks identical to a manual Stop, so
    a loop that ran out of cycles was indistinguishable from the agent
    stopping on its own — the most confusing failure mode of the babysit
    feature. The wording distinguishes WHICH bound fired via the same
    ``runtime_budget_exceeded`` predicate ``_timer`` enforces with; when
    both are exhausted the cycle cap wins, matching the enforcement order.
    A loop stopped because it could not obtain tool approval is a third
    case naming a different remedy — restore the authorization, do not
    raise a bound — so reporting it as a cap would send the operator to
    change a setting that was never the problem. It is read from the
    persisted reason and ranked below the two bounds, again matching
    ``_timer``, which tests it last.

    Best-effort by construction: ``notify()`` never raises (it swallows
    validation errors and logs), and the whole call is wrapped anyway
    because this runs inside ``_emit``'s observer loop, where an exception
    would be caught and logged but would also skip the WS broadcast that
    follows it.
    """
    if not self.dashboard_state:
        return False
    try:
        key = loop.slot_key
        # Channel-bound loops get NO synthesized meta: _notif_meta's generic
        # ``chan:ts`` split would read the NAMESPACE as the channel id,
        # producing a dead link (and a Slack URL for a Discord loop).
        # Dashboard loops bind on the BARE slot key, so re-qualify those to
        # get a working jump-to-source slot link.
        meta = None if is_channel_key(key) else self._notif_meta(f"dashboard:{key}")
        if is_structured_monitor_loop(loop):
            monitor = loop.monitor
            assert monitor is not None
            outcome = monitor.outcome
            reason = monitor.stopped_reason
            if outcome is MonitorOutcome.SUCCESS and reason == "pull_request_merged":
                title = "Pull request monitor finished — pull request merged"
                body = "The pull request was merged. No action needed for this watch."
            elif outcome is MonitorOutcome.SUCCESS:
                title = "Pull request monitor finished — review readiness reached"
                body = "The pull request is ready for review."
            elif outcome is MonitorOutcome.BUDGET:
                title = "Pull request monitor spent its budget"
                body = (
                    "The monitor stopped at its configured budget "
                    "before the pull request became review-ready. "
                    "Start a new watch with a larger budget to keep monitoring."
                )
            elif outcome is MonitorOutcome.TARGET_UNAVAILABLE:
                title = "Pull request monitor could not deliver an action"
                body = (
                    "This monitor's conversation could not accept work. "
                    "Start a new watch from an active conversation."
                )
            elif reason == "pull_request_closed":
                title = "Pull request monitor stopped — pull request closed unmerged"
                body = (
                    "The pull request was closed without merging. Decide whether "
                    "to reopen it or abandon this watch. Restart the monitor if you reopen it."
                )
            elif reason == MONITOR_STOP_VERDICT_STALL:
                # NOT the terminal-blocker map below, whose copy sends the reader to
                # repair something. But it must not claim the opposite either: the
                # DOMINANT way a watch reaches twelve identical ticks is an
                # already-alerted red sitting inside its re-alert interval, so
                # "nothing is wrong" would be false exactly when it matters. Name the
                # verdict it kept reaching and let the reader judge it. The minutes
                # are derived so the sentence cannot outlive the floor.
                last_seen = monitor.last_observation_reason_code or "no change"
                title = "Pull request monitor stopped — its verdict stopped moving"
                body = (
                    f"The monitor reached the same conclusion {DEFAULT_MONITOR_STALL_TICKS} "
                    f"checks in a row, over at least {DEFAULT_MONITOR_STALL_MIN_SECS // 60} "
                    f"minutes. Last seen: {last_seen}. It stopped because nothing was "
                    "moving, not because of a new problem -- act on that verdict yourself, "
                    "or start a new watch if you expect it to change."
                )
            else:
                title = "Pull request monitor stopped on a terminal blocker"
                body = {
                    "provider_authentication": (
                        "The provider rejected the monitor's credentials. "
                        "Restore its credentials before restarting."
                    ),
                    "provider_authorization": (
                        "The monitor no longer has permission to read this pull request. "
                        "Restore provider access before restarting."
                    ),
                    "provider_setup": (
                        "The monitor's provider setup is unavailable. "
                        "Repair the provider configuration before restarting."
                    ),
                    MONITOR_STOP_APPROVAL_STALL: (
                        "The monitor could not get tool approval. "
                        "Restore approval access before restarting."
                    ),
                    MONITOR_STOP_COMPLETION_UNAVAILABLE: (
                        "The monitor could not confirm completion of its last action. "
                        "Check the conversation before restarting to avoid repeating work."
                    ),
                    MONITOR_STOP_SESSION_UNAVAILABLE: (
                        "This monitor's conversation is unavailable. "
                        "Start a new watch from an active conversation."
                    ),
                    MONITOR_STOP_UNSUPPORTED_VERSION: (
                        "This watch was created by a newer Kiro Crew version. "
                        "Update Kiro Crew to inspect or restart it."
                    ),
                    MONITOR_STOP_INVALID_RECORD: (
                        "The saved monitor record is invalid. "
                        "Inspect its details before starting a new watch."
                    ),
                }.get(
                    reason,
                    "The monitor stopped before review readiness. Open its details "
                    "in the dashboard and resolve the reported problem before restarting.",
                )
            body = f"{body}\n\n{monitor.target}"
            body, _ = redact_exfiltration_urls(body)
            body, _ = redact_credentials(body)
            self.dashboard_state.notify("agent", title, body, meta=meta, channel=MONITOR_CHANNEL)
            return True
        capped_out = loop.max_cycles and loop.cycle_count >= loop.max_cycles
        # Every branch below except the terminal one explains why the loop stopped
        # SHORT of its goal. A terminal subject is not short of anything, so it
        # outranks all of them -- expressed ONCE here rather than as a guard added to
        # each branch after a reviewer finds it, which is how the cap and then the
        # runtime budget each came to preempt it in turn.
        #
        # An OWED terminal turn is terminal news too, and it is the third way this
        # same precedence has been lost. A CHANNEL-bound loop deliberately does not
        # settle on observation -- it learns its watch finished from a delivered turn,
        # so the probe records the owed turn in ``terminal_pending`` and leaves the
        # loop active with no ``outcome`` and no ``MONITOR_TERMINAL_REASON``. If that
        # final turn is refused (a busy thread, the ordinary case) and the retry finds
        # a bound spent, ``_timer`` deactivates on the bound before the settlement
        # that would have promoted the debt ever runs. Reading ``stopped_reason``
        # alone then contradicts a fact already durably on disk, and announces a watch
        # that SUCCEEDED with the same signal as one that ran out of cycles.
        #
        # Scope: the debt is consulted for the WORDING only. The bound that actually
        # stopped the loop keeps its own ``stopped_reason`` untouched -- so the spent
        # cap stays observable, and every consumer of that literal (notably the
        # monitor_update revival affordance, which revives a ``cycle_cap`` loop when
        # the cap is raised) sees that literal unchanged.
        owed = ""
        if loop.monitor:
            # A landed turn awaiting settlement keeps the same fact in
            # ``terminal_delivered``.
            owed = str(
                getattr(loop.monitor, "terminal_pending", "")
                or getattr(loop.monitor, "terminal_delivered", "")
                or ""
            )
        terminal = loop.stopped_reason == MONITOR_TERMINAL_REASON or bool(owed)
        if not terminal and not capped_out and runtime_budget_exceeded(loop):
            title = "Monitoring loop spent its time budget"
            body = (
                f"The loop stopped after {loop.cycle_count} cycles because "
                f"its {loop.max_runtime_secs}s runtime budget (awake time "
                "while the gateway runs; a restart counts the time since "
                "created_ts on the wall clock) ran out "
                "without it reporting done, so its goal may still be "
                "unmet. Restart it from the goal popover, or ask the agent "
                "to raise the budget (monitor_update)."
            )
        elif not terminal and not capped_out and loop.stopped_reason == APPROVAL_STALL_REASON:
            title = "Monitoring loop stopped — it could not get tool approval"
            body = (
                f"The loop stopped after {loop.cycle_count} cycles because a "
                "tool it needed went unanswered at the approval prompt, so "
                "further cycles would wake, be declined and accomplish "
                "nothing. A prompt you were merely away for counts too: if "
                "approval is available now, just restart the loop from the "
                "goal popover; otherwise re-enable auto-approve first. For "
                "runs meant to go unattended overnight, Settings → "
                "agent.yolo_duration has an 'until_shutdown' option that "
                "has no timed expiry."
            )
        elif not terminal and not capped_out and loop.stopped_reason == MODEL_REFUSED_REASON:
            title = "Monitoring loop stopped — the model declined its last cycle"
            body = (
                f"The loop stopped after {loop.cycle_count} cycles because "
                "the model's content filter declined its last cycle and "
                "nothing retried it on another model, so further cycles "
                "would send the same prompt to the same model and be "
                "declined the same way. Open the session to see the "
                "refusal, then reword the goal, switch the session's model "
                "or set agent.refusal_fallback_model, and restart the loop "
                "from the goal popover."
            )
        elif not terminal and not capped_out and loop.stopped_reason == CONSECUTIVE_FAILURE_REASON:
            title = "Monitoring loop stopped — its cycles kept failing"
            body = (
                f"The loop stopped after {loop.cycle_count} cycles because "
                "several in a row reached a model session and then died "
                "(a backend error, a persistent tool failure or a timeout), "
                "so further cycles would wake, fail the same way and "
                "accomplish nothing. Look at the session for the error, fix "
                "the cause, then restart the loop from the goal popover — a "
                "cycle that completes clears the streak, so a loop that can "
                "make progress again is never held back."
            )
        elif terminal:
            settled = getattr(loop.monitor, "outcome", None) if loop.monitor else None
            decided = getattr(settled, "value", settled) or owed
            if decided == "success":
                title = "Monitoring loop finished — what it was watching is done"
                body = (
                    f"The loop stopped after {loop.cycle_count} cycles because "
                    "the pull request it was watching was merged, so there is "
                    "nothing left to observe. No action needed; arm a new loop "
                    "if you want to watch something else."
                )
            elif decided == "blocked":
                title = "Monitoring loop stopped — its subject was closed unmerged"
                body = (
                    f"The loop stopped after {loop.cycle_count} cycles because "
                    "the pull request it was watching was closed WITHOUT being "
                    "merged. Nothing is left to observe, but the work is not "
                    "finished: decide whether to reopen it or abandon it, then "
                    "arm a new loop if you reopen."
                )
            else:
                title = "Monitoring loop finished — it reported done"
                body = (
                    f"The loop stopped after {loop.cycle_count} cycles because its "
                    "goal reported completion. Open the session for the final details."
                )
        else:
            title = "Monitoring loop hit its cycle cap"
            body = (
                f"The loop stopped after {loop.cycle_count} of "
                f"{loop.max_cycles} cycles without reporting done, so its "
                "goal may still be unmet. Reopen the goal popover to raise "
                "the cap or restart it."
            )
        self.dashboard_state.notify("agent", title, body, meta=meta, channel=MONITOR_CHANNEL)
        return True
    except Exception:
        logger.debug("AutoNudge expiry notification failed", exc_info=True)
        return False

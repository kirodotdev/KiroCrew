"""Monitoring behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        _CLK_TCK,
        _REAPER_INTERVAL,
        _SUPPRESS_CEILING,
        OUTCOME_FAILED,
        OUTCOME_INTERRUPTED,
        SUBAGENT_COMPLETION_PREFIX,
        VERDICT_DEAD,
        VERDICT_STUCK_INPUT,
        VERDICT_UNKNOWN,
        VERDICT_WORKING,
        LivenessOracle,
        SubagentInfo,
        _agent_dir,
        _attributed_count,
        _proc_subtree_sample,
        _redact,
        _redact_and_truncate,
        agent_dir_for_display,
        append_cost_sample,
        asyncio,
        compact_cost_log,
        consult_offloaded,
        has_dashboard_surface,
        list_orphans,
        logger,
        maintenance_executor,
        prune_stale_tombstones,
        sel,
        single_completion_meta,
        subprocess_executor,
        time,
        write_tombstone,
    )


class OrphanStallMonitor(ManagerComponent):
    """Own monitoring transitions while state remains facade-owned."""

    __slots__ = ()

    def start_reaper_impl(self) -> None:
        """Start the periodic reaper loop.  Call once after the event loop is running."""
        if self._manager._reaper_task is None:
            self._manager._reaper_task = asyncio.create_task(self._manager._reaper_loop())
            # One-shot orphan reconciliation on startup
            self._manager._reconcile_task = asyncio.create_task(self._manager._reconcile_orphans())

    async def _reconcile_orphans_impl(self) -> None:
        """Scan for orphaned agent folders from a prior gateway run.

        For each orphan (folder with state.json but no tombstone.json
        and not tracked in ``_agents``):
        - PID alive → SIGKILL, tombstone (gateway_restart)
        - PID dead + result → tombstone (gateway_restart, delivered)
        - PID dead + no result → tombstone (gateway_restart, notification_pending)
        """
        try:

            orphans = list_orphans()
            if not orphans:
                return
            logger.info("Reconciling %d orphaned subagent(s)", len(orphans))
            processed = 0
            # DM-fallback messages are DIGESTED: collected across the whole
            # scan and delivered as ONE message at the end — a restart with N
            # in-flight agents must never produce N pings. (The session-
            # injection path batches naturally via the parent slot's pending-
            # failures drain.)
            dm_pending: list[str] = []
            for state in orphans:
                agent_id = state.get("id", "")
                if not agent_id or agent_id in self._manager._agents:
                    continue  # tracked in current run, skip
                try:
                    pid = state.get("pid")
                    has_result = False
                    try:

                        rp = _agent_dir(agent_id) / "result.txt"
                        has_result = rp.exists() and rp.stat().st_size > 0
                    except OSError:
                        pass

                    recovery = "undeliverable"
                    if pid and self._manager._is_pid_alive(pid):
                        # Use pid_recorded_at (when PID was actually written) instead of
                        # started (folder creation time) to avoid false negatives under load
                        pid_recorded_at = state.get("pid_recorded_at", state.get("started", 0))
                        if self._manager._is_orphan_process(pid, pid_recorded_at):
                            self._manager._kill_orphan_pid(pid)
                            try:
                                sel().log_tool_invocation(
                                    session_key=f"subagent:{agent_id}",
                                    source="subagent",
                                    tool_name="orphan_reconcile_kill",
                                    outcome="killed",
                                    metadata={"subagent_id": agent_id, "pid": pid},
                                )
                            except Exception:
                                logger.debug("SEL audit failed for orphan %s", agent_id)
                        recovery = "result_available" if has_result else "notification_pending"
                    elif has_result:
                        recovery = "result_available"
                    else:
                        recovery = "notification_pending"

                    try:
                        write_tombstone(
                            agent_id,
                            cause="gateway_restart",
                            recovery_action=recovery,
                            pid=pid,
                            turns=state.get("turns", 0),
                            last_tool=state.get("last_tool", ""),
                        )
                    except Exception:
                        logger.debug("Failed to tombstone orphan %s", agent_id, exc_info=True)

                    # Retain-by-default: session files are deliberately NOT
                    # deleted here — an orphaned run's transcript is still
                    # spawn_continue resume material after the restart. The
                    # tombstone pruner owns deletion (with the keep guard for
                    # promoted conversations).

                    logger.info(
                        "Reconciled orphan %s: recovery=%s, pid=%s, has_result=%s",
                        agent_id,
                        recovery,
                        pid,
                        has_result,
                    )
                    # Notify user about the orphaned agent. Injection happens
                    # per-orphan (it rides the parent slot's batched pending-
                    # failures queue); DM fallback is deferred to the digest.
                    try:
                        undelivered = await self._manager._notify_orphan(
                            agent_id, state, recovery, has_result
                        )
                        if undelivered:
                            dm_pending.append(undelivered)
                    except Exception:
                        logger.debug("Notification failed for orphan %s", agent_id, exc_info=True)
                except Exception:
                    logger.warning("Failed to reconcile orphan %s", agent_id, exc_info=True)

                # Rate limit: yield to event loop every 50 entries
                processed += 1
                if processed % 50 == 0:
                    await asyncio.sleep(0)

            # Single digest for everything the injection path couldn't deliver.
            if dm_pending:
                if len(dm_pending) == 1:
                    digest = dm_pending[0]
                else:
                    digest = (
                        f"[Subagent restart digest] {len(dm_pending)} subagent(s) "
                        f"orphaned by a gateway restart:\n\n" + "\n\n".join(dm_pending)
                    )
                try:
                    await self._manager._send_orphan_slack_dm(digest)
                except Exception:
                    logger.debug("Orphan digest DM failed", exc_info=True)
        except Exception:
            logger.warning("Orphan reconciliation failed", exc_info=True)

    async def _notify_orphan_impl(
        self, agent_id: str, state: dict, recovery: str, has_result: bool
    ) -> str | None:
        """Notify user about an orphaned subagent.

        1. Try session injection if parent session still exists (delivered
           messages return ``None``).
        2. Otherwise return the redacted message so the caller can batch all
           undelivered notifications into a SINGLE digest DM — never N pings.
        """
        task_preview = (state.get("task", "") or "")[:100]
        parent_session = state.get("parent_session", "")
        result_path = str(agent_dir_for_display(agent_id) / "result.txt")

        if has_result:
            msg = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{agent_id}` ⚠️ orphaned by gateway restart\n"
                f"Task: {task_preview}\n"
                f"Result saved at: `{result_path}`\n"
                f"Use the read tool to retrieve it."
            )
            # A restart orphan whose result survived on disk: interrupted, not a
            # plain failure. The note is the only explanation the header carries.
            row_meta = single_completion_meta(
                agent_id=agent_id,
                outcome=OUTCOME_INTERRUPTED,
                task=task_preview,
                note="orphaned by gateway restart",
                requested_model=str(state.get("requested_model") or ""),
                resolved_model=str(state.get("resolved_model") or ""),
            )
        else:
            msg = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{agent_id}` ❌ lost to gateway restart\n"
                f"Task: {task_preview}\n"
                f"No result was captured before the restart."
            )
            row_meta = single_completion_meta(
                agent_id=agent_id,
                outcome=OUTCOME_FAILED,
                task=task_preview,
                note="lost to gateway restart",
                requested_model=str(state.get("requested_model") or ""),
                resolved_model=str(state.get("resolved_model") or ""),
            )

        # Redact before any delivery path (injection or Slack DM)
        msg = _redact(msg)

        # Try session injection first. The question is whether a tab is OPEN to
        # receive the notice, not where the conversation started — a channel-born
        # parent keeps its channel session key while its tab is open, and with no
        # tab the digest DM below is the only surface.
        if has_dashboard_surface(parent_session):
            try:
                injected = await self._manager._try_inject_orphan_notification(
                    parent_session, msg, row_meta
                )
                if injected:
                    # Update tombstone recovery_action
                    try:
                        write_tombstone(
                            agent_id,
                            cause="gateway_restart",
                            recovery_action="delivered",
                            pid=state.get("pid"),
                            turns=state.get("turns", 0),
                            last_tool=state.get("last_tool", ""),
                        )
                    except Exception:
                        pass
                    return None
            except Exception:
                logger.debug("Injection failed for orphan %s", agent_id, exc_info=True)

        # Undelivered: hand back for the caller's single digest DM.
        return msg

    async def _try_inject_orphan_notification_impl(
        self, parent_session: str, msg: str, meta: dict | None = None
    ) -> bool:
        """Try to inject a message into the parent dashboard session.

        Delegates to the gateway-wired ``on_orphan_notify`` callback, which
        appends the (already-redacted) message to the parent slot's transcript
        and queues it into ``slot._pending_subagent_failures`` so the LLM
        learns about the orphan on its next turn. Returns True if delivered.

        ``meta`` carries the structured completion facts for the dashboard card
        (#1792) so the orphan row renders without re-parsing its prose header.
        """
        if self._manager._on_orphan_notify is None:
            return False
        try:
            delivered = bool(await self._manager._on_orphan_notify(parent_session, msg, meta))
        except Exception:
            logger.debug("on_orphan_notify raised for %s", parent_session, exc_info=True)
            return False
        if delivered:
            try:
                sel().log_api_access(
                    caller=parent_session,
                    operation="subagent.orphan_notification_injected",
                    outcome="ok",
                    source="subagent",
                )
            except Exception:
                logger.debug("SEL audit for orphan injection failed", exc_info=True)
        return delivered

    async def _send_orphan_slack_dm_impl(self, msg: str) -> None:
        """Deliver an orphan notification via the owner DM / notification path.

        Delegates to the gateway-wired ``on_orphan_dm`` callback (Slack DM +
        dashboard notification). Falls back to a log line when no callback is
        wired (e.g. slack-only setups constructed without the gateway hooks).
        """
        if self._manager._on_orphan_dm is not None:
            try:
                delivered = bool(await self._manager._on_orphan_dm(msg))
                if delivered:
                    return
            except Exception:
                logger.debug("on_orphan_dm raised", exc_info=True)
        logger.warning("Orphan notification (no delivery channel wired): %s", msg[:200])

    def _live_shared_count_impl(self, pid: int | None, agents: "list[SubagentInfo]") -> int:
        """Count live session-shared subagents sharing runtime *pid* (>= 1).

        Used to average the shared AcpRuntime's measured RSS/CPU across the
        sessions currently running inside it, so each shared subagent is charged
        an empirical per-session share rather than the whole process.

        *agents* is the registry snapshot the caller already took, and is
        required: the sole caller runs on a worker thread (see
        ``_sample_live_costs``), where iterating the live registry would raise
        ``RuntimeError`` the moment the event loop registered or evicted an
        agent. An on-loop caller passes ``list(self._agents.values())``.
        """
        if not pid:
            return 1
        n = sum(1 for a in agents if not a.done and a._session_sharing and a._pid == pid)
        return n if n > 0 else 1

    def _sample_live_costs_impl(self) -> None:
        """Sample high-water RSS/CPU for each live agent (reaper-loop piggyback).

        Updates per-run peaks on ``SubagentInfo`` (dynamic-subagent-sizing.md
        §4.1). RSS is the subtree VmRSS in GB; CPU is cores used since the last
        sample = Δ(utime+stime jiffies) / (CLK_TCK × Δt). The first sample only
        seeds the CPU baseline (no delta yet). Best-effort: a dead/unreadable
        pid is simply skipped.

        BLOCKING, and therefore off-loop: every live agent costs ONE ``/proc``
        subtree walk (:func:`_proc_subtree_sample`, which returns RSS, CPU
        jiffies and the process/stub counts from a single frontier), so the
        caller hands this to :func:`maintenance_executor` and the body must stay
        thread-safe. Concretely that means it takes ONE snapshot of the agent
        registry up front and derives everything, sharer counts included, from
        that list: iterating the live dict from a worker thread would raise
        ``RuntimeError`` the moment the event loop registered or evicted an
        agent mid-sweep. Writes are plain float/int field assignments on
        ``SubagentInfo``, which the surface only ever reads.
        """
        now = time.monotonic()
        agents = list(self._manager._agents.values())
        for info in agents:
            if info.done or not info._pid:
                continue
            # Session-shared subagents run inside the parent's AcpRuntime process;
            # every sharing subagent reports the SAME runtime PID, so naive
            # per-PID sampling would attribute the whole shared process to each
            # of them. Instead attribute the runtime's measured RSS/CPU divided
            # by the number of concurrently-live shared sessions on that PID — an
            # empirical per-session average, not a guessed constant
            # (dynamic-subagent-sizing.md §session-sharing cost model).
            #
            # Sole tenant of its own process: the subtree reading IS this run's,
            # which is a share of one.
            shared_n = (
                self._manager._live_shared_count(info._pid, agents) if info._session_sharing else 1
            )
            sample = _proc_subtree_sample(info._pid)
            if sample.rss_kb > 0 and shared_n > 0:
                gb = (sample.rss_kb / (1024 * 1024)) / shared_n
                info.last_rss_gb = gb
                if gb > info.peak_rss_gb:
                    info.peak_rss_gb = gb
            info.last_procs = _attributed_count(sample.procs, shared_n, info.last_procs)
            info.last_stubs = _attributed_count(sample.matched, shared_n, info.last_stubs)
            jiffies = sample.jiffies
            if info._cpu_sample_ts > 0.0 and jiffies >= info._cpu_jiffies_prev and shared_n > 0:
                dt = now - info._cpu_sample_ts
                if dt > 0:
                    cores = ((jiffies - info._cpu_jiffies_prev) / (_CLK_TCK * dt)) / shared_n
                    info.last_cpu_cores = cores
                    if cores > info.peak_cpu_cores:
                        info.peak_cpu_cores = cores
            info._cpu_jiffies_prev = jiffies
            info._cpu_sample_ts = now

    def _record_cost_impl(self, info: SubagentInfo) -> None:
        """Persist this run's high-water RSS/CPU to the learned-cost store."""
        if info.peak_rss_gb <= 0 and info.peak_cpu_cores <= 0:
            return  # never sampled (e.g. finished before the first reaper sweep)
        try:
            append_cost_sample(info.agent, info.peak_rss_gb, info.peak_cpu_cores)
        except Exception:
            logger.debug("Failed to record subagent cost for %s", info.id, exc_info=True)

    async def _reaper_loop_impl(self) -> None:
        """Periodically force-kill subagents that exceed the timeout.

        Defense-in-depth: catches cases where ``asyncio.wait_for`` in
        ``_run()`` fails to fire (event-loop saturation, orphaned tasks,
        or ``reset()`` hanging in the finally block).
        """
        try:
            compact_cost_log()  # startup FIFO trim (§4.2)
        except Exception:
            logger.debug("Reaper: startup cost-log compaction failed", exc_info=True)
        while True:
            await asyncio.sleep(_REAPER_INTERVAL)
            now = time.time()
            if not self._manager._conv_registry_rebuilt:
                # First pass after (re)start: re-seed the conversation TTL
                # registry from state.json so promoted conversations survive
                # a gateway restart under sweep ownership (#1114). The flag
                # is set only on SUCCESS — a failed rebuild retries on the
                # next sweep instead of silently restoring the pre-#1114
                # orphaning until the next restart (Arbiter, PR #1246).
                try:
                    await self._manager._rebuild_conversation_registry()
                    self._manager._conv_registry_rebuilt = True
                except Exception:
                    logger.warning(
                        "Reaper: conversation registry rebuild failed — retrying next sweep",
                        exc_info=True,
                    )
            # Off the event loop: the sweep is several /proc walks per live agent,
            # and the reaper shares the loop with every chat turn and heartbeat.
            try:
                await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(), self._manager._sample_live_costs
                )
            except Exception:
                logger.debug("Reaper: live-cost sample failed", exc_info=True)
            # Wave liveness backstop: reconcile waves wedged by submissions
            # lost before the process boundary (see _sweep_stuck_waves).
            try:
                self._manager._sweep_stuck_waves(now)
            except Exception:
                logger.debug("Reaper: stuck-wave sweep failed", exc_info=True)
            # Digest hold deadline: release completed wave results that a
            # straggler (or a hung member) has been withholding.
            try:
                self._manager._sweep_digest_holds(now)
            except Exception:
                logger.debug("Reaper: digest-hold sweep failed", exc_info=True)
            try:
                self._manager._sweep_conversations(now)
            except Exception:
                logger.debug("Reaper: conversation sweep failed", exc_info=True)
            try:
                compact_cost_log()  # periodic FIFO trim (also bounds a long-running gateway)
            except Exception:
                logger.debug("Reaper: cost-log compaction failed", exc_info=True)
            for agent_id, info in list(self._manager._agents.items()):
                if info.done:
                    continue
                elapsed = now - info.started
                # Startup watchdog: a subagent that entered execution but is
                # still on turn 0 with no runtime PID after the startup window
                # is wedged in startup (e.g. a hung provider/ACP handshake that
                # never launches the child process). Reap it fast with a clear
                # "failed to start" error instead of burning the full deadline
                # and surfacing a misleading 30-minute turn-0 timeout.
                if self._manager._is_startup_stalled(info, now):
                    logger.warning(
                        "Reaper: subagent %s failed to start within %ds "
                        "(turn 0, no runtime launched), force-killing",
                        agent_id,
                        self._manager._startup_deadline,
                    )
                    try:
                        await self._manager._force_reap(
                            agent_id,
                            info,
                            now - (info._exec_started or now),
                            reason="startup_timeout",
                        )
                    except Exception:
                        logger.exception("Reaper: failed to reap %s", agent_id)
                    continue
                # Idle-stall detection (see _maybe_flag_stall). The main-agent
                # watchdog stack does not govern subagents; this is their
                # equivalent — surface a "stalled" UI signal well before the
                # 30-min ceiling. Surface-only: it never terminates the agent
                # (users close it from the UX), so we always fall through to
                # the wall-clock check below.
                await self._manager._maybe_flag_stall(agent_id, info, now)
                if elapsed <= self._manager._default_timeout:
                    continue
                logger.warning(
                    "Reaper: subagent %s exceeded %ds (ran %.0fs), force-killing",
                    agent_id,
                    self._manager._default_timeout,
                    elapsed,
                )
                try:
                    await self._manager._force_reap(agent_id, info, elapsed)
                except Exception:
                    logger.exception("Reaper: failed to reap %s", agent_id)

            # Prune stale tombstoned folders (>7 days old)
            try:
                pruned = await asyncio.get_running_loop().run_in_executor(
                    maintenance_executor(),
                    prune_stale_tombstones,
                    7,
                    self._manager._result_ttl_secs,
                )
                if pruned:
                    logger.info("Reaper: pruned %d stale tombstone(s)", pruned)
            except Exception:
                logger.debug("Reaper: tombstone pruning failed", exc_info=True)

    def _is_startup_stalled_impl(self, info: SubagentInfo, now: float) -> bool:
        """True if a subagent is wedged in startup and should be reaped early.

        A subagent qualifies only once it has actually entered execution
        (``_exec_started`` set by ``_run_inner``) yet has launched no runtime
        (``_pid is None``) and produced no turn (``turns == 0``) within
        ``_startup_deadline`` seconds. Keying on ``_exec_started`` — not the
        registration timestamp ``started`` — means an agent merely awaiting
        spawn approval (never entered ``_run_inner``) is never caught here.
        """
        exec_started = info._exec_started
        if exec_started is None:
            return False
        return (
            info.turns == 0
            and info._pid is None
            and (now - exec_started) > self._manager._startup_deadline
        )

    async def _stall_verdict_impl(self, info: SubagentInfo) -> tuple[str, str]:
        """Liveness verdict for an idle subagent: working, wedged, or unknown.

        Idle time alone cannot separate a hung tool call from a slow silent one,
        so this consults the same ``LivenessOracle`` the main agent's watchdog
        uses (:mod:`kiro_crew.acp.liveness`) for ``/proc`` evidence.

        The attribution is what makes it sound. With the in-flight tool's real
        ``is_shell`` + command, the consult takes the oracle's shell-child
        branch, which matches a live descendant by CMDLINE and then tracks that
        pid — so the evidence belongs to THIS subagent's own child even when the
        runtime is shared with sibling subagents. That is the distinction an
        earlier whole-subtree attempt could not make: a subtree aggregate is
        dominated by kiro-cli's own background socket/keepalive traffic, so a
        ``sleep``-only subagent read as "working" and was never flagged.

        Returns ``(verdict, evidence)``; any failure degrades to
        ``(VERDICT_UNKNOWN, ...)`` so the caller falls back to idle time.
        """
        if not info._pid:
            return VERDICT_UNKNOWN, "no runtime pid"
        tool = info._inflight_tool
        if tool is None:
            # Idle with no tool in flight is a model-wait, not a hung command.
            # The model-wait branch reads the whole runtime subtree, which is not
            # attributable on a shared runtime — so decline rather than guess.
            return VERDICT_UNKNOWN, "no tool in flight"
        if not tool.is_shell:
            # A non-shell MCP tool has no child process to match, so the oracle
            # can only offer the same unattributable subtree aggregate. Decline.
            return VERDICT_UNKNOWN, "non-shell tool — not attributable"
        if info._stall_oracle is None:
            info._stall_oracle = LivenessOracle()
        # The consult is a SYNCHRONOUS /proc filesystem walk (``iter_descendants``
        # over the runtime's descendant subtree, plus ``os.readlink`` on
        # ``/proc/<pid>/fd/*``, which can block on the very wedged fd being
        # investigated) — and this runs on the reaper's event loop, the same loop
        # that serves every chat turn and the liveness heartbeat, sweeping agents
        # serially. Inline, one wedged read freezes the gateway until the
        # loop-stall watchdog kills it. Offload it exactly as the main-agent path
        # does (``AcpSessionHandle._consult_oracle_offloaded``): bounded await,
        # and at most ONE outstanding walk per agent so a permanently wedged read
        # cannot leave a new blocked worker behind on every sweep.
        # ``consult_offloaded`` owns that whole sequence -- one outstanding walk per
        # holder, submission inside the guard, exception retrieval attached at
        # submission, every failure degrading to UNKNOWN -- for the two watchdog
        # paths that already depend on it, so a fix there lands here too.
        # ``SubagentInfo`` satisfies its ``ConsultFutureHolder`` protocol via
        # ``_consult_future``. That handle deliberately OUTLIVES snapshot
        # retirement: ``_clear_tool_dispatch`` bumps ``_stall_gen`` (which
        # invalidates a stale verdict, below) but leaves the future in place, so a
        # walk still wedged on a stuck fd keeps suppressing resubmission instead
        # of letting each later sweep strand another blocked worker.
        submitted_gen = info._stall_gen
        verdict = await consult_offloaded(
            info,
            info._stall_oracle.check_tool,
            (info._pid, tool),
            executor_factory=subprocess_executor,
            log_label=f"stall consult for {info.id}",
        )
        # The consult awaits, so fresh activity, a final tool result, or the next
        # dispatch can retire this snapshot while the walk is still running. A
        # verdict about a tool that is no longer in flight must not be applied to
        # whatever replaced it: DEAD/STUCK_INPUT skips the two-sweep confirmation,
        # so a stale one would flag an agent that has demonstrably resumed working.
        if info._stall_gen != submitted_gen:
            return VERDICT_UNKNOWN, "superseded mid-consult"
        return verdict

    async def _maybe_flag_stall_impl(self, agent_id: str, info: SubagentInfo, now: float) -> None:
        """Idle-stall detection for a running subagent (surface-only).

        A subagent that has started (>=1 turn or a live runtime PID) but has
        emitted no stream activity for ``_stall_idle_secs`` may be wedged in a
        hung tool call — or simply running one slow, silent command. Idle time
        cannot tell those apart, so the flag is gated on a ``LivenessOracle``
        consult (:meth:`_stall_verdict`) that attributes evidence to the
        subagent's OWN child process by cmdline match:

        * ``WORKING`` — a live matched child, so it is progressing: not flagged.
        * ``DEAD`` / ``STUCK_INPUT`` — the child exited with no result frame, or
          its subtree is flat and blocked on a tty/stdin read. That is positive
          evidence of a wedge, so it flags IMMEDIATELY, skipping the two-sweep
          confirmation the idle-time path needs.
        * ``UNKNOWN`` — no attributable evidence (no shell child to match, no
          tool in flight, unreadable ``/proc``). Falls back to idle time with the
          two-sweep confirmation, i.e. exactly the previous behaviour.

        Still deliberately *surface-only*: it emits a ``subagent_stalled`` UI
        signal and records the slow command, but NEVER terminates the agent, so
        a slow-but-healthy command can only ever produce a self-clearing badge.
        Escalating a ``DEAD`` verdict to an early reap would be a change to kill
        semantics and is intentionally NOT part of this; the wall-clock reaper at
        ``_TIMEOUT_SECS`` remains the only automatic terminator.
        """
        if not (info.turns > 0 or info._pid is not None):
            return
        # A subagent blocked on a human tool-approval prompt is healthy, not
        # stalled — the permission request bumps `turns` before the approval
        # wait, so without this a slow approval would be mislabelled idle.
        if info._awaiting_approval:
            return
        idle = now - info.last_activity
        if not info.stalled and idle > self._manager._stall_idle_secs:
            verdict, evidence = await self._manager._stall_verdict(info)
            if (
                verdict == VERDICT_WORKING
                and idle < self._manager._stall_idle_secs * _SUPPRESS_CEILING
            ):
                # Attributable progress in this subagent's own child: silent, not
                # stalled. Leave the suspicion open (do not reset
                # _stall_suspect_at) so the badge appears as soon as that child
                # stops moving or exits.
                #
                # The ceiling above bounds how long a WORKING reading may hold the
                # badge back, because attribution is not infallible: under
                # ``session_sharing`` two siblings running similar commands can
                # cmdline-match the SAME child, so a wedged agent can read WORKING
                # for as long as its sibling's child lives. Without a bound that
                # turns an old true positive into a permanent false negative —
                # strictly worse than the idle-time-only path it replaces. With
                # it, misattribution costs latency, not the signal.
                logger.debug(
                    "Reaper: subagent %s idle %.0fs but working (%s) — not flagging",
                    agent_id,
                    idle,
                    evidence,
                )
                return
            # A wedged verdict normally skips it: DEAD/STUCK_INPUT is positive
            # evidence about this agent's child rather than a guess from elapsed
            # silence, so dampening it would only delay a signal already earned.
            #
            # BUT that trust is only warranted when the cmdline match cannot have
            # landed on someone else's child. ``_SUPPRESS_CEILING`` exists
            # precisely because the match is fallible under a shared runtime, and
            # a DEAD derived from a fallible match is exactly as wrong as the
            # WORKING the ceiling bounds — a sibling's matched child exiting would
            # otherwise raise an immediate badge on a healthy agent, skipping the
            # very dampening added to keep the badge trustworthy at scale. So the
            # skip is withdrawn whenever another session could be the one being
            # measured, and the wedged verdict then earns its badge the same way
            # an idle-time guess does: by holding across two sweeps.
            #
            # The gate keys on ``session_sharing`` itself, not on a count of live
            # siblings, because the confusable co-tenant is not only a sibling:
            # ``_create_shared_session`` puts the subagent on the PARENT's
            # AcpRuntime ("one process hosts everything"), so ``info._pid`` is the
            # parent's process and the parent's own tool children are descendants
            # of it too. ``_live_shared_count`` iterates the subagent registry and
            # therefore cannot see the parent, so a LONE subagent counted 1 and
            # kept the fast path while still able to cmdline-match the parent's
            # child — and flag instantly when that child exited. Since a shared
            # runtime always has the parent in it, "could this match belong to
            # someone else?" is true for every session-sharing agent.
            wedged = verdict in (VERDICT_DEAD, VERDICT_STUCK_INPUT)
            if wedged and info._session_sharing:
                wedged = False
            # Two-sweep confirmation (scale dampening): at 60-100 concurrent
            # agents a single-window trip ambers several healthy-slow agents at
            # any moment, training users to ignore the badge. Require the idle
            # threshold to hold across TWO consecutive reaper sweeps before
            # flagging — a stream event between sweeps resets the suspicion
            # (_touch_activity clears both flags). Adds at most one sweep
            # interval (~60s) of latency to a genuine stall.
            if not wedged and info._stall_suspect_at <= 0.0:
                info._stall_suspect_at = now
                return
            info.stalled = True
            logger.warning(
                "Reaper: subagent %s idle %.0fs (verdict=%s; %s) — marking stalled",
                agent_id,
                idle,
                verdict,
                evidence,
            )
            # Persist the slow command for future analysis. Best-effort; must
            # not disturb the still-running agent (NOT a tombstone — the agent
            # is alive, not dead).
            self._manager._record_slow_command(info, idle)
            try:
                await self._manager._fire_event(
                    "subagent_stalled",
                    info,
                    # The verdict and its evidence are deliberately NOT on the
                    # wire: no consumer reads them (the frontend narrows this
                    # payload to {slot, id, stalled, idle_secs} on arrival, and
                    # the coalesced batch update forwards only `stalled`), and the
                    # event is app-sdk-forwarded, so shipping unread keys would
                    # create semi-permanent surface. The log line above records
                    # both for diagnosis; add them here when something renders it.
                    {"stalled": True, "idle_secs": int(idle)},
                )
            except Exception:
                logger.debug(
                    "Reaper: failed to emit subagent_stalled for %s", agent_id, exc_info=True
                )

    def task_memory_rows_impl(self) -> list[dict[str, object]]:
        """Per-running-task memory/CPU rows for the session-memory surface.

        Reads the samples the reaper sweep already takes (``_sample_live_costs``,
        every ``_REAPER_INTERVAL`` seconds) — this method itself does no ``/proc``
        work, so it is safe on the event loop. ``rss_mb`` is 0.0 until the first
        sweep observes the agent, which is why ``sampled`` is reported separately:
        a fresh task genuinely has no measurement yet, and rendering that as
        "0 MB" would be a lie.

        ``shared`` mirrors ``_session_sharing``: the value is that runtime's
        measurement divided by the number of concurrently-live sharing sessions
        on the same pid, i.e. an average share, not an exclusive figure. The same
        split applies to ``procs``/``mcp`` (see ``_attributed_count``), which are
        null until a sweep has counted them — a task row that reported no MCP
        stubs because the field was simply absent read as "subagents do not use
        the MCP pool", which is the opposite of what they do.
        """
        return [
            {
                "id": a.id,
                "task": _redact_and_truncate(a.task, 80),
                "agent": _redact(a.agent),
                "parent": a.parent_session_key,
                "rss_mb": round(a.last_rss_gb * 1024, 1),
                "peak_rss_mb": round(a.peak_rss_gb * 1024, 1),
                "cpu_cores": round(a.last_cpu_cores, 2),
                "procs": a.last_procs,
                "mcp": a.last_stubs,
                "started_at": a.started,
                "shared": a._session_sharing,
                "pid": a._pid,
                "sampled": a.last_rss_gb > 0.0 or a.peak_rss_gb > 0.0,
            }
            for a in self._manager._agents.values()
            if not a.done and not a.queued
        ]

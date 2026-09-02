"""Admission behavior for the SubagentManager facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._component import ManagerComponent

if TYPE_CHECKING:
    from ..subagent import (
        KiroCrewConfig,
        Stats,
        SubagentInfo,
        _context_groups_field,
        _validate_agent,
        _vet_spawn_governance,
        asyncio,
        cached_admission_check,
        check_memory_available,
        create_agent_folder,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        time,
        uuid,
        validate_cwd,
    )


class SpawnAdmissionCoordinator(ManagerComponent):
    """Own admission transitions while state remains facade-owned."""

    __slots__ = ()

    def spawn_impl(
        self,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        max_turns: int = 0,
        model: str | None = None,
        reasoning_effort: str = "",
        allowed_tools: list[str] | None = None,
        bare: bool = False,
        cwd: str = "",
        approval_mode: str | None = None,
        silent: bool = False,
        batch_id: str = "",
        batch_total: int = 0,
        keep: bool = False,
        conversation_key: str = "",
        app: str = "",
        include_memory: bool = True,
        include_lessons: bool = True,
        include_project: bool = True,
        _agent_prevalidated: bool = False,
        _from_queue: bool = False,
        _preassigned_id: str = "",
    ) -> SubagentInfo | None:
        """Spawn a subagent for *task*.

        Approval priority (first match wins):

        1. YOLO mode → immediate execution
        2. ``approval_mode="auto"`` from caller → immediate execution
        3. ``auto_approve_subagent_spawn`` config → auto-approved execution
        4. ``on_spawn_approval`` callback → interactive approval
        5. Otherwise → rejected

        When ``approval_mode="auto"`` is set, it has two effects:
        - Skips the spawn approval gate (this method)
        - Sets the subagent's session-level tool approval policy to
          "auto" in ``_run_inner()``, meaning all tool calls within
          the subagent are auto-approved for its entire lifetime.

        This dual behavior is intentional for headless callers (e.g.
        Mochi bg agent) that have no UI to respond to approval prompts.
        The parameter is only accepted via the internal ``POST /api/spawn``
        endpoint (requires X-Internal-Secret), not from LLM tool calls.

        Args:
            task (str): The prompt/task description for the subagent.
            parent_session_key (str): Session key of the caller.
            agent (str): Agent name override (default: "kirocrew").
            model (str): Model override for CC provider (ignored for ACP).
            reasoning_effort (str): Per-call reasoning-effort override; wins
                over the ``role_efforts['subagent']`` pin. ``""`` defers to it.
            allowed_tools (list): Tool allowlist for CC provider (ignored for ACP).
            bare (bool): Launch CC in bare mode (ignored for ACP).
            cwd (str): Optional absolute path where the subagent subprocess
                launches instead of the default ``subagent_<id>`` sandbox.
                Validated against ``AgentConfig.subagent_cwd_allowed_roots``;
                rejected spawns return a done ``SubagentInfo`` with ``error``
                set. Enables cwd-relative resource globs (``AGENTS.md``,
                ``.kiro/steering``, ``CLAUDE.md``) to resolve correctly.
            approval_mode (str | None): "auto" to skip spawn gate and
                set session-level auto-approve.  Only honored from
                authenticated internal callers (X-Internal-Secret).
            silent (bool): Suppress completion notifications.

        Returns:
            SubagentInfo | None: Agent metadata, or None if at capacity.
        """
        # Identity is assigned ONCE, here, and used by every exit path — the
        # queued return, each rejection, and the started record. That is what
        # makes the id the caller is handed the id it will actually see again:
        # ``spawn_run`` prints this id into its wave roster, and the dashboard
        # resolves a wave by matching those printed ids against live per-agent
        # events. A drained spawn passes the id it was queued under back in via
        # ``_preassigned_id``, so a member that waits behind the stagger /
        # concurrency gate keeps its identity across the round-trip instead of
        # being announced under one id and starting under another.
        agent_id: str = _preassigned_id or uuid.uuid4().hex[:8]
        # Submission accounting: count this member as
        # submitted BEFORE any rejection or queue/registration branching. A
        # member refused below (empty task, low memory, bad cwd, governance)
        # never registers and never completes — if it weren't counted here,
        # batch_members_pending() would see submitted < expected FOREVER and
        # the wave digest would never fire, permanently stranding every
        # sibling's held result. A queued member re-enters
        # spawn() via _drain_queue — never double-count it.
        if batch_id and not _from_queue:
            _bs = self._manager._batch_submitted.setdefault(batch_id, [0, max(0, int(batch_total))])
            _bs[0] += 1
            self._manager._batch_progress_ts[batch_id] = time.time()
        # --- Task guard: refuse empty/whitespace-only tasks (defense in depth).
        # The HTTP handler (api_spawn) and MCP tool schemas validate too, but
        # direct Python callers reach this choke point unvalidated. An empty
        # task produces a useless subagent and a blank Activity card. Must run
        # BEFORE the redaction below, which would raise on a None task. ---
        if not task or not task.strip():
            logger.warning("Subagent spawn refused: empty task (parent=%s)", parent_session_key)
            # Audit is best-effort: the rejection must be returned even if
            # SEL is unavailable (a graceful refusal must not become an
            # unhandled exception in api_spawn / MCP tool callers).
            try:
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_empty_task",
                    metadata={"agent": agent},
                )
            except Exception:
                logger.debug("SEL audit failed for empty-task rejection", exc_info=True)
            return self._manager._announce_rejection(
                SubagentInfo(
                    id=agent_id,
                    task="",
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error="spawn refused: task must be a non-empty string",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        # --- Redact task once for all SubagentInfo storage (raw task kept for kiro-cli prompt) ---
        _redacted_task = redact_credentials(redact_exfiltration_urls(task)[0])[0]

        # --- Memory guard: refuse to spawn if system memory is critically low ---
        try:
            min_mem = KiroCrewConfig.load().agent.spawn_min_memory_gb
        except Exception:
            min_mem = 4.0
        mem_ok, avail_gb = check_memory_available(min_gb=min_mem)
        if not mem_ok:
            logger.warning(
                "Subagent spawn refused: only %.2f GB available (min %.1f GB required)",
                avail_gb,
                min_mem,
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_low_memory",
                metadata={
                    "available_gb": avail_gb,
                    "min_gb": min_mem,
                    "task": _redacted_task[:120],
                },
            )
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                parent_session_key=parent_session_key,
                done=True,
                error=f"spawn refused: only {avail_gb:.1f} GB memory available (need {min_mem:.0f} GB)",
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )
            return self._manager._announce_rejection(info)

        # --- Admission gate: refuse NEW spawns while host memory posture is
        # critical. Complements the absolute spawn_min_memory_gb floor above
        # with the posture tier (resource_critical_gb) and shares its
        # off-switch (agent.admission_gate) with the cron scheduler's deferral
        # gate. This method is sync and runs on the gateway event loop, so it
        # reads the CACHED off-thread verdict — never inline config/procfs
        # I/O; bounded staleness is acceptable for pressure-shedding.
        # In-flight subagents are untouched; direct user chat turns are
        # not gated; fails open on an unknown posture. ---
        admission = cached_admission_check()
        if not admission.admitted:
            logger.warning("Subagent spawn refused: %s", admission.reason)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_memory_critical",
                metadata={
                    "available_gb": admission.available_gb,
                    "posture": admission.posture,
                    "task": _redacted_task[:120],
                },
            )
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                parent_session_key=parent_session_key,
                done=True,
                error=f"spawn refused: {admission.reason}",
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )
            return self._manager._announce_rejection(info)

        # --- CWD validation: reject bad paths before consuming a slot ---
        resolved_cwd = ""
        if cwd:
            try:
                allowed_roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
            except Exception:
                # Fail closed: if config is unavailable, treat cwd override as
                # disabled. Defaulting to the permissive default here would
                # silently re-enable the feature for admins who set
                # subagent_cwd_allowed_roots=[] to disable it.
                allowed_roots = []
            resolved_cwd, cwd_err = validate_cwd(cwd, allowed_roots)
            if cwd_err:
                logger.warning("Subagent spawn refused: invalid cwd %r: %s", cwd, cwd_err)
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_invalid_cwd",
                    metadata={"cwd": cwd[:200], "reason": cwd_err, "task": _redacted_task[:120]},
                )
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"spawn refused: {cwd_err}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                return self._manager._announce_rejection(info)

        # --- Governance: spawn capability gate (blast-radius containment) ---
        # A policy/profile may disable sub-agent spawning entirely, or bound it
        # to named agents (capabilities.spawn.scopes.agents).  Resolved against
        # the PARENT surface so a per-app/per-surface profile contains what it
        # can spawn — even if the kiro side would allow it.
        gov_spawn_err = _vet_spawn_governance(parent_session_key, agent, app=app)
        if gov_spawn_err:
            logger.warning("Subagent spawn refused by governance: %s", gov_spawn_err)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error=gov_spawn_err,
                metadata={"agent": agent, "task": _redacted_task[:120]},
            )
            return self._manager._announce_rejection(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"spawn refused by governance: {gov_spawn_err}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        now = time.monotonic()
        should_queue, slot_free = self._manager._should_stagger_queue(now)
        if should_queue:
            # A prevalidated app spawn must NOT sit in the queue. _agent_prevalidated
            # skips the agent-directory ownership scan on drain (it was validated
            # off the loop at request time); if it waited in the queue, the app
            # could be disabled and its agent file removed meanwhile, and the drain
            # would then run a same-named FOREIGN agent under the app's auto-approval
            # without re-checking ownership. Fail closed: reject so the caller
            # re-requests and re-validates ownership fresh. Only the app SpawnSDK
            # sets _agent_prevalidated, and because such a spawn never enters the
            # queue, a drain re-entry (_from_queue) never carries the flag.
            if _agent_prevalidated:
                logger.warning(
                    "Rejecting prevalidated app spawn that would queue "
                    "(agent=%s, app=%s): retry to revalidate ownership",
                    agent,
                    app,
                )
                return self._manager._announce_rejection(
                    SubagentInfo(
                        id=agent_id,
                        task=_redacted_task,
                        agent=agent,
                        parent_session_key=parent_session_key,
                        done=True,
                        error=(
                            "spawn queue is at capacity; the app spawn was not queued "
                            "to avoid a stale ownership check — retry to revalidate and "
                            "spawn"
                        ),
                        batch_id=batch_id,
                        batch_total=max(0, int(batch_total)),
                    )
                )
            # Carry this spawn's id (assigned at the top) in the queue entry so
            # the drained spawn runs under it. The identity must survive the
            # round-trip because it is the only handle the caller gets: spawn_run
            # prints the id this call returns, and the inline SubagentRunCard
            # resolves a wave by matching those printed ids against live
            # per-agent events. Returning a throwaway sentinel (the old
            # ``q<n>``) and minting a fresh uuid on drain meant every wave member
            # after the first was announced under an id no agent ever had — with
            # the default 2s stagger that is EVERY member after the first, so a
            # 2-agent wave permanently rendered "1 agent running" while the
            # sidebar and Subagents panel correctly showed 2.
            self._manager._queue.append(
                {
                    "task": task,
                    "parent_session_key": parent_session_key,
                    "agent": agent,
                    "max_turns": max_turns,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "allowed_tools": allowed_tools,
                    "bare": bare,
                    "cwd": resolved_cwd,
                    "approval_mode": approval_mode,
                    "silent": silent,
                    "batch_id": batch_id,
                    "batch_total": batch_total,
                    "keep": keep,
                    "conversation_key": conversation_key,
                    "app": app,
                    "include_memory": include_memory,
                    "include_lessons": include_lessons,
                    "include_project": include_project,
                    "_agent_prevalidated": _agent_prevalidated,
                    "_preassigned_id": agent_id,
                }
            )
            logger.info(
                "Subagent queued (%d running, %d queued, slot_free=%s)",
                self._manager._running_count,
                len(self._manager._queue),
                slot_free,
            )
            # Advisory UI signal: tell the chip how many agents are now waiting
            # to start for this parent so it can appear immediately and show a
            # "waiting" count instead of only running/completed ones.
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            # If a slot is free, no running agent will trigger the drain on
            # completion — schedule the staggered pump at the interval boundary
            # so the queued spawn still launches.
            if slot_free:
                delay = max(
                    0.0, self._manager._spawn_stagger_secs - (now - self._manager._last_spawn_ts)
                )
                try:
                    asyncio.get_event_loop().call_later(delay, self._manager._drain_queue)
                except RuntimeError:
                    pass  # no running loop (sync/test context)
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                app=app,
                queued=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
            return info

        # `_agent_prevalidated` skips the on-loop agent-directory scan: a caller
        # that already confirmed the agent exists OFF the loop (the app SpawnSDK
        # validates via `list_agents()` in a thread) would otherwise make
        # `_validate_agent` re-scan/stat every agent file synchronously here,
        # stalling chat and the heartbeat on a populated agents directory. Only
        # the app path sets it; every other caller still validates inline.
        if agent and not _agent_prevalidated:
            # Validate against the cwd the subagent will ACTUALLY run in. When no
            # explicit cwd was given the runtime falls back to the session pool's
            # cwd, so validating only the explicit value refused a project agent
            # kiro-cli would have loaded — the same interface asymmetry the project
            # scope exists to remove, just one layer down.
            effective_cwd = resolved_cwd or str(
                getattr(self._manager._sessions, "_pool_cwd", "") or ""
            )
            agent, err, err_code = _validate_agent(agent, effective_cwd)
            if err:
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent="",
                    parent_session_key=parent_session_key,
                    done=True,
                    error=err,
                    error_code=err_code,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                return self._manager._announce_rejection(info)

        info = SubagentInfo(
            id=agent_id,
            task=_redacted_task,
            parent_session_key=parent_session_key,
            agent=agent,
            app=app,
            approval_mode=approval_mode or "",
            silent=silent,
            max_turns=max_turns,
            model=model or "",
            reasoning_effort=reasoning_effort or "",
            allowed_tools=list(allowed_tools) if allowed_tools else [],
            bare=bare,
            cwd=resolved_cwd,
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
            keep=keep,
            conversation_key=conversation_key,
            include_memory=include_memory,
            include_lessons=include_lessons,
            include_project=include_project,
        )
        info._raw_task = task  # unredacted prompt for kiro-cli execution
        self._manager._agents[agent_id] = info
        self._manager._running_count += 1
        self._manager._last_spawn_ts = time.monotonic()  # stagger gate: one start per interval
        # Batch lifecycle: announce the wave ONCE, on its first member to
        # actually start (queued members haven't started yet — the event marks
        # execution begin, and the UI uses it to key batch progress).
        if batch_id and batch_id not in self._manager._seen_batches:
            self._manager._seen_batches.add(batch_id)
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(
                    self._manager._fire_event(
                        "spawn_batch_started",
                        info,
                        {"batch_id": batch_id, "count": info.batch_total},
                    )
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)

        # Check parent session trust (approval_policy="auto") set by dashboard trust toggle.
        parent_trusted = (
            parent_session_key
            and self._manager._sessions.get_approval_policy(parent_session_key) == "auto"
        )

        if self._manager._is_yolo and self._manager._is_yolo():
            self._manager._tasks[agent_id] = asyncio.create_task(self._manager._run(info))
            self._manager._log_spawned(info)
        elif approval_mode == "auto":
            self._manager._tasks[agent_id] = asyncio.create_task(self._manager._run(info))
            self._manager._log_spawned(info)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="auto_approved_spawn",
                metadata={"subagent_id": agent_id, "reason": "approval_mode_auto"},
            )
        elif parent_trusted:
            self._manager._tasks[agent_id] = asyncio.create_task(self._manager._run(info))
            self._manager._log_spawned(info)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="auto_approved_spawn",
                metadata={"subagent_id": agent_id, "reason": "parent_trusted"},
            )
        elif self._manager._ctx_builder and self._manager._ctx_builder.hooks:
            if self._manager._ctx_builder.hooks.auto_approve_subagent_spawn is True:
                self._manager._tasks[agent_id] = asyncio.create_task(self._manager._run(info))
                self._manager._log_spawned(info)
                sel().log_tool_invocation(
                    session_key=info.parent_session_key,
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="auto_approved_spawn",
                    metadata={"subagent_id": agent_id, "reason": "tool_calls_gated"},
                )
            elif self._manager._on_spawn_approval:
                self._manager._tasks[agent_id] = asyncio.create_task(
                    self._manager._spawn_with_approval(info)
                )
            else:
                info.done = True
                info.error = "spawn rejected: no approval mechanism configured"
                self._manager._running_count -= 1
                self._manager._drain_queue()
                sel().log_tool_invocation(
                    session_key=info.parent_session_key,
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_spawn",
                    metadata={"subagent_id": agent_id, "reason": "no_approval_mechanism"},
                )
                # Batch members must still reach the gateway's completion
                # consumer: this is a REGISTERED rejection
                # (done=True in _agents), so batch_members_pending() already
                # counts it as complete — without an announce, a wave whose
                # final member lands here closes with no event and every held
                # sibling digest strands forever.
                return self._manager._announce_rejection(info)
        elif self._manager._on_spawn_approval:
            self._manager._tasks[agent_id] = asyncio.create_task(
                self._manager._spawn_with_approval(info)
            )
        else:
            info.done = True
            info.error = "spawn rejected: no approval mechanism configured"
            self._manager._running_count -= 1
            self._manager._drain_queue()
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": agent_id, "reason": "no approval mechanism"},
            )
            logger.warning("Subagent %s rejected: no approval callback", agent_id)
            if self._manager._on_done:
                self._manager._tasks[agent_id] = asyncio.ensure_future(
                    self._manager._safe_announce(info)
                )

        return info

    async def _safe_announce_impl(self, info: SubagentInfo) -> None:
        """Notify completion callback with error handling.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._manager._on_done is not None
        try:
            await self._manager._on_done(info)
        except Exception:
            logger.exception("Subagent announce failed for %s", info.id)

    def _announce_rejection_impl(self, info: SubagentInfo) -> SubagentInfo:
        """Route a terminal spawn rejection through the done callback.

        A rejected batch member is counted as submitted (top of ``spawn``)
        but never registers and never reaches ``_run``'s completion path.
        Without an announce, the gateway's wave accounting never sees its
        terminal state — and when the rejection is the wave's FINAL
        submission, no later completion event re-evaluates the wave, so
        every sibling result already held for the digest strands forever.
        Announcing lets ``_subagent_done`` count the member
        as failed and release the digest when it closes the wave.

        Non-batch rejections skip the announce: the caller already receives
        the error synchronously in the returned info, and injecting a
        completion turn for them would double-report. That holds for
        queue-drained non-batch rejections too — ``_drain_queue`` announces
        those itself off the returned info, so announcing here as well would
        inject the completion twice.
        """
        if info.batch_id and self._manager._on_done:
            try:
                self._manager._tasks[f"reject-{info.id}"] = asyncio.ensure_future(
                    self._manager._safe_announce(info)
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
        return info

    def _should_stagger_queue_impl(self, now: float) -> tuple[bool, bool]:
        """Decide whether a spawn arriving at *now* must be queued.

        Returns ``(should_queue, slot_free)``. A spawn is queued when either no
        slot is free (at capacity) OR a spawn started within the stagger window
        (``subagent_spawn_stagger_secs``) — so the initial fill never bursts and
        no two agents start within the interval (dynamic-subagent-sizing.md §5.3).
        """
        slot_free = self._manager._running_count < self._manager._max_concurrent
        too_soon = (now - self._manager._last_spawn_ts) < self._manager._spawn_stagger_secs
        return (not slot_free or too_soon, slot_free)

    def _drain_queue_impl(self) -> None:
        """Spawn the next queued task if a slot is available and the stagger
        interval has elapsed.

        This is the single staggered pump: at most one start per
        ``subagent_spawn_stagger_secs`` (dynamic-subagent-sizing.md §5.3). If a
        slot is free but a spawn started too recently, it reschedules itself at
        the interval boundary rather than bursting.
        """
        if (
            not self._manager._queue
            or self._manager._running_count >= self._manager._max_concurrent
        ):
            return
        elapsed = time.monotonic() - self._manager._last_spawn_ts
        if elapsed < self._manager._spawn_stagger_secs:
            # Too soon since the last start — reschedule at the boundary.
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._spawn_stagger_secs - elapsed, self._manager._drain_queue
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
            return
        params = self._manager._queue.pop(0)
        # A run can be cancelled WHILE it waits here — a user stop, or a session
        # deleted out from under it. Starting it anyway would execute tools for
        # work already reported as stopped, so skip it and drain the next one
        # instead: `cancel()` marks the info terminal but cannot unqueue this.
        queued_id = str(params.get("_preassigned_id") or "")
        if queued_id:
            waiting = self._manager._agents.get(queued_id)
            if waiting is not None and (waiting.done or waiting.user_stopped or waiting.reaped):
                logger.info("Skipping queued spawn %s: cancelled while waiting", queued_id)
                self._manager._emit_queue_depth(
                    str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
                )
                if self._manager._queue:
                    self._manager._drain_queue()
                return
        logger.info(
            "Draining queue: spawning '%s' (%d left)",
            str(params.get("task", ""))[:40],
            len(self._manager._queue),
        )
        # The popped item's parent just lost one waiting agent — re-emit its
        # queued depth (0 when this was its last) so the chip's "waiting" count
        # tracks the drain. Done before spawn() so an immediate re-queue there
        # (still too soon since last start) re-bumps it correctly afterwards.
        self._manager._emit_queue_depth(
            str(params.get("parent_session_key", "")), str(params.get("batch_id", ""))
        )
        # spawn() re-checks the gate; since elapsed >= stagger and a slot is
        # free, it starts immediately and updates _last_spawn_ts. Forward the FULL
        # kwarg set so approval_mode / silent / model / allowed_tools / bare survive
        # the queue round-trip — including `_preassigned_id`, which makes the agent
        # start under the id its caller was already told (and, if the gate re-queues
        # it, keeps that id across the second round-trip too).
        drained = self._manager.spawn(**params, _from_queue=True)
        # A drained spawn has NO synchronous reader: this call site is a timer
        # callback, and the original caller was handed a queued info long ago. So a
        # terminal rejection here — the cwd was deleted while the run waited, the
        # agent stopped resolving — was dropped on the floor: no completion event,
        # and the caller's own bookkeeping showed the run as still going. Crew left
        # such a topic `running` forever.
        #
        # Only for NON-batch runs, which is exactly the set `_announce_rejection`
        # skips (it announces batch members itself, from inside `spawn`). Announcing
        # regardless double-counted a queued batch rejection: the wave's own
        # accounting closed early and emitted a duplicate or incomplete digest.
        if (
            drained is not None
            and drained.done
            and drained.error
            and not drained.batch_id
            and self._manager._on_done
        ):
            try:
                self._manager._tasks[f"reject-{drained.id}"] = asyncio.ensure_future(
                    self._manager._safe_announce(drained)
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
        if self._manager._queue and self._manager._running_count < self._manager._max_concurrent:
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._spawn_stagger_secs, self._manager._drain_queue
                )
            except RuntimeError:
                pass

    async def _spawn_with_approval_impl(self, info: SubagentInfo) -> None:
        """Request approval before starting the subagent.

        If approval is denied the subagent is marked as done with an
        error and the running count is decremented without executing.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._manager._on_spawn_approval is not None
        request_id: str = f"spawn:{info.id}"
        try:
            from kiro_crew.security import (
                redact_credentials,
                redact_exfiltration_urls,
            )

            task_safe, _ = redact_exfiltration_urls(info.task)
            task_safe, _ = redact_credentials(task_safe)
            task_preview: str = task_safe[:80]
            # Mark the pre-execution spawn gate as a human-wait so the reaper
            # does not misreport it. This is the SAME lifecycle the mid-run TOOL
            # approvals use in run.py: set before the await, cleared in a
            # finally. The run has NOT started here (_exec_started is None),
            # which is exactly what lets _force_reap distinguish a never-answered
            # spawn approval from a mid-run tool prompt and report the accurate
            # cause.
            info._awaiting_approval = True
            # Name the wait as well as marking it. The flag above is machine
            # state read by the reaper and (now) by the wire; this is the line an
            # operator gets. #6484's reporter had no lead at all because
            # ``kirocrew logs`` held no record keyed to the affected run id —
            # while a wait with no deadline of its own held the run at turn 0.
            # ``parent_session_key`` is in the record on purpose: an unowned
            # spawn (the CLI posts none) raises its prompt with ``slot=""``, so
            # it is surfaced only on the global approvals feed and appears in no
            # chat tab, which is the case with the least other evidence.
            logger.info(
                "Subagent %s awaiting spawn approval (request_id=%s, parent=%s)",
                info.id,
                request_id,
                info.parent_session_key or "<unowned>",
            )
            try:
                approved: bool = await self._manager._on_spawn_approval(
                    request_id, f"spawn_run({task_preview})", info.parent_session_key
                )
            finally:
                info._awaiting_approval = False
        except Exception:
            logger.exception("Spawn approval failed for %s", info.id)
            approved = False

        if not approved:
            info.done = True
            info.error = "spawn rejected"
            # Slot accounting through the one-shot token, NOT a bare decrement.
            # A user Stop funnels into `_force_reap` and can land while this
            # approval is still pending (a human prompt has no deadline), and
            # `_force_reap` releases the slot and reports. A bare decrement here
            # would double-release — driving `_running_count` negative — and the
            # announce below would double-report the completion.
            if self._manager._release_slot(info):
                self._manager._running_count -= 1
                self._manager._drain_queue()
            self._manager._tasks.pop(info.id, None)
            sel().log_tool_invocation(
                session_key=info.parent_session_key,
                source="subagent",
                tool_name="spawn_run",
                outcome="rejected",
                metadata={"subagent_id": info.id},
            )
            logger.info("Subagent %s spawn rejected", info.id)
            # Report ownership through the same claim every other terminal path
            # uses, so a concurrent reap/stop cannot also announce.
            if self._manager._on_done and self._manager._claim_finalize(info):
                await self._manager._safe_announce(info)
            return

        self._manager._log_spawned(info)
        await self._manager._run(info)

    def _log_spawned_impl(self, info: SubagentInfo) -> None:
        """Record spawn metrics and audit log entry.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        # Persist agent folder to disk for orphan recovery
        try:

            create_agent_folder(
                info.id,
                task=info.task,
                agent=info.agent,
                parent_session=info.parent_session_key,
                max_turns=info.max_turns,
                context_groups=_context_groups_field(info),
            )
        except Exception:
            logger.warning("Failed to create agent folder for %s", info.id, exc_info=True)

        Stats().inc_subagent_spawned()
        # Beside that stat, and for the same reason: this is the confirmed-start
        # funnel. Every path reaches it only AFTER the spawn is approved -- the
        # approval path calls it once the user allows and returns earlier on a
        # rejection -- so a rejected or unstarted spawn is never counted, which
        # the admission-time increment could not promise. ``concurrency`` is the
        # live running count, bounded by ``_max_concurrent``, so the aggregator's
        # MAX over that attribute is the concurrency high-water mark without a
        # second instrument.
        #
        # Imported HERE, not at module scope: ``bind_component_globals`` rebinds
        # every ``*_impl`` function's ``__globals__`` to ``subagent``'s namespace
        # for patch compatibility, so a module-level import in this file is not
        # visible from inside this function at all.
        try:
            from kiro_crew.metrics.events import SUBAGENTS_SPAWNED, emit_counter

            emit_counter(
                SUBAGENTS_SPAWNED,
                {
                    "concurrency": self._manager._running_count,
                    "batched": bool(getattr(info, "batch_id", "")),
                },
            )
        except Exception:
            logger.debug("subagent spawned counter failed", exc_info=True)
        sel().log_tool_invocation(
            session_key=info.parent_session_key,
            source="subagent",
            tool_name="spawn_run",
            outcome="spawned",
            metadata={
                "subagent_id": info.id,
                "agent": info.agent or "kirocrew",
                "cwd": info.cwd,
            },
        )
        logger.info("Subagent %s spawned: %s", info.id, info.task[:80])

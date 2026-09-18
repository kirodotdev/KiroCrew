"""The spawn gate: every policy and capacity check between a request and its row (``spawn_impl``)."""

from __future__ import annotations

import logging as _logging
from typing import TYPE_CHECKING

from .._component import ManagerComponent
from .types import ClaimPoint, PreparedSpawn

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    pass

    from ...subagent import (
        KiroCrewConfig,
        SubagentInfo,
        _validate_agent,
        _validate_app_agent_ownership,
        _vet_spawn_governance,
        asyncio,
        cached_admission_check,
        check_memory_available,
        logger,
        platform_compat,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        time,
        uuid,
        validate_cwd,
    )


class _GateMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        CLAIM_UNAVAILABLE: str

        TASK_STORE_UNAVAILABLE_CODE: str

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
        memory_store: str = "",
        _agent_prevalidated: bool = False,
        _from_queue: bool = False,
        _preassigned_id: str = "",
        _store_accepted: bool = False,
        _prepare_only: bool = False,
        _stop_before_claim: bool = False,
        _claimed: "tuple[int, bool, str] | None" = None,
        _window_hint: "bool | None" = None,
        _child_registration: bool = True,
        _memory_mode: str | None = None,
        *,
        crew: str = "",
    ) -> "SubagentInfo | PreparedSpawn | ClaimPoint | None":
        """Spawn a subagent for *task*.

        Approval priority (first match wins):

        1. YOLO mode → immediate execution
        2. ``approval_mode="auto"`` from caller → immediate execution
        3. parent session trust (``approval_policy == "auto"``, the dashboard
           Trust toggle) → auto-approved execution
        4. ``auto_approve_subagent_spawn`` config → auto-approved execution
        5. ``on_spawn_approval`` callback → interactive approval, unless the
           callback reports it has no surface to raise the prompt on, in which
           case the spawn is refused immediately (see
           ``_spawn_with_approval_impl``)
        6. Otherwise → rejected

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
        # sibling's held result. Counted exactly ONCE, on the FIRST entry: a
        # queued member re-enters via _drain_queue and an accepted
        # ``spawn_async`` member re-enters with ``_store_accepted`` -- neither
        # is a new submission. The prepare pass IS the first entry, so a
        # member ``prepare_spawn`` refuses is counted like any other refusal,
        # which is what makes ``/api/spawn``'s ``counted: true`` true.
        if batch_id and not _from_queue and not _store_accepted:
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

        # Synchronous and yield-free with registration below: a spawn is either
        # visible to the updater's busy count before the pause, or rejected after
        # SessionManager closes admission. MagicMock-based embedders only block
        # when they expose the literal boolean True.
        if getattr(self._manager._sessions, "admission_closed", False) is True:
            return self._manager._announce_rejection(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error="spawn refused: gateway admission is closed",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        def _refuse_row(info: SubagentInfo) -> SubagentInfo:
            """A policy refusal of a spawn whose row ALREADY exists (a drained
            row the pump re-checks) marks that row failed in the same step,
            so the refusal the caller sees is also the store's verdict and
            the pump can never dispatch work that was refused."""
            if _from_queue and info.error:
                self._manager._admission.taskq_fail(agent_id, info.error)
            return self._manager._announce_rejection(info)

        # The mutable policy gates (memory identity, cwd allowlist,
        # governance) run ONCE per submission: on the first entry, and again
        # when the pump drains a stored row (the re-check before dispatch). An
        # accepted ``spawn_async`` row re-entering with ``_store_accepted``
        # passed them moments ago in ``prepare_spawn`` and must not be refused
        # AFTER its row was committed -- a refusal here would leave executable
        # work queued while the caller was told it was refused.
        _gate = not _store_accepted and _claimed is None
        # ``_claimed`` is the second half of the event-loop dispatcher's split:
        # the first half stopped at ``_stop_before_claim`` with every gate
        # passed AND the slot reserved (running count + stagger token taken
        # synchronously), the claim was taken on the writer thread, and this
        # re-entry goes straight to registration, CONSUMING that reservation.
        # The capacity gates are not re-run: the reservation is the slot, and a
        # second check would read our own reservation as a full cap.
        _dispatch_now = _claimed is not None

        # Freeze before queueing or awaiting approval; a replacement parent must
        # not change the mode of work already admitted under its predecessor. A
        # re-entry carries the frozen mode in its params, so this only re-checks
        # it.
        try:
            if _memory_mode is None:
                resolver = self._manager._memory_mode_for_session
                _memory_mode = (
                    resolver(parent_session_key) if resolver is not None else "persistent"
                )
            if not isinstance(_memory_mode, str) or _memory_mode not in {
                "persistent",
                "incognito",
                "temporary",
            }:
                raise ValueError("unknown memory mode")
        except Exception:
            return _refuse_row(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    parent_session_key=parent_session_key,
                    done=True,
                    error="memory_unavailable: the parent's memory mode could not be established",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        # Validate before queueing/starting. An explicit private identity may
        # never degrade to V1 after deletion, a config error, or a restart.
        try:
            if not isinstance(memory_store, str):
                raise ValueError("the supplied memory identity is malformed")
            if memory_store and _gate:
                from kiro_crew.memory_stores import require_memory_store

                memory_store = require_memory_store(memory_store)
        except (OSError, ValueError) as exc:
            return _refuse_row(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"memory_unavailable: {exc}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        # --- CWD validation: reject bad paths before consuming a slot ---
        resolved_cwd = cwd if cwd and not _gate else ""
        if cwd and _gate:
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
                return _refuse_row(info)

        # --- Governance: spawn capability gate (blast-radius containment) ---
        # A policy/profile may disable sub-agent spawning entirely, or bound it
        # to named agents (capabilities.spawn.scopes.agents).  Resolved against
        # the PARENT surface so a per-app/per-surface profile contains what it
        # can spawn — even if the kiro side would allow it.
        gov_spawn_err = _vet_spawn_governance(parent_session_key, agent, app=app) if _gate else None
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
            return _refuse_row(
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

        # --- Persist BEFORE any resource check: write-before-ack. Policy refusals
        # above (empty task, memory identity, cwd, governance) never reach the
        # store, so a refused spawn leaves no row; from here on the row exists
        # and every later exit either starts it, defers it, or marks it failed.
        # A drained spawn (_from_queue) already has its row. ---
        queue_params: dict = {
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
            # Queued alongside the context triple, and for the same
            # reason: the drain re-enters `spawn` from this dict alone, so
            # a field missing here is a scope the run silently regains.
            # For the store that means a delegation which happened to hit
            # the concurrency gate runs against the GLOBAL memory instead
            # of the crew it was handed to.
            "memory_store": memory_store,
            "crew": crew,
            "_memory_mode": _memory_mode,
            "_agent_prevalidated": _agent_prevalidated,
            "_preassigned_id": agent_id,
        }
        if _prepare_only:
            # ``spawn_async``: every policy gate above has passed; hand back the
            # row to write OFF-LOOP, then re-enter with ``_store_accepted``.
            return PreparedSpawn(
                agent_id=agent_id,
                params=dict(queue_params),
                record=self._manager._admission.taskq_build_record(
                    agent_id,
                    queue_params,
                    parent_session_key=parent_session_key,
                    memory_store=memory_store,
                    app=app,
                    model=model,
                    allowed_tools=allowed_tools,
                    approval_mode=approval_mode,
                ),
            )
        if not _from_queue and not _store_accepted:
            store_err = self._manager._admission.taskq_accept(
                agent_id,
                queue_params,
                parent_session_key=parent_session_key,
                memory_store=memory_store,
                app=app,
                model=model,
                allowed_tools=allowed_tools,
                approval_mode=approval_mode,
            )
            if store_err:
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="refused_task_store",
                    metadata={"error": store_err[:200], "subagent_id": agent_id},
                )
                return self._manager._announce_rejection(
                    SubagentInfo(
                        id=agent_id,
                        task=_redacted_task,
                        agent=agent,
                        parent_session_key=parent_session_key,
                        done=True,
                        error=f"spawn refused: task store unavailable ({store_err})",
                        error_code=self.TASK_STORE_UNAVAILABLE_CODE,
                        batch_id=batch_id,
                        batch_total=max(0, int(batch_total)),
                    )
                )
        _durable = self._manager._admission.taskq_store() is not None

        def _deferred(reason: str, refused: SubagentInfo) -> SubagentInfo | None:
            # Pressure is a scheduling fact, not a verdict on the task: the row
            # stays queued, holds nothing, and is re-checked after the admit
            # wait. None when the store holds no such row (a legacy in-memory
            # entry): there is nothing durable to park, so the caller refuses --
            # ``_from_queue`` alone does not prove a row exists, because
            # ``_queue`` also holds entries that never reached the store, so the
            # write's BOOLEAN is what separates the two and is never discarded.
            # WHERE that write runs is the caller's: a row this very call wrote
            # (``_store_accepted``) is queued either way and posts it, a
            # coroutine dispatcher (``_stop_before_claim``) owns every DB phase
            # and gets it parked with both answers, and only a caller with no
            # loop to hand it to takes ``BEGIN IMMEDIATE`` here -- on the loop
            # that wait is the whole busy timeout, with chat and the heartbeat
            # behind it.
            queued = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                app=app,
                parent_session_key=parent_session_key,
                queued=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
            if _store_accepted:
                self._manager._admission.taskq_defer_posted(agent_id, reason=reason)
            elif _stop_before_claim:
                self._manager._admission.park_defer(
                    agent_id,
                    reason=reason,
                    parent_session_key=parent_session_key,
                    batch_id=batch_id,
                    queued=queued,
                    refused=refused,
                )
                return queued
            elif not self._manager._admission.taskq_defer(agent_id, reason=reason):
                return None
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            return queued

        # --- Memory guard: defer (durable) or refuse (legacy) while host memory
        # is critically low. ---
        try:
            min_mem = KiroCrewConfig.load().agent.spawn_min_memory_gb
        except Exception:
            min_mem = 4.0
        mem_ok, avail_gb = (True, -1.0) if _dispatch_now else check_memory_available(min_gb=min_mem)
        if not mem_ok:
            logger.warning(
                "Subagent spawn %s: only %.2f GB available (min %.1f GB required)",
                "deferred" if _durable else "refused",
                avail_gb,
                min_mem,
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="deferred_low_memory" if _durable else "refused_low_memory",
                metadata={
                    "available_gb": avail_gb,
                    "min_gb": min_mem,
                    "task": _redacted_task[:120],
                },
            )
            # Built ahead of the deferral, not after it: a parked defer whose
            # write finds no row answers with this same refusal, off-loop.
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
            deferred = (
                _deferred(f"low memory: {avail_gb:.1f} GB available, need {min_mem:.0f} GB", info)
                if _durable
                else None
            )
            if deferred is not None:
                return deferred
            return self._manager._announce_rejection(info)
        if avail_gb < 0 and platform_compat.IS_LINUX and not _dispatch_now:
            # A negative reading means the guard did not run: /proc/meminfo is
            # unreadable on the one platform where it must exist. Proceeding
            # is the stated fail-open contract for an unmeasurable host, but
            # on Linux it must be observable rather than indistinguishable
            # from a healthy check. macOS/Windows structurally lack
            # /proc/meminfo, so emitting there would fire on every spawn and
            # drown the signal.
            logger.warning(
                "Subagent memory guard could not run (min %.1f GB); proceeding unchecked",
                min_mem,
            )
            # Context-aware pass so a host with a companion loaded is not
            # audited with the weaker OSS baseline (the census gate in
            # test_security_posture.py pins the baseline site count). Imported
            # here because this function runs rebound on the subagent module's
            # namespace, where a module-level import in this file is inert
            # (see _component.bind_component_globals). The slice comes AFTER
            # redaction: slicing first could split a companion-only credential
            # at the boundary and persist an unmatched fragment.
            from kiro_crew.platform.context import redact_log_via_context

            task_note = redact_log_via_context(_redacted_task)[:120]
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="memory_check_unavailable",
                metadata={"min_gb": min_mem, "task": task_note},
            )

        # --- Admission gate: DEFER new spawns while host memory posture is
        # critical (refuse only when no durable store backs the deferral).
        # Complements the absolute spawn_min_memory_gb floor above with the
        # posture tier (resource_critical_gb) and shares its off-switch
        # (agent.admission_gate) with the cron scheduler's deferral gate. This
        # method is sync and runs on the gateway event loop, so it reads the
        # CACHED off-thread verdict -- never inline config/procfs I/O; bounded
        # staleness is acceptable for pressure-shedding. In-flight subagents
        # are untouched; direct user chat turns are not gated; fails open on
        # an unknown posture. ---
        admission = cached_admission_check()
        if not admission.admitted and not _dispatch_now:
            logger.warning(
                "Subagent spawn %s: %s", "deferred" if _durable else "refused", admission.reason
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="deferred_memory_critical" if _durable else "refused_memory_critical",
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
            deferred = _deferred(str(admission.reason), info) if _durable else None
            if deferred is not None:
                return deferred
            return self._manager._announce_rejection(info)

        now = time.monotonic()
        should_queue, slot_free = self._manager._should_stagger_queue(now)
        if _dispatch_now:
            should_queue = False
        # Child reserve (RFC §6, Q3): a depth-0 start may not take the last
        # reserved slot(s) while nested work is pending or a parent waits on
        # its children; only children and resuming parents may. The gate
        # above answered for the whole cap, so narrow it here for roots.
        _is_child = bool(self._manager._admission.taskq_parent_id_for(parent_session_key))
        if (
            not should_queue
            and not _dispatch_now
            and not _is_child
            and not self._manager._admission.root_may_start()
        ):
            should_queue, slot_free = True, False
        if should_queue:
            # A prevalidated app spawn does not carry its prevalidation INTO the
            # queue. `_agent_prevalidated` skips the agent-directory ownership
            # scan (it was validated off the loop at request time); while the
            # spawn waits, the app could be disabled and its agent file removed,
            # and the drain would then run a same-named FOREIGN agent under the
            # app's auto-approval. Capacity is a scheduling fact, not a verdict:
            # the row was accepted (write-before-ack), so it QUEUES like any
            # other spawn -- with the flag cleared, so the drain re-validates
            # the agent AND, for an app spawn, re-proves app ownership
            # (`_validate_app_agent_ownership`) before it starts.
            if _agent_prevalidated:
                queue_params["_agent_prevalidated"] = False
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
            # The in-memory queue is a bounded WINDOW over the store's queued
            # rows: a new row joins it only when there is room and no older
            # row is waiting outside it (FIFO across the boundary); otherwise
            # it waits on disk and the drain's refill brings it in. A drained
            # spawn that hit the stagger gate re-joins the window directly --
            # it is already the oldest eligible row.
            if _from_queue or (
                _window_hint
                if _window_hint is not None
                else self._manager._admission.taskq_should_window(agent_id)
            ):
                self._manager._queue.append(queue_params)
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
                parent_session_key=parent_session_key,
                queued=True,
                memory_mode=_memory_mode,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
            # A queued child still blocks a parent waiting in spawn_sub_agents:
            # the parent yields its slot now, which is what lets the queue it
            # is waiting on actually drain (taskq.waits, W3). An event-loop
            # caller (``_child_registration=False``) runs that branch itself,
            # awaited, with its store reads and writes on the writer thread.
            if _child_registration:
                self._manager._admission.taskq_child_registered(info)
            return info

        # `_agent_prevalidated` skips the on-loop agent-directory scan: a caller
        # that already confirmed the agent exists OFF the loop (the app SpawnSDK
        # validates via `list_agents()` in a thread) would otherwise make
        # `_validate_agent` re-scan/stat every agent file synchronously here,
        # stalling chat and the heartbeat on a populated agents directory. Only
        # the app path sets it; every other caller still validates inline.
        if agent and app and not _agent_prevalidated:
            # An app spawn that waited in the queue re-proves ownership here:
            # the same filename-prefix test the SpawnSDK ran off-loop at request
            # time (an app may only run its OWN materialized agents).
            owner_err = _validate_app_agent_ownership(agent, app)
            if owner_err:
                self._manager._admission.taskq_fail(agent_id, owner_err)
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    agent=agent,
                    app=app,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=owner_err,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                return self._manager._announce_rejection(info)
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
                # The row was accepted; an agent name that does not resolve at
                # dispatch is a terminal failure of THAT row, never a silent drop.
                self._manager._admission.taskq_fail(agent_id, err)
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

        # --- Atomic claim: the ONE write that takes the row for dispatch. It
        # bumps the generation every later write is fenced with, and it fails
        # for a row cancelled while it waited (the store is re-read here, after
        # the wait, which is what makes cancel-vs-drain safe). ---
        if _claimed is not None:
            taskq_generation, proceed, claim_reason = _claimed
        else:
            if _stop_before_claim and self._manager._admission.taskq_store() is not None:
                # Reserve-then-commit: take the slot NOW, before the caller
                # awaits the claim, so nothing admitted during that await can
                # overshoot the cap or skip the stagger.
                self._manager._running_count += 1
                self._manager._last_spawn_ts = time.monotonic()
                return ClaimPoint(agent_id)
            taskq_generation, proceed, claim_reason = self._manager._admission.taskq_claim(agent_id)
        if not proceed and claim_reason == self.CLAIM_UNAVAILABLE:
            # The store could not take the row (busy / unavailable). Starting
            # anyway would run work no lease tracks -- generation 0, invisible
            # to reconcile, restartable by the next pump. The row stays
            # ``queued`` on disk; the caller keeps a QUEUED handle and the
            # pump retries after the admit wait.
            logger.warning("taskq: claim of %s unavailable; left queued for the pump", agent_id)
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            try:
                asyncio.get_event_loop().call_later(
                    self._manager._admission.taskq_admit_wait_secs(), self._manager._drain_queue
                )
            except RuntimeError:
                pass
            return SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                app=app,
                parent_session_key=parent_session_key,
                queued=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
        if not proceed:
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            return SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                parent_session_key=parent_session_key,
                queued=True,
                done=True,
                user_stopped=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )

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
            memory_store=memory_store or "",
            crew=crew,
            memory_mode=_memory_mode,
        )
        info._raw_task = task  # unredacted prompt for kiro-cli execution
        info._memory_mode_ready = not bool(conversation_key)
        info._taskq_generation = taskq_generation
        self._manager._agents[agent_id] = info
        if not _dispatch_now:  # a ClaimPoint re-entry already holds its reservation
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
                self._manager._admission.taskq_settle(info)
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

        if info.done:
            # Rejected after the claim (no approval mechanism): terminal in the
            # store too, under the generation the claim minted.
            self._manager._admission.taskq_settle(info)
        else:
            # Registered and handed to a run (or to the approval prompt, which
            # is part of starting): admitted -> starting. ``running`` is written
            # by the run itself at its first stream event.
            self._manager._admission.taskq_mark(info, "starting")
            # Nested: a parent blocked in spawn_sub_agents yields its lane slot
            # for this child (taskq.waits, W3); an event-loop caller awaits the
            # off-loop variant instead.
            if _child_registration:
                self._manager._admission.taskq_child_registered(info)
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

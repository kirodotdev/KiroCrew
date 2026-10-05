"""The spawn gate: every policy and capacity check between a request and its row (``spawn_impl``)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._component import ManagerComponent
from .types import ClaimPoint, MemoryReadPoint, PreparedSpawn

if TYPE_CHECKING:
    from typing import Any

    from ...execution_context import ExecutionContext
    from ...subagent import (
        _PRESSURE_EPISODE_MAX_GAP_SECS,
        _PRESSURE_HOLD_PRUNE_FACTOR,
        AGENT_NOT_AVAILABLE_CODE,
        DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
        MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE,
        MEMORY_CAUSE_READ_UNANSWERED,
        MEMORY_PRESSURE_DETAIL,
        MEMORY_PRESSURE_NEVER_STARTED,
        MEMORY_PRESSURE_RECHECK_SECS,
        QUEUED_REASON_ADAPTIVE_CAP_ZERO,
        QUEUED_REASON_CONCURRENCY_LIMIT,
        QUEUED_REASON_LOW_MEMORY,
        QUEUED_REASON_MEMORY_PRESSURE,
        QUEUED_WAIT_EXPIRED_TEXT,
        SEL_MEMORY_PRESSURE_NEVER_STARTED,
        AgentCheck,
        KiroCrewConfig,
        ParentSpawnPolicy,
        SubagentInfo,
        _cost_bucket,
        _dedicated_start_price_gb,
        _owns_dedicated_runtime,
        _shared_start_price_gb,
        _spawn_memory_floor_and_cost,
        _startup_memory_reserve_gb,
        _validate_agent,
        _validate_app_agent_ownership,
        _vet_parent_available_agents,
        _vet_spawn_governance,
        adaptive_pause_text,
        asyncio,
        check_memory_available,
        logger,
        parent_spawn_policy,
        platform_compat,
        pop_memory_check_cause,
        pressure_level_held,
        read_memory_pressure_level,
        redact_credentials,
        redact_exfiltration_urls,
        sel,
        time,
        validate_cwd,
    )


#: ``tool`` on a spawn prompt's crew-log entries. The parent's own tool call is
#: what is waiting on the human, so the name is that call's, not the child's
#: agent -- and it is one constant because the request and the decision must
#: agree for a reader pairing them by ``approval_id``.
_SPAWN_APPROVAL_TOOL = "spawn_run"


class _GateMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        CLAIM_UNAVAILABLE: str
        CLAIM_RETAINED: str

        TASK_STORE_UNAVAILABLE_CODE: str
        WINDOW_ENTRY_RECOVERING: str
        MEMORY_WAIT_UNTIL_KEY: str

    def resolve_spawn_execution(
        self,
        *,
        parent_session_key="",
        agent="",
        conversation_key="",
        memory_store="",
        app="",
        crew="",
        target_member=None,
        _memory_mode="persistent",
        _execution_context=None,
        _record=...,
        _inherited_selection=None,
    ) -> ExecutionContext:
        """Resolve routing on-loop; async admission supplies its off-loop record read."""
        from dataclasses import replace

        from kiro_crew.config.loader import KiroCrewConfig, member_template_id
        from kiro_crew.execution_context import (
            ExecutionContext,
            derive_execution,
            execution_for_store,
            execution_from_record,
            read_session_execution,
        )

        execution: ExecutionContext | None
        if _execution_context is not None:
            execution = execution_from_record({"execution_context": _execution_context})
        elif conversation_key:
            from kiro_crew.subagent_persistence import read_run_execution

            execution = (
                read_run_execution(conversation_key.removeprefix("subagent:"))
                if _record is ...
                else _record
            )
            if execution.selection_kind == "member" and not agent:
                # Refresh ordinary persona/capability selection for this new
                # invocation, while retaining the original memory identity.
                from kiro_crew.execution_context import member_config_for_id

                config = KiroCrewConfig.load()
                if execution.member_id is not None:
                    alias, selected = member_config_for_id(config, execution.member_id)
                else:
                    alias = execution.selection_name
                    selected = config.agents.get(alias)
                    if selected is None:
                        raise ValueError("selected member is unavailable")
                execution = replace(
                    execution,
                    template_id=member_template_id(selected),
                    selection_name=alias,
                )
        else:
            execution = read_session_execution(parent_session_key) if _record is ... else _record
            if execution is None:
                inherited = ("template", "")
                if parent_session_key and not agent:
                    inherited = (
                        self._manager._sessions.get_agent_selection(parent_session_key)
                        if _inherited_selection is None
                        else _inherited_selection
                    )
                    if (
                        not isinstance(inherited, tuple)
                        or len(inherited) != 2
                        or inherited[0] not in ("template", "member")
                        or not isinstance(inherited[1], str)
                    ):
                        raise ValueError("effective agent template is invalid")
                execution = execution_for_store(
                    memory_store,
                    memory_mode=_memory_mode,
                    app=app,
                    template_id=agent or inherited[1],
                )
                if inherited[0] == "member":
                    from kiro_crew.crewmate_prune_migration import (
                        removed_crewmate_names as _removed_crewmate_names,
                    )

                    selected = KiroCrewConfig.load().agents.get(inherited[1])
                    if selected is not None:
                        execution = replace(
                            execution,
                            selection_kind="member",
                            selection_name=inherited[1],
                            template_id=member_template_id(selected),
                        )
                    elif (
                        execution.store.store_id != "default"
                        or execution.member_id is not None
                        or inherited[1] not in _removed_crewmate_names()
                    ):
                        raise ValueError("selected member is unavailable")
                    # Otherwise the member is a synced crewmate the startup prune
                    # removed: same rule ``adopt_removed_synced_crewmate`` applies
                    # to records, so the run keeps its template on the shared store.
            execution = derive_execution(
                execution,
                target_member=target_member or crew or None,
                requested_mode=_memory_mode,
            )
        execution = execution.with_mode(_memory_mode)
        if agent and not conversation_key:
            if not crew and not target_member:
                # The delegate split: the parent's store and identity, the selected
                # template's namespace. A member with no persisted id has no
                # identity field, so its child is a plain template run on the
                # parent's store (the `session_create` arm keeps that member's
                # selection instead; the record cannot say "this member, under
                # that template" on either path).
                execution = execution.with_template(agent, agent)
            else:
                execution = replace(execution, template_id=agent)
        if execution.app and app and execution.app != app:
            raise ValueError("subagent app ownership does not match its parent")
        app = execution.app or app
        if execution.app != app:
            execution = replace(execution, app=app)
        return execution

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
        _crew_log_asked: "tuple[str, int] | None" = None,
        _memory_mode: str | None = None,
        *,
        crew: str = "",
        target_member: str | None = None,
        delegation: dict[str, str] | None = None,
        _execution_context: dict | None = None,
        _parent_spawn_policy: "ParentSpawnPolicy | None" = None,
        _agent_check: "AgentCheck | None" = None,
        _recovering_row: bool = False,
        _stop_before_memory_read: bool = False,
        _memory_reading: "tuple[float, str] | None" = None,
    ) -> "SubagentInfo | PreparedSpawn | ClaimPoint | MemoryReadPoint | None":
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
        agent_id: str = _preassigned_id or self._manager._mint_agent_id()
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
        # which is what makes ``/api/spawn``'s ``counted: true`` true. The
        # second half of a memory read (``_memory_reading``) is a re-entry too.
        _memory_reentry = _memory_reading is not None
        if batch_id and not _from_queue and not _store_accepted and not _memory_reentry:
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
        delegation = {
            key: redact_credentials(redact_exfiltration_urls(value)[0])[0]
            for key, value in (delegation or {}).items()
        }

        # Synchronous and yield-free with registration below: a spawn is either
        # visible to the updater's busy count before the pause, or rejected after
        # SessionManager closes admission. MagicMock-based embedders only block
        # when they expose the literal boolean True.
        if getattr(self._manager._sessions, "admission_closed", False) is True:
            closed = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                agent=agent,
                parent_session_key=parent_session_key,
                done=True,
                error="spawn refused: gateway admission is closed",
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )
            if _store_accepted and _claimed is None:
                # ``spawn_async`` committed this row before its awaits (the
                # accept write, the window decision, the memory read), and
                # admission closed during one of them. The caller is told it
                # was refused, so the store is told the same: a row left
                # queued would run once admission reopens.
                self._manager._admission.taskq_fail(agent_id, closed.error)
            return self._manager._announce_rejection(closed)

        def _refuse_row(info: SubagentInfo) -> SubagentInfo:
            """A policy refusal of a spawn whose row ALREADY exists marks that
            row failed in the same step, so the refusal the caller sees is
            also the store's verdict and the pump can never dispatch work that
            was refused. Two entries carry such a row: a drained row the pump
            re-checks (``_from_queue``), and a row ``spawn_async`` committed
            before its awaits (``_store_accepted``, not yet claimed).

            A drained run is registered as a terminal record too: its caller
            was told it was accepted, and its next ``GET /api/spawn/{id}``
            must read this failure, not a 404 for an id that is neither
            queued nor started any more. ``spawn_async``'s caller has not been
            answered yet, and receives this refusal as its answer."""
            if info.error and (_from_queue or (_store_accepted and _claimed is None)):
                self._manager._admission.taskq_fail(agent_id, info.error)
                if _from_queue:
                    self._manager._agents.setdefault(info.id, info)
            return self._manager._announce_rejection(info)

        # The mutable policy gates (cwd allowlist, governance, the parent
        # spec's allowlist) run on the first entry, again when the pump drains
        # a stored row (the re-check before dispatch), and again on the second
        # half of a memory read (``MemoryReadPoint``) on every path, a row
        # ``spawn_async`` already committed (``_store_accepted``) included: the
        # read is an await of up to ``_HOST_READ_OFF_LOOP_SECS``, and a
        # governance change made during it must hold. Only its batch count and
        # row write are not repeated. A refusal of a row that already exists
        # fails that row (``_refuse_row``), so a refused spawn never leaves
        # executable work queued. The one entry that skips them is
        # ``_store_accepted``'s first pass, right after ``prepare_spawn`` ran
        # them: it never starts anything itself, it stops at the read (whose
        # second half re-runs them) or leaves the row queued (whose drain does).
        _gate = _claimed is None and (not _store_accepted or _memory_reentry)
        # ``_claimed`` is the second half of the event-loop dispatcher's split:
        # the first half stopped at ``_stop_before_claim`` with every gate
        # passed AND the slot reserved (running count + stagger token taken
        # synchronously), the claim was taken on the writer thread, and this
        # re-entry goes straight to registration, CONSUMING that reservation.
        # The capacity gates are not re-run: the reservation is the slot, and a
        # second check would read our own reservation as a full cap.
        _dispatch_now = _claimed is not None
        # A nested spawn (its parent is a subagent): the child reserve below lets
        # it take the reserved slots, and the kernel memory-pressure hold never
        # applies to it.
        _is_child = bool(self._manager._admission.taskq_parent_id_for(parent_session_key))

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

        # Capture the immutable parent/continuation before queueing or awaiting.
        try:
            execution = self.resolve_spawn_execution(
                parent_session_key=parent_session_key,
                agent=agent,
                conversation_key=conversation_key,
                memory_store=memory_store,
                app=app,
                crew=crew,
                target_member=target_member,
                _memory_mode=_memory_mode,
                _execution_context=_execution_context,
            )
            app = execution.app
            memory_store = execution.store.legacy_name
            _memory_mode = execution.memory_mode
        except (OSError, ValueError) as exc:
            return _refuse_row(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"memory_unavailable: {exc}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        _persistent_diagnostics = _memory_mode == "persistent"
        _task_audit = (
            {"task": _redacted_task[:120]} if _persistent_diagnostics else {"subagent_id": agent_id}
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
                if _persistent_diagnostics:
                    logger.warning("Subagent spawn refused: invalid cwd %r: %s", cwd, cwd_err)
                else:
                    logger.warning("Subagent %s refused: invalid cwd", agent_id)
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome="rejected_invalid_cwd",
                    metadata=(
                        {"cwd": cwd[:200], "reason": cwd_err, **_task_audit}
                        if _persistent_diagnostics
                        else _task_audit
                    ),
                )
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
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
            if _persistent_diagnostics:
                logger.warning("Subagent spawn refused by governance: %s", gov_spawn_err)
            else:
                logger.warning("Subagent %s refused by governance", agent_id)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error=gov_spawn_err if _persistent_diagnostics else "spawn denied by governance",
                metadata=(
                    {"agent": agent, **_task_audit} if _persistent_diagnostics else _task_audit
                ),
            )
            return _refuse_row(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"spawn refused by governance: {gov_spawn_err}",
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        # --- Parent agent spec: ``toolsSettings.subagent.availableAgents`` ---
        # kiro-cli's own allowlist of what THIS agent may spawn, honoured here
        # because Kiro Crew's sub-agents bypass kiro-cli's built-in ``subagent``
        # tool. Checked against the EFFECTIVE child template (explicit,
        # inherited, or a member's), so ``crew=`` cannot route around it, and
        # only when the parent's spec declares the key -- omitted is "allow
        # all", the unchanged case. An intersection with the governance gate
        # above: both must admit.
        # ``_parent_spawn_policy`` is the event-loop callers' OFF-loop read
        # (``spawn_async``, ``/api/spawn`` and the durable pump resolve it
        # through ``to_thread``). The inline fallback serves the synchronous
        # ``spawn()`` callers and the in-memory queue's synchronous drain, which
        # re-enters WITHOUT a stored copy so the declaration is read fresh at
        # dispatch (``queue_params`` below says why).
        if _gate and _parent_spawn_policy is None:
            _parent_spawn_policy = parent_spawn_policy(parent_session_key)
        allowlist_err = (
            _vet_parent_available_agents(_parent_spawn_policy, execution.template_id, app=app)
            if _gate and _parent_spawn_policy is not None
            else None
        )
        if allowlist_err:
            if _persistent_diagnostics:
                logger.warning("Subagent spawn refused by parent agent spec: %s", allowlist_err)
            else:
                logger.warning("Subagent %s refused by parent agent spec", agent_id)
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="denied",
                error=allowlist_err if _persistent_diagnostics else "spawn denied by agent spec",
                metadata=(
                    {"agent": execution.template_id, **_task_audit}
                    if _persistent_diagnostics
                    else _task_audit
                ),
            )
            return _refuse_row(
                SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=f"spawn refused: {allowlist_err}",
                    error_code=AGENT_NOT_AVAILABLE_CODE,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
            )

        # --- Persist BEFORE any resource check: write-before-ack. Policy refusals
        # above (empty task, memory identity, cwd, governance, the parent spec's
        # allowlist) never reach the
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
            "delegation": delegation,
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
            "_execution_context": execution.to_record(),
            # Deliberately NOT queued: ``_parent_spawn_policy``. The queued
            # entry waits on capacity, so the wait is unbounded in time, and
            # the declaration it was admitted under may have been tightened
            # while it waited. Every drain re-reads it: the durable pump
            # off-loop before its re-check, the in-memory synchronous drain
            # through the gate's inline fallback above -- a memo-pinned
            # directory read (``_PARENT_ALLOWLIST_MEMO``), so a ``scandir``
            # in the ordinary case, and a full parse only when the memo
            # declines to pin.
            "crew": crew,
            "_memory_mode": _memory_mode,
            # Same rule for the asking turn: `spawn_async` re-enters from this
            # dict (prepare -> write -> re-enter), so a follow-up whose asking
            # ordinal was pinned by its watcher would otherwise be re-read from
            # the parent's LIVE turn -- the very misfiling `_crew_log_asked` exists
            # to prevent. A drained member ignores it (already pinned).
            "_crew_log_asked": _crew_log_asked,
            "_agent_prevalidated": _agent_prevalidated,
            "_preassigned_id": agent_id,
        }
        if _recovering_row:
            # The window's ``recovering`` mark (``WINDOW_ENTRY_RECOVERING``)
            # rides the round-trip like the id: a drained restart survivor
            # this gate re-queues is still unclaimed, so its row is still
            # ``recovering`` and the chip must keep leaving it out.
            queue_params[self.WINDOW_ENTRY_RECOVERING] = True
        if _prepare_only and _memory_mode == "persistent":
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
        if (
            not _from_queue
            and not _store_accepted
            and not _memory_reentry
            and _memory_mode == "persistent"
        ):
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
                        memory_mode=_memory_mode,
                        agent=agent,
                        parent_session_key=parent_session_key,
                        done=True,
                        error=f"spawn refused: task store unavailable ({store_err})",
                        error_code=self.TASK_STORE_UNAVAILABLE_CODE,
                        batch_id=batch_id,
                        batch_total=max(0, int(batch_total)),
                    )
                )
        _durable = (
            _memory_mode == "persistent" and self._manager._admission.taskq_store() is not None
        )
        if _durable and approval_mode:
            # The one process-local param a waiting row must keep: a window
            # refill rebuilds its entry from the store, which never carries it
            # (``_window_entry``), and the drain would then raise a prompt the
            # caller -- an App Kit spawn, typically -- has no surface for. This
            # process only; a restart replays the row without it, as documented.
            self._manager._held_approval_modes[agent_id] = approval_mode
        admitted_memory_mode: str = _memory_mode

        def _deferred(
            reason: str, refused: SubagentInfo, *, wait: dict[str, Any]
        ) -> SubagentInfo | None:
            if not _durable:
                return None
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
            # ``wait`` is the same verdict as a label: it rides on the returned
            # record and on the ``subagent_queued`` event, so the UI and
            # ``POST /api/spawn`` can say a MEMORY deferral is one instead of
            # rendering it as the capacity queue. It is recorded only by the
            # depth request that FOLLOWS the defer (each branch below carries it
            # to its own request), so a row the store refused -- not queued --
            # leaves no label behind for the parent's other rows to wear.
            queued = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                memory_mode=admitted_memory_mode,
                agent=agent,
                app=app,
                parent_session_key=parent_session_key,
                queued=True,
                queued_reason=str(wait.get("reason", "")),
                queued_reason_detail=reason,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                delegation=dict(delegation or {}),
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
                    wait=wait,
                )
                return queued
            elif not self._manager._admission.taskq_defer(agent_id, reason=reason):
                return None
            self._manager._emit_queue_depth(parent_session_key, batch_id, wait=wait)
            return queued

        def _defer_or_refuse(
            reason: str, refused: SubagentInfo, *, wait: dict[str, Any]
        ) -> SubagentInfo:
            """The memory exits' one epilogue: deferred when a row backs it, else refused."""
            deferred = _deferred(reason, refused, wait=wait)
            if deferred is not None:
                return deferred
            return self._manager._announce_rejection(refused)

        # --- Agent validation, BEFORE the memory and capacity gates: a name that
        # does not resolve is refused now (``agent_not_found``), never queued
        # behind a wait it will fail at the end of. A queued row clears
        # ``_agent_prevalidated`` (below), so its drain validates again. ---
        # `_agent_prevalidated` skips the on-loop agent-directory scan: a caller
        # that already confirmed the agent exists OFF the loop (the app SpawnSDK
        # validates via `list_agents()` in a thread) would otherwise make
        # `_validate_agent` re-scan/stat every agent file synchronously here,
        # stalling chat and the heartbeat on a populated agents directory. Only
        # the app path sets it; every other caller still validates inline.
        # The off-loop answer, when the caller took one for this exact
        # ``(agent, cwd, app)``: both checks below walk the agents directory, which
        # must not happen on the gateway loop.
        _checked = (
            _agent_check
            if _agent_check is not None
            and _agent_check[:3]
            == (
                agent,
                resolved_cwd or str(getattr(self._manager._sessions, "_pool_cwd", "") or ""),
                app,
            )
            else None
        )
        if agent and app and not _agent_prevalidated:
            # An app spawn that waited in the queue re-proves ownership here:
            # the same filename-prefix test the SpawnSDK ran off-loop at request
            # time (an app may only run its OWN materialized agents).
            owner_err = (
                _checked[3] if _checked is not None else _validate_app_agent_ownership(agent, app)
            )
            if owner_err:
                self._manager._admission.taskq_fail(agent_id, owner_err)
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
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
            # An event-loop caller (``spawn_async``, the coroutine pump) took
            # this answer off the loop; it is used for the same inputs only, so a
            # cwd the gate resolved differently is checked here as before.
            if _checked is not None and effective_cwd == _checked[1]:
                agent, err, err_code = _checked[4:]
            else:
                agent, err, err_code = _validate_agent(agent, effective_cwd)
            if err:
                # The row was accepted; an agent name that does not resolve at
                # dispatch is a terminal failure of THAT row, never a silent drop.
                self._manager._admission.taskq_fail(agent_id, err)
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
                    agent="",
                    parent_session_key=parent_session_key,
                    done=True,
                    error=err,
                    error_code=err_code,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                return self._manager._announce_rejection(info)

        # --- Memory guard: a start that does not fit QUEUES, durable or not.
        # A capacity verdict is a scheduling fact, never a refusal: the policy
        # refusals above (memory identity, cwd, governance, the parent spec)
        # are the only ones. ---
        agents_snapshot = list(self._manager._agents.values())
        loaded_cfg = None
        try:
            loaded_cfg = KiroCrewConfig.load()
        except Exception:
            logger.debug("Subagent spawn: config unreadable; pricing at defaults", exc_info=True)
        # Clamped once here, so every price below is built on the same
        # non-negative cost.
        min_mem, start_cost = _spawn_memory_floor_and_cost(
            loaded_cfg.agent if loaded_cfg is not None else None
        )
        # The floor itself, before the reserve below is added: the off switch the
        # kernel memory-pressure hold shares with it.
        floor_gb = min_mem
        # This start's price: what it is reserved at here, and what the row
        # carries (``_start_price_gb``) so every later admission charges it the
        # same until it settles. A start the run will put on its parent's
        # runtime launches no process; the decision is the run's own
        # (``_sharing_plan``), and only a real True prices it shared -- an
        # unknown answer is the dedicated projection, since it may become one.
        candidate_price: float | None = None
        priced_shared = False
        settled = self._manager._learned_settled_gb
        # The fields the sharing decision reads, shared by the prediction's probe
        # and the row registered below so the two cannot describe different runs.
        # ``agent`` is passed apart, as the validation above normalized it.
        run_fields: dict[str, Any] = {
            "id": agent_id,
            "parent_session_key": parent_session_key,
            "model": model or "",
            "reasoning_effort": reasoning_effort or "",
            "allowed_tools": list(allowed_tools) if allowed_tools else [],
            "bare": bare,
            "keep": keep,
            "execution_context": execution,
        }
        if _dispatch_now:
            # The claim re-entry registers at the price its first half CHECKED;
            # recomputing here would store a price no admission ever tested. A
            # re-entry that does not proceed (a retained claim) keeps the entry,
            # so the slot it still holds stays charged at that price.
            claim = self._manager._claim_prices
            candidate_price, priced_shared = (
                claim.pop(agent_id, (None, False))
                if _claimed is not None and _claimed[1]
                else claim.get(agent_id, (None, False))
            )
        elif min_mem > 0:
            candidate_price = _dedicated_start_price_gb(
                start_cost, settled, _cost_bucket(agent, execution)
            )
            try:
                plan = self._manager._sharing_plan(
                    SubagentInfo(task="", agent=agent, **run_fields), cfg=loaded_cfg
                )
                if plan.shared is True:
                    candidate_price = _shared_start_price_gb(candidate_price)
                    priced_shared = True
            except Exception:
                logger.debug("Subagent spawn: sharing prediction failed", exc_info=True)
            # RSS grows after a process starts. Reserve the unobserved part so
            # a fast drain cannot repeatedly spend the same free memory before
            # the next controller sample: this start at its own price, and each
            # row still warming at the price it was admitted at. Memory used by
            # work a run launches later is not priced here.
            min_mem += _startup_memory_reserve_gb(
                agents_snapshot,
                running_count=self._manager._running_count,
                cost_gb=start_cost,
                next_start_gb=candidate_price,
                settled_gb=settled,
                claim_prices=[price for price, _ in self._manager._claim_prices.values()],
            )
        # A disabled floor (``agent.spawn_min_memory_gb`` <= 0) takes no reading
        # at all, so an unanswered read can never hold back a start it does not
        # gate. ``min_mem`` is still the raw floor here: the reserve above is
        # only added to a positive one.
        floor_off = min_mem <= 0
        if _dispatch_now or floor_off:
            mem_ok, avail_gb, memory_cause = True, -1.0, ""
        elif _memory_reading is not None:
            # The second half of an off-loop read (``MemoryReadPoint``): the
            # reading is the worker's, the fit is decided HERE, on the loop,
            # against the bar recomputed above, so a start admitted while the
            # read ran is charged. -1 is the reader's "unmeasurable": fail open.
            # A worker that never answered read nothing at all: it waits.
            avail_gb, memory_cause = _memory_reading
            mem_ok = memory_cause != MEMORY_CAUSE_READ_UNANSWERED and (
                avail_gb < 0 or avail_gb >= min_mem
            )
        elif _stop_before_memory_read:
            # An event-loop caller: the reader walks cgroup files, so it is taken
            # on a worker and this method re-entered with it. Nothing is reserved
            # yet, so nothing leaks if the caller never comes back.
            return MemoryReadPoint(min_gb=min_mem, params=dict(queue_params))
        else:
            pop_memory_check_cause()  # drop a cause left by any earlier reading
            mem_ok, avail_gb = check_memory_available(min_gb=min_mem)
            memory_cause = pop_memory_check_cause()
        # A non-durable start that does not fit waits in the in-memory window
        # (the capacity queue below), labelled and re-checked like a durable one.
        memory_wait: dict[str, Any] | None = None
        memory_detail = ""
        if not mem_ok:
            # An unreadable cgroup usage file, or an off-loop read that never
            # answered, still defers; only the words change.
            unknown_note = {
                MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE: (
                    "memory headroom unknown: a finite cgroup memory limit is set but its "
                    "usage is unreadable; restore read access to memory.current / "
                    "memory.usage_in_bytes"
                ),
                MEMORY_CAUSE_READ_UNANSWERED: (
                    "memory headroom unknown: the host memory reading did not answer in time"
                ),
            }.get(memory_cause, "")
            unknown_usage = bool(unknown_note)
            logger.warning(
                "Subagent spawn deferred: %s, need %.2f GB (this start "
                "priced at %.2f GB, plus the starts still warming, with "
                "agent.spawn_min_memory_gb left over).",
                unknown_note if unknown_usage else f"only {avail_gb:.2f} GB available",
                min_mem,
                candidate_price or 0.0,
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="deferred_low_memory",
                metadata={
                    # No figure when nothing was read: -1 is not an amount.
                    **(
                        {}
                        if memory_cause == MEMORY_CAUSE_READ_UNANSWERED
                        else {"available_gb": avail_gb}
                    ),
                    "min_gb": min_mem,
                    "startup_cost_gb": start_cost,
                    "start_price_gb": candidate_price,
                    **({"cause": memory_cause} if memory_cause else {}),
                    **_task_audit,
                },
            )
            memory_detail = (
                f"{unknown_note}; need {min_mem:.1f} GB"
                if unknown_usage
                else f"low memory: {avail_gb:.1f} GB available, need {min_mem:.1f} GB "
                f"({candidate_price or 0.0:.2f} GB for this start)"
            )
            # A floor wait carries no pressure clock (the hold below is not
            # evaluated for it): one an earlier hold started is dropped, so time
            # spent below the floor never counts toward the hold's bound.
            self._manager._pressure_holds.pop(agent_id, None)
            self._manager._pressure_hold_expired.discard(agent_id)
            memory_wait = {
                "reason": QUEUED_REASON_LOW_MEMORY,
                # No figure when nothing was read: -1 is not an amount.
                **({"available_gb": round(float(avail_gb), 2)} if avail_gb >= 0 else {}),
                "required_gb": round(float(min_mem), 2),
            }
            if _durable:
                # Built ahead of the deferral, not after it: a durable defer the
                # store could not write -- no row behind a ``_queue`` entry, or
                # the store unavailable for the write -- answers with this
                # refusal, since no pump could ever pick a wait nothing recorded.
                # It is the STORE's verdict, so it says so and carries the
                # store's retry code; the memory figures ride along as context.
                info = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=(
                        "spawn refused: the task store could not record this start's "
                        f"memory wait ({memory_detail})"
                    ),
                    error_code=self.TASK_STORE_UNAVAILABLE_CODE,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                # The row keeps only ``next_run_at``, so the refill that brings it
                # back stamps it as a floor wait from this mark and the pick
                # leaves it to this gate, floor first. A refusal below forgets it.
                self._manager._floor_deferred_ids.add(agent_id)
                return _defer_or_refuse(memory_detail, info, wait=memory_wait)
            # No durable row, so the store sweep cannot bound this wait: the
            # time it has spent PARKED on the floor is kept here (closed parks,
            # each cut at its planned end, so a later wait for a slot is not
            # counted) and checked as it is parked again, under the same live
            # ``agent.subagent_queue_max_wait_secs`` (0 is no bound).
            floor_now = time.monotonic()
            floor_parked, park_from, park_end = self._manager._floor_waits.get(
                agent_id, (0.0, floor_now, floor_now)
            )
            floor_parked += max(0.0, min(park_end, floor_now) - park_from)
            bound = self._manager._admission.taskq_memory_wait_bound_secs()
            if bound > 0 and floor_parked >= bound:
                logger.warning(
                    "Subagent %s waited for memory longer than %.0fs; ending it (%s)",
                    agent_id,
                    bound,
                    QUEUED_WAIT_EXPIRED_TEXT,
                )
                self._manager._forget_pending_start(agent_id)
                ended = SubagentInfo(
                    id=agent_id,
                    task=_redacted_task,
                    memory_mode=_memory_mode,
                    agent=agent,
                    parent_session_key=parent_session_key,
                    done=True,
                    error=QUEUED_WAIT_EXPIRED_TEXT,
                    batch_id=batch_id,
                    batch_total=max(0, int(batch_total)),
                )
                self._manager._agents.setdefault(agent_id, ended)
                self._manager._emit_queue_depth(parent_session_key, batch_id)
                return self._manager._announce_rejection(ended)
            self._manager._floor_waits[agent_id] = (
                floor_parked,
                floor_now,
                floor_now + self._manager._admission.taskq_admit_wait_secs(),
            )
        if (
            mem_ok
            and avail_gb < 0
            and (platform_compat.IS_LINUX or platform_compat.IS_MACOS)
            and not _dispatch_now
            and not floor_off
        ):
            # A negative reading means the guard did not run: /proc/meminfo on
            # Linux, or the Mach reclaimable figure on macOS, is unreadable on a
            # platform where it must exist. Proceeding is the stated fail-open
            # contract for an unmeasurable host, but it must be observable
            # rather than indistinguishable from a healthy check. Windows is
            # left quiet: its reader is a best-effort probe whose failure would
            # fire on every spawn and drown the signal.
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

            task_note = (
                redact_log_via_context(_redacted_task)[:120] if _persistent_diagnostics else ""
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="memory_check_unavailable",
                metadata={
                    "min_gb": min_mem,
                    **({"task": task_note} if _persistent_diagnostics else _task_audit),
                },
            )

        now = time.monotonic()
        should_queue, slot_free = self._manager._should_stagger_queue(now)
        if _dispatch_now:
            should_queue = False
        if memory_wait is not None:
            should_queue = True
        # Child reserve (RFC §6, Q3): a depth-0 start may not take the last
        # reserved slot(s) while nested work is pending or a parent waits on
        # its children; only children and resuming parents may. The gate
        # above answered for the whole cap, so narrow it here for roots.
        if (
            not should_queue
            and not _dispatch_now
            and not _is_child
            and not self._manager._admission.root_may_start()
        ):
            should_queue, slot_free = True, False
        # The macOS kernel memory-pressure hold, the floor's second input: a root
        # start waits in the window below like a capacity wait, re-checked on
        # every slot release and pump pass. Every rule of it is stated once, in
        # subagent.md (*macOS: the kernel memory-pressure hold*). A start that
        # did not fit the floor already waits as ``low_memory``: the floor's
        # own verdict outranks the kernel's, and that wait carries no pressure
        # clock, so it can never be ended as "never started".
        pressure_level = (
            None
            if _dispatch_now or _is_child or memory_wait is not None
            else self._manager._memory_pressure_hold(floor_gb=floor_gb)
        )
        verdict = (
            self._manager._memory_pressure_holds(
                agent_id,
                pressure_level,
                parent_session_key=parent_session_key,
                available_gb=avail_gb,
            )
            if pressure_level is not None
            else "release"
        )
        if verdict == "expired":
            # Never proceeds into the pressure it waited on: ended, never
            # started, its row failed so the depth stops counting it, and
            # registered as a terminal record so a caller that was told it was
            # queued reads this outcome by id rather than a 404.
            ended = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                memory_mode=_memory_mode,
                agent=agent,
                parent_session_key=parent_session_key,
                done=True,
                error=MEMORY_PRESSURE_NEVER_STARTED,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )
            self._manager._admission.taskq_fail(agent_id, MEMORY_PRESSURE_NEVER_STARTED)
            self._manager._agents.setdefault(agent_id, ended)
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            return self._manager._announce_rejection(ended)
        if verdict == "held":
            should_queue, slot_free = True, False
        else:
            pressure_level = None
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
            # the default 0.25s stagger that is EVERY member after the first, so a
            # 2-agent wave permanently rendered "1 agent running" while the
            # sidebar and Subagents panel correctly showed 2.
            # The in-memory queue is a bounded WINDOW over the store's queued
            # rows: a new row joins it only when there is room and no older
            # row is waiting outside it (FIFO across the boundary); otherwise
            # it waits on disk and the drain's refill brings it in. A drained
            # spawn that hit the stagger gate re-joins the window directly --
            # it is already the oldest eligible row.
            # Restricted work has no stored row to refill; retain its only copy.
            # One that did not fit the memory floor is not eligible again until
            # the admit wait passes, as a durable row's ``next_run_at`` is not.
            admit_wait = self._manager._admission.taskq_admit_wait_secs()
            if memory_wait is not None:
                queue_params[self.MEMORY_WAIT_UNTIL_KEY] = now + admit_wait
            if (
                not _durable
                or _from_queue
                or (
                    _window_hint
                    if _window_hint is not None
                    else self._manager._admission.taskq_should_window(agent_id)
                )
            ):
                self._manager._queue.append(queue_params)
            logger.info(
                "Subagent queued (%d running, %d queued, slot_free=%s, in_startup=%d/%d)",
                self._manager._running_count,
                len(self._manager._queue),
                slot_free,
                self._manager._startup_population(),
                self._manager._startup_cap(),
            )
            # Which wait this is. A cap the adaptive controller has squeezed to 0
            # is the one capacity queue "behind the concurrency limit" misreads:
            # nothing runs, the configured cap still reads N, and the row waits
            # for the controller's probe, not for a slot. The stagger tick and a
            # genuinely full cap both clear on their own and keep the default.
            # The paused kind is answered to callers as a DEFERRAL, so it carries
            # the same human sentence the memory kinds do; the ordinary kind is
            # never surfaced as prose and stays bare.
            # A memory wait keeps its own label and sentence, the same ones a
            # durable deferral carries.
            adaptive_paused = self._manager._max_concurrent <= 0
            capacity_wait: dict[str, Any] = memory_wait or {
                "reason": (
                    QUEUED_REASON_ADAPTIVE_CAP_ZERO
                    if adaptive_paused
                    else QUEUED_REASON_CONCURRENCY_LIMIT
                )
            }
            capacity_detail = memory_detail or (
                adaptive_pause_text(self._manager._user_max_concurrent) if adaptive_paused else ""
            )
            if pressure_level is not None and not adaptive_paused:
                # No GB figures: the figure cleared the floor, so any "N GB free,
                # needs M GB" pair would contradict the verdict. A paused cap
                # keeps its own label: nothing starts before the controller's
                # probe recovers, whatever the kernel says.
                capacity_wait = {"reason": QUEUED_REASON_MEMORY_PRESSURE}
                capacity_detail = MEMORY_PRESSURE_DETAIL
            # Advisory UI signal: tell the chip how many agents are now waiting
            # to start for this parent so it can appear immediately and show a
            # "waiting" count instead of only running/completed ones.
            self._manager._emit_queue_depth(parent_session_key, batch_id, wait=capacity_wait)
            # If a slot is free, no running agent will trigger the drain on
            # completion — schedule the staggered pump at the interval boundary
            # so the queued spawn still launches. A memory wait is re-checked
            # when its admit wait passes instead.
            if memory_wait is not None:
                self._manager._admission.arm_memory_wait(queue_params[self.MEMORY_WAIT_UNTIL_KEY])
            elif slot_free:
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
                queued_reason=str(capacity_wait["reason"]),
                queued_reason_detail=capacity_detail,
                memory_mode=_memory_mode,
                execution_context=execution,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                delegation=dict(delegation or {}),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
            self._record_crew_log_dispatch(info, from_queue=_from_queue, asked=_crew_log_asked)
            # A queued child still blocks a parent waiting in spawn_sub_agents:
            # the parent yields its slot now, which is what lets the queue it
            # is waiting on actually drain (taskq.waits, W3). An event-loop
            # caller (``_child_registration=False``) runs that branch itself,
            # awaited, with its store reads and writes on the writer thread.
            if _child_registration:
                self._manager._admission.taskq_child_registered(info)
            return info

        # --- Atomic claim: the ONE write that takes the row for dispatch. It
        # bumps the generation every later write is fenced with, and it fails
        # for a row cancelled while it waited (the store is re-read here, after
        # the wait, which is what makes cancel-vs-drain safe). ---
        if _claimed is not None:
            taskq_generation, proceed, claim_reason = _claimed
        elif _memory_mode != "persistent":
            taskq_generation, proceed, claim_reason = 0, True, ""
        else:
            if _stop_before_claim and self._manager._admission.taskq_store() is not None:
                # Reserve-then-commit: take the slot NOW, before the caller
                # awaits the claim, so nothing admitted during that await can
                # overshoot the cap or skip the stagger.
                self._manager._running_count += 1
                # The reservation is also an admitted-but-unstarted agent
                # for the in-startup bound (``_startup_population``): the
                # re-entry skips the admission gate, so it must have been
                # counted by every admission decided in between.
                self._manager._startup_reservations += 1
                self._manager._last_spawn_ts = time.monotonic()
                if candidate_price is not None:
                    # Charged at its checked price while the claim is pending,
                    # and carried to the re-entry that registers it.
                    self._manager._claim_prices[agent_id] = (candidate_price, priced_shared)
                return ClaimPoint(agent_id, parent_session_key)
            taskq_generation, proceed, claim_reason = self._manager._admission.taskq_claim(agent_id)
        if not proceed and claim_reason in (self.CLAIM_UNAVAILABLE, self.CLAIM_RETAINED):
            # A pre-claim outage leaves the row QUEUED and needs an ordinary
            # refill. A post-claim outage leaves it ADMITTED under this process;
            # claim_and_start retains its generation and reservation, and its
            # dedicated retry pass owns the wake.
            retained = claim_reason == self.CLAIM_RETAINED
            logger.warning(
                "taskq: %s of %s unavailable; %s",
                "post-claim settlement" if retained else "claim",
                agent_id,
                "retained admitted generation" if retained else "left queued for the pump",
            )
            # The row is still QUEUED (or ADMITTED and retained), and the depth
            # published here counts both: ``taskq_overflow`` includes admitted
            # rows no run is registered for. A pump that popped it marked it
            # dispatching; that mark describes an attempt that just ended.
            self._manager._dispatching_ids.discard(agent_id)
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            if not retained:
                try:
                    asyncio.get_event_loop().call_later(
                        self._manager._admission.taskq_admit_wait_secs(),
                        self._manager._drain_queue,
                    )
                except RuntimeError:
                    pass
            info = SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                memory_mode=_memory_mode,
                agent=agent,
                app=app,
                parent_session_key=parent_session_key,
                queued=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
                delegation=dict(delegation or {}),
                include_memory=include_memory,
                include_lessons=include_lessons,
                include_project=include_project,
            )
            # Pinned HERE, on the pass that still knows which turn asked. The
            # pump re-enters this method after the store answers, where that
            # turn cannot be read; the pin this leaves is what that pass carries
            # forward, and ``remember_child_origin`` refuses to move it.
            self._record_crew_log_dispatch(info, from_queue=_from_queue, asked=_crew_log_asked)
            return info
        if not proceed:
            # Cancelled while it waited: it will never start.
            self._manager._forget_pending_start(agent_id)
            self._manager._emit_queue_depth(parent_session_key, batch_id)
            return SubagentInfo(
                id=agent_id,
                task=_redacted_task,
                memory_mode=_memory_mode,
                agent=agent,
                parent_session_key=parent_session_key,
                queued=True,
                done=True,
                user_stopped=True,
                batch_id=batch_id,
                batch_total=max(0, int(batch_total)),
            )

        info = SubagentInfo(
            task=_redacted_task,
            agent=agent,
            app=app,
            approval_mode=approval_mode or "",
            silent=silent,
            max_turns=max_turns,
            cwd=resolved_cwd,
            batch_id=batch_id,
            batch_total=max(0, int(batch_total)),
            conversation_key=conversation_key,
            delegation=dict(delegation or {}),
            include_memory=include_memory,
            include_lessons=include_lessons,
            include_project=include_project,
            memory_store=memory_store or "",
            crew=crew,
            memory_mode=_memory_mode,
            **run_fields,
        )
        info._start_price_gb = candidate_price
        info._start_priced_shared = priced_shared
        info._raw_task = task  # unredacted prompt for kiro-cli execution
        info._memory_mode_ready = not bool(conversation_key)
        info._taskq_generation = taskq_generation
        self._manager._agents[agent_id] = info
        self._manager._forget_pending_start(agent_id)
        self._record_crew_log_dispatch(info, from_queue=_from_queue, asked=_crew_log_asked)
        if not _dispatch_now:  # a ClaimPoint re-entry already holds its reservation
            self._manager._running_count += 1
        else:
            # Registered: the info now counts in ``_startup_population``
            # itself, so the reservation stands down.
            self._manager._startup_reservations = max(
                0, int(self._manager._startup_reservations) - 1
            )
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
            # by the run itself at its first stream event addressed to its session.
            self._manager._admission.taskq_mark(info, "starting")
            # Nested: a parent blocked in spawn_sub_agents yields its lane slot
            # for this child (taskq.waits, W3); an event-loop caller awaits the
            # off-loop variant instead.
            if _child_registration:
                self._manager._admission.taskq_child_registered(info)
        return info

    def _memory_pressure_hold_impl(self, *, floor_gb: float | None = None) -> int | None:
        """The level the macOS kernel memory-pressure hold applies at now, or None.

        Gateway-wide and read fresh; the rules are subagent.md's (*macOS: the
        kernel memory-pressure hold*). The level is read first, so a host where
        it is never held (every non-macOS one) pays no config read. While the
        hold applies, one timer re-pumps every ``MEMORY_PRESSURE_RECHECK_SECS``.
        When it stops applying, the WARNING latch resets and a parent still
        labelled with the pressure reason is relabelled as a capacity wait. A
        hold that has applied without a break for the live
        ``agent.subagent_queue_max_wait_secs`` (``taskq_memory_wait_bound_secs``;
        0 is no bound) marks its episode spent (``_pressure_episode_spent``)
        until it stops applying, and every row the hold would keep then expires
        at once.
        """
        mgr = self._manager
        level: int | None = None
        try:
            reading = read_memory_pressure_level()
            if pressure_level_held(reading):
                if floor_gb is None:
                    floor_gb, _cost = _spawn_memory_floor_and_cost()
                if floor_gb > 0 and _owns_dedicated_runtime(
                    mgr._agents.values(), claim_prices=mgr._claim_prices
                ):
                    level = reading
            # The episode is the hold APPLYING without a break, so a host at WARN
            # with nothing of ours running never spends it. A gap between reads
            # longer than a few recheck intervals is a break nobody observed, so
            # the episode restarts rather than being assumed continuous across it.
            now = time.monotonic()
            bound = mgr._admission.taskq_memory_wait_bound_secs()
            unobserved = now - mgr._pressure_episode_read_at > _PRESSURE_EPISODE_MAX_GAP_SECS
            mgr._pressure_episode_read_at = now
            since = mgr._pressure_episode_since
            if level is None or unobserved or since is None:
                mgr._pressure_episode_since = now if level is not None else None
                mgr._pressure_episode_spent = False
            elif bound > 0 and now - since >= bound:
                if not mgr._pressure_episode_spent:
                    mgr._pressure_episode_spent = True
                    logger.warning(
                        "macOS memory pressure has held subagent starts for %.0fs; root "
                        "starts it would hold are ended, never started, until it eases",
                        now - since,
                    )
            else:
                # The bound is live: one raised past the episode, or set to 0 (no
                # bound), un-spends an episode spent under the earlier value.
                mgr._pressure_episode_spent = False
        except Exception:
            logger.debug("Subagent memory-pressure hold: reading failed", exc_info=True)
            level = None
        if level is None:
            mgr._pressure_hold_level = None
            if mgr._pressure_hold_on:
                mgr._pressure_hold_on = False
                capacity = (
                    QUEUED_REASON_ADAPTIVE_CAP_ZERO
                    if mgr._max_concurrent <= 0
                    else QUEUED_REASON_CONCURRENCY_LIMIT
                )
                for parent, wait in list(mgr._queue_wait.items()):
                    if wait.get("reason") == QUEUED_REASON_MEMORY_PRESSURE:
                        mgr._emit_queue_depth(parent, wait={"reason": capacity})
            # A row's clock outlives a pause in the hold (our last runtime ending
            # between two of its starts), so only clocks far past any wait are
            # dropped here: rows that left without a registration or a refusal.
            now = time.monotonic()
            prune_after = _PRESSURE_HOLD_PRUNE_FACTOR * max(
                mgr._admission.taskq_memory_wait_bound_secs(),
                float(DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS),
            )
            for agent_id, since in list(mgr._pressure_holds.items()):
                if now - since >= prune_after:
                    del mgr._pressure_holds[agent_id]
                    mgr._pressure_hold_expired.discard(agent_id)
            return None
        mgr._pressure_hold_on = True
        if not mgr._shutting_down:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            handle = mgr._pressure_recheck_handle
            # Re-armed when none is pending, including one whose loop went away
            # before it fired (past due by more than a second).
            if loop is not None and (
                handle is None or handle.cancelled() or handle.when() < loop.time() - 1.0
            ):

                def _recheck() -> None:
                    mgr._pressure_recheck_handle = None
                    mgr._drain_queue()

                mgr._pressure_recheck_handle = loop.call_later(
                    MEMORY_PRESSURE_RECHECK_SECS, _recheck
                )
        return level

    def _memory_pressure_holds_impl(
        self,
        agent_id: str,
        level: int,
        *,
        parent_session_key: str = "",
        batch_id: str = "",
        available_gb: float | None = None,
        relabel: bool = False,
        commit_expiry: bool = True,
    ) -> str:
        """What the hold, applying at *level*, does with root start *agent_id*.

        ``"held"`` while it waits; ``"expired"`` once its own wait has run out
        (``agent.subagent_queue_max_wait_secs`` from its first hold, read live;
        0 is no bound) or the episode has
        outlived that bound (``_pressure_episode_spent``): the caller ends it,
        never started (``MEMORY_PRESSURE_NEVER_STARTED``). *available_gb* is the
        floor's figure when the caller read one (negative: unreadable), None
        when it read none. *relabel* publishes the pressure label on the row's
        first hold, for a caller (the pump) that is not about to emit one.
        *commit_expiry* False only classifies an expired row (the pump's pick,
        which hands it to the gate's re-check): the WARNING and SEL row are
        written by the caller that actually ends it.
        """
        mgr = self._manager
        now = time.monotonic()
        first = agent_id not in mgr._pressure_holds
        since = mgr._pressure_holds.setdefault(agent_id, now)
        name = platform_compat.memory_pressure_name(level)
        bound = mgr._admission.taskq_memory_wait_bound_secs()
        own_wait_ran_out = bound > 0 and now - since >= bound
        if own_wait_ran_out or mgr._pressure_episode_spent:
            if commit_expiry and agent_id not in mgr._pressure_hold_expired:
                mgr._pressure_hold_expired.add(agent_id)
                # Which bound ended it is part of the audit: a start the spent
                # episode ends at once waited nothing itself, and reporting it as
                # one that sat out its own bound would misstate both.
                cause = "wait" if own_wait_ran_out else "episode"
                episode_since = mgr._pressure_episode_since
                episode_secs = (
                    round(now - episode_since, 1)
                    if cause == "episode" and episode_since is not None
                    else None
                )
                if cause == "wait":
                    logger.warning(
                        "Subagent %s: never started -- macOS reported memory pressure "
                        "(%s) for its whole wait (%.0fs)",
                        agent_id,
                        name,
                        now - since,
                    )
                else:
                    logger.warning(
                        "Subagent %s: never started -- macOS memory pressure (%s) has "
                        "lasted %.0fs, past the %ds bound, so it was ended without waiting",
                        agent_id,
                        name,
                        episode_secs or 0.0,
                        int(bound),
                    )
                expiry: dict[str, Any] = {
                    "memory_pressure_level": level,
                    "waited_secs": round(now - since, 1),
                    "expired_by": cause,
                    "subagent_id": agent_id,
                }
                if episode_secs is not None:
                    expiry["episode_secs"] = episode_secs
                sel().log_tool_invocation(
                    session_key=parent_session_key or "",
                    source="subagent",
                    tool_name="spawn_run",
                    outcome=SEL_MEMORY_PRESSURE_NEVER_STARTED,
                    metadata=expiry,
                )
            return "expired"
        if first:
            log = logger.debug
            if mgr._pressure_hold_level != level:
                mgr._pressure_hold_level = level
                log = logger.warning
            metadata: dict[str, Any] = {"memory_pressure_level": level, "subagent_id": agent_id}
            figure = ""
            if available_gb is not None:
                unreadable = available_gb < 0
                metadata["available_gb"] = None if unreadable else available_gb
                figure = (
                    " (figure unreadable)"
                    if unreadable
                    else f" ({available_gb:.2f} GB reclaimable)"
                )
            log(
                "Subagent %s held: macOS reports memory pressure (%s) while a dedicated "
                "subagent of this gateway is running%s",
                agent_id,
                name,
                figure,
            )
            sel().log_tool_invocation(
                session_key=parent_session_key or "",
                source="subagent",
                tool_name="spawn_run",
                outcome="deferred_memory_pressure",
                metadata=metadata,
            )
            if relabel:
                mgr._emit_queue_depth(
                    parent_session_key, batch_id, wait={"reason": QUEUED_REASON_MEMORY_PRESSURE}
                )
        return "held"

    async def _safe_announce_impl(self, info: SubagentInfo) -> None:
        """Notify completion callback with error handling.

        Args:
            info (SubagentInfo): The subagent metadata.
        """
        assert self._manager._on_done is not None
        if info.id in getattr(self._manager, "_teardown_cancelled_ids", ()):
            # Same statement as the terminal-report gate, at the OTHER announce
            # entry point. ``_on_done`` resolves the parent key through the
            # session registry and injects, which CREATES a session when none is
            # live, so announcing here rebuilds the conversation the teardown
            # just took down. The rejection paths reach this function directly
            # rather than through ``_report_terminal``, so the gate that covers
            # the run that EXECUTED does not cover the run that was refused
            # before it ever started -- and a spawn parked on its approval is
            # marked by the teardown precisely because its delivery must not
            # land. The id is left in the gate rather than discarded: this
            # function is one of several announce entry points, and the age
            # backstop is what retires the mark.
            logger.info("Skipping parent announce for %s - its parent ended", info.id)
            return
        try:
            await self._manager._on_done(info)
        except Exception as exc:
            logger.error("Subagent announce failed for %s (%s)", info.id, type(exc).__name__)

    def _record_crew_log_dispatch(
        self,
        info: SubagentInfo,
        *,
        from_queue: bool,
        asked: "tuple[str, int] | None" = None,
    ) -> None:
        """PIN the parent session and turn that asked for *info*. Writes nothing.

        Called from both accepted exits of ``spawn`` and from neither rejection.
        Pinning here and writing elsewhere is deliberate: this is normally the only
        moment the ASKING turn is knowable -- a spawn arrives as a tool call inside
        the parent's turn -- but being accepted is not being started. Registration
        is followed by the spawn approval gate, and a decline returns through
        ``_claim_finalize`` + ``_safe_announce`` without ever reaching ``_run``, so
        an entry written here would be an opener nothing closes. The entry is
        written by ``_log_spawned``, the one site that means the run is really
        starting, from the pin this leaves behind.

        ``asked`` overrides that reading, and one caller needs it: a queued
        follow-up is DISPATCHED by its watcher after the run it continues has
        finished, so no turn is asking at this moment and the parent may be on an
        unrelated one. That caller pinned the asking ordinal where the follow-up was
        requested and hands it in; without it the continuation would be filed under
        a turn that did not ask for it. A caller that supplies no ``asked`` is
        dispatching from inside its own turn, where the live reading is correct.

        ``from_queue`` marks a member re-entering under the same id after the
        stagger or concurrency gate held it, and the pin it was accepted with must
        stand. ``remember_child_origin`` is what keeps it: it refuses to move an
        origin already in the map. What that map does not survive is a restart, and
        the durable queue replays a queued row into this site in a process whose map
        is empty -- so the pin is re-established on that pass rather than skipped,
        or the child's spawn, steer and terminal entries are all omitted with
        nothing to report the gap. The turn is left UNOBSERVED there: the turn that
        asked died with the process that recorded it, the parent's live turn names
        one that did not ask, and the writers omit an unobserved ordinal rather
        than stamping a turn that never existed.

        Best-effort throughout. A spawn must not fail because a log entry could
        not be pinned, and an unresolvable parent yields an empty session id, which
        the pin refuses -- leaving the later write with nothing to open, which is
        the correct outcome rather than a guessed one.

        Every name is imported inside the body on purpose. This method does NOT
        end in ``_impl``, so ``bind_component_globals`` leaves it running on this
        module's own globals -- where the facade's imports, ``logger`` included,
        exist only under ``TYPE_CHECKING``.
        """
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.resolve import unit_for_session_key
        from kiro_crew.subagent import logger as _logger

        try:
            if not crew_log_emit.enabled():
                return
            if asked is not None:
                sid, turn = asked
            else:
                sid = unit_for_session_key(self._manager._sessions, info.parent_session_key)
                if from_queue:
                    # A drained member: in THIS process its origin is already
                    # pinned and the pin refuses to move, so this pass changes
                    # nothing. In a process that replayed the row from the durable
                    # queue the map is empty and this is the pass that restores it,
                    # with the turn unobserved because the one that asked is gone
                    # with the process that held it.
                    turn = 0
                else:
                    turn = crew_log_emit.live_turn(sid) if sid else 0
            if not sid:
                return
            crew_log_emit.remember_child_origin(info.id, sid, turn)
        except Exception:
            _logger.debug("crew log: pinning a subagent dispatch failed", exc_info=True)

    def _record_crew_log_spawn_started(self, info: SubagentInfo) -> None:
        """Write *info*'s ``subagent/spawned`` into the PARENT session's crew log.

        Called from ``_log_spawned``, which every path that actually starts a run
        goes through and no rejection does -- including the approval gate, which
        reaches it only after a human said yes.

        The turn comes from the pin, never from the parent's live turn. This site
        can be reached an unbounded human approval after the ask, by which point the
        parent is very likely on a different turn; reading it here is exactly the
        after-the-fact re-derivation the log may not contain. An unpinned child (the
        flag came on mid-flight, or the parent could not be resolved) writes nothing.

        No ``ref`` into the child's log. The schema describes one and a child that
        had a crew log would deserve it, but no subagent path opens one, so the
        citation would name a file that does not exist -- indistinguishable, to a
        reader, from one that was deleted.
        """
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.subagent import logger as _logger

        try:
            if not crew_log_emit.enabled():
                return
            sid, asked_turn = crew_log_emit.open_child_origin(info.id)
            if not sid:
                return
            crew_log_emit.on_subagent_spawned(
                sid,
                asked_turn,
                agent_id=info.id,
                agent=info.agent,
                model=info.model,
                task=info.task,
                scope={
                    "memory": info.include_memory,
                    "lessons": info.include_lessons,
                    "project": info.include_project,
                },
            )
        except Exception:
            _logger.debug("crew log: recording a subagent spawn failed", exc_info=True)

    def _record_crew_log_spawn_approval_requested(
        self, info: SubagentInfo, *, approval_id: str, reason: str
    ) -> "tuple[str, int]":
        """Write *info*'s spawn prompt as an ``approval/requested`` entry.

        Returns the parent session and asking turn the entry was filed under, so
        the decision is recorded beside its own request -- hand it to
        :meth:`ManagerComponent._record_crew_log_approval_decided`, the shared
        closer. An empty session id means nothing was written and that closer is
        a no-op too, so the pair is all-or-nothing by construction rather than by
        two separate checks.

        The origin comes from the pin, read through ``dispatch_origin`` because the
        prompt happens BEFORE the opener: the dispatch has been accepted and the
        run has not started, which is the one window ``child_origin`` is designed
        to refuse. Reading the parent's live turn instead would file the prompt
        under whatever turn the parent reached while a person took their time.

        ``tool`` is the spawn tool rather than the child's own agent name, because
        what is waiting on the human is the parent's tool call.
        """
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.subagent import logger as _logger

        try:
            if not crew_log_emit.enabled():
                return ("", 0)
            sid, asked_turn = crew_log_emit.dispatch_origin(info.id)
            if not sid:
                return ("", 0)
            crew_log_emit.on_approval_requested(
                sid,
                asked_turn,
                approval_id=approval_id,
                tool=_SPAWN_APPROVAL_TOOL,
                reason=reason,
            )
            return (sid, asked_turn)
        except Exception:
            _logger.debug("crew log: recording a spawn approval request failed", exc_info=True)
            return ("", 0)

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
        self._manager._forget_pending_start(info.id)
        if info.batch_id and self._manager._on_done:
            try:
                self._manager._tasks[f"reject-{info.id}"] = asyncio.ensure_future(
                    self._manager._safe_announce(info)
                )
            except RuntimeError:
                pass  # no running loop (sync/test context)
        return info

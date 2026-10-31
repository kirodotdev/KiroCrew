"""Construct the gateway's long-lived services and run it.

The memory, skills, hooks, context, history and session stack; the task runner; the
dashboard or API-only server; ``run`` itself, which brings everything up in order
and blocks until shutdown; and the post-bind session-start sizing log.

Orchestrator fields read: ``_background_tasks``, ``_bot_token``, ``_cfg``,
``_cron_injecting``, ``_json_ready``, ``_no_crons``, ``_no_dashboard``, ``_no_open``,
``_owner_id``, ``_port_override``, ``_runner_admission_adopted``, ``_slack_enabled``,
``_test_mode``,
``_tracking_channels``, ``autonudge_svc``, ``cron_svc``, ``subagent_mgr``.
Orchestrator fields written: ``_configured_host``, ``_dashboard_port``,
``_dashboard_runner``, ``_feature_video_task``, ``_local_only``, ``_marker_write_task``,
``_member_reconcile_task``, ``_memory_startup``, ``_persistence_watchdog_task``,
``_profile_watch_task``, ``_session_environments_ready``, ``_sign_in_refresher_task``,
``_update_check_task``, ``channel_history``, ``consolidator``, ``conv_log``, ``ctx_builder``,
``dashboard_state``, ``sessions``, ``slack``, ``task_runner``, ``vector_memory``.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import contextlib
    import json
    import os
    import sys
    import time
    import uuid
    import webbrowser

    from kiro_crew.slack.gateway import (
        _SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS,
        BOOT_RESTART_HINT,
        MAX_SESSION_TTL_SECS,
        ChannelHistory,
        ContextBuilder,
        ConversationLog,
        GatewayOrchestrator,
        HistoryConsolidator,
        HookManager,
        LessonStore,
        MemoryStore,
        RealSlackClient,
        SessionManager,
        SkillsLoader,
        Task,
        TaskRunner,
        _live_session_work_dirs,
        _run_signing_key_preflight_bounded,
        _session_work_dir,
        _start_coder_retention,
        _sweep_predecessor_session_work_dirs,
        build_dashboard_url,
        build_provider_factory,
        cautious_boot,
        check_bundle_freshness,
        crash_guard,
        current_context,
        data_home,
        datetime,
        format_dashboard_urls,
        generate_token,
        hooks_config_from_config_dict,
        is_local_only,
        kiro_agents_dir,
        live,
        log_probe_failure,
        logger,
        parse_dashboard_url,
        platform_compat,
        probe_data_home,
        redact_credentials,
        redact_exfiltration_urls,
        resolve_dashboard_host,
        resolve_session_start_sizing,
        run_persistence_watchdog,
        run_sign_in_refresher,
        run_stale_asset_watchdog,
        safe_context_call,
        sel,
        shutdown_event,
        start_dashboard,
        subprocess_executor,
        supervisor_reentry,
        timezone,
        warm_backend,
        warn_unreadable_tracked_channels,
    )

    # The facade's KAS floor task (``process_entry._start_kas_cli_floor``); read only.
    _KAS_CLI_FLOOR_TASK: asyncio.Task[None] | None


async def _auto_open_dashboard(self: GatewayOrchestrator, dashboard_url: str) -> None:
    """Open the dashboard in the operator's browser, best effort.

    Offloaded to the subprocess executor — ``webbrowser.open()`` can block
    indefinitely on a wedged ``/usr/bin/open``, which would starve the
    default thread pool if this used ``asyncio.to_thread()``. The subprocess
    executor is a dedicated pool for exactly this class of hang.

    Runs as a background task rather than inline on the boot path so a slow
    browser launch overlaps the MCP probe instead of adding to it. The URL
    has already been printed by the time this is called, so a failure here
    costs the operator a click, not the address.
    """
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                webbrowser.open,
                dashboard_url,
            ),
            timeout=5.0,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.debug("webbrowser.open timed out — skipping")
        print(
            "👻 Browser was slow to open — skipping auto-open.\n"
            "   Dashboard is running. Open this URL manually:\n"
            f"   {dashboard_url}\n"
            "   Or run: kirocrew token"
        )


async def _init_services(self: GatewayOrchestrator) -> None:
    """Initialize memory, skills, hooks, context, history, sessions.

    Async so the kiro-cli version probe and pip dep repair run off the
    event loop. Memory restore, database initialization and FTS rebuilding
    belong to the tracked worker started after dashboard readiness. Object
    CONSTRUCTION deliberately stays on the loop thread:
    ``SessionManager.__init__`` creates asyncio primitives (locks,
    semaphores, queues), so hopping the whole method into a worker thread
    would trade a blocking bug for a thread-affinity one. (Other sync
    filesystem steps — e.g. agent-config install — remain on the loop;
    they are bounded small-file work, not usage-scaled. The builtin-skills
    sync verifies user-owned trees before replacing them, so it runs as a
    tracked background task in a worker thread and never gates readiness.)
    """
    if not self._slack_enabled:
        logger.info("Slack not configured — starting without the Slack gateway")

    # Check kiro-cli version (--agent requires >= 1.26)
    await self._warn_if_kiro_cli_outdated()

    # Auto-repair missing pip deps (handles chicken-and-egg after auto-update)
    try:
        await self._check_missing_deps()
    except Exception:
        logger.warning("Dep check failed", exc_info=True)

    # Auto-install agent config so MCP servers are always up to date
    try:
        from kiro_crew.agent import rebuild_agent_config  # circular import

        # Off-loop, with the fork refresh deferred: the rebuild gates
        # readiness, and per-fork work scales with fork count.
        # rebuild_agent_config owns the deferral (skip + background
        # schedule together), so this caller cannot get half the pair.
        path = await asyncio.to_thread(rebuild_agent_config, refresh_forks="defer")
        logger.info("Agent config installed: %s", path)

        # Deliver shim + one-time stale-MCP purge automatically — the
        # desktop app launches the gateway but never runs `kirocrew setup`.
        #
        # Routed through the CPP seam (``AgentRuntime.run_first_run_setup``)
        # rather than importing ``agent.run_first_run_setup`` directly, so
        # first-run setup is genuinely extensible: an edition composes an
        # adapter that adds its own one-time provisioning on top. The
        # ``DefaultAgentRuntime`` delegates to exactly the same
        # ``agent.run_first_run_setup()`` a direct import would call, so the
        # standalone build is behaviorally identical (asserted in
        # test_cpp_wiring_standalone).
        #
        # ``safe_context_call`` keeps a transient adapter error from breaking
        # startup (``fallback=None`` matches the seam's ``-> None`` contract),
        # matching the best-effort posture of this block.
        # The fail-closed guarantee for a non-standalone host is already
        # discharged EARLIER, by ``boot_platform`` in ``run_gateway``: it
        # aborts before the orchestrator is built, so a companion that cannot
        # compose never reaches this line. (Note the enclosing
        # ``except Exception`` would itself absorb a PlatformCompositionError
        # raised here — which is why boot, not this call site, is where that
        # invariant is enforced.)
        safe_context_call(
            lambda: current_context().agent_runtime.run_first_run_setup(),
            fallback=None,
            log_message="agent_runtime.run_first_run_setup failed",
        )
    except Exception:
        # Boot deliberately continues: a gateway with no agent spec still
        # serves the dashboard, and one command repairs it. But this is NOT a
        # warning-level event — without a spec on disk kiro-cli answers every
        # session/set_mode with "Mode '<name>' not found", so EVERY chat turn
        # and every background turn fails for the life of the install. Log at
        # ERROR and print the remedy, mirroring _check_missing_deps: the
        # desktop app launches the gateway with no terminal in sight, so the
        # log is the only durable record and the print is what a
        # console-launched operator actually sees.
        logger.error("Agent config install failed", exc_info=True)
        print(
            "ERROR: agent config install failed — chat sessions cannot start. "
            "Repair with: kirocrew setup --agent-only --clean"
        )

    # Verify what actually landed on disk, whether or not the block above
    # raised. An exception is only one of the ways to end up with no spec (see
    # missing_required_agent_specs for the two silent ones), and the failure
    # mode is identical in all of them: every turn dies at session/set_mode.
    # Deliberately outside the try above so a raising install still gets
    # verified, and best-effort itself so a stat error cannot break boot.
    try:
        from kiro_crew.agent import missing_required_agent_specs  # circular import

        missing = missing_required_agent_specs()
        if missing:
            logger.error(
                "Agent specs missing after install: %s (in %s) — every chat turn "
                "will fail at session/set_mode with \"Mode '<name>' not found\". "
                "Repair with: kirocrew setup --agent-only --clean",
                ", ".join(missing),
                kiro_agents_dir(),
            )
            print(
                f"ERROR: agent specs missing after install: {', '.join(missing)} — "
                "chat sessions cannot start. "
                "Repair with: kirocrew setup --agent-only --clean"
            )
    except Exception:
        logger.debug("Agent spec verification failed", exc_info=True)

    self.slack = RealSlackClient(self._bot_token) if self._slack_enabled else None
    # Slack agent sessions (status, title, native Stop): the driver is attached
    # at boot and reads slack.agent_sessions (and the install's capability) live.
    from kiro_crew.slack.agent_sessions.status import configure as _configure_agent_sessions

    _configure_agent_sessions(self)
    factory = build_provider_factory(self._cfg)

    # Only in-memory wiring belongs on the boot path. The closed barrier
    # refuses memory consumers until the preparation worker has restored and
    # initialized every shared tier. No old store is opened in the meantime.
    from kiro_crew.memory_startup import MemoryStartup

    self._memory_startup = MemoryStartup.begin()

    # Any telemetry.* write rebuilds the metrics recorder, so a field other than
    # `enabled` (retention, export interval, OTLP endpoint) is not frozen at
    # first use.
    try:
        from kiro_crew.dashboard.chat_persistence import watch_config as _watch_entry_cache_config
        from kiro_crew.metrics.provider import watch_config as _watch_telemetry_config

        _watch_telemetry_config()
        # Entry-cache bounds are memoised on the chat save path; the watcher
        # drops the memo so a new bound applies to the next flush.
        _watch_entry_cache_config()
    except Exception:
        logger.debug("service config watchers not fully registered", exc_info=True)

    # Session search splits Chinese runs into dictionary words in THIS process,
    # which holds the host's one copy of the dictionary; mcp-core processes ask
    # it over /api/search/segment. In-memory wiring only: the dictionary itself
    # loads on the first Chinese query.
    from kiro_crew.history_search import register_cjk_segmenter
    from kiro_crew.history_words import segment_runs

    register_cjk_segmenter(segment_runs)

    # Memory, skills, hooks, lessons
    memory = MemoryStore()

    # Vector memory (structured semantic store)
    from kiro_crew.vector_memory import VectorMemoryStore

    self.vector_memory = VectorMemoryStore(
        embedding_dim=self._cfg.memory.embedding_dim, config=self._cfg
    )
    # Preserve one object for context, consolidation and the dashboard;
    # its database opens only in the authorized preparation worker.
    memory.vector_store = self.vector_memory

    # Bind-fast: construct the loader WITHOUT syncing (it reads whatever
    # is on disk now) and run the builtin sync as a tracked background
    # task in a worker thread. The sync verifies user-owned trees before
    # it may replace them, so its cost scales with what users put in the
    # skills dir — it must gate neither the event loop nor the dashboard
    # socket. Listings pick the synced skills up as soon as it completes.
    skills = SkillsLoader(install_builtins=False)
    # The in-place restarts end in os.execv, which kills the usage ledger's
    # daemon flush thread with up to 60 s of hits still in memory. The pre-exec
    # seam runs this before both of them; _shutdown_and_exit flushes on its own.
    from kiro_crew import skill_usage
    from kiro_crew.pre_exec_stops import register_pre_exec_stop

    register_pre_exec_stop("Skill-usage ledger", skill_usage.flush_all_before_exec)

    async def _sync_builtin_skills() -> None:
        try:
            await asyncio.to_thread(skills.sync_builtins)
        except Exception:
            logger.warning("builtin-skill sync failed", exc_info=True)

    _skills_sync_task = asyncio.create_task(_sync_builtin_skills())
    self._background_tasks.add(_skills_sync_task)
    _skills_sync_task.add_done_callback(self._background_tasks.discard)
    # Opt-out state comes from the keystone denied_commands.json (agent-
    # unwritable), not config.json's hooks section.
    hooks = HookManager(hooks_config_from_config_dict(self._cfg.hooks))
    # Follow config.json's hooks section live. The heartbeat-scoped manager is
    # re-derived from this one per cycle, so it inherits the reload without
    # subscribing (and must not, see HookManager.watch_config).
    hooks.watch_config()
    lessons = LessonStore()
    self.ctx_builder = ContextBuilder(
        memory=memory,
        skills=skills,
        hooks=hooks,
        lessons=lessons,
        bot_name=self._cfg.agent.bot_name,
    )

    self.conv_log = ConversationLog()
    self.conv_log.init()
    self.ctx_builder.conversation_log = self.conv_log

    self.sessions = SessionManager(self._cfg, provider_factory=factory)  # type: ignore[arg-type]

    self.consolidator = HistoryConsolidator(
        log=self.conv_log,
        memory=memory,
        sessions=self.sessions,
        lesson_store=lessons,
        history_idle_secs=self._cfg.memory.history_idle_hours * 3600,
        vector_store=self.vector_memory,
        migrated=self._cfg.memory.migrated,
        skills_loader=skills,
        auto_skills_enabled=self._cfg.skills.auto_create_from_sessions,
        auto_refine_enabled=self._cfg.skills.auto_refine_on_deviation,
        auto_min_tool_calls=self._cfg.skills.auto_min_tool_calls,
        auto_similarity_threshold=self._cfg.skills.auto_similarity_threshold,
        approval_required=self._cfg.skills.approval_required,
        max_auto_skills=self._cfg.skills.max_auto_skills,
        stale_after_days=self._cfg.skills.stale_after_days,
        archive_after_days=self._cfg.skills.archive_after_days,
        generate_scripts=self._cfg.skills.generate_scripts,
        judge_model=self._cfg.skills.judge_model,
    )

    # Trigger skill extraction when sessions expire (idle/orphan)
    self.sessions.on_session_expire = self.consolidator.consolidate_session
    # A span abandoned at the consolidation attempt cap is otherwise only a
    # log WARNING; surface it in the notification bell.
    self.consolidator.on_abandoned = self._notify_consolidation_abandoned

    # Same expiry paths, the other direction: a parent with a completion
    # injection in flight must not be expired under it. This counter is the
    # only witness in the window between committing the injected turn and
    # acquiring the session, which is why the three reset sites in this file
    # consult it too.
    self.sessions.set_injection_probe(
        lambda key: self._cron_injecting.get(key, 0) > 0,
    )

    # Channel history buffer. data_home(), not config_dir(): this method is
    # async and config_dir() re-runs start-of-process maintenance (mkdir,
    # breadcrumb refresh, archive sweep) on every call.
    self.channel_history = ChannelHistory(
        observe_max_entries=self._cfg.observe_max_messages,
        observe_ttl_secs=int(self._cfg.observe_ttl_hours * 3600),
        history_dir=data_home() / "history",
    )
    self.ctx_builder.channel_history = self.channel_history

    # Register observe-mode channels for deeper history buffer
    from kiro_crew.config.loader import ACTIVATION_OBSERVE

    for ch_id, ch_cfg in self._cfg.slack_channels.items():
        if ch_cfg.activation == ACTIVATION_OBSERVE:
            self.channel_history.set_observe(ch_id)


def _notify_consolidation_abandoned(
    self: GatewayOrchestrator,
    key: str,
    message_count: int,
    reason: str,
    char_count: int = 0,
    last_slice: bool = False,
    retired: bool = False,
) -> None:
    """Tell the user a session span was dropped from memory consolidation.

    The consolidator gives up on a span after its attempt cap and marks it
    consolidated so it stops re-billing; that span's history, preferences
    and lessons are never extracted. This note is the user-visible record
    of that, since a marked span does not appear in any pending listing.

    ``char_count`` is non-zero when the abandon dropped ONE budget-sized
    slice of a single over-budget message (sub-chunking) rather than whole
    messages, so the notice names the character size of the dropped slice
    instead of a message count. ``last_slice`` is True when that slice was
    the message's final one: the marker has moved past the whole message, so
    there is no next slice and the notice must not promise one.

    ``retired`` is True when the span was dropped DELIBERATELY by the
    per-head slice ceiling (a cost cap), not after repeated failures. Nothing
    failed and the gateway log holds no error for it, so the notice must not
    say "repeated consolidation failures" or point at an "underlying error".
    """
    state = self.dashboard_state
    if state is None:
        return
    if char_count > 0:
        # One slice of a single over-budget message was dropped; the rest of
        # the session is unaffected, so the title says "part of a session".
        title = "Memory consolidation gave up on part of a session"
        if retired:
            # The per-head slice ceiling retired the rest of the message to
            # bound cost. The marker has moved past the whole message, so
            # there is no next slice; nothing failed, so no "underlying error".
            tail = "lessons were not extracted; the message is now fully " "consolidated."
            body = (
                f"A {char_count}-character remainder of an oversized message in "
                f"session {key} was dropped from memory to bound consolidation "
                f"cost ({reason}). That remainder's history, preferences and "
                f"{tail}"
            )
        else:
            if last_slice:
                tail = (
                    "lessons were not extracted; the message is now fully "
                    "consolidated. The gateway log has the underlying error."
                )
            else:
                tail = (
                    "lessons were not extracted; consolidation continues from "
                    "the next slice. The gateway log has the underlying error."
                )
            body = (
                f"A {char_count}-character slice of an oversized message in session "
                f"{key} was dropped from memory after repeated consolidation "
                f"failures ({reason}). That slice's history, preferences and "
                f"{tail}"
            )
    else:
        title = "Memory consolidation gave up on a session"
        body = (
            f"{message_count} messages from session {key} were dropped from memory "
            f"after repeated consolidation failures ({reason}). Their history, "
            "preferences and lessons were not extracted. The gateway log has the "
            "underlying error."
        )
    state.notify(
        "agent",
        title,
        body,
        meta={"session_key": key, "kind": "consolidation-abandoned"},
    )


def _schedule_session_start_sizing(self: GatewayOrchestrator) -> None:
    """Run :meth:`_log_session_start_sizing` as a tracked background task.

    Called after the dashboard/API socket binds, so the host probe never
    delays READY (AUTOSDE ``no-new-work-on-gateway-boot-path``).
    """
    task = asyncio.create_task(self._log_session_start_sizing())
    self._background_tasks.add(task)
    task.add_done_callback(self._background_tasks.discard)


async def _log_session_start_sizing(self: GatewayOrchestrator) -> None:
    """Size ``agent.session_start_concurrency`` off the loop and log it.

    The setting is ``restart=True`` and ``"auto"`` reads the host (affinity,
    cgroup ``cpu.max`` files, the memory probe) once per process. Doing it
    here, in a worker thread scheduled right after the socket binds, keeps
    that file I/O off the event loop and off the boot path: the subagent
    manager and the SessionStartGate resolve the width lazily and then hit
    the per-process cache. The log line is the record of the width the
    running gateway's gate uses.

    The cold-start widths (``agent.cold_start_concurrency`` and
    ``agent.runtime_spawn_concurrency``) follow the same reading, so the same
    thread resolves and logs them. The session manager was built before this
    reading existed, at the cached widths (the floors for ``"auto"``), so
    once it lands :meth:`SessionManager.widen_cold_start_queues` grows its
    queues to the logged widths, on the loop and without another probe.
    """
    agent = self._cfg.agent

    def _resolve_all() -> tuple[str | None, str | None]:
        # Each half is guarded on its own so one failing does not leave the
        # other unresolved (and its host read for the event loop to do).
        session_start = cold_start = None
        try:
            session_start = resolve_session_start_sizing(agent.session_start_concurrency).describe()
        except Exception:
            logger.warning("Session start concurrency sizing failed", exc_info=True)
        try:
            from kiro_crew.cold_start_sizing import describe_cold_start_limits

            cold_start = describe_cold_start_limits(agent)
        except Exception:
            logger.warning("Cold-start concurrency sizing failed", exc_info=True)
        return session_start, cold_start

    try:
        session_start, cold_start = await asyncio.to_thread(_resolve_all)
    except Exception:
        logger.warning("Session start sizing failed", exc_info=True)
        return
    if session_start is not None:
        logger.info("Session start concurrency: %s", session_start)
    if cold_start is not None:
        logger.info("Cold-start concurrency: %s", cold_start)
        sessions = getattr(self, "sessions", None)
        if sessions is not None:
            try:
                sessions.widen_cold_start_queues()
            except Exception:
                logger.warning("Cold-start queue widening failed", exc_info=True)


def _init_task_runner(self: GatewayOrchestrator) -> None:
    """Initialize the task runner."""

    async def _task_notify(
        title: str, body: str, task_id: str = "", *, session_key: str = ""
    ) -> None:
        if self.dashboard_state:
            body, _ = redact_exfiltration_urls(body)
            body, _ = redact_credentials(body)
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
            meta = {"task_id": task_id} if task_id else None
            self.dashboard_state.notify("taskrunner", title, body, meta=meta)
            self.dashboard_state.push_refresh("taskrunner")
        # Send approval-related notifications to Slack DM so user knows even when away.
        # Match on specific title patterns from task_executor, not broad keywords
        # (avoids false positives like "Investigating gateway error").
        if "requires approval" in title.lower() or "denied" in title.lower():
            try:
                safe_t = redact_credentials(redact_exfiltration_urls(title)[0])[0]
                safe_b = redact_credentials(redact_exfiltration_urls(body)[0])[0]
                notice = f"*{safe_t}*\n{safe_b}"
                # Governed channel ladder first, owner DM as the fallback —
                # the same ordering the cron delivery path uses, and for the
                # same reason: a task blocked on an approval nobody was told
                # about is indistinguishable from a hung one. The owner DM
                # reaches Slack only, so a Telegram-only operator learned
                # nothing and the run simply stalled. ``session_key`` is the
                # ORIGINATING conversation, threaded down from
                # ``start_background``; it is empty for a dashboard- or
                # CLI-started run, and ``_deliver_channel_reply`` also
                # returns False for a Slack, dashboard or unrecognized key,
                # so the DM below is reached in every case that has no
                # channel behind it.
                if session_key and await self._deliver_channel_reply(session_key, notice):
                    return
                if self.slack and self._owner_id:
                    ch = await self.slack.open_dm(self._owner_id)
                    if ch:
                        await self.slack.post_message(ch, notice)
            except Exception as exc:
                # Not "to Slack DM" any more: the try now spans the channel
                # ladder as well, so naming one surface would misdirect
                # whoever reads this line.
                logger.warning("Failed to send task approval notification: %s", exc)

    assert self.sessions is not None
    self.task_runner = TaskRunner(
        sessions=self.sessions,
        context_builder=self.ctx_builder,
        on_notify=_task_notify,
        work_dir=_session_work_dir("taskrunner:main"),
        conversation_log=self.conv_log,
        consolidator=self.consolidator,
        lesson_store=LessonStore(),
        max_parallel_steps=self._cfg.taskrunner.max_parallel_steps,
        workspace_dir=self._cfg.taskrunner.workspace_dir,
        test_cmd=self._cfg.taskrunner.test_command,
        require_verification=self._cfg.taskrunner.require_verification,
    )
    self.task_runner._on_tool_approval = self._interactive_approval("taskrunner")

    # Task-level approval handler: blocks until user approves via dashboard UI
    async def _task_approval(task: "Task") -> bool:
        if not self.dashboard_state:
            logger.warning("No dashboard state — denying task %d approval", task.index)
            sel().log_api_access(
                caller="taskrunner",
                operation="task.force_approval",
                outcome="denied",
                source="gateway",
                resources=f"task-{task.index}",
                error="no dashboard state available",
            )
            return False
        clean_title, _ = redact_exfiltration_urls(task.title or "")
        clean_title, _ = redact_credentials(clean_title)
        approval_id = f"task-gate-{task.index}-{uuid.uuid4().hex[:8]}"
        result = await self.dashboard_state.request_approval(
            approval_id=approval_id,
            source="taskrunner",
            tool=f"Task {task.index}: {clean_title}",
            tool_purpose="Task requires manual approval before execution",
        )
        sel().log_api_access(
            caller="taskrunner",
            operation="task.force_approval",
            outcome="approved" if result else "denied",
            source="dashboard",
            resources=f"task-{task.index}",
        )
        return result

    # mypy checks this owner before the facade's bindings, so this function runs in a
    # deferred pass, where the nested def above drops the narrowing from the assignment.
    self.task_runner._on_approval = _task_approval  # type: ignore[union-attr]


async def _init_dashboard(self: GatewayOrchestrator) -> None:
    """Start the dashboard web server."""
    assert self.sessions is not None
    assert self.cron_svc is not None

    configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
    # --port override (literal int or "auto" for ephemeral)
    if self._port_override == "auto":
        dashboard_port = 0
    elif self._port_override is not None:
        dashboard_port = int(self._port_override)
    self._dashboard_port = dashboard_port
    self._configured_host = configured_host
    self._local_only = is_local_only(configured_host, self._slack_enabled)
    self._dashboard_runner, self.dashboard_state = await start_dashboard(
        sessions=self.sessions,
        crons=self.cron_svc,
        lessons=LessonStore(),
        port=dashboard_port,
        subagents=self.subagent_mgr,
        context_builder=self.ctx_builder,
        conversation_log=self.conv_log,
        consolidator=self.consolidator,
        task_runner=self.task_runner,
        slack_connected=self._slack_enabled,
        local_only=self._local_only,
        configured_host=configured_host,
        dashboard_url=self._cfg.dashboard.url,
        slack_client=self.slack,
        owner_id=self._owner_id,
        assume_kiro_ready=self._test_mode,
        kiro_prerequisite_service=self._boot_kiro_service,
        defer_channel_agent_resume=True,
        schedule_memory_preparation=self._schedule_memory_preparation,
    )
    # When --port auto was requested, read the OS-assigned ephemeral port
    # back from the runner so subsequent URL building and the READY line
    # use the real bound port.
    if dashboard_port == 0 and self._dashboard_runner is not None:
        addresses = self._dashboard_runner.addresses
        if addresses:
            self._dashboard_port = addresses[0][1]
    if self.slack and self.dashboard_state:
        self.dashboard_state.slack_client = self.slack
    if self.dashboard_state:
        self.dashboard_state.no_crons = self._no_crons  # dashboard mode
    if self.dashboard_state and not self._test_mode:
        # Told once, after the KAS floor answers: a Kiro CLI pick now runs on KAS.
        from kiro_crew import kiro_on_kas_notice

        kiro_on_kas_notice.schedule(self.dashboard_state, _KAS_CLI_FLOOR_TASK)


async def _init_api_server(self: GatewayOrchestrator) -> None:
    """Start a minimal API-only HTTP server for MCP tool transport."""
    from kiro_crew.dashboard import start_api_server

    assert self.sessions is not None
    assert self.cron_svc is not None
    configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
    # --port override (literal int or "auto" for ephemeral)
    if self._port_override == "auto":
        dashboard_port = 0
    elif self._port_override is not None:
        dashboard_port = int(self._port_override)
    self._dashboard_port = dashboard_port
    self._configured_host = configured_host
    self._local_only = is_local_only(configured_host, self._slack_enabled)
    self._dashboard_runner, self.dashboard_state = await start_api_server(
        sessions=self.sessions,
        context_builder=self.ctx_builder,
        crons=self.cron_svc,
        lessons=LessonStore(),
        port=dashboard_port,
        subagents=self.subagent_mgr,
        task_runner=self.task_runner,
        slack_client=self.slack,
        owner_id=self._owner_id,
        local_only=self._local_only,
        configured_host=configured_host,
        assume_kiro_ready=self._test_mode,
        kiro_prerequisite_service=self._boot_kiro_service,
        conversation_log=self.conv_log,
        schedule_memory_preparation=self._schedule_memory_preparation,
    )
    if dashboard_port == 0 and self._dashboard_runner is not None:
        addresses = self._dashboard_runner.addresses
        if addresses:
            self._dashboard_port = addresses[0][1]
    if self.dashboard_state:
        self.dashboard_state.no_crons = self._no_crons  # API-only mode


async def run(self: GatewayOrchestrator) -> None:
    """Start all services and block until shutdown signal."""
    # ── Crash guard (D1/D2 of Lorikeets-3929) ──
    # Install the asyncio exception handler on the running loop.
    # atexit + excepthook were already installed in cli.py before asyncio.run().
    crash_guard.install_loop_handler(asyncio.get_running_loop())

    # Log process identity to the gateway log (D3 of Lorikeets-3929)
    logger.info(
        "=== GATEWAY PID=%d STARTED AT %s ===",
        os.getpid(),
        datetime.now(timezone.utc).isoformat(),
    )
    # Raise FD limit — each kiro-cli session uses ~6 FDs (3 pipes)
    # plus MCP server subprocesses. Default macOS limit (256) is too low.
    # No-op on Windows (no per-process descriptor rlimit).
    platform_compat.raise_nofile_soft_limit(10240)

    # Refuse to boot when the data home cannot persist state. Every save
    # path (chat history, cron history, session PIDs) needs file creation +
    # advisory locking in the data home; when either is broken (e.g. a
    # seccomp filter inherited from a sandboxed parent turns flock/mkstemp
    # into ENOSYS) the gateway would still serve traffic while silently
    # dropping every write — and the very next call below would crash with
    # a raw traceback anyway. Failing here is loud, early, and actionable.
    # Off-loop: the probe does real filesystem I/O (mkstemp + flock), which
    # on a stalled filesystem would otherwise wedge the event loop.
    def _probe_persistence() -> str | None:
        return platform_compat.probe_file_persistence(data_home())

    try:
        persistence_error = await asyncio.to_thread(_probe_persistence)
    except RuntimeError as exc:
        # asyncio.to_thread could not get a worker thread (executor
        # exhaustion/shutdown). A process that cannot spawn one thread at
        # boot cannot run session pools either — route through the clean
        # preflight exit below instead of dying with a raw traceback.
        persistence_error = f"cannot run the persistence preflight: {exc}"
    if persistence_error is not None:
        logger.critical(
            "Persistence preflight failed — refusing to start: %s",
            persistence_error,
        )
        print(
            f"❌ Cannot persist state: {persistence_error}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Refuse to boot on a truncated signing key (see signing_key_preflight).
    # Off-loop for the same reason as the persistence probe, and bounded: a
    # key leaf on a disconnected mount must refuse the boot, not stall it.
    try:
        await _run_signing_key_preflight_bounded()
    except (TimeoutError, asyncio.TimeoutError) as exc:
        logger.critical(
            "Signing key preflight failed — refusing to start: it did not finish "
            "within %.0fs (unresponsive data home?)",
            _SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS,
        )
        print(
            f"❌ The signing key preflight did not finish within "
            f"{_SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS:.0f}s. Check that the data "
            f"home is reachable, then restart: {BOOT_RESTART_HINT}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        # No worker thread at boot (executor exhaustion/shutdown). Running
        # the preflight inline would put its lstat and key load on the event
        # loop, so a stalled data home would wedge boot instead of refusing
        # it. Exit loud, the way the persistence preflight above does.
        logger.critical(
            "Signing key preflight failed — refusing to start: cannot run it off the "
            "event loop: %s",
            exc,
        )
        print(f"❌ Cannot run the signing key preflight: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    # Clean up orphaned kiro-cli processes from previous runs
    from kiro_crew.session import cleanup_orphaned_sessions

    # Off-loop: the sweep is synchronous filesystem work and this runs
    # inside the orchestrator coroutine.
    #
    # ``narrow_with_leaders=False`` keeps boot doing exactly what it did
    # before the recycled-pid work: the leaders snapshot is a /proc read this
    # path may not carry, since no-new-work-on-gateway-boot-path names orphan
    # sweeps.
    #
    # The narrowing is asked for on the graceful-shutdown sweep only, so a
    # gateway that is hard-killed never runs it and its recycled-pid mappings
    # wait for a later clean exit. That is the accepted cost of keeping this
    # path, and the force-exit handler, doing exactly their pre-existing work.
    await asyncio.to_thread(cleanup_orphaned_sessions, narrow_with_leaders=False)

    # KAS MCP launch snapshots no session of this run references: the previous run's
    # agent processes, and the launchers they started, ended just above, and no
    # session of this run exists yet. The cutoff is taken HERE, so a snapshot this
    # run writes is never older than it; the scan itself runs on a tracked task, the
    # way the telemetry backfill below does, so its cost never delays readiness.
    _snapshot_sweep_boot_time = time.time()

    async def _sweep_kas_launch_snapshots() -> None:
        try:
            from kiro_crew.agent_sdk.agent_specs import sweep_kas_launch_snapshots

            await asyncio.to_thread(sweep_kas_launch_snapshots, _snapshot_sweep_boot_time)
        except Exception:  # housekeeping must never break boot; skipped, not run
            logger.warning("KAS launch snapshot sweep skipped", exc_info=True)

    _snapshot_sweep_task = asyncio.create_task(_sweep_kas_launch_snapshots())
    self._background_tasks.add(_snapshot_sweep_task)
    _snapshot_sweep_task.add_done_callback(self._background_tasks.discard)

    # Same "previous run left residue" concern as the orphan sweep above, for
    # telemetry rather than processes: any open-session crumb on disk belongs
    # to a session that never reached a teardown path, so it is emitted as
    # end_reason=crashed.
    #
    # Deliberately NOT awaited here. The scan is a glob plus a read/stat/unlink
    # per crumb, so its cost scales with accumulated user data, and running it
    # inline would delay readiness in proportion to that. It goes to a worker
    # thread on a tracked task instead, and the process-start cutoff means it
    # cannot mistake a session THIS process opens in the meantime for a
    # casualty of the last one -- so it does not have to finish before the
    # gateway starts serving.
    _telemetry_backfill_cutoff = time.time()

    async def _backfill_unclean_session_telemetry() -> None:
        try:
            from kiro_crew.metrics.sessions import backfill_crashed_sessions

            count = await asyncio.to_thread(backfill_crashed_sessions, _telemetry_backfill_cutoff)
            if count:
                logger.info(
                    "Telemetry: back-filled %d unclean session lifetime(s) "
                    "from the previous run",
                    count,
                )
        except Exception:  # telemetry must never break boot
            logger.debug("unclean-session telemetry backfill failed", exc_info=True)

    _backfill_task = asyncio.create_task(_backfill_unclean_session_telemetry())
    self._background_tasks.add(_backfill_task)
    _backfill_task.add_done_callback(self._background_tasks.discard)

    # Fill the sandbox probe cache BEFORE any on-loop spawn path can reach
    # detect_backend(). Waiting (off-loop) rather than firing-and-forgetting
    # is what makes that guarantee hold: a fire-and-forget prewarm leaves the
    # very next caller racing the warm thread and reading a cold-cache
    # transient as "no sandbox backend on this host".
    try:
        await asyncio.to_thread(warm_backend)
    except RuntimeError:
        logger.warning("sandbox warm_backend skipped (thread exhaustion); cache stays cold")

    # Enterprise build only: no kiro sign-in, no Crew. Checked before any service
    # starts and long before the HTTP server binds; a failure exits non-zero. The
    # check reads the one enterprise sign-in (Crew's vault sign-in) and
    # always hands back None, so start_dashboard builds its own setup service.
    from kiro_crew.enterprise_boot import require_kiro_login

    self._boot_kiro_service = await require_kiro_login(assume_ready=self._test_mode)

    # ── Initialise all services ──
    from kiro_crew.slack.events import SeenCache, init_socket_mode
    from kiro_crew.slack.interactions import init as init_interactions

    # Cautious boot: decide ONCE — off-loop — whether the previous instance
    # left a recent loop-stall crash dump. If it did, the pause_before()
    # calls below (and in start_dashboard) stagger the startup battery so
    # a host that is possibly still under the same memory pressure is not
    # hit with everything at once. Fails open: any error means normal boot.
    await cautious_boot.initialize()

    seen = SeenCache()
    await self._init_services()

    # Wire in-process embeddings (always-on) and kick background model download
    await self._start_embeddings()

    # Auto-migration starts only after deferred restore and memory init.

    # Start MCP gateway sidecar before any ACP session can spawn.  The
    # rewriter writes the agent-JSON overlay first so kiro-cli picks up
    # the broker-wired MCP entries the moment a session starts.  No-op
    # when ``mcp_gateway.enabled`` is False.
    await cautious_boot.pause_before("MCP gateway sidecar")
    await self._init_mcp_gateway()

    # Loading the cron scheduler reads and reconciles durable jobs. Under
    # cautious boot this pause keeps that work out of the app/MCP launch
    # window; overdue callbacks remain disarmed until memory is prepared.
    await cautious_boot.pause_before("cron scheduler")
    # Load and reconcile durable jobs for the dashboard, but do not arm
    # overdue callbacks while the restore/open worker still owns memory.
    await self._init_cron(arm=False)
    self._init_mcp_discovery()
    self._init_subagents()
    self._init_task_runner()

    async def _release_orphan_notices_once_rebuilt(tabs_rebuilt: asyncio.Event) -> None:
        await tabs_rebuilt.wait()
        if self.subagent_mgr is not None:
            self.subagent_mgr.release_orphan_notices()

    try:
        if not self._no_dashboard:
            await self._init_dashboard()
            self._announce_cron_quarantine()
        else:
            await self._init_api_server()
    finally:
        # The boot orphan sweep's notices go to the tabs that show their parents.
        # A started dashboard has restored its own chats, but a channel
        # conversation's tab comes back with the channel reconciler's first
        # complete pass, which runs in the background: the release waits for it
        # on a tracked task, so the boot does not (``startup_tabs_rebuilt`` is
        # already set when channel surfacing is off). The API-only server has no
        # tabs, and a start that failed before the dashboard state existed
        # releases at once too, so the sweep can still send the owner DM digest;
        # if the process exits first, the runs stay untombstoned and the next
        # start announces them.
        state = None if self._no_dashboard else self.dashboard_state
        if state is not None and not state.startup_tabs_rebuilt.is_set():
            _release_task = asyncio.create_task(
                _release_orphan_notices_once_rebuilt(state.startup_tabs_rebuilt)
            )
            self._background_tasks.add(_release_task)
            _release_task.add_done_callback(self._background_tasks.discard)
        elif self.subagent_mgr is not None:
            self.subagent_mgr.release_orphan_notices()
    # The socket is bound. Move the boot heap into the permanent generation so
    # later full collections stop re-scanning it (~150k objects, a 50 ms
    # stop-the-world pass on the event loop), then turn cyclic GC back on:
    # `kirocrew gateway` imports with it off (kiro_crew._bootstrap.main).
    # O(1), and deliberately no gc.collect() first: that is a blocking pass.
    import gc

    gc.freeze()
    gc.enable()
    # The dashboard/API socket is bound now. A missing wrapper can take the
    # full pip timeout to repair, so track that work without delaying READY.
    # The task itself catches and logs failures; startup remains available.
    self._schedule_console_script_repair()
    # Size agent.session_start_concurrency ("auto" reads the host) after
    # the bind, as a tracked background task: nothing before READY needs
    # the width, and readers resolve it lazily from the same cache.
    self._schedule_session_start_sizing()

    # Record this gateway's own kirocrew launcher, keyed by the port it
    # serves, so a remote token-mint execs THIS install's venv instead of
    # a stale ~/.local/bin/kirocrew that may point at an uninstalled
    # worktree. See kiro_crew.instances.run_marker. Written for headless
    # API-only gateways too: the marker's filename is what lets a client
    # or MCP child discover a non-default port when neither KIROCREW_PORT
    # nor dashboard.url names one. Dispatched as a tracked background
    # task, never awaited: write_marker does atomic file writes plus a
    # prune scan over prior runs' markers, so on a slow filesystem an
    # await here would gate READY on file-count-scaled maintenance. The
    # marker is best-effort discovery metadata — nothing at boot depends
    # on it, and write_marker never raises. The guard keeps startup alive
    # even when dashboard init was skipped and no port was ever resolved.
    try:
        from kiro_crew.instances import run_marker

        if self._dashboard_port:
            _marker_task = asyncio.create_task(
                asyncio.to_thread(self._write_marker_worker, run_marker, self._dashboard_port)
            )
            self._marker_write_task = _marker_task
            self._background_tasks.add(_marker_task)
            _marker_task.add_done_callback(self._background_tasks.discard)
    except Exception:
        logger.debug("Gateway run-marker write skipped", exc_info=True)

    # Publish the MCP-gateway broker + apply callbacks onto
    # DashboardState now that it exists (the broker started earlier).
    self._wire_mcp_gateway_dashboard()

    # Emit machine-readable READY line for test harnesses (--json-ready).
    # Printed BEFORE bg_session and other startup chatter so the harness
    # can read it deterministically with a single readline() in the
    # KIROCREW_READY: prefix matcher.
    #
    # The dashboard/API factory synchronously published the memory task at
    # its ready=True boundary. No suspension has followed its return, so the
    # restore/open worker still cannot run before this process-ready marker.
    if self._json_ready:
        ready_token = generate_token(
            self._owner_id or "local-startup", ttl_seconds=MAX_SESSION_TTL_SECS
        )
        ready_payload = {
            "port": self._dashboard_port,
            "token": ready_token,
            "pid": os.getpid(),
            "home": str(data_home()),
        }
        print(f"KIROCREW_READY:{json.dumps(ready_payload)}", flush=True)

    self._session_environments_ready = True
    if not self._test_mode:
        _start_coder_retention(live.snapshot() or self._cfg)

    # The HTTP socket is bound, and the optional machine-readable marker
    # has been emitted. Background approval-state writes may start now.
    approval_ready = getattr(self, "_mcp_launch_approval_ready", None)
    if approval_ready is not None:
        approval_ready.set()

    # Protect the running tree before any update work can prune it, but
    # keep filesystem work off both the readiness path and the event loop.
    try:
        from kiro_crew.platform.tree_liveness import hold_running_tree_lock

        await asyncio.to_thread(hold_running_tree_lock)
    except Exception:
        logger.debug("Gateway tree liveness hold skipped", exc_info=True)

    self._install_shutdown_signal_handlers()

    # Warn when this gateway's code is a git work tree someone could rebase
    # under it. Past KIROCREW_READY (no-new-work-on-gateway-boot-path),
    # off-loop, fire-and-forget: it only logs.
    if not self._test_mode:
        from kiro_crew.live_checkout import warn_if_running_from_git_checkout

        _checkout_task = asyncio.create_task(asyncio.to_thread(warn_if_running_from_git_checkout))
        self._background_tasks.add(_checkout_task)
        _checkout_task.add_done_callback(self._background_tasks.discard)

    # Disable and mark pending any app whose sessionApproval grant was never
    # consented to. Past KIROCREW_READY (no-new-work-on-gateway-boot-path):
    # the grant is refused on read already, so readiness need not wait.
    # Fire-and-forget, file work off-loop, per-app lifecycle lock, never raises.
    if not self._test_mode:
        from kiro_crew.apps.session_consent import reconcile_after_ready

        _consent_task = asyncio.create_task(reconcile_after_ready())
        self._background_tasks.add(_consent_task)
        _consent_task.add_done_callback(self._background_tasks.discard)

    # The run directories a PREDECESSOR gateway of this data home left behind
    # (session_work_dir). Ordered after ``cleanup_orphaned_sessions`` above,
    # which reaped this home's pid ledger -- the ledger's remaining entries
    # are what the sweep keeps -- and before any session writer below can
    # create a directory. Past KIROCREW_READY so readiness does not wait for
    # it (no-new-work-on-gateway-boot-path); off-loop, bounded by entries and
    # wall clock, fail-open, and skipped in test_mode like the hourly wake.
    # Nothing this process has marked is ever its business, so the ordering
    # is for determinism, not safety.
    if not self._test_mode:
        try:
            await asyncio.to_thread(
                _sweep_predecessor_session_work_dirs,
                _live_session_work_dirs(self.sessions),
            )
        except Exception:
            logger.debug("boot session work-dir sweep failed", exc_info=True)

    # TaskRunner + workflow agent calls join the durable task queue and the
    # runner lane now that both consumers exist (the WorkflowService is
    # built by the dashboard server). AFTER the READY print, not before it:
    # the coordinator's first build runs `rebuild()` over every waiting row,
    # and `test_memory_startup` pins that no such work precedes readiness.
    # Nothing dispatches in between -- the subagent pump is held closed
    # (``defer_queue_dispatch``) and the dashboard workers and cron start
    # further down, after the memory barrier.
    await self._ensure_subagent_coordinator()
    self._wire_runner_admission()

    if not await self._wait_for_memory_preparation():
        await self._shutdown_and_exit()
        return
    # The startup crewmate prune judges each sync-generated crewmate from
    # the session history it can see; every writer below (subagent pump,
    # channel agent resume, cron) can bind a crewmate to a NEW session, so
    # none may start until the pass has RETURNED. Past KIROCREW_READY, so
    # readiness does not wait. A pass that outlives its budget is told to
    # stop deleting and is still waited for; it always returns (bounded
    # locks, non-blocking opens), so this cannot hold the gateway for good.
    if self.dashboard_state is not None:
        from kiro_crew.dashboard.server import await_crewmate_prune_settled

        await await_crewmate_prune_settled(
            self.dashboard_state, before="the memory-backed session writers"
        )
    if self.subagent_mgr is not None:
        await self.subagent_mgr.wait_taskq_ready()
        # The store exists now; bind the coordinator and the adoption sweep
        # the socket-bind pass could not see. Before the dashboard workers
        # and cron start, so the sweep never races live runner work.
        await self._runner_admission_store_ready()
        # Also off-loop for the health wiring the controller start does: the
        # pass above binds no coordinator when it wired no admission.
        await self._ensure_subagent_coordinator()
        # Memory is prepared and the store is bound: the durable rows that
        # survived the restart may start now.
        await self._start_subagent_dispatch_after_memory_ready()
        self._start_adaptive_controller()
        # The manager exists and readiness is past, so the session ledger's
        # repair can be given its child-liveness probe. Before the dashboard
        # workers and cron start, so no session can open a ledger and repair
        # it while the probe is missing -- which would let the repair close a
        # child that is still running.
        self._register_child_liveness()
    # Past the store-ready pass: with no adoption sweep armed, none will
    # decide the runs a restart recovered, so fair admission stops holding them.
    if self.task_runner is not None and not self._runner_admission_adopted:
        self.task_runner.end_recovery_wait()

    # Persisted Crew work and legacy channel agents can dispatch providers
    # immediately when resumed, so start them only after the shared memory
    # barrier. The dashboard control shell existed throughout preparation.
    if not self._no_dashboard:
        self._start_dashboard_workers_after_memory_ready()

    # These services can run memory-backed work as soon as they start.
    # Arm them only after preparation; the dashboard socket remains bound
    # throughout recovery so status and owner controls stay available.
    await self._start_cron_after_memory_ready()
    await self._init_heartbeat()

    self._start_memory_after_ready()

    # ── Central governance-policy refresh ──
    # Started HERE, after readiness, not on the boot path: the
    # no-new-work-on-gateway-boot-path rule applies, and nothing about this
    # loop needs to exist before the gateway can serve. Boot has already
    # established the ceiling from the same source (the load tier does that),
    # so this only keeps it current.
    #
    # A detached daemon thread, NOT awaited, for the reason the beacon is: the
    # fetch is blocking urllib and must never sit on the event loop. It is a
    # no-op unless a policy or the environment names a source AND an interval,
    # and it waits one full interval before its first poll, so a fleet
    # restarting together does not stampede the admin's endpoint.
    #
    # This is what makes an admin's push land on a running fleet: a changed
    # document is validated through the same floor gates boot applies and then
    # installed in place. One that fails them is refused and the running
    # ceiling is kept, so a bad push cannot take down hosts already up.
    #
    # ``_test_mode`` skips it so the offline E2E gate never makes an outbound
    # request.
    if not self._test_mode:
        # A governance profile is saved by editing a file under the profiles
        # directory, which no distribution poll observes, and a host with no
        # distribution source runs no poll at all. This watch re-stats that
        # directory off the loop and re-derives the on-disk auto-approvals when the
        # combined ceiling + profile answer has moved; on every other tick it is a
        # directory stat and an integer compare. Sleeps first, so boot does no extra
        # work, and starts in its own block, so a failure seeding the baseline or
        # starting the refresher below does not take the watch down with it.
        try:
            from kiro_crew.agent import reproject_for_profile_change

            async def _watch_profile_projection() -> None:
                while True:
                    await asyncio.sleep(_PROFILE_PROJECTION_POLL_S)
                    try:
                        await asyncio.to_thread(reproject_for_profile_change)
                    except Exception:
                        logger.warning(
                            "governance profile re-projection failed; the profile "
                            "watch retries it after its raise backoff",
                            exc_info=True,
                        )

            self._profile_watch_task = asyncio.create_task(
                _watch_profile_projection(), name="governance-profile-projection"
            )
        except Exception:
            logger.warning("the governance profile watch did not start", exc_info=True)
        with contextlib.suppress(Exception):
            from kiro_crew.agent import (
                prime_ceiling_projection,
                reproject_for_ceiling_change,
            )
            from kiro_crew.dashboard.tailnet_serve import (
                revoke_if_governance_now_pins_off,
            )
            from kiro_crew.platform.policy_distribution import (
                register_post_install_hook,
                start_refresher,
            )

            # Hooks are registered BEFORE the poller starts, so the first installed
            # ceiling already re-derives what was materialised from the previous one.
            # Most governed controls are live evaluations and need nothing here. These two
            # are the exceptions: a published tailnet origin, whose gate fires when
            # publish is CALLED and so does not retract what is already serving, and the
            # agent config's ``allowedTools``, which kiro-cli reads from the FILE — so a
            # list written under a looser ceiling keeps auto-approving what the fleet has
            # since forbidden.
            _tailnet_port = self._dashboard_port
            register_post_install_hook(lambda: revoke_if_governance_now_pins_off(_tailnet_port))
            # Seeded BEFORE the poller starts: the first poll can itself install a new
            # ceiling, and a baseline taken on the hook's first call would record that
            # generation and skip the rebuild it needed.
            await asyncio.to_thread(prime_ceiling_projection)
            register_post_install_hook(reproject_for_ceiling_change)

            await asyncio.to_thread(start_refresher)

    # ── Hosted feature-video clips ──
    # Started HERE, after readiness, for the same reason the refresher above is:
    # the no-new-work-on-gateway-boot-path rule. Nothing downstream needs a clip
    # before the gateway can serve.
    #
    # The import sits INSIDE the flag test so that the TRANSFER's startup is
    # deferred behind the switch: with the feature off, no task is scheduled,
    # no config beyond the flag is read, and no manifest is looked for. The
    # modules themselves are already loaded — the dashboard imports both at
    # route setup — so this is about work, not import cost.
    #
    # The kill switch is read here because it decides whether this subsystem
    # exists for this process. The ceiling and the manifest stay inside the
    # task, where blocking work is allowed.
    if self._cfg.dashboard.feature_videos_enabled:
        from kiro_crew.feature_videos_cache import (
            start_background_feature_video_download,
        )

        self._feature_video_task = start_background_feature_video_download()

    # Claim the install-scoped telemetry reporter role for this process.
    # Install-level inventory (crons, skills, knowledge, config toggles) is the
    # same for every process in the install, so if each telemetry-enabled
    # process published it -- the gateway, gatewayd, spawned agents -- an
    # aggregate over installs would count this install once per process. This
    # is the gateway, so it is the one that publishes.
    #
    # It lives in run() itself, unconditionally: the claim belongs to being the
    # gateway, not to any single service, and a feature-flagged host would take
    # the whole subsystem down with it. _init_autonudge() below returns early
    # under KIROCREW_AUTONUDGE=0, so a claim made in there means no process ever
    # publishes inventory -- and nothing says so, because the gauge callbacks
    # bail before probing and leave probe.failures empty, which reads exactly
    # like a host that stopped exporting. test_reporter_claim_is_unconditional
    # pins the call site. Best-effort: telemetry is never a boot blocker.
    try:
        from kiro_crew.metrics.inventory_gauges import mark_install_reporter

        mark_install_reporter()
    except Exception:  # noqa: BLE001 -- telemetry must never break gateway boot
        logger.debug("could not claim the telemetry inventory role", exc_info=True)

    # AutoNudge must run after dashboard init — _fire callback dereferences
    # self.dashboard_state. In --no-dashboard mode the guard inside _fire
    # early-returns so persisted loops are harmless until a dashboard
    # process takes over.
    await self._init_autonudge()

    # Per-member event-log startup reconcile. Runs AFTER AutoNudge is
    # constructed (it consults live loops to decide patrol closers) and
    # after slot restoration: member slots are rehydrated lazily on demand
    # rather than eagerly at boot, so ``state._slots`` here holds whatever
    # the dashboard restored, and any driving.open slot not present is
    # closed as interrupted. Off-loop (ensure/append are synchronous file
    # IO) and best-effort — the helper swallows its own failures so a
    # logging fault never blocks boot.
    if self.dashboard_state is not None:
        from kiro_crew import eventlog_hooks

        async def _reconcile_members() -> None:
            # Off-loop (ensure/append are synchronous file IO, and first-boot
            # migration fsyncs per record) and best-effort. Run as a background
            # task rather than an awaited boot step: it must not delay Slack
            # availability or socket connect, and it is idempotent on reboot.
            try:
                await asyncio.to_thread(
                    eventlog_hooks.reconcile_members_at_startup,
                    self._cfg,
                    self.dashboard_state,
                    self.autonudge_svc,
                )
            except Exception:
                logger.debug("member event-log startup reconcile failed", exc_info=True)

        # Retain a strong reference: a bare create_task is only weakly held,
        # so the loop could garbage-collect it mid-run.
        self._member_reconcile_task = asyncio.create_task(_reconcile_members())

    # Wire up event routing and interactive handlers
    init_interactions(self)
    # Awaited ON the loop, never offloaded whole: WSSocketModeClient's
    # __init__ ends in ``asyncio.ensure_future``, which needs a current
    # event loop in the *constructing* thread — a ``to_thread`` worker has
    # none, so offloading the whole function to keep its blocking YOLO-grant
    # profiles walk off the loop crashes every Slack-enabled boot with
    # "There is no current event loop".  The two
    # blocking calls inside (the YOLO grant and the enterprise auth.test)
    # are offloaded individually within the coroutine instead, preserving
    # the security-relevant early-return ordering.  Pinned by
    # test_slack_events_coverage.py::TestInitSocketMode.
    await init_socket_mode(self, seen)

    await self._start_channel_transports()

    # Update coordinator — fire-and-forget, NOT awaited. The first cycle runs
    # immediately; later cycles own both checking and automatic application.
    # Registered so shutdown can cancel a stalled network or installer child.
    print("👻 Checking for updates…")
    self._update_check_task = asyncio.create_task(self._run_update_checks())
    # Named so the shutdown's did-not-stop warning can say which update task it was.
    self._update_check_task.set_name("update-coordinator")
    self._background_tasks.add(self._update_check_task)
    self._update_check_task.add_done_callback(self._background_tasks.discard)

    # ── Announce the dashboard URL — deliberately NOT behind the probe ──
    # The HTTP port is already listening (bound by _init_dashboard above), and
    # nothing about building, formatting or printing a URL depends on MCP
    # state. The ordering constraint documented below covers ONLY session
    # spawn. Printing here instead of after the probe removes up to
    # ~mcp_probe_timeout_secs+15 of "no URL on screen" from every boot, and
    # all of it from the timed-out path.
    dashboard_url = ""
    if not self._no_dashboard:
        try:
            host = resolve_dashboard_host(self._local_only, self._configured_host)
            _cfg_url = self._cfg.dashboard.url
            if _cfg_url and "://" in _cfg_url:
                base_url = _cfg_url.rstrip("/")
            else:
                base_url = f"http://{host}:{self._dashboard_port}"
            startup_token = generate_token(
                self._owner_id or "local-startup", ttl_seconds=MAX_SESSION_TTL_SECS
            )
            dashboard_url = build_dashboard_url(
                base_url, startup_token, local_only=self._local_only
            )
            # Off the loop: the Remote hint resolves the host's own name, and
            # a name the resolver cannot answer (every hosted macOS runner)
            # would otherwise sit on the loop for the lookup's bound.
            url_lines = await asyncio.to_thread(
                format_dashboard_urls,
                dashboard_url,
                port=self._dashboard_port,
                local_only=self._local_only,
                has_custom_host=bool(self._configured_host),
            )
            for line in url_lines:
                print(line)

            # Auto-open dashboard — skip on headless remote sessions
            _is_ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))
            _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            _skip_open = _is_ssh and not _has_display and sys.platform != "darwin"
            if self._no_open or not self._cfg.dashboard.auto_open_browser:
                pass  # suppressed via --no-open flag or config
            elif _skip_open:
                print("👻 Headless remote session — skipping browser auto-open")
            else:
                # Runs as a task so a slow browser launch overlaps the MCP
                # probe instead of delaying it. Tracked so it is not GC'd
                # mid-flight and is reaped on shutdown with the rest.
                _open_task = asyncio.create_task(self._auto_open_dashboard(dashboard_url))
                self._background_tasks.add(_open_task)
                _open_task.add_done_callback(self._background_tasks.discard)
        except Exception:
            # Announcing the URL is BEST EFFORT and must never abort boot.
            # It sits ON the boot path, where an escaping failure WOULD take
            # the gateway down, so every failure is contained here rather than
            # propagated. The dashboard is already listening either way — the
            # operator loses a printed line, not
            # the service, and `kirocrew token` still produces a URL.
            logger.warning("Dashboard URL announcement failed", exc_info=True)

    # Wait for MCP probe to finish before warming sessions —
    # kiro-cli reads MCP config at spawn time, so sessions must
    # start AFTER the probe has synced all servers to mcp.json.
    from kiro_crew.dashboard.handlers import _bg_mcp_probe

    print("👻 Probing MCP servers…")
    # self._cfg is the config this boot already loaded — re-reading it here
    # would pay a deepcopy plus a full nested-dataclass rebuild for one scalar.
    _probe_t = self._cfg.dashboard.mcp_probe_timeout_secs + 15
    try:
        await asyncio.wait_for(_bg_mcp_probe(self.sessions), timeout=_probe_t)
    except asyncio.TimeoutError:
        print("👻 MCP probe timed out — continuing without full probe")

    # ── Start background session (this IS gated on the probe) ──
    async def _start_bg_session() -> None:
        try:
            assert self.sessions is not None
            await self.sessions.start_pool(blocking=False)
            logger.info("Background session starting")
        except Exception:
            logger.warning("Background session start failed", exc_info=True)

    _bg_session_task = asyncio.create_task(_start_bg_session())
    self._background_tasks.add(_bg_session_task)
    _bg_session_task.add_done_callback(self._background_tasks.discard)

    # Stale-asset watchdog: detects when an update prunes the running
    # install's static assets and triggers graceful shutdown so the
    # supervisor can restart a fresh process. It first drains in-flight
    # backend turns (count_in_flight) so active work isn't killed
    # mid-prompt by the restart, and stays up when its supervisor could
    # not relaunch it (supervisor_reentry).
    _watchdog = asyncio.create_task(
        run_stale_asset_watchdog(
            shutdown_event,
            count_in_flight=self._count_in_flight_work,
            reentry_check=supervisor_reentry,
        )
    )
    self._background_tasks.add(_watchdog)
    _watchdog.add_done_callback(self._background_tasks.discard)

    # Persistence watchdog: the boot preflight refuses to start a gateway
    # whose data home cannot create/lock/replace/remove files, but it runs
    # once. A seccomp filter inherited from a sandboxed parent passes that
    # probe at boot (the sandbox is still alive) and then breaks every file
    # open with ENOSYS when the sandbox is torn down minutes later — at
    # which point every static asset 404s silently, file logging dies, and
    # the process never exits. This re-runs the SAME probe periodically and,
    # on a re-confirmed restart-curable (ENOSYS) failure, exits non-zero so
    # a supervisor restarts a fresh process with no inherited filter.
    _persistence_watchdog = asyncio.create_task(
        run_persistence_watchdog(
            shutdown_event,
            probe_data_home(data_home()),
        )
    )
    self._background_tasks.add(_persistence_watchdog)
    _persistence_watchdog.add_done_callback(self._background_tasks.discard)
    self._persistence_watchdog_task = _persistence_watchdog

    # Renew the stored Kiro sign-in before it expires, not only when an
    # agent turn asks for a token. Loads the auth subsystem only
    # once a sign-in vault exists; see dashboard/sign_in_refresher.
    _sign_in_refresher = asyncio.create_task(run_sign_in_refresher(shutdown_event))
    self._sign_in_refresher_task = _sign_in_refresher
    self._background_tasks.add(_sign_in_refresher)
    _sign_in_refresher.add_done_callback(self._background_tasks.discard)
    # Bundle-freshness guard: a one-shot WARN (never a shutdown) if the
    # served SPA dist was built from a different commit than this backend —
    # the present-but-stale case the vanish watchdog above cannot catch. A
    # restart would re-serve the same stale dist, so warning (not shutting
    # down) is the correct response. Best-effort; never raises. Run on the
    # subprocess executor: the dev-checkout fallback shells out to
    # `git rev-parse` (up to 5s if git wedges), which must not block the
    # event loop.
    await asyncio.get_running_loop().run_in_executor(subprocess_executor(), check_bundle_freshness)

    connected = await self._connect_slack()
    # Record the real socket outcome so status surfaces (e.g. the Slack
    # settings badge) can distinguish "connected" from "tokens present
    # but connect failed" — slack_client alone only proves the latter.
    if self.dashboard_state:
        self.dashboard_state.slack_socket_connected = connected
        self.dashboard_state.slack_connect_error = getattr(self, "_slack_connect_error", "")

    # Deferred tracked-channel capability probe (fire-and-forget, never
    # awaited — boot latency is unaffected). A Slack install created before
    # the manifest gained groups:history keeps its old grant, so a tracked
    # private channel delivers no events and nothing logs; the probe turns
    # that silent-dead state into a warning + dashboard notification.
    if connected and self.slack is not None and self._tracking_channels:
        _scope_task = asyncio.create_task(
            warn_unreadable_tracked_channels(
                self.slack,
                set(self._tracking_channels),
                notify=self.dashboard_state.notify if self.dashboard_state else None,
            )
        )
        self._background_tasks.add(_scope_task)
        _scope_task.add_done_callback(self._background_tasks.discard)
        _scope_task.add_done_callback(log_probe_failure)

    # Boot is finished here: the dashboard is bound and the process serves until
    # it is stopped. This is the last line a foreground gateway prints, so it
    # must not read as a step still in progress.
    print("👻 Kiro Crew gateway running — press Ctrl+C to stop")
    await shutdown_event.wait()
    await self._shutdown_and_exit(_watchdog)


# How often the gateway re-stats the governance profiles directory and re-derives
# the on-disk auto-approvals when a saved profile has moved the governance answer.
# Bounds how long a profile that withdraws an auto-approved tool can be bypassed by
# a newly started session; an unchanged tick costs a directory stat.
_PROFILE_PROJECTION_POLL_S = 15.0

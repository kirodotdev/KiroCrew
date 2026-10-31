"""What a sub-agent's turn does on its session before the stream starts.

``run_turn._run_inner_impl`` calls these in order: the tool-approval policy the run
inherits (``resolve_parent_policy``), the session it runs on, shared or dedicated
(``acquire_session``), the cleanup identity of that session (``capture_cleanup_identity``),
the child's memory, crew activity and folder steering (``prepare_child_context``), the
records and the spawn event a restart and the panel read (``persist_spawn_records``), and
the ``AgentSpawn`` and ``UserPromptSubmit`` hooks with their clock bookkeeping
(``fire_prompt_hooks``). Each takes the run's ``RunEventCoordinator`` as ``self``.

The turn body runs on ``kiro_crew.subagent``'s globals (``bind_component_globals``), and
tests patch names there (``sel``, ``time``, ``_redact``, the session re-projection
helpers). These functions read those names through ``_facade()`` at call time, so a patch
on the facade reaches them exactly as it reaches the turn body. ``turn_`` and not ``run_``:
every ``run_*.py`` sibling is a module ``RunEventCoordinator`` binds members from, and these
are plain functions the turn body calls.

The manager fields this module reads (it writes none): ``_await_late_start``,
``_create_shared_session``, ``_ctx_builder``, ``_ensure_dedicated_start_priced``,
``_fire_event``, ``_gate_exit_reset``, ``_gate_wait_mark``, ``_global_approval_mode``,
``_is_cc_provider``, ``_is_yolo``, ``_note_startup_progress``, ``_parent_steering_dirs``,
``_provider_label_of``, ``_sessions``, ``_write_state_off_loop``, ``hook_store``.
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..subagent import SubagentInfo
    from .run import RunEventCoordinator


def _facade() -> ModuleType:
    """``kiro_crew.subagent``, read from ``sys.modules`` when called: the patch target."""
    return sys.modules["kiro_crew.subagent"]


def resolve_parent_policy(self: RunEventCoordinator, info: SubagentInfo) -> str:
    """The approval policy the child's tool calls inherit: the parent session's, else
    ``auto`` from the spawn's own ``approval_mode``, yolo, the global config (only with no
    live parent) or ``auto_approve_subagent_tools``, each fallback audited."""
    _sa = _facade()
    parent_policy = self._manager._sessions.get_approval_policy(info.parent_session_key)
    # Explicit approval_mode from spawn caller (e.g. Mochi bg agent)
    if not parent_policy and info.approval_mode == "auto":
        parent_policy = "auto"
        _sa.sel().log_api_access(
            caller=info.parent_session_key or f"subagent:{info.id}",
            operation="subagent.approval_mode_auto_policy",
            outcome="ok",
            source="subagent",
            resources=f"subagent_id={info.id}",
        )
    if not parent_policy and self._manager._is_yolo and self._manager._is_yolo():
        parent_policy = "auto"
        _sa.sel().log_api_access(
            caller=info.parent_session_key,
            operation="subagent.yolo_policy_fallback",
            outcome="ok",
            source="subagent",
            resources=f"subagent_id={info.id}",
        )
    if not parent_policy and self._manager._global_approval_mode == "auto":
        # Apply global config as fallback only when parent is absent or
        # confirmed garbage-collected (absent from the session store).
        # If parent session still exists but returned no policy, deny by
        # default — the session is alive and intentionally non-auto.
        if not info.parent_session_key:
            _parent_gone = True  # no_parent
        elif self._manager._sessions.has_session(info.parent_session_key) is False:
            _parent_gone = True  # parent_gc
        else:
            _parent_gone = False  # parent alive or store error → deny
            _sa.sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.config_policy_fallback",
                outcome="denied",
                source="subagent",
                resources=f"subagent_id={info.id},reason=parent_alive_or_store_error",
            )
        if _parent_gone:
            parent_policy = "auto"
            _reason = "parent_gc" if info.parent_session_key else "no_parent"
            _sa.sel().log_api_access(
                caller=f"subagent:{info.id}",
                operation="subagent.config_policy_fallback",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id},reason={_reason}",
            )
    # auto_approve_subagent_tools auto-approves tool calls inside
    # subagents (separate from the spawn gate, deny-by-default).
    if not parent_policy and self._manager._ctx_builder and self._manager._ctx_builder.hooks:
        if self._manager._ctx_builder.hooks.auto_approve_subagent_tools is True:
            parent_policy = "auto"
            _sa.sel().log_api_access(
                caller=info.parent_session_key or f"subagent:{info.id}",
                operation="subagent.auto_approve_subagent_tools_policy",
                outcome="ok",
                source="subagent",
                resources=f"subagent_id={info.id}",
            )
    return parent_policy


async def acquire_session(
    self: RunEventCoordinator,
    info: SubagentInfo,
    session_key: str,
    agent: str,
    *,
    parent_policy: str,
    extra_kwargs: dict[str, Any],
    use_session_sharing: bool,
) -> tuple[Any, bool, bool, bool, bool]:
    """Start the run's session on the parent's shared runtime or a dedicated process.

    Returns ``(client, is_new, resumed, is_cc, use_session_sharing)``; the last is False
    when the shared runtime was unavailable and the run fell back to a dedicated process.
    """
    _sa = _facade()
    if use_session_sharing:
        from kiro_crew.agent_sdk.drivers.acp_vocab import AcpRequestTimeout as _StartTimeout

        try:
            client = await self._manager._create_shared_session(info, session_key, agent)
        except _StartTimeout as exc:
            # CONGESTION is never a reason for a dedicated process (RFC
            # §4.4): a timed-out session/new may still create its session,
            # and a second runtime here would double the load that caused
            # the timeout. The StartCollector attached to the exception
            # owns the outstanding request; wait for its verdict and either
            # continue on the adopted session or end this attempt.
            client = await self._manager._await_late_start(info, session_key, exc)
            is_new = True
            _resumed = False
            is_cc = False
        except Exception as exc:
            # The shared runtime itself is unavailable (dead, spawn failed):
            # not congestion. The failure is counted on the ladder's L3
            # rung for the parent's runtime (two inside the cooldown
            # escalate to L4, one notice); the dedicated process stays the
            # per-run recovery because the shared runtime's rebuild belongs
            # to the session that owns it, not to a child run.
            from kiro_crew.recovery.ladder import L3_ACP_RUNTIME, default_ladder

            try:
                default_ladder().observe_failure(
                    L3_ACP_RUNTIME,
                    f"runtime:{info.parent_session_key or 'companion'}",
                    reason=f"shared runtime unavailable: {exc}"[:200],
                    task_id=info.id,
                )
            except Exception:
                _sa.logger.debug("ladder L3 observe_failure failed", exc_info=True)
            _sa.logger.warning(
                "Subagent %s: shared runtime unavailable (%s), using a dedicated process",
                info.id,
                exc,
            )
            info._session_sharing = False
            info._shared_provider = None
            use_session_sharing = False
            # Admitted at the shared price; the process this launches was
            # never reserved. Top up (and re-check the floor) first.
            await self._manager._ensure_dedicated_start_priced(info)

            def _fallback_claim():
                return self._manager._sessions.get_or_create(
                    session_key,
                    agent=agent or None,
                    approval_policy=parent_policy,
                    on_gate_acquired=self._manager._gate_exit_reset(info),
                    on_gate_queued=self._manager._gate_wait_mark(info),
                    **extra_kwargs,
                )

            # Same claim-time re-check as the dedicated arm below.
            client, is_new, _resumed = await _sa.reproject_claimed_session(
                self._manager._sessions,
                session_key,
                agent,
                await _fallback_claim(),
                _fallback_claim,
            )
            is_cc = self._manager._is_cc_provider(client)
        else:
            is_new = True
            _resumed = False
            is_cc = False
        if use_session_sharing:
            # A shared session's batch is built during its session/new; a
            # PreToolUse hook added meanwhile is not in it, and nothing else
            # re-checks a session this run owns outright. Replaced if stale.
            client = await _sa.replace_stale_shared_session(
                client,
                agent,
                lambda: self._manager._create_shared_session(info, session_key, agent),
            )
    else:
        # The dedicated process's ``session/new`` runs under the same
        # ``SessionStartGate`` as a shared session's; hand it the same
        # gate clock callbacks (``_gate_wait_mark`` at entry,
        # ``_gate_exit_reset`` at exit), threaded through the provider
        # factory to ``AcpProvider``.
        #
        # A kept KAS session whose registered batch auto-approves what a
        # PreToolUse hook now covers is reset first, so the claim re-projects.
        await _sa.invalidate_stale_kas_session(self._manager._sessions, session_key, agent)
        # Admission may have priced this start shared and the decision
        # turned dedicated since (parent gone, sharing off, a role pin set
        # while it waited): reserve the process before launching it.
        await self._manager._ensure_dedicated_start_priced(info)
        # This arm owns its process: a flag an earlier shared attempt of the
        # run left (a cancel-recovery respawn) would hand this process's
        # teardown to the shared arm. The old attempt's teardown, which read
        # it, has finished before this one runs.
        info._session_sharing = False
        info._shared_provider = None

        def _claim():
            return self._manager._sessions.get_or_create(
                session_key,
                agent=agent or None,
                approval_policy=parent_policy,
                on_gate_acquired=self._manager._gate_exit_reset(info),
                on_gate_queued=self._manager._gate_wait_mark(info),
                **extra_kwargs,
            )

        # Decided again under the lease: the pre-claim reset is declined for a
        # session another turn holds, and this claim may have waited for it.
        client, is_new, _resumed = await _sa.reproject_claimed_session(
            self._manager._sessions, session_key, agent, await _claim(), _claim
        )
        is_cc = self._manager._is_cc_provider(client)
    return client, is_new, _resumed, is_cc, use_session_sharing


def capture_cleanup_identity(
    self: RunEventCoordinator, info: SubagentInfo, session_key: str, client: Any, *, is_cc: bool
) -> None:
    """Record the acquired session's cleanup identity on ``info`` and publish it live."""
    _sa = _facade()
    # Capture cleanup identity immediately after successful session
    # acquisition. Every later step can fail and tombstone the run, so
    # delaying this until the state write leaves provider files unidentified.
    try:
        cleanup_session_id = str(client.session_id or "") if hasattr(client, "session_id") else ""
        cleanup_provider = self._manager._provider_label_of(client)
        # Continuations use this record to recover their project on every
        # backend, even when continuing a follow-up after gateway restart.
        provider_cwd = getattr(client, "cwd", "")
        cleanup_cwd = provider_cwd if isinstance(provider_cwd, str) and provider_cwd else info.cwd
        if not cleanup_cwd and is_cc:
            inner = getattr(client, "client", None)
            work_dir = getattr(inner, "_work_dir", None)
            if work_dir:
                cleanup_cwd = str(work_dir)
        setattr(info, "_session_id", cleanup_session_id)
        setattr(info, "_session_provider", cleanup_provider)
        setattr(info, "_session_cwd", cleanup_cwd)
        self._publish_identity(
            info.id,
            session_id=cleanup_session_id,
            provider=cleanup_provider,
            cwd=cleanup_cwd,
            keep=info.keep,
            conversation_key=session_key if info.keep else "",
        )
    except Exception:
        _sa.logger.debug("Failed to capture live cleanup identity for %s", info.id, exc_info=True)


async def prepare_child_context(
    self: RunEventCoordinator,
    info: SubagentInfo,
    session_key: str,
    execution: Any,
    *,
    own_worker: bool,
) -> str | None:
    """Prepare the child's memory store, record crew activity and read the parent's folder
    steering into ``info.steering_dirs``; return the project the prompt is scoped to."""
    _sa = _facade()
    # The child's own memory silo. Without it every subagent reads the
    # operator's global store however the parent crew is bound, which makes
    # a crew's isolation end at the moment it delegates.
    #
    # Prepare before the offloaded build because vector initialization is
    # blocking file IO. A private store that cannot be prepared refuses the
    # turn; it cannot continue with Global memory.
    from kiro_crew.context import prepare_store_vectors
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    if info.memory_mode != "temporary":
        try:
            await prepare_store_vectors(
                self._manager._ctx_builder, info.memory_store, session_key=session_key
            )
        except MemoryStartupUnavailable:
            # Not an optional-recall miss: the gateway's memory fence is
            # closed (still preparing, stopped, or this store's restore
            # failed). A chat turn is refused at admission in that state;
            # a run admitted here would execute with its memory silently
            # absent, so it fails with the fence's own reason instead.
            raise
        except (OSError, ValueError, RuntimeError):
            # Learned recall is optional for prompt construction. Explicit
            # memory tools still report the unavailable captured store.
            _sa.logger.debug("Subagent learned memory is unavailable", exc_info=True)
    from kiro_crew.session_work_dir import is_subagent_work_dir

    # A continuation of a run spawned without a cwd restores that run's
    # per-run scratch directory as info.cwd; it is still not a project.
    _is_scratch = bool(info.cwd) and await asyncio.to_thread(is_subagent_work_dir, info.cwd)
    _project: str | None = None if _is_scratch else (info.cwd or None)
    if info.crew:
        from kiro_crew.members import record_activity

        activity_member = execution.selection_name
        if own_worker:
            # A worker record names the worker template, not the crewmate; the
            # crewmate is the one its member id names (the alias it has now).
            from kiro_crew.config.loader import KiroCrewConfig as _Config
            from kiro_crew.execution_context import member_config_for_id

            _cfg = await asyncio.to_thread(_Config.load)
            try:
                activity_member, _ = member_config_for_id(_cfg, execution.member_id or "")
            except ValueError:
                activity_member = info.crew

        # A respawn of this run re-enters with the same key; the dedupe keeps one entry.
        await asyncio.to_thread(
            record_activity,
            activity_member,
            session_key,
            execution.memory_mode,
            project=_project or "",
            via="spawn",
            dedupe_session=True,
        )
    # The parent's folder steering, so a child spawned from a chat filed in a
    # folder follows the same guides its parent does. build_message gates the
    # section on the project group, so a run that withheld it skips the read.
    info.steering_dirs = ()
    steering_resolver = self._manager._parent_steering_dirs
    if steering_resolver is not None and info.include_project and info.parent_session_key:
        try:
            info.steering_dirs = tuple(await steering_resolver(info.parent_session_key))
        except Exception:  # noqa: BLE001 - steering is optional; it must never fail the run
            _sa.logger.warning(
                "Folder steering unavailable for subagent %s", info.id, exc_info=True
            )
    return _project


async def persist_spawn_records(
    self: RunEventCoordinator, info: SubagentInfo, session_key: str, agent: str, client: Any
) -> None:
    """Persist the run's model provenance, cleanup identity, PID and session record, and
    fire ``subagent_spawn``, in the order a gateway restart needs them on disk."""
    _sa = _facade()
    # Reports inherited agent (not just info.agent) so telemetry shows
    # the actual agent used for this subagent session.
    #
    # Read back the model the live session actually resolved to serve, so
    # the panel shows what ran rather than only what was requested.
    # Best-effort at spawn: the ACP session/new response already
    # carries the served id (readable now, even on the backend default),
    # while the raw CC path only knows it after the first turn — so this is
    # refreshed authoritatively at completion in ``run_turn``. Only overwrite a prior
    # non-empty value with another non-empty one, so a spawn-time read that
    # succeeded is never clobbered back to "" by a transient later miss.
    _spawn_model = _sa._resolved_model_of(client)
    if _spawn_model:
        info.resolved_model = _spawn_model
    # Persist provenance to disk BEFORE the spawn event so a gateway restart
    # in the window between the event and the later session_id state write
    # cannot lose it — orphan recovery reads these from disk. Off-loop and
    # drained on cancellation via _write_state_off_loop (see
    # that helper for why a detached worker is the hazard). Best-effort with
    # ONE bounded retry: this write is the SINGLE owner of these two fields
    # on the spawn path — the later session_id write does not
    # double as a fallback, so a transient failure gets its second chance
    # HERE rather than from a second writer downstream. update_state reports
    # a silently-skipped merge (unreadable state) as False, which counts as a
    # failure for the retry — only a REPORTED write ends the loop. A
    # persistence hiccup must still never block the spawn. A cancellation
    # ends the loop instead of retrying: `except Exception` does not catch
    # CancelledError, so the helper's post-drain re-raise propagates rather
    # than starting a second writer for the same fields.
    for _provenance_attempt in range(2):
        try:
            _wrote = await self._manager._write_state_off_loop(
                info,
                "model provenance",
                requested_model=info.requested_model,
                resolved_model=info.resolved_model,
                # What a continuation after a restart keeps: the caller's own
                # pin, not ``requested_model`` (which also holds a role pin or
                # a routed tier), and the effort.
                pinned_model=info.model,
                reasoning_effort=info.reasoning_effort,
            )
            if _wrote:
                break
            _sa.logger.debug("Provenance write skipped (unreadable state) for %s", info.id)
        except Exception:
            _sa.logger.debug("Failed to persist model provenance for %s", info.id, exc_info=True)
    # The live generation was published synchronously at acquisition so
    # terminal tombstones are cancellation-safe. Persist the sidecar only
    # after the drained provenance write: cancellation during provenance must
    # not prevent its required model fields from landing, and cancellation
    # here still leaves the live tombstone snapshot complete.
    try:
        await self._remember_identity_off_loop(
            info,
            session_id=str(getattr(info, "_session_id", "")),
            provider=str(getattr(info, "_session_provider", "")),
            cwd=str(getattr(info, "_session_cwd", "")),
            keep=info.keep,
            conversation_key=session_key if info.keep else "",
        )
    except Exception:
        _sa.logger.debug("Failed to persist cleanup identity for %s", info.id, exc_info=True)
    await self._manager._fire_event(
        "subagent_spawn",
        info,
        {
            "task": _sa._redact(info.task),
            "agent": agent or "",
            # Caller free text like ``task``, so redacted the same way.
            "label": _sa._redact(info.label),
            "model": info.resolved_model,
            # The requested pin is caller-supplied (spawn_run.model), so it
            # is redacted like every other free-text field on the frame -- an
            # unavailable/AKIA-shaped pin must never reach the dashboard
            # socket raw.
            "requested_model": _sa._redact(info.requested_model),
            # The sub-agent's own session key (see build_subagent_snapshot):
            # lets a client fetch this node's own context-trace.
            "child_session": info.conversation_key or f"subagent:{info.id}",
        },
    )
    # Stream results to disk for orchestrated chat.

    # Record PID for orphan recovery. Off-loop and drained on cancellation
    # via _write_state_off_loop: update_state ends in a
    # synchronous fsync, and the reaper, every chat turn and the heartbeat
    # share this loop. Off-loop also means the write TAKES update_state's
    # per-agent lock, which on-loop callers skip, so it cannot
    # interleave with another pool writer's read-merge-rewrite.
    try:
        pid = self._manager._sessions.get_pid(session_key)
        if pid:
            info._pid = pid  # make available for _write_tombstone
            # Out of startup: a queued spawn held by the in-startup bound
            # may start now (``_note_startup_progress``).
            self._manager._note_startup_progress(info)
            await self._manager._write_state_off_loop(
                info, "PID record", pid=pid, pid_recorded_at=_sa.time.time()
            )
    except Exception:
        _sa.logger.debug("Failed to record PID for %s", info.id, exc_info=True)

    # Persist the cleanup identity captured immediately after session
    # acquisition, together with mutable retention intent.
    try:
        state_update: dict[str, object] = {
            # Diagnostics only; continuation uses the protected agent.json.
            "agent": agent,
            "session_id": str(getattr(info, "_session_id", "")),
            "provider": str(getattr(info, "_session_provider", "")),
            # Model provenance (requested_model/resolved_model) is NOT
            # re-written here: the crash-safe write BEFORE the
            # subagent_spawn event above is the single owner of those two
            # fields on the spawn path, and a transient failure there is
            # handled by that write's own bounded retry.
            "keep": info.keep,
            "conversation_key": session_key if info.keep else "",
        }
        cleanup_cwd = str(getattr(info, "_session_cwd", ""))
        if cleanup_cwd:
            state_update["cwd"] = cleanup_cwd
        # Same off-loop, drained write as the PID record above.
        # This one also carries `keep`, the field the two remaining
        # on-loop writers (promote / release) contend for -- taking the
        # per-agent lock here is what orders it against any other pool
        # writer.
        #
        # This write is the SINGLE owner of `cwd` and `session_id` on the
        # spawn path (continuation reads `cwd` back through
        # recorded_cwd), so it gets the same ONE bounded retry as the
        # model-provenance write above: an exception or a reported skip
        # (False -- update_state could not read the state) gets a second
        # chance, and only a reported write ends the loop. CancelledError
        # is not an Exception, so a cancellation still ends the loop
        # instead of starting a second writer. A write that fails both
        # times is logged at WARNING, so a run whose record lacks `cwd` is
        # visible in the log.
        _session_record_written = False
        for _session_record_attempt in range(2):
            try:
                if await self._manager._write_state_off_loop(
                    info, "session record", **state_update
                ):
                    _session_record_written = True
                    break
                _sa.logger.debug("Session record write skipped (unreadable state) for %s", info.id)
            except Exception:
                _sa.logger.debug("Failed to record session_id for %s", info.id, exc_info=True)
        if not _session_record_written:
            _sa.logger.warning(
                "Session record (session_id/cwd) was not persisted for %s after 2 attempts",
                info.id,
            )
    except Exception:
        _sa.logger.debug("Failed to record session_id for %s", info.id, exc_info=True)


async def fire_prompt_hooks(
    self: RunEventCoordinator,
    info: SubagentInfo,
    session_key: str,
    *,
    task: str,
    fresh_session: bool,
    spec: Any,
) -> str:
    """Fire the child's prompt-start hooks and return the ``[Hook context]`` prefix."""
    _sa = _facade()
    from kiro_crew.subagent_manager.prompt_hooks import prompt_hook_prefix

    # Before the first stream, as a dashboard turn fires them before its own.
    # Hook time is not the runtime's silence: the reaper's startup, first-prompt
    # and stall checks skip the run while they run, the startup clock does not
    # count them, and the activity clock restarts when they end, on every exit.
    info._prompt_hooks_running = True
    _hooks_t0 = _sa.time.monotonic()
    try:
        _hook_prefix = await prompt_hook_prefix(
            self._manager.hook_store,
            info,
            session_key=session_key,
            task=task,
            fresh_session=fresh_session,
            spec_hooks=spec.hooks,
            spec_hooks_cwd=spec.cwd,
        )
    finally:
        info._prompt_hooks_secs += _sa.time.monotonic() - _hooks_t0
        info.last_activity = _sa.time.time()
        info._last_activity_mono = _sa.time.monotonic()
        info._prompt_hooks_running = False
    return _hook_prefix

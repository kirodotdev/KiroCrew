"""Task execution logic for the task runner.

Handles running individual tasks, retries, process crash recovery,
approval gates, self-review, test verification, and context compaction.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kiro_crew import git_coord, name_grant, platform_compat, shutdown_event
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, fire_tool_hooks, get_global_hook_store
from kiro_crew.llm_helpers import provider_last_turn_usage, stream_and_collect_json
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
)
from kiro_crew.safety_override import safety_override
from kiro_crew.sandbox import (
    create_subprocess_limited,
    drain_bounded,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.task_models import (
    MAX_RECOVERIES,
    MAX_RETRIES,
    SESSION_PREFIX,
    TEST_TIMEOUT,
    Project,
    Task,
    TaskStatus,
)
from kiro_crew.task_planner import group_parallel_tasks

_MID_STREAM_COMPACT_PCT = 90.0


class _ContextOverflow(Exception):
    """Raised when context usage exceeds threshold mid-stream."""


if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)


def _uses_generic_git_commit(run: Project) -> bool:
    """Keep per-task Git commits enabled only for ordinary Task Runner runs."""
    return bool(
        run.branch_name
        and run.execution_mode != "review_fix"
        and run.commit_policy != "manual_group"
    )


async def _pin_review_fix_model(run: Project, client) -> None:
    """Pin every review-fix turn to the concrete model captured at creation."""
    if run.execution_mode != "review_fix" or not run.review_fix:
        return
    model_id = run.review_fix.model.resolved_model_id
    if not model_id:
        raise RuntimeError("review-fix model resolution is incomplete")
    setter = getattr(client, "set_model", None)
    if not callable(setter):
        raise RuntimeError("review-fix provider cannot pin a model")
    await setter(model_id)


async def _check_error_loop(
    task: "Task",
    consecutive: int,
    on_notify,
    run: "Project",
) -> bool:
    """Return True if the task should fail due to a repeated-error loop."""
    if consecutive >= 2:
        task.error = f"Loop detected: same error {consecutive + 1} times"
        task.status = TaskStatus.FAILED
        return True
    if consecutive >= 1:
        await on_notify(
            "⚠️ Possible loop",
            f"Same error {consecutive + 1}x: {task.error[:100]}",
            run=run,
        )
    return False


async def execute_single_task(
    run: Project,
    task: Task,
    history_key: str,
    sessions: "SessionManager",
    ctx: "ContextBuilder | None",
    agent: str,
    on_notify: Callable,
    on_approval: Callable | None,
    on_tool_approval: Callable | None,
    auto_test: bool,
    test_cmd: list[str] | None,
    work_dir: Path,
    log_task_fn: Callable,
    extract_lesson_fn: Callable,
    session_key: str = "",
) -> bool:
    """Execute one task with approval gate, self-review, and memory update."""
    if not session_key:
        session_key = f"{SESSION_PREFIX}:{run.task_id}:task{task.index}"

    task.status = TaskStatus.IN_PROGRESS
    now = _time.time()
    if not task.created_at:
        task.created_at = now
    task.started_at = now

    # Approval gate
    if task.requires_approval or task.force_approval:
        run.last_task_time = _time.time()
        label = "🚧 GATE" if task.force_approval else "⏸️"
        safe_title, _ = redact_exfiltration_urls(task.title or "")
        safe_title, _ = redact_credentials(safe_title)
        await on_notify(
            f"{label} Task {task.index} requires approval",
            safe_title,
            run=run,
        )
        if on_approval:
            approved = await on_approval(task)
            if approved and task.force_approval:
                sel().log_api_access(
                    caller="taskrunner",
                    operation="task.force_approval",
                    outcome="approved",
                    source="taskrunner",
                    resources=f"task-{task.index}",
                )
            if not approved:
                # Design: denial pauses (not fails) so user can edit and retry.
                # force_approval is user-controlled (single-principal, no ACL boundary) —
                # the gate re-triggers on resume requiring explicit re-approval, and
                # shutdown/cancellation denies by default via CancelledError.
                if task.force_approval:
                    sel().log_api_access(
                        caller="taskrunner",
                        operation="task.force_approval",
                        outcome="denied",
                        source="taskrunner",
                        resources=f"task-{task.index}",
                        error="user denied force_approval gate",
                    )
                else:
                    sel().log_api_access(
                        caller="taskrunner",
                        operation="task.approval",
                        outcome="denied",
                        source="taskrunner",
                        resources=f"task-{task.index}",
                        error="user denied approval gate",
                    )
                task.status = TaskStatus.PENDING
                task.error = ""
                run.status = "paused"
                run.error = f"Task {task.index} approval denied — paused for editing"
                await on_notify(
                    f"⏸️ Task {task.index} denied",
                    "Paused for editing. Modify the task and Resume.",
                    run=run,
                )
                return False
        elif task.force_approval:
            # force_approval MUST block — cannot auto-approve
            logger.error(
                "Task %d has force_approval but no handler — blocking as failed", task.index
            )
            sel().log_api_access(
                caller="taskrunner",
                operation="task.force_approval",
                outcome="denied",
                source="taskrunner",
                resources=f"task-{task.index}",
                error="no approval handler configured",
            )
            task.status = TaskStatus.FAILED
            task.error = "force_approval task has no approval handler configured"
            task.finished_at = _time.time()
            # Prevent replanning around a security gate
            run.status = "failed"
            run.error = f"Task {task.index} denied: {task.error}"
            await on_notify(
                f"❌ Task {task.index} failed",
                "No approval handler configured for force_approval gate",
                run=run,
            )
            return False
        else:
            logger.warning("Task %d requires approval but no handler — auto-approving", task.index)

    run.current_task = task.index
    success = await execute_task(
        run,
        task,
        sessions,
        ctx,
        agent,
        on_tool_approval,
        auto_test,
        test_cmd,
        work_dir,
        on_notify,
        session_key,
    )
    run.last_task_time = _time.time()

    if success:
        committed = False
        if _uses_generic_git_commit(run):
            try:
                sha = await git_coord.commit_step(run, task)
                committed = bool(sha)
            except Exception:
                logger.debug("Git commit failed for task %d", task.index, exc_info=True)

        task.status = TaskStatus.REVIEWING
        review_ok = await self_review(run, task, sessions, agent, session_key)
        if not review_ok:
            if committed and _uses_generic_git_commit(run):
                try:
                    await git_coord.revert_step(run)
                except Exception:
                    logger.debug("Git revert before retry failed", exc_info=True)
            task.status = TaskStatus.FAILED
            success = await execute_task(
                run,
                task,
                sessions,
                ctx,
                agent,
                on_tool_approval,
                auto_test,
                test_cmd,
                work_dir,
                on_notify,
                session_key,
            )
            if success and run.branch_name:
                try:
                    await git_coord.commit_step(run, task)
                except Exception:
                    pass

        if success:
            task.status = TaskStatus.PASSED
            log_task_fn(history_key, run, task)
            result_preview = (task.result or "")[:500]
            await on_notify(
                f"✅ Task {task.index}/{len(run.tasks)}",
                f"{task.title}\n\n{result_preview}" if result_preview else task.title,
                run=run,
            )
    else:
        await on_notify(
            f"❌ Task {task.index}/{len(run.tasks)} failed",
            f"{task.title}\n{task.error}",
            run=run,
        )
        await extract_lesson_fn(task, run)

    task.finished_at = _time.time()
    return success


async def _reject_and_log(client, history, session_key, agent, event, *, metadata=None):
    """Reject a tool call and log the rejection."""
    await client.reject_tool(event.request_id)
    history.log_tool_invocation(
        session_key=session_key,
        agent=agent or "kirocrew",
        source="taskrunner",
        tool_name=event.title,
        tool_kind=event.tool_kind,
        outcome="rejected",
        request_id=event.request_id,
        **({"metadata": metadata} if metadata else {}),
    )


async def execute_task(
    run: Project,
    task: Task,
    sessions: "SessionManager",
    ctx: "ContextBuilder | None",
    agent: str,
    on_tool_approval: Callable | None,
    auto_test: bool,
    test_cmd: list[str] | None,
    work_dir: Path,
    on_notify: Callable,
    session_key: str = "",
) -> bool:
    """Execute a single task with retries and process recovery.

    Separate budgets:
    - Logic/test failures: up to MAX_RETRIES attempts
    - Process crashes (AcpProcessDied): up to MAX_RECOVERIES, not counted as attempts.
    """
    if not session_key:
        session_key = f"{SESSION_PREFIX}:{run.task_id}:task{task.index}"
    recoveries = 0
    compactions = 0
    attempt = 0
    previous_error = ""
    consecutive_same_error = 0
    result_prefix = ""

    while attempt < MAX_RETRIES:
        attempt += 1

        if shutdown_event.is_set():
            task.status = TaskStatus.FAILED
            task.error = "Shutdown"
            return False

        task.status = TaskStatus.IN_PROGRESS
        task.attempts = attempt

        logger.info("Task %d/%d (attempt %d): %s", task.index, len(run.tasks), attempt, task.title)

        _acquired = False
        try:
            await check_context(session_key, sessions)

            client, is_new, _resumed = await sessions.open_task_session(
                f"{SESSION_PREFIX}:{run.task_id}:runtime",
                session_key,
                agent=agent or None,
                cwd=str(work_dir) if work_dir else None,
            )
            _acquired = True
            await _pin_review_fix_model(run, client)

            task_prompt = await build_task_prompt(run, task, attempt, work_dir)
            if ctx:
                # Off-loop: build_message embeds the episodic query (blocking urllib).
                full_prompt, _ = await run_in_embed_pool(
                    ctx.build_message,
                    task_prompt,
                    is_new,
                    session_key,
                    agent=agent or None,
                    project=str(work_dir) if work_dir else None,
                    provider_type=KiroCrewConfig.load().agent.provider,
                )
            else:
                full_prompt = task_prompt

            result_text = ""
            _chunk_count = 0
            _complete_event: LLMEvent | None = None
            # Wall clock for THIS turn only. The acp provider never assigns
            # TurnUsage.duration_ms, so the record builder falls back to this
            # local measurement (elapsed_ms). Bracket ONLY the stream: the prompt
            # build and episodic-query embed above are turn setup, not the turn,
            # and this loop re-runs per attempt so each row measures its own turn.
            _turn_t0 = _time.monotonic()
            async for event in client.stream(full_prompt):
                if event.kind == EVENT_TEXT_CHUNK:
                    result_text += event.text
                    _chunk_count += 1
                    if _chunk_count % 50 == 0:
                        task.result = redact_credentials(
                            redact_exfiltration_urls(result_prefix + result_text)[0]
                        )[0]
                    run.last_task_time = _time.time()
                    run.tokens_used += max(1, len(event.text) // 4)
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    # Honor the user-configured auto-approve trust (hook
                    # TOOL_AUTO_APPROVE from hooks.auto_approve_tools) before the
                    # interactive prompt, so explicit trust is respected instead
                    # of always prompting.
                    _auto_approved = False
                    _auto_reason = ""
                    if ctx:
                        tool_result = ctx.hooks.on_tool_call(
                            event.title,
                            session_key=session_key,
                            agent=agent,
                            tool_kind=event.tool_kind,
                            raw_params=event.raw_tool_params,
                            command=event.shell_command,
                            is_shell=event.is_shell,
                        )
                        if tool_result.action == TOOL_DENY:
                            await client.reject_tool(event.request_id)
                            sel().log_tool_invocation(
                                session_key=session_key,
                                agent=agent or "kirocrew",
                                source="taskrunner",
                                tool_name=event.title,
                                tool_kind=event.tool_kind,
                                outcome="denied",
                                request_id=event.request_id,
                                error="hook_deny",
                            )
                            continue
                        if tool_result.action == TOOL_AUTO_APPROVE:
                            # The hook granted this by NAME (its
                            # `auto_approve_tools` globs, or the read-only
                            # allowlist). Honour it only while each program name
                            # in the command still resolves to the program it
                            # appears to name; a shadowed, agent-tree or
                            # unidentified resolution DOWNGRADES to this
                            # surface's normal path below (interactive approval
                            # when a handler is present, deny-by-default when
                            # headless) — never a hard block.
                            _ng_refusal = await name_grant.refusal_for_event(event)
                            if _ng_refusal is None:
                                _auto_approved = True
                                _auto_reason = "hook_auto_approve"
                            else:
                                logger.warning(
                                    "declining a hook auto-approve: %s; the request "
                                    "falls through to the task runner's normal "
                                    "approval path",
                                    _ng_refusal.log_text,
                                )
                                name_grant.log_decline(
                                    source="taskrunner",
                                    session_key=session_key,
                                    agent=agent or "kirocrew",
                                    event=event,
                                    refusal=_ng_refusal,
                                    tier="hook_auto_approve",
                                    sel_factory=sel,
                                )

                    # Per-run trust toggle: the user explicitly opted THIS run into
                    # unattended execution via the dashboard. It is NOT the global
                    # SafetyOverride singleton (which would leak trust to every
                    # session); instead the authoritative grant is a task-scoped
                    # SafetyOverride grant (scope `taskrunner:{task_id}:autoapprove`)
                    # activated through the singleton's fail-closed audited
                    # `activate_scoped()` and TTL-bounded there (dashboard window,
                    # ≤24h ceiling) — so no independent approval state lives on the
                    # run, and it satisfies the backend-security-controls expiry rule.
                    # `run.auto_approve` is only the UI intent flag; the live decision
                    # is `is_scope_active()`, re-checked before EVERY approval and
                    # revoked the moment the grant lapses. It is deny-by-default (only a
                    # literal `true` enables it), dashboard source-gated, SEL-audited as
                    # `run_auto_approve`, and reset on crash-recovery. Compensating
                    # controls stay intact: hook deny-lists / sensitive-path blocks
                    # (handled above) still reject, and force_approval / requires_approval
                    # task gates (separate task-level path) still pause for approval.
                    if not _auto_approved and getattr(run, "auto_approve", False):
                        _scope = f"{SESSION_PREFIX}:{run.task_id}:autoapprove"
                        if safety_override().is_scope_active(_scope):
                            _auto_approved = True
                            _auto_reason = "run_auto_approve"
                            # Slide the grant forward on activity so an actively
                            # progressing run does not lose trust at the base TTL;
                            # still hard-capped at the 24h ceiling from first grant,
                            # so an abandoned (idle) run lapses as intended.
                            safety_override().renew_scoped(_scope, source="dashboard")
                        else:
                            # Grant lapsed / absent — clear BOTH trust representations
                            # together (intent flag + scoped grant, idempotent) and fall
                            # through to interactive / deny-by-default. Bounds unattended
                            # tool execution to the grant window.
                            run.auto_approve = False
                            safety_override().deactivate_scope(_scope)
                            sel().log_api_access(
                                caller="taskrunner",
                                operation="task.auto_approve_expired",
                                outcome="expired",
                                source="taskrunner",
                                resources=f"task-{task.index}",
                            )

                    # Mid-stream context check runs BEFORE authorization resolution:
                    # context management is orthogonal to approval and must fire even
                    # on the headless deny path, or overflow recovery (compact/reset)
                    # would be skipped whenever a tool is about to be rejected.
                    pct = client.context_usage_pct()
                    if pct >= _MID_STREAM_COMPACT_PCT:
                        await _reject_and_log(
                            client,
                            sel(),
                            session_key,
                            agent,
                            event,
                            metadata={"reason": "context_overflow", "pct": pct},
                        )
                        raise _ContextOverflow(pct)

                    # Positive-authorization resolution (deny-by-default shape):
                    # each path is explicit — no falsy-guard fall-through.
                    if _auto_approved:
                        approve_reason = _auto_reason or "hook_auto_approve"
                    elif on_tool_approval:
                        run.last_task_time = _time.time()
                        approved = await on_tool_approval(event)
                        if not approved:
                            await _reject_and_log(client, sel(), session_key, agent, event)
                            continue
                        approve_reason = "interactive_approved"
                    else:
                        # No interactive handler and no explicit hook auto-approve:
                        # the task runner is running headless (autonomous project /
                        # cron) with no positive authorization for THIS tool.
                        # Deny-by-default — reject. Tools the user explicitly trusts
                        # via hooks.auto_approve_tools still pass (handled above as
                        # TOOL_AUTO_APPROVE, independent of handler presence). We do
                        # NOT read approval_mode or safety_override here: raw config
                        # would bypass the SafetyOverride 24h TTL, and honoring the
                        # override would reintroduce the global-YOLO dependency this
                        # change deliberately avoids.
                        await _reject_and_log(
                            client,
                            sel(),
                            session_key,
                            agent,
                            event,
                            metadata={"reason": "headless_no_authorization"},
                        )
                        continue

                    await client.approve_tool(event.request_id)
                    run.last_task_time = _time.time()
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        source="taskrunner",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="approved",
                        request_id=event.request_id,
                        metadata={
                            "task": task.index,
                            "task_id": run.task_id,
                            "reason": approve_reason,
                            # Trust provenance: which launch surface granted this run.
                            # run_auto_approve is dashboard-gated at the API boundary.
                            "source": run.source,
                        },
                    )
                elif event.kind == EVENT_TOOL_CALL:
                    # Fire PreToolUse hooks for auto-approved tools (informational only)
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        source="taskrunner",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        metadata={"task": task.index, "task_id": run.task_id},
                    )
                    await fire_tool_hooks(
                        get_global_hook_store(),
                        event.title,
                        event.tool_input,
                        parent_session_key=session_key or None,
                        agent_role=(agent or "kirocrew"),
                    )
                elif event.kind == EVENT_COMPLETE:
                    _complete_event = event
                    break

            final_result = result_prefix + result_text
            task.result = redact_credentials(redact_exfiltration_urls(final_result)[0])[0]
            sessions.record_success(session_key)
            sessions.check_context_usage(session_key, client)

            # ── Per-turn usage row: attribute task-runner spend. ──
            try:
                # circular import: reached while kiro_crew.slack.handler is still
                # initialising (dashboard/handlers/files.py imports is_tracked_channel
                # from it), so a module-scope import raises ImportError under the
                # suite's import order.
                from kiro_crew.dashboard.handlers.usage import (
                    persist_token_record_async,
                    read_context_tokens,
                    read_effective_agent,
                )

                _usage_cfg = KiroCrewConfig.load()
                _used, _window = read_context_tokens(client)
                await persist_token_record_async(
                    session_key,
                    # Blank, not the global config model: open_task_session may
                    # have resolved a custom agent's own model, and an explicit
                    # value here would outrank model_source and record the
                    # global default instead of what actually ran.
                    "",
                    _complete_event,
                    provider=_usage_cfg.agent.provider,
                    surface=telemetry_channel_of(session_key),
                    agent=read_effective_agent(client) or agent or "",
                    context_used=_used,
                    context_window=_window,
                    elapsed_ms=int((_time.monotonic() - _turn_t0) * 1000),
                    model_source=client,
                )
            except Exception:
                logger.debug("usage row (taskrunner) persist failed", exc_info=True)

        except AcpProcessDied:
            recoveries += 1
            partial = task.result or ""
            task.error = f"Process died (recovery {recoveries}/{MAX_RECOVERIES})"
            logger.warning(
                "Task %d: process died (recovery %d/%d), partial: %.200s",
                task.index,
                recoveries,
                MAX_RECOVERIES,
                partial,
            )
            await sessions.reset(session_key)

            if recoveries > MAX_RECOVERIES:
                task.status = TaskStatus.FAILED
                task.error = f"Process died {recoveries} times — giving up"
                return False

            if partial:
                task.error = (
                    f"Process crashed. Partial output before crash:\n"
                    f"{partial[:500]}\n\nContinue from where you left off."
                )
            await on_notify(
                f"💀 Task {task.index}: process died",
                f"Recovering ({recoveries}/{MAX_RECOVERIES})…",
                run=run,
            )
            run.last_task_time = _time.time()
            attempt -= 1
            continue

        except _ContextOverflow as cof:
            compactions += 1
            pct = cof.args[0] if cof.args else 0
            logger.warning("Task %d: context at %.0f%%, compacting mid-stream", task.index, pct)
            if compactions > MAX_RECOVERIES:
                task.status = TaskStatus.FAILED
                task.error = f"Context overflow {compactions} times — giving up"
                return False
            result_prefix += (
                f"⚠️ Context window {pct:.0f}% full — compressing conversation history. "
                "Accuracy may degrade due to summarized context.\n\n"
            )
            try:
                await client.compact()
                compact_result = await client.wait_for_compaction()
                if compact_result.get("type") == "completed":
                    logger.info("Task %d: compaction succeeded", task.index)
                else:
                    logger.warning(
                        "Task %d: compaction %s, resetting session",
                        task.index,
                        compact_result.get("type", "unknown"),
                    )
                    await sessions.reset(session_key)
            except Exception:
                logger.debug("Mid-stream compaction failed, resetting", exc_info=True)
                await sessions.reset(session_key)
            await on_notify(
                f"🗜️ Task {task.index}: context compressed",
                f"Context was {pct:.0f}% full — compacted to continue.",
                run=run,
            )
            run.last_task_time = _time.time()
            attempt -= 1
            continue

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task.error = str(exc)
            logger.warning("Task %d attempt %d failed: %s", task.index, attempt, exc)
            await sessions.record_failure(session_key)

            # Reset session between retries to avoid StreamReader corruption
            # ("readuntil() called while another coroutine is already waiting")
            try:
                await sessions.reset(session_key)
            except Exception:
                logger.debug("Session reset between retries failed", exc_info=True)

            if previous_error and task.error == previous_error:
                consecutive_same_error += 1
                should_fail = await _check_error_loop(
                    task,
                    consecutive_same_error,
                    on_notify,
                    run,
                )
                if should_fail:
                    return False
            else:
                consecutive_same_error = 0
            previous_error = task.error
            if attempt < MAX_RETRIES:
                continue
            task.status = TaskStatus.FAILED
            return False
        finally:
            if _acquired:
                sessions.release(session_key)

        # Run tests if configured
        if auto_test and test_cmd:
            test_ok, test_output = await run_tests(test_cmd, work_dir)
            if not test_ok:
                task.error = f"Tests failed:\n{test_output}"
                logger.warning("Task %d tests failed (attempt %d)", task.index, attempt)
                # Same retry logic as main task failure above
                if previous_error and task.error == previous_error:
                    consecutive_same_error += 1
                    should_fail = await _check_error_loop(
                        task, consecutive_same_error, on_notify, run
                    )
                    if should_fail:
                        return False
                else:
                    consecutive_same_error = 0
                previous_error = task.error
                if attempt < MAX_RETRIES:
                    continue
                task.status = TaskStatus.FAILED
                return False

        task.status = TaskStatus.PASSED
        task.error = ""
        return True

    task.status = TaskStatus.FAILED
    return False


async def build_task_prompt(run: Project, task: Task, attempt: int, work_dir: Path) -> str:
    """Build the prompt for executing a task."""
    parts: list[str] = []

    role = (
        "You are an autonomous execution agent in a multi-task pipeline.\n"
        "Execute the current task precisely. Do not describe what you will do — just do it.\n"
    )
    if run.branch_name:
        role += (
            f"Your changes are tracked on git branch `{run.branch_name}`.\n"
            "A separate review agent will verify your work against the actual diff.\n"
            "Check what already exists before creating files — prior tasks may have created them.\n"
        )
    if attempt > 1:
        role += f"This is retry attempt {attempt}. Fix the previous error, don't start over.\n"
    parts.append(role)

    if run.branch_name:
        try:
            git_ctx = await git_coord.get_state_summary(run)
            if git_ctx:
                parts.append(git_ctx + "\n")
        except Exception:
            pass

    memory_text = run.memory.summary()
    if memory_text:
        parts.append(memory_text + "\n")

    # Full execution plan with grouping
    completed_indices = {t.index for t in run.tasks if t.status == TaskStatus.PASSED}
    groups = group_parallel_tasks(run.tasks, completed_indices)
    parts.append("## Full Execution Plan (follow strictly)\n")
    parts.append(
        "Execute ONLY what the current task describes. Do not skip ahead or combine tasks.\n"
    )
    for gi, group in enumerate(groups, 1):
        parallel = len(group) > 1
        label = f"### Group {gi}" + (" (parallel)" if parallel else "")
        parts.append(label)
        for t in group:
            if t.status == TaskStatus.PASSED:
                icon = "✅"
            elif t.index == task.index:
                icon = "🔄"
            else:
                icon = "⬜"
            dep_str = (
                f" (depends on: {', '.join(str(d) for d in t.depends_on)})" if t.depends_on else ""
            )
            line = f"{icon} {t.index}. {t.title}{dep_str}"
            if t.index == task.index:
                line += " ← YOU ARE HERE"
            parts.append(line)
            if t.description and t.description != t.title:
                parts.append(f"   {t.description[:200]}")
        parts.append("")

    if run.original_input:
        parts.append("## Original Context\n")
        parts.append(run.original_input[:2000])
        parts.append("")
    elif run.spec_content:
        parts.append("## Original Spec\n")
        parts.append(run.spec_content[:2000])
        parts.append("")

    parts.append(f"## Current Task ({task.index}/{len(run.tasks)})\n")
    parts.append(f"**{task.title}**\n\n{task.description}\n")

    if attempt > 1 and task.error:
        parts.append(
            f"\n## Previous Attempt Failed (attempt {attempt - 1})\n"
            f"Error: {task.error}\n\nFix the error and try again.\n"
        )

    wd = run.work_dir or str(work_dir)
    if run.branch_name:
        parts.append(
            "\n## Instructions\n"
            f"**CRITICAL: All files MUST be created in `{wd}`.**\n"
            "Do NOT create files in ~/Downloads, ~/Desktop, or any other directory.\n"
            f"Use absolute paths starting with `{wd}/` for every file operation.\n"
            "Changes outside this directory will NOT be committed to git.\n"
            "Execute this task. Make the required code changes or run commands.\n"
            "Be precise and complete.\n"
        )
    else:
        parts.append(
            "\n## Instructions\n"
            f"Working directory: `{wd}`\n"
            "Create all files in this directory.\n"
            "Execute this task. Make the required code changes or run commands.\n"
            "Be precise and complete.\n"
        )

    return "\n".join(parts)


async def check_context(session_key: str, sessions: "SessionManager") -> None:
    """Compact the session if context usage is high.

    Routed through :meth:`SessionManager.compact_if_needed` — the same shared
    path gateway compaction uses — so the task runner inherits concurrent-
    trigger dedup, the failure/ineffective cooldown, turn-semaphore exclusion,
    the still-critical post-compaction reset, and skills-index reinjection,
    instead of bypassing them all with a direct ``provider.compact()``
    (#4686). A ``"busy"`` decline (a turn holds the semaphore) is final for
    this check: never fall back to a direct compact — the next check retries
    once the turn drains.
    """
    try:
        outcome = await sessions.compact_if_needed(session_key)
        if outcome not in ("absent", "below_threshold"):
            logger.info("TaskRunner: context check for %s — %s", session_key, outcome)
    except Exception:
        logger.debug("Context check failed", exc_info=True)


async def self_review(
    run: Project,
    task: Task,
    sessions: "SessionManager",
    agent: str,
    session_key: str = "",
) -> bool:
    """Review task using a separate session that reads the actual git diff."""
    review_key = f"{SESSION_PREFIX}:{run.task_id}:review"
    try:
        diff = ""
        if run.branch_name:
            try:
                if run.execution_mode == "review_fix" and run.review_fix:
                    from kiro_crew.review_fix_git import candidate_patch

                    base_sha = run.review_fix.target.head_sha or run.review_fix.source_head_sha
                    diff = (await candidate_patch(run.work_dir, base_sha)).patch_text
                else:
                    diff = await git_coord.get_step_diff(run)
            except Exception:
                pass

        if diff.strip():
            prompt = (
                "You are an independent review agent. You did NOT write this code.\n"
                "A separate execution agent made these changes. Your job: verify the diff\n"
                "matches the task intent. Flag real issues only — do not nitpick style.\n\n"
                f"Review this diff for task '{task.title}'.\n\n"
                f"## Task Description\n{task.description}\n\n"
                f"## Actual Diff\n```diff\n{diff[:8000]}\n```\n\n"
                "Check:\n"
                "1. Does the diff match the task description?\n"
                "2. Any obvious bugs, typos, or missing pieces?\n"
                "3. Any files that should have been changed but weren't?\n\n"
                'Respond with ONLY JSON: {"ok": true} or {"ok": false, "issue": "..."}\n'
            )
        else:
            prompt = (
                "You are a review agent verifying the execution agent's work.\n"
                f"Task {task.index}: {task.title}\n\n"
                "Review the changes. Check:\n"
                "1. Did it modify the correct files?\n"
                "2. Any obvious bugs or typos?\n"
                "3. Anything missing from the task description?\n\n"
                'Respond with ONLY JSON: {"ok": true} or {"ok": false, "issue": "..."}\n'
            )

        client, *_ = await sessions.open_task_session(
            f"{SESSION_PREFIX}:{run.task_id}:runtime",
            review_key,
            agent=agent or None,
            cwd=str(run.work_dir) if run.work_dir else None,
        )
        await _pin_review_fix_model(run, client)
        # Wall clock for the review turn (see execute_task): the acp provider
        # reports no duration, so this local measurement is the fallback. Bracket
        # ONLY the model stream, not open_task_session / diff fetch / prompt build.
        _review_t0 = _time.monotonic()
        result = await stream_and_collect_json(client, prompt)

        # ── Per-turn usage row: self-review is a separate model turn. ──
        try:
            # circular import: reached while kiro_crew.slack.handler is still
            # initialising (dashboard/handlers/files.py imports is_tracked_channel
            # from it), so a module-scope import raises ImportError under the
            # suite's import order.
            from kiro_crew.dashboard.handlers.usage import (
                persist_token_record_async,
                read_context_tokens,
                read_effective_agent,
            )

            _rv_cfg = KiroCrewConfig.load()
            _used, _window = read_context_tokens(client)
            await persist_token_record_async(
                review_key,
                # Blank — see the task site above; model_source reports what ran.
                "",
                provider_last_turn_usage(client),
                provider=_rv_cfg.agent.provider,
                surface=telemetry_channel_of(review_key),
                agent=read_effective_agent(client) or agent or "",
                context_used=_used,
                context_window=_window,
                elapsed_ms=int((_time.monotonic() - _review_t0) * 1000),
                model_source=client,
            )
        except Exception:
            logger.debug("usage row (self_review) persist failed", exc_info=True)

        if result and not result.get("ok", True):
            issue = result.get("issue", "Review found issues")
            task.error = f"Self-review: {issue}"
            logger.info("Self-review failed for task %d: %s", task.index, issue)
            run.memory.blockers.append(f"Task {task.index} review: {issue}")
            return False
        return True
    except Exception:
        logger.debug("Self-review failed", exc_info=True)
        return True  # don't block on review failure
    finally:
        sessions.release(review_key)
        await sessions.reset(review_key)


# Grace period between SIGTERM and SIGKILL when reaping a timed-out test's
# process group.
_TEST_KILL_GRACE = 5.0

# Hard parent-side cap on buffered test output. communicate() buffered the
# ENTIRE stream first, so a noisily-looping test OOM'd the gateway before the
# 2000-char failure tail could ever be applied; drain_bounded caps the buffer
# and discards the excess while the child keeps its normal exit semantics.
_TEST_OUTPUT_CAP_BYTES = 512 * 1024

# `run_tests` reports a missing test executable as success so the generic Task
# Runner keeps treating "nothing to run" as a skip. The string is also the
# contract: review-fix validation detects the skip by this exact sentinel and
# refuses to credit it as passing evidence (see review_fix.validate_group).
TESTS_SKIPPED_OUTPUT = "test command not found (skipped)"


async def _reap_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill a spawned test process and its entire process tree.

    The child is spawned as a group/session leader (``start_new_session`` on
    POSIX, ``CREATE_NEW_PROCESS_GROUP`` on Windows), so reaping the whole tree —
    not just ``proc.pid`` — cleans up any shell wrapper or grandchildren it
    forked. Without this a test that exceeds ``TEST_TIMEOUT`` — or crashes the
    pump — would orphan processes that keep holding CPU/memory/file handles and
    locks, accumulating across runs.

    Routed through :mod:`platform_compat` so the tree kill is portable:
    ``killpg(getpgid)`` on POSIX (with the shim's broadcast-guard against
    signalling pgid<=1 / our own group) and ``taskkill /T /F`` on Windows.
    Sends SIGTERM to the tree, waits briefly, then escalates to SIGKILL; both
    signal calls tolerate an already-exited target.
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    if pid is None:
        return
    try:
        await platform_compat.kill_process_tree_async(pid, platform_compat.SIGTERM)
    except (ProcessLookupError, OSError):
        # Already gone, or a group we refuse to signal — nothing to escalate.
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TEST_KILL_GRACE)
        return
    except Exception:
        pass
    try:
        await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TEST_KILL_GRACE)
    except Exception:
        pass


def _close_proc_pipes(proc: asyncio.subprocess.Process) -> None:
    """Close the subprocess transport so its pipe fds are not leaked."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:  # pragma: no cover — defensive
            logger.debug("run_tests: closing proc transport failed", exc_info=True)


async def run_tests(test_cmd: list[str], work_dir: Path) -> tuple[bool, str]:
    """Run the configured test command. Returns (success, output)."""
    # The test command and its working directory are both agent-influenced, so
    # route the spawn through the sandbox chokepoint: OS-level isolation plus a
    # credential-scrubbed environment.
    argv, env, cleanup = await sandboxed_spawn_argv_async(
        list(test_cmd), _prepare=sandboxed_spawn_argv
    )
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await create_subprocess_limited(
            *argv,
            cwd=str(work_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            # Lead a new session/process group so a timeout can signal the whole
            # tree (shell wrapper + any children) rather than just the top pid.
            # start_new_session is silently ignored on Windows, where
            # CREATE_NEW_PROCESS_GROUP makes the tree taskkill /T-reapable.
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        assert proc is not None
        # Drain with a hard parent-side cap instead of communicate(): a
        # noisily-looping test buffered in FULL here OOMs the gateway before
        # any downstream truncation runs. Excess output is read and discarded,
        # so the child keeps its normal exit semantics.
        data, truncated = await asyncio.wait_for(
            drain_bounded(proc.stdout, _TEST_OUTPUT_CAP_BYTES), timeout=TEST_TIMEOUT
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TEST_KILL_GRACE)
        except asyncio.TimeoutError:
            # Still running after closing stdout: the finally-block reaps the
            # tree; returncode stays None -> reported as failure.
            pass
        output = data.decode(errors="replace") if data else ""
        if truncated:
            output += (
                f"\n...[output truncated at {_TEST_OUTPUT_CAP_BYTES // 1024} KiB; "
                "excess discarded]"
            )
        success = proc.returncode == 0
        if success:
            logger.info("TaskRunner: tests passed")
        else:
            logger.warning("TaskRunner: tests failed (rc=%s)", proc.returncode)
            if len(output) > 2000:
                output = "...\n" + output[-2000:]
        return success, output
    except asyncio.TimeoutError:
        logger.warning("TaskRunner: tests timed out after %ds", TEST_TIMEOUT)
        return False, f"Test timed out after {TEST_TIMEOUT}s"
    except FileNotFoundError:
        logger.debug("Test command not found, skipping tests")
        return True, TESTS_SKIPPED_OUTPUT
    finally:
        # Reap the process group on EVERY exit path (timeout, crash, or normal
        # return where communicate() left the child alive) so orphaned test
        # processes cannot survive holding CPU/memory/fds/locks. Then close the
        # transport to avoid leaking pipe fds.
        if proc is not None:
            await _reap_process_group(proc)
            _close_proc_pipes(proc)
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)

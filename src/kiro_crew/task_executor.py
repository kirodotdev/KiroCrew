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

from kiro_crew import git_coord, name_grant, platform_compat, runtime_death, shutdown_event
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.agent_sdk.drivers.acp_vocab import (
    STOP_CLASS_CANCELLED,
    STOP_CLASS_FAILED,
    STOP_CLASS_RECOVERING,
    STOP_CLASS_STALLED,
    STOP_RECOVERY_MAX_RETRIES,
    classify_stop_reason,
)
from kiro_crew.agent_sdk.spec_hooks import (
    invalidate_stale_kas_session,
    refuse_stale_switch,
    reproject_claimed_session,
    running_agent,
    turn_spec_hooks,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.constants import DENY_CAUSE_POLICY, DENY_CAUSE_SURFACE_POLICY
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import (
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    fire_tool_hooks,
    get_global_hook_store,
    hook_gate_kwargs,
    permission_pre_tool_block,
)
from kiro_crew.llm_helpers import (
    _steer_host_deny,
    provider_last_turn_usage,
    stream_and_collect_json,
)
from kiro_crew.messaging.dispatch import consume_reinjection, rearm_reinjection
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR
from kiro_crew.providers.base import (
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
)
from kiro_crew.recovery.ladder import L3_ACP_RUNTIME, default_ladder
from kiro_crew.safety_override import safety_override
from kiro_crew.sandbox import (
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.task_models import (
    MAX_RECOVERIES,
    MAX_RETRIES,
    RESUME_HINT,
    SESSION_PREFIX,
    TEST_TIMEOUT,
    Project,
    Task,
    TaskStatus,
)
from kiro_crew.task_planner import group_parallel_tasks

if TYPE_CHECKING:
    from kiro_crew.taskq.adapters.runner import Admitted

_MID_STREAM_COMPACT_PCT = 90.0

#: What the model is told when an unattended task run refuses a tool call it has
#: no positive authorization for. Says what the surface permits (the
#: surface-policy notice tells the model to read exactly that) and nothing
#: about the call itself, which was never judged.
_HEADLESS_DENY_REASON = (
    "this task run is unattended: no approval handler is attached and no hook "
    "auto-approve trusts this tool, so only tools listed in hooks.auto_approve_tools "
    "can run here"
)


class _ContextOverflow(Exception):
    """Raised when context usage exceeds threshold mid-stream."""


class _TurnNotCompleted(Exception):
    """The stream ended on a non-success stop reason (stall, cancel, error).

    The task runner consumes the same ACP completions as the main chat and
    the sub-agent run, so it maps them through the same classifier
    (``classify_stop_reason``). Raised INSIDE the attempt's try, and handled by
    its own branch in :func:`execute_task`: a ``stalled`` / ``recovering``
    class re-runs the turn after the recovery ladder's L3 delay (the row is
    ``recovering`` meanwhile), a ``cancelled`` or non-retryable ``failed``
    class ends the step FAILED with its partial preserved, and a retryable
    ``error:`` class goes through the existing bounded retry ladder.
    """

    def __init__(
        self, stop_class: str, stop_reason: str, *, partial: bool, retryable: bool = False
    ) -> None:
        self.stop_class = stop_class
        self.stop_reason = stop_reason
        self.partial = partial
        self.retryable = retryable
        note = " — partial output preserved in the task result" if partial else ""
        super().__init__(f"turn ended {stop_class} (stop_reason={stop_reason!r}){note}")

    @property
    def recoverable(self) -> bool:
        return self.stop_class in (STOP_CLASS_STALLED, STOP_CLASS_RECOVERING)


async def _recovery_delay(secs: float) -> None:
    """Module seam for the ladder's wait between a stalled turn and its re-run."""
    if secs > 0:
        await asyncio.sleep(secs)


if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

# Prefix of ``task.error`` when self-review could not judge the step at all.
_REVIEW_UNVERIFIED = "Self-review could not verify the step"


async def _reset_session_quietly(sessions: "SessionManager", session_key: str) -> None:
    """Reset the ACP session for the next turn; a reset that fails is not fatal.

    Every re-prompt of a session whose previous turn did not finish goes through
    one, because a StreamReader another coroutine is still waiting on answers
    ``readuntil() called while another coroutine is already waiting``.
    """
    try:
        await sessions.reset(session_key)
    except Exception:
        logger.debug("Session reset before a re-run failed", exc_info=True)


async def _end_stalled_step(
    task: "Task",
    sessions: "SessionManager",
    session_key: str,
    stop: "_TurnNotCompleted",
    why: str,
) -> bool:
    """Fail *task* on a stalled turn whose in-place recovery cannot be taken.

    Always False, for the caller to return. *why* is the ONE thing that differs
    between the answers -- the in-place budget spent, the durable ``recovering``
    write refused, the row gone before the re-claim, the re-claimed row refusing
    the ``running`` mark -- and the partial already in ``task.result`` is kept
    whichever it was.
    """
    await _reset_session_quietly(sessions, session_key)
    task.status = TaskStatus.FAILED
    task.error = f"{stop.stop_class} ({stop.stop_reason}) — {why} — partial result preserved"
    return False


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
    taskq: "Admitted | None" = None,
) -> bool:
    """Execute one task with approval gate, self-review, and memory update.

    ``taskq`` is the step's admitted task-queue row (``taskq.adapters.runner``)
    when the runner has one attached; it carries the stop-reason recovery and
    the dependency / input waits through the shared store. ``None`` is the
    legacy path.
    """
    if not session_key:
        session_key = f"{SESSION_PREFIX}:{run.task_id}:task{task.index}"
    _exec_kw = {"taskq": taskq} if taskq is not None else {}

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
            # The callback keeps its one-argument contract; the task names
            # its run, so a gate can say WHICH run's task it holds (up to three
            # runs execute at once and their task indexes overlap).
            task.run_task_id = run.task_id
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
        **_exec_kw,
    )
    run.last_task_time = _time.time()

    if success:
        committed = commit_failed = False
        if run.branch_name:
            try:
                sha = await git_coord.commit_step(run, task)
                committed = bool(sha)
            except Exception:
                commit_failed = True
                logger.debug("Git commit failed for task %d", task.index, exc_info=True)

        task.status = TaskStatus.REVIEWING
        # Passed only when set, so the common call keeps its existing shape.
        _review_kw = {"commit_failed": True} if commit_failed else {}
        review_ok = await self_review(
            run, task, sessions, agent, session_key, ctx=ctx, **_review_kw
        )
        if not review_ok and task.error.startswith(_REVIEW_UNVERIFIED):
            # Nothing to judge the step by: fail it. A re-run would repeat any
            # push or CR the step made and pass without a review.
            task.status = TaskStatus.FAILED
            success = False
            await on_notify(
                f"❌ Task {task.index}/{len(run.tasks)} failed",
                f"{task.title}\n{task.error}",
                run=run,
            )
        elif not review_ok:
            if committed and run.branch_name:
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
                **_exec_kw,
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


async def _reject_and_log(
    client,
    history,
    session_key,
    agent,
    event,
    *,
    cause: str | None,
    reason: str = "",
    outcome: str = "rejected",
    error: str | None = None,
    metadata=None,
):
    """Audit a tool rejection, tell the model WHO refused it, then answer the wire.

    The task runner's single reject funnel: every ``reject_tool`` in this module
    goes through here so a path added later cannot deny by omission
    (``test_taskrunner_deny_notice`` walks the file to keep that true).

    *cause* is REQUIRED and says whether the HOST refused this call. A rejected
    permission reaches the model as kiro-cli's fixed "User denied tool
    execution"; for a host deny that is a refusal that never happened, and the
    model abandons or routes around a call nobody objected to. So a host cause
    (``DENY_CAUSE_POLICY`` for a hook or policy verdict on the call itself,
    ``DENY_CAUSE_SURFACE_POLICY`` for the unattended run refusing every
    un-trusted call) steers the in-band notice through ``llm_helpers``'
    ``_steer_host_deny`` BEFORE the reject -- while the permission request is
    still unanswered the turn is provably in flight, which is what gets the
    notice queued rather than dropped (see ``kiro_crew.deny_notice``). ``None``
    is the explicit verdict that this is NOT a host deny and must stay bare:
    the interactive handler said no (kiro-cli's wording is then the truth, and
    "this was NOT a user action" would be a lie), or the turn is being torn
    down for a mid-stream compaction and re-run, so there is no continuing turn
    for a notice to correct. A caller has to write one or the other; there is
    no default to inherit the wrong answer from.

    The SEL row is written FIRST, before the steer and the reject, as on every
    other deny surface: the steer is one more bounded await on the ACP pipe, and
    a backend that stops reading stdin cancels this coroutine at the turn
    deadline with the decision acted on and never audited if the row came last.
    ``outcome`` / ``error`` let a hook deny keep the row shape it always wrote.
    """
    history.log_tool_invocation(
        session_key=session_key,
        agent=agent or "kirocrew",
        source="taskrunner",
        tool_name=event.title,
        tool_kind=event.tool_kind,
        outcome=outcome,
        request_id=event.request_id,
        **({"error": error} if error else {}),
        **({"metadata": metadata} if metadata else {}),
    )
    if cause is not None:
        await _steer_host_deny(client, event, reason, cause=cause)
    await client.reject_tool(event.request_id)


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
    taskq: "Admitted | None" = None,
) -> bool:
    """Execute a single task with retries and process recovery.

    Separate budgets:
    - Logic/test failures: up to MAX_RETRIES attempts
    - Process crashes (AcpProcessDied): up to MAX_RECOVERIES, not counted as attempts.
    - Stalled / recovering turns (stop reason): the recovery ladder's L3 rung
      (``taskq.admission.ladder`` when attached, the process default
      otherwise) approves each re-run and spaces it, and
      ``STOP_RECOVERY_MAX_RETRIES`` is the ceiling on how many this step takes
      in place; not counted as attempts, because the turn that stalled never
      finished being attempted once and the two budgets bound different things.
    - A dependency signal (429, 5xx, auth) parks the step in
      ``waiting_dependency`` through ``taskq`` and re-runs it when woken; not
      counted as an attempt. Terminal signals fail the step.
    """
    if not session_key:
        session_key = f"{SESSION_PREFIX}:{run.task_id}:task{task.index}"
    recoveries = 0
    compactions = 0
    stop_recoveries = 0
    dependency_waits = 0
    attempt = 0
    # The agent a mid-step mode switch moved the session to, carried across
    # attempts so a retry on that same session is gated by ITS hooks. Only the
    # hook lookup reads it; the claim still asks for the step's own agent.
    switched_agent = ""
    previous_error = ""
    consecutive_same_error = 0
    result_prefix = ""

    # Operator answers applied to this step's prompt. They are marked consumed
    # (``input_consumed``) only once the step has durably COMPLETED: a crash
    # between applying an answer and the turn finishing must leave it
    # replayable, or the re-dispatched step asks the operator again.
    applied_answers: list[str] = []

    async def _consume_applied_answers() -> None:
        if taskq is None:
            return
        for question_id in applied_answers:
            await taskq.admission.consume_answer_async(taskq.task_id, question_id)
        applied_answers.clear()

    if taskq is not None:
        # A step re-dispatched after a crash that landed between the operator's
        # answer and the re-admission: the answer lives on the row's wake event
        # (never only in RAM), so the resumed turn sees it exactly as the
        # uninterrupted turn would have.
        recorded = await taskq.admission.recorded_answer_async(taskq.task_id)
        if recorded is not None:
            applied_answers.append("recovered")
            task.description = f"{task.description}\n\n## Operator input\n{recorded}\n"

    # ``attempt`` is the TURN ordinal (the re-run prompt reads it), and
    # ``MAX_RETRIES`` bounds the LOGIC failures only: an in-place stall recovery
    # the ladder approved raises this ceiling by its own turn instead of
    # spending an attempt on work that never finished being attempted once.
    # ``stop_recoveries`` is itself bounded (``STOP_RECOVERY_MAX_RETRIES``), so
    # the ceiling is too.
    while attempt < MAX_RETRIES + stop_recoveries:
        attempt += 1

        if shutdown_event.is_set():
            task.status = TaskStatus.FAILED
            task.error = "Shutdown"
            return False

        task.status = TaskStatus.IN_PROGRESS
        task.attempts = attempt

        logger.info("Task %d/%d (attempt %d): %s", task.index, len(run.tasks), attempt, task.title)

        _acquired = False
        # Post-compaction re-injection bookkeeping for the finally: whether this
        # turn consumed the one-shot flag, and whether it landed (recorded success).
        _needs_reinjection = False
        _turn_landed = False
        # Whether this attempt's stream produced output or a tool call: a death
        # after either may have left work done, so its retry resumes rather than
        # restates the step (the chat runner's ``turn_emitted``).
        _attempt_emitted = False
        # The provider THIS attempt ran on, for the death handler's attribution
        # question. Reset per attempt and set only once the session is open, so a
        # death before the open asks about nothing (and is charged, as before)
        # rather than about the previous attempt's provider -- ``client`` itself
        # survives the loop, so reading it directly would attribute this
        # attempt's death to a runtime it never used.
        _turn_provider: object | None = None
        try:
            from kiro_crew.context import inherit_session_memory

            memory_store = await inherit_session_memory(
                ctx, f"{SESSION_PREFIX}:{run.task_id}:runtime", session_key
            )
            await check_context(session_key, sessions)
            # A reused KAS session whose registered batch auto-approves what a
            # PreToolUse hook now covers is reset, so the claim re-projects it.
            await invalidate_stale_kas_session(sessions, session_key, agent or "kirocrew")

            def _claim():
                return sessions.open_task_session(
                    f"{SESSION_PREFIX}:{run.task_id}:runtime",
                    session_key,
                    agent=agent or None,
                    cwd=str(work_dir) if work_dir else None,
                )

            client, is_new, _resumed = await _claim()
            _acquired = True
            # Decided again under the lease: the pre-claim reset is declined for a
            # session another turn holds, and this claim may have waited for it.
            client, is_new, _resumed = await reproject_claimed_session(
                sessions, session_key, agent or "kirocrew", (client, is_new, _resumed), _claim
            )
            _turn_provider = client

            task_prompt = await build_task_prompt(run, task, attempt, work_dir)
            if ctx:
                # The check_context above (and the post-turn usage check) can
                # compact this session in place, which drops its session-start
                # context. Read-and-clear the one-shot flag so this turn
                # re-injects that context exactly once; the finally re-arms it
                # if the turn never lands.
                _needs_reinjection = consume_reinjection(sessions, session_key)
                # Off-loop: build_message embeds the episodic query (blocking urllib).
                full_prompt, _ = await run_in_embed_pool(
                    ctx.build_message,
                    task_prompt,
                    is_new,
                    session_key,
                    agent=agent or None,
                    project=str(work_dir) if work_dir else None,
                    provider_type=KiroCrewConfig.load().agent.provider,
                    memory_store=memory_store,
                    context_provider=client,
                    resumed=_resumed,
                    needs_reinjection=_needs_reinjection,
                )
            else:
                full_prompt = task_prompt

            # The step's agent spec hooks, when its backend never receives them
            # (none on kiro-cli, whose harness runs the field itself). On such a
            # backend PreToolUse hooks gate each permission request; the KAS
            # projection turns every call they cover into one.
            # A step with no agent runs the runtime's default one, and that is
            # the spec the session's projection gated.
            # The agent this session runs NOW: a retry on a session an earlier
            # attempt switched runs the switched-to agent, so its hooks gate.
            _spec = await turn_spec_hooks(
                client, running_agent(client, switched_agent or agent or "kirocrew")
            )

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
                    _attempt_emitted = True
                    result_text += event.text
                    _chunk_count += 1
                    if _chunk_count % 50 == 0:
                        task.result = redact_credentials(
                            redact_exfiltration_urls(result_prefix + result_text)[0]
                        )[0]
                    run.last_task_time = _time.time()
                    run.tokens_used += max(1, len(event.text) // 4)
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    _spec_block = None
                    if _spec.gated:
                        _spec_block = (
                            "the agent spec's hooks could not be read"
                            if _spec.unreadable
                            else await permission_pre_tool_block(
                                get_global_hook_store(),
                                _spec.hooks,
                                _spec.cwd,
                                event.title,
                                event.tool_input,
                                tool_identity=event.tool_name,
                                mcp_server=event.mcp_server_name,
                                harness_tool_id=event.harness_tool_id,
                                parent_session_key=session_key or None,
                                agent_role=(agent or "kirocrew"),
                            )
                        )
                    if _spec_block is not None:
                        logger.warning("task step PreToolUse hook blocked a tool: %s", _spec_block)
                        # A PreToolUse gate verdict on the call itself (a
                        # delivered deny, or a gate with no verdict, which
                        # blocks) -- the policy cause, as the chat runner
                        # steers the same BLOCKED strings.
                        await _reject_and_log(
                            client,
                            sel(),
                            session_key,
                            agent,
                            event,
                            cause=DENY_CAUSE_POLICY,
                            reason=_spec_block,
                            metadata={"reason": "spec_hook_deny"},
                        )
                        continue
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
                            **hook_gate_kwargs(event),
                        )
                        if tool_result.action == TOOL_DENY:
                            # The hook judged the call itself: a policy
                            # verdict, with the hook's own reason so the class
                            # remediation can key off it.
                            await _reject_and_log(
                                client,
                                sel(),
                                session_key,
                                agent,
                                event,
                                cause=DENY_CAUSE_POLICY,
                                reason=tool_result.reason or "",
                                outcome="denied",
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
                        # Not a verdict on the call: the turn is abandoned
                        # here and re-run after compaction, so there is no
                        # continuing turn for a deny notice to correct.
                        await _reject_and_log(
                            client,
                            sel(),
                            session_key,
                            agent,
                            event,
                            cause=None,
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
                            # The person (or the surface answering for them)
                            # said no: kiro-cli's "user denied" is the truth
                            # here, so no notice -- interactive_rejected.
                            await _reject_and_log(
                                client, sel(), session_key, agent, event, cause=None
                            )
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
                        # The SURFACE refuses the call, not a rule about the
                        # call itself: nothing here can approve it, so the
                        # notice names what this run permits instead of a
                        # sanctioned alternative the model should run.
                        await _reject_and_log(
                            client,
                            sel(),
                            session_key,
                            agent,
                            event,
                            cause=DENY_CAUSE_SURFACE_POLICY,
                            reason=_HEADLESS_DENY_REASON,
                            metadata={"reason": "headless_no_authorization"},
                        )
                        continue

                    approval_sent = await client.approve_tool(event.request_id)
                    if approval_sent is not False:
                        run.last_task_time = _time.time()
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        source="taskrunner",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome=(
                            "approved"
                            if approval_sent is not False
                            else OUTCOME_REJECTED_TRANSPORT_FLOOR
                        ),
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
                elif event.kind == EVENT_AGENT_SWITCHED:
                    # A mid-run mode switch runs a different agent, so ITS spec hooks gate
                    # the permission requests that follow, not the previous agent's. An
                    # unnamed switch falls back to the agent the session recorded for it.
                    switched_agent = event.text or switched_agent
                    _spec = await turn_spec_hooks(client, event.text or "")
                    await refuse_stale_switch(client, event.text or "")
                elif event.kind == EVENT_TOOL_CALL:
                    _attempt_emitted = True
                    # Fire PreToolUse hooks for auto-approved tools (informational only).
                    # On a gated turn this frame precedes the call's permission request,
                    # so nothing has approved it yet.
                    sel().log_tool_invocation(
                        session_key=session_key,
                        agent=agent or "kirocrew",
                        source="taskrunner",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="invoked" if _spec.gated else "auto_approved",
                        metadata={"task": task.index, "task_id": run.task_id},
                    )
                    # A gated turn runs them on the permission request instead.
                    if not _spec.gated:
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
            # A stream that ended without any EVENT_COMPLETE proves nothing: the
            # provider never said the turn finished, so it is not a PASSED step
            # (the classifier below would read the absent reason as a normal
            # end of turn). Retryable, like the transport-death ``error:``
            # family: the bounded retry ladder re-prompts from the partial.
            if _complete_event is None:
                raise _TurnNotCompleted(
                    STOP_CLASS_FAILED, "", partial=bool(result_text), retryable=True
                )
            # EVENT_COMPLETE only says the stream ended: a watchdog stall, a
            # runtime cancel or a transport death must not become a PASSED
            # step. Same mapping as chat_runner / subagent run.py.
            _stop = classify_stop_reason(str(getattr(_complete_event, "stop_reason", "") or ""))
            if not _stop.is_success:
                raise _TurnNotCompleted(
                    _stop.name,
                    _stop.stop_reason,
                    partial=bool(result_text),
                    retryable=bool(_stop.retryable),
                )
            sessions.record_success(session_key)
            # The prompt (with any re-injected context) reached the model and
            # the turn completed, so the finally must NOT restore the flag. A
            # cancelled or stalled stream, or one that ended without any
            # completion, never gets here: the raises above hand those to the
            # retry ladder, and the finally re-arms.
            _turn_landed = True
            # The ONE place the resume hint is cleared: an attempt completed
            # normally, so nothing it steered is left undone. See Task.resume_hint.
            task.resume_hint = ""
            # A landed turn proves recovery worked, so the next shared death
            # starts its own count instead of inheriting one -- the same reason
            # the chat runner clears it on a landed turn.
            runtime_death.clear_shared_deaths(session_key)
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

        except AcpProcessDied as _died_exc:
            # Whose failure was this? A task runs its sub-agents on its own
            # runtime, so a death here can be a process event several accounts
            # witnessed rather than this task's fault -- and MAX_RECOVERIES then
            # fails a task that did nothing wrong. The death was classified once
            # where it was detected; this reads that record. A single-tenant
            # runtime, and a death before the session opened, are charged exactly
            # as before.
            _own_fault = runtime_death.caused_by_this_session(_turn_provider)
            if _own_fault:
                recoveries += 1
            else:
                runtime_death.note_shared_death(session_key)
            # ``recoveries`` stays MONOTONIC. Assigning the shared streak into it
            # would refund budget already spent: two own-fault deaths followed by
            # one shared death would read as 1, and the third own-fault death
            # would still be under the limit -- replaying task work that is not
            # idempotent. So the two counts run side by side and the LIMIT is
            # tested against whichever is further along, exactly as the chat
            # runner does with its own `_death_attempts`.
            _death_attempts = max(recoveries, runtime_death.shared_deaths(session_key))
            if not _own_fault:
                logger.warning(
                    "Task %d lost a turn to a SHARED runtime's death (%d running) — "
                    "not charging this task's recovery budget",
                    task.index,
                    runtime_death.shared_deaths(session_key),
                )
            partial = task.result or ""
            task.error = f"Process died (recovery {_death_attempts}/{MAX_RECOVERIES})"
            logger.warning(
                "Task %d: process died (recovery %d/%d), partial: %.200s",
                task.index,
                _death_attempts,
                MAX_RECOVERIES,
                partial,
            )
            if getattr(_died_exc, "ambiguous_delivery", False) or _attempt_emitted:
                # The step's prompt may already have run: the death followed a
                # stdin stall the live child may still read past
                # (``ambiguous_delivery``), or the attempt had produced output or
                # a tool call. Re-stating the task verbatim would re-run its
                # (possibly non-idempotent) tools, so every later attempt --
                # including a Resume or retry of a run that gives up below --
                # opens by inspecting current state. Carried in resume_hint, not
                # task.error, because a death keeps the attempt number and
                # task.error renders only at attempt > 1. Set before the reset's
                # await, so a cancel landing there cannot drop it. See
                # Task.resume_hint.
                task.resume_hint = RESUME_HINT
            await sessions.reset(session_key)

            if _death_attempts > MAX_RECOVERIES:
                task.status = TaskStatus.FAILED
                task.error = (
                    f"Process died {_death_attempts} times — giving up"
                    if _own_fault
                    else f"The runtime this task shares died {_death_attempts} times — giving up"
                )
                return False

            if partial:
                task.error = (
                    f"Process crashed. Partial output before crash:\n"
                    f"{partial[:500]}\n\nContinue from where you left off."
                )
            await on_notify(
                f"💀 Task {task.index}: process died",
                f"Recovering ({_death_attempts}/{MAX_RECOVERIES})…",
                run=run,
            )
            run.last_task_time = _time.time()
            attempt -= 1
            continue

        except _ContextOverflow as cof:
            if _attempt_emitted:
                # The compaction can end in a session reset, after which the step
                # prompt is restated on a fresh session: the same hazard as a
                # death after output or a tool call.
                task.resume_hint = RESUME_HINT
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
        except _TurnNotCompleted as tnc:
            # ``task.result`` already carries the flagged partial (set before
            # the classifier ran); every branch below preserves it.
            task.error = str(tnc)
            if tnc.recoverable and taskq is not None:
                # Stalled / recovering: the recovery ladder's L3 rung (the ACP
                # runtime) says whether one more re-run is allowed and how
                # long to wait; the row is ``recovering`` for the wait and is
                # re-claimed under a new generation before the re-run.
                # Imported here, not at module scope, for the reason the
                # dependency arm below states: a disabled queue never pays for
                # ``kiro_crew.taskq`` on this module's import path.
                from kiro_crew.taskq.adapters.runner import (
                    RunnerAdmissionRefused,
                    RunnerTaskCancelled,
                )

                stop_recoveries += 1
                reason = f"{tnc.stop_class}: {tnc.stop_reason}"
                decision = taskq.admission.decide_recovery(taskq, unit=session_key, reason=reason)
                # This step's OWN in-place ceiling, spent by its stalls and by
                # nothing else. The ladder's L3 count is per-unit and DECAYS
                # after its cooldown, so a stall once per cooldown would be
                # approved for ever; ``STOP_RECOVERY_MAX_RETRIES`` is the
                # non-decaying bound the chat slot's ``_tool_stall_retries``
                # and the sub-agent's ``_stop_recovery_used`` also spend.
                in_place_left = stop_recoveries <= STOP_RECOVERY_MAX_RETRIES
                if decision is not None:
                    retry = decision.retry and in_place_left
                    delay = float(decision.delay_secs)
                else:
                    retry = in_place_left
                    delay = (
                        default_ladder().backoff_secs(L3_ACP_RUNTIME, stop_recoveries)
                        if retry
                        else 0.0
                    )
                if not retry:
                    return await _end_stalled_step(
                        task,
                        sessions,
                        session_key,
                        tnc,
                        f"in-place recovery exhausted after {stop_recoveries} attempt(s)",
                    )
                if not await taskq.recovering_async(reason=reason, delay_secs=delay):
                    # PERSIST BEFORE PUBLISH: the ``recovering`` row IS the
                    # statement that this turn's work is to be re-run, and the
                    # re-claim below takes only a row the store put back in a
                    # claimable state. A write that did not commit therefore
                    # ends the step -- never a wait on a row that is still
                    # ``running``, whose re-claim raises the whole run down.
                    return await _end_stalled_step(
                        task,
                        sessions,
                        session_key,
                        tnc,
                        "the durable recovering write did not commit, so no re-run was started",
                    )
                await on_notify(
                    f"\u267b\ufe0f Task {task.index}: turn {tnc.stop_class}",
                    f"{tnc.stop_reason} — re-running in {delay:.0f}s (recovery {stop_recoveries})",
                    run=run,
                )
                await _recovery_delay(delay)
                try:
                    await taskq.reclaim()
                except (RunnerTaskCancelled, RunnerAdmissionRefused) as exc:
                    # The row ended while it waited (an operator cancel during
                    # the backoff), or the store refused the claim. In band,
                    # because raising unwinds an accepted RUN over one step
                    # whose own row already says what became of it.
                    return await _end_stalled_step(
                        task, sessions, session_key, tnc, f"the row was not re-claimed: {exc}"
                    )
                if not await taskq.running_async(
                    {"index": task.index, "recovery": stop_recoveries}
                ):
                    # The same fence one state later: the reclaim's ``admit``
                    # committed ``starting`` under the new generation, so a
                    # refused ``starting -> running`` is a newer owner or a
                    # store outage. Re-running the turn under it would leave the
                    # row unable to take any wait the turn needs (``starting``
                    # reaches no WAITING state), and under a fence it would run
                    # work whose row belongs to another incarnation.
                    return await _end_stalled_step(
                        task,
                        sessions,
                        session_key,
                        tnc,
                        "the re-claimed row did not take the running mark",
                    )
                # The re-run is the next TURN (``attempt`` rises, so its prompt
                # names the stall and continues from the partial) but not a
                # logic attempt, which is why ``stop_recoveries`` raises the
                # ceiling this loop is bounded by instead of spending it.
                await _reset_session_quietly(sessions, session_key)
                run.last_task_time = _time.time()
                continue
            elif not tnc.recoverable and (
                tnc.stop_class == STOP_CLASS_CANCELLED or not tnc.retryable
            ):
                # A runtime cancel or a refusal is not a logic failure to
                # re-prompt around: the step ends with what it produced.
                task.status = TaskStatus.FAILED
                task.error = f"{tnc.stop_class} ({tnc.stop_reason}) — partial result preserved"
                return False
            # Then the bounded retry ladder every failed attempt goes through:
            # a stall without a task queue, and a retryable ``error:`` (pipe
            # death, process exit) with or without one. The retry prompt names
            # the stall and continues from the partial -- never a bare re-run.
            logger.warning("Task %d attempt %d failed: %s", task.index, attempt, tnc)
            await sessions.record_failure(session_key)
            try:
                await sessions.reset(session_key)
            except Exception:
                logger.debug("Session reset between retries failed", exc_info=True)
            if previous_error and task.error == previous_error:
                consecutive_same_error += 1
                if await _check_error_loop(task, consecutive_same_error, on_notify, run):
                    return False
            else:
                consecutive_same_error = 0
            previous_error = task.error
            run.last_task_time = _time.time()
            if attempt < MAX_RETRIES + stop_recoveries:
                continue
            task.status = TaskStatus.FAILED
            return False
        except Exception as exc:
            task.error = str(exc)
            logger.warning("Task %d attempt %d failed: %s", task.index, attempt, exc)
            await sessions.record_failure(session_key)

            # A dependency error (a 429 with Retry-After, a 5xx, an auth
            # failure) is not a logic failure: the step parks in
            # ``waiting_dependency`` and its lane slot is released until the
            # coordinator wakes it. Terminal signals end the step.
            # Imported here, not at module scope: at module scope this pulls
            # ``kiro_crew.taskq`` (and its store) into every importer of
            # ``kiro_crew.dashboard.handlers``, which the AUTOSDE boot-path rule
            # forbids for a subsystem a disabled queue never uses.
            if taskq is None:
                signal = None
            else:
                from kiro_crew.taskq.dependency import (
                    DEFAULT_MAX_ATTEMPTS as DEPENDENCY_MAX_ATTEMPTS,
                )
                from kiro_crew.taskq.dependency import (
                    classify_exception,
                )

                signal = classify_exception(exc)
            if signal is not None and taskq is not None:
                dependency_waits += 1
                if not signal.retryable or dependency_waits > DEPENDENCY_MAX_ATTEMPTS:
                    task.status = TaskStatus.FAILED
                    task.error = (
                        f"dependency {signal.dependency_scope} {signal.kind}: {signal.detail}"
                    )
                    return False
                await on_notify(
                    f"\u23f3 Task {task.index}: waiting on {signal.dependency_scope}",
                    f"{signal.kind}: {signal.detail or exc}",
                    run=run,
                )
                resumed = await taskq.admission.yield_dependency(taskq, signal)
                if not resumed:
                    task.status = TaskStatus.FAILED
                    task.error = (
                        f"dependency {signal.dependency_scope} unavailable: "
                        f"{signal.detail or exc}"
                    )
                    return False
                # No ``running`` mark here: the row already carries one when the
                # wait returns True -- a wake that re-claimed it wrote the mark,
                # and a wait that never persisted left the row running.
                run.last_task_time = _time.time()
                attempt -= 1
                continue
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
            if attempt < MAX_RETRIES + stop_recoveries:
                continue
            task.status = TaskStatus.FAILED
            return False
        finally:
            # A turn that consumed the post-compaction flag but never landed
            # discarded the prompt carrying the re-injected context; put the
            # flag back so the next attempt re-injects it.
            rearm_reinjection(
                sessions, session_key, consumed=_needs_reinjection, landed=_turn_landed
            )
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
                if attempt < MAX_RETRIES + stop_recoveries:
                    continue
                task.status = TaskStatus.FAILED
                return False

        task.status = TaskStatus.PASSED
        task.error = ""
        await _consume_applied_answers()
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

    # Rendered regardless of attempt count: a process death keeps the attempt
    # number, so this cannot ride on the attempt > 1 guard above. Never cleared
    # here; see Task.resume_hint for the one rule.
    if task.resume_hint:
        parts.append(f"\n## Resume (do not restart)\n{task.resume_hint}\n")

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
    instead of bypassing them all with a direct ``provider.compact()``.
    A ``"busy"`` decline (a turn holds the semaphore) is final for
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
    *,
    ctx: "ContextBuilder | None" = None,
    commit_failed: bool = False,
) -> bool:
    """Review task using a separate session that reads the actual git diff.

    Fails closed: a review error, or a git run whose step has no diff to show,
    returns False with ``task.error`` starting ``_REVIEW_UNVERIFIED``. A step
    whose commit failed (non-fatal by design) has no diff for that reason, so it
    keeps the generic review instead.
    """
    review_key = f"{SESSION_PREFIX}:{run.task_id}:review"
    from kiro_crew.context import inherit_session_memory

    await inherit_session_memory(ctx, f"{SESSION_PREFIX}:{run.task_id}:runtime", review_key)
    try:
        diff = ""
        if run.branch_name:
            try:
                diff = await git_coord.get_step_diff(run)
            except Exception:
                pass

        if run.branch_name and not commit_failed and not diff.strip():
            task.error = f"{_REVIEW_UNVERIFIED}: the step left no diff to review"
            logger.warning("Task %d unverified: no diff on a git run", task.index)
            return False
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
        logger.warning("Self-review errored for task %d", task.index, exc_info=True)
        task.error = f"{_REVIEW_UNVERIFIED}: the review itself failed"
        return False
    finally:
        sessions.release(review_key)
        await sessions.reset(review_key)


# Grace period between SIGTERM and SIGKILL when reaping a timed-out test's
# process group.
_TEST_KILL_GRACE = 5.0


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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=TEST_TIMEOUT)
        output = stdout.decode(errors="replace") if stdout else ""
        success = proc.returncode == 0
        if success:
            logger.info("TaskRunner: tests passed")
        else:
            logger.warning("TaskRunner: tests failed (rc=%d)", proc.returncode)
            if len(output) > 2000:
                output = "...\n" + output[-2000:]
        return success, output
    except asyncio.TimeoutError:
        logger.warning("TaskRunner: tests timed out after %ds", TEST_TIMEOUT)
        return False, f"Test timed out after {TEST_TIMEOUT}s"
    except FileNotFoundError:
        logger.debug("Test command not found, skipping tests")
        return True, "test command not found (skipped)"
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

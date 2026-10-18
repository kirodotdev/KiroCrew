"""Running a planned project's steps within the run's bounds.

Dependency groups run in order, a group's steps in parallel under the run's cap; each step
is admitted, published and executed through ``task_executor``, a failed step is re-planned
within the replan and total-step limits, and the global timeout, token budget and watchdog
(heartbeat, stall notice, stall cancel) bound the whole run. ``_MonotonicAge`` is the one
clock both the executor and the watchdog read a run's age from. A new bound on a running
plan goes here.

Instance fields written: ``_stall_cancelled_ids``.

Instance fields read: ``_agent``, ``_auto_test``, ``_ctx``, ``_global_timeout``,
``_max_parallel_steps``, ``_on_approval``, ``_on_tool_approval``, ``_require_verification``,
``_sessions``, ``_task_admission``, ``_test_cmd``, ``_token_budget``, ``_work_dir``,
``_workflow_service``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import git_coord, shutdown_event
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.task_executor import build_task_prompt, execute_single_task
from kiro_crew.task_executor import self_review as self_review_fn
from kiro_crew.task_models import (
    DEFAULT_TOKEN_BUDGET,
    MAX_REPLAN,
    MAX_TOTAL_TASKS,
    STALL_CANCEL_TIMEOUT,
    STALL_TIMEOUT,
    Project,
    Task,
    TaskStatus,
)
from kiro_crew.task_planner import group_parallel_tasks
from kiro_crew.taskrunner_parts.run_scope import _SESSION_PREFIX

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


_MAX_REPLAN = MAX_REPLAN
_MAX_TOTAL_TASKS = MAX_TOTAL_TASKS


_STALL_TIMEOUT = STALL_TIMEOUT
_STALL_CANCEL_TIMEOUT = STALL_CANCEL_TIMEOUT
_DEFAULT_TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET
_HEARTBEAT_INTERVAL = 30  # watchdog checks process liveness every 30s
_DEAD_THRESHOLD = 2  # consecutive dead checks before fail-fast reset
_RESULT_MEM_CAP = 4000  # truncate task.result in memory after step completes
_WORKFLOW_RESULT_SUMMARY_CAP = 120


class _MonotonicAge:
    """How old one of a run's wall-clock stamps is, measured on the monotonic clock.

    ``started_at`` and ``last_task_time`` stay wall-clock for display and
    ``runs.json``; the watchdog and the executor read their AGE from here, so a
    wall-clock step (an NTP correction, a manual change, a restored VM) neither
    cancels or times out a task that is progressing nor spares one that is
    stalled or past its global timeout. A reader sees a new stamp on its next
    read, so the age a new stamp starts at is bounded by the monotonic time
    since the previous read. The age is awake time: ``time.monotonic()`` stops
    while the host is suspended on Linux and macOS, so a suspend there is
    neither stall time nor timeout time (see ``taskrunner.md``).
    """

    def __init__(self, wall: float) -> None:
        self._wall = wall
        self._read = time.monotonic()
        self._at = self._read - max(0.0, time.time() - wall)

    def age(self, wall: float) -> float:
        mono = time.monotonic()
        if wall != self._wall:
            self._wall = wall
            self._at = mono - min(max(0.0, time.time() - wall), mono - self._read)
        self._read = mono
        return mono - self._at

    @classmethod
    def of_run_start(cls, run: Project, *, fresh: bool = False) -> _MonotonicAge:
        """The one age of *run*'s ``started_at`` that the executor and the watchdog read.

        Kept on the run instance, outside the dataclass fields, so ``runs.json``
        never carries it. Each place that stamps ``started_at`` passes
        ``fresh=True``, so the age is taken at the start the global timeout
        counts from; a run nothing stamped (a test driving one reader) takes it
        at its first read.
        """
        age = run.__dict__.get("_run_start_age")
        if fresh or not isinstance(age, cls):
            age = cls(run.started_at)
            run.__dict__["_run_start_age"] = age
        return age


# Named by the path callers import them from, which is what tracebacks and error
# chains render as ``module.qualname``.
_MonotonicAge.__module__ = "kiro_crew.taskrunner"


async def _execute_tasks(self: TaskRunner, run: Project, history_key: str) -> None:
    if self._require_verification and not (self._auto_test and self._test_cmd):
        # No step could ever verify: stop before any agent turn edits the tree.
        run.status = "failed"
        run.error = "taskrunner.require_verification is on but no test command will run"
        await self._notify("\U0001f9ea Verification required", run.error, run=run)
        return
    # Bound once per execution: a config reload adopted at a LATER run's
    # entry (``_refresh_from_config``) must not resize this run's groups.
    max_parallel_steps = self._max_parallel_steps
    # The run's age as the watchdog reads it (``_watchdog_loop``), taken where
    # ``started_at`` was stamped, so the two global-timeout checks cannot disagree.
    run_age = _MonotonicAge.of_run_start(run)
    pending = [t for t in run.tasks if t.status == TaskStatus.PENDING]
    already_done = {
        t.index for t in run.tasks if t.status in (TaskStatus.PASSED, TaskStatus.SKIPPED)
    }
    groups = group_parallel_tasks(pending, already_done)
    for group in groups:
        if run.status != "running" or shutdown_event.is_set():
            if shutdown_event.is_set():
                if run.status == "pausing":
                    run.status = "paused"
                else:
                    run.status = "cancelled"
                    run.error = "Shutdown signal received"
            break
        if self._global_timeout > 0 and run_age.age(run.started_at) >= self._global_timeout:
            run.status = "failed"
            run.error = f"Global timeout ({int(self._global_timeout)}s) exceeded"
            await self._notify("\u23f1\ufe0f Task timed out", run.error, run=run)
            break
        if self._token_budget > 0 and run.tokens_used >= self._token_budget:
            run.status = "failed"
            run.error = f"Token budget exhausted ({run.tokens_used}/{self._token_budget})"
            await self._notify("\U0001f4b0 Token budget exceeded", run.error, run=run)
            break
        resolved = [next((t for t in run.tasks if t.index == ref.index), ref) for ref in group]
        if len(resolved) == 1:
            task = resolved[0]
            sk = f"{_SESSION_PREFIX}:{run.task_id}:task{task.index}"
            try:
                success = await self._execute_single_task(run, task, history_key, session_key=sk)
            finally:
                try:
                    await asyncio.shield(self._sessions.reset(sk))
                except (asyncio.CancelledError, Exception):
                    pass
            failed_task = None if success else task
        else:
            titles = ", ".join(t.title for t in resolved)
            await self._notify(
                "\u26a1 Parallel group", f"Running {len(resolved)} tasks: {titles}", run=run
            )
            # Bound concurrency with a semaphore sized by the configurable
            # `taskrunner.max_parallel_steps` knob (bound per run above),
            # not a hardcoded batch size. All ready tasks are dispatched at once
            # and the semaphore caps how many run simultaneously, so a slow task
            # never stalls a whole fixed-size batch. The knob is the single
            # place to lift concurrency (capped by the memory-sized ceiling).
            results: list[bool | BaseException] = []
            sem = asyncio.Semaphore(max_parallel_steps)

            async def _run_bounded(t: Task) -> bool:
                async with sem:
                    return await self._execute_single_task(
                        run,
                        t,
                        history_key,
                        session_key=f"{_SESSION_PREFIX}:{run.task_id}:task{t.index}",
                    )

            try:
                results = await asyncio.gather(  # type: ignore[assignment]
                    *(_run_bounded(t) for t in resolved),
                    return_exceptions=True,
                )
            finally:
                # Reset sessions even if CancelledError interrupts the gather
                for t in resolved:
                    try:
                        await asyncio.shield(
                            self._sessions.reset(f"{_SESSION_PREFIX}:{run.task_id}:task{t.index}")
                        )
                    except (asyncio.CancelledError, Exception):
                        pass
            failed_task = None
            for task, result in zip(resolved, results):
                if isinstance(result, Exception) or not result:
                    failed_task = task
                    break
        if failed_task is not None:
            revised = await self._try_replan(run, failed_task)
            if not revised and run.status == "running":
                run.status = "failed"
                clean_err, _ = redact_exfiltration_urls(failed_task.error or "")
                clean_err, _ = redact_credentials(clean_err)
                run.error = f"Task {failed_task.index} failed: {clean_err}"
            return
        for t in resolved:
            if t.result and len(t.result) > _RESULT_MEM_CAP:
                t.result = t.result[:_RESULT_MEM_CAP]
        await self._apersist_runs()  # persist after each group so crash recovery preserves progress


async def _build_task_prompt(self: TaskRunner, run: Project, task: Task, attempt: int = 1) -> str:
    """Delegate to standalone build_task_prompt for backward compat."""
    work_dir = Path(run.work_dir) if run.work_dir else Path.cwd()
    return await build_task_prompt(run, task, attempt, work_dir)


async def self_review(self: TaskRunner, run: Project, task: Task, session_key: str = "") -> bool:
    """Delegate to standalone self_review for backward compat."""
    return await self_review_fn(
        run, task, self._sessions, self._agent, session_key=session_key, ctx=self._ctx
    )


async def _execute_single_task(
    self: TaskRunner,
    run: Project,
    task: Task,
    history_key: str = "",
    session_key: str = "",
) -> bool:
    service = self._workflow_service
    # Admission FIRST: the step holds no session and no slot until the
    # lane grants one, so a queued step costs a row and a future, nothing
    # else. A cancel that lands while it waits is settled by the ADMISSION
    # (``RunnerAdmission.admit``), not here: ``CancelledError`` is a
    # BaseException and passes this arm, which catches only the row this
    # incarnation finds already ended.
    #
    # A REFUSED admission is a FAILED STEP, in band. Both refusals reach
    # here -- the store would not accept or claim the row (a fenced
    # ``starting`` write, an outage, a row another owner holds) and the
    # lane is held end to end by this step's own ancestors
    # (``RunnerLaneSelfBlocked``, a ``RunnerAdmissionRefused`` subclass) --
    # and raising out of one step would unwind the whole accepted RUN past
    # ``_try_replan``, which is the runner's answer to a step that did not
    # land. The parallel branch already gets that answer, because
    # ``gather(return_exceptions=True)`` turns the same raise into a failed
    # step; the sequential branch has no such net, so the verdict is
    # returned here and both branches route through one path.
    if self._task_admission is None:
        handle = None
    else:
        from kiro_crew.taskq.adapters import runner as _runner_adapter

        try:
            handle = await self._taskq_admit_step(run, task)
        except _runner_adapter.RunnerTaskCancelled as exc:
            task.status = TaskStatus.FAILED
            task.error = f"step cancelled before it started: {exc}"
            task.finished_at = time.time()
            return False
        except _runner_adapter.RunnerAdmissionRefused as exc:
            task.status = TaskStatus.FAILED
            task.error = f"the durable queue refused the step: {exc}"
            task.finished_at = time.time()
            return False
    if handle is not None and not await handle.running_async(
        {"index": task.index, "title": task.title[:120]}
    ):
        # PERSIST BEFORE PUBLISH: ``admit`` committed ``starting`` under this
        # generation one statement ago, so a refused ``starting -> running``
        # is a newer owner or a store outage, never a forbidden edge. The
        # step body does not run under it: a row left ``starting`` reaches no
        # WAITING state, so the first dependency or input wait the step needs
        # could not persist, and a fenced row means another incarnation owns
        # the work. The terminal write below is fenced the same way -- it
        # commits for the outage and is refused for the newer owner, which is
        # the row that owner already ended.
        task.status = TaskStatus.FAILED
        task.error = "the durable row did not take the running mark; the step did not run"
        task.finished_at = time.time()
        await handle.fail_async(task.error)
        return False
    if service is not None and run.workflow_run_id:
        try:
            await service.step(run.workflow_run_id, task.index, task.title, status="running")
        except Exception:
            logger.debug("TaskRunner workflow step start publication failed", exc_info=True)
    success = False
    try:
        success = await execute_single_task(
            run=run,
            task=task,
            history_key=history_key,
            sessions=self._sessions,
            ctx=self._ctx,
            agent=self._agent,
            on_notify=self._notify,
            on_approval=self._on_approval,
            on_tool_approval=self._on_tool_approval,
            auto_test=self._auto_test,
            test_cmd=self._test_cmd,
            # Run-scoped workspace wins over the runner default: a run whose
            # workspace_dir selected project B must EXECUTE against B, not the
            # runner's startup dir A (planning already used run.work_dir —
            # executing elsewhere edits/tests the wrong project).
            # _build_task_prompt above prefers run.work_dir too, but falls back
            # to the process directory rather than the runner default.
            work_dir=Path(run.work_dir) if run.work_dir else self._work_dir,
            log_task_fn=self._log_task,
            extract_lesson_fn=self._extract_lesson,
            session_key=session_key,
            require_verification=self._require_verification,
            **({"taskq": handle} if handle is not None else {}),
        )
    except asyncio.CancelledError:
        # The SYNCHRONOUS write on both exceptional arms, deliberately: an
        # ``await`` here can be interrupted before the terminal write is
        # submitted, and a dropped one leaves the step row active for the
        # next boot's reconciler to re-dispatch.
        if handle is not None:
            handle.cancel("step cancelled")
        raise
    except BaseException as exc:
        if handle is not None:
            handle.fail(f"{type(exc).__name__}: {exc}"[:500])
        raise
    else:
        if handle is not None:
            if success:
                await handle.done_async()
            elif run.status == "paused":
                await handle.cancel_async(task.error or "run paused")
            else:
                await handle.fail_async(task.error or "step failed")
    if service is not None and run.workflow_run_id:
        try:
            await service.step(
                run.workflow_run_id,
                task.index,
                task.title,
                status="finished" if success else "failed",
                result=task.result[:_WORKFLOW_RESULT_SUMMARY_CAP],
                error=task.error,
            )
        except Exception:
            logger.debug("TaskRunner workflow step finish publication failed", exc_info=True)
    return success


async def _try_replan(self: TaskRunner, run: Project, failed_task: Task) -> bool:
    if run.replan_count >= _MAX_REPLAN:
        run.status = "failed"
        clean_err, _ = redact_exfiltration_urls(failed_task.error or "")
        clean_err, _ = redact_credentials(clean_err)
        run.error = f"Task {failed_task.index} failed: {clean_err}"
        return False
    if len(run.tasks) >= _MAX_TOTAL_TASKS:
        run.status = "failed"
        run.error = f"Task limit reached ({_MAX_TOTAL_TASKS})"
        return False
    run.replan_count += 1
    err_preview = failed_task.error[:200]
    await self._notify(
        f"\U0001f504 Re-planning (attempt {run.replan_count}/{_MAX_REPLAN})",
        f"Task '{failed_task.title}' failed: {err_preview}",
        run=run,
    )
    completed = [t for t in run.tasks if t.status == TaskStatus.PASSED]
    completed_summary = "\n".join(f"- \u2705 {t.title}" for t in completed)
    memory_ctx = ""
    if run.branch_name:
        try:
            memory_ctx = await git_coord.get_state_summary(run)
        except Exception:
            pass
    if not memory_ctx:
        memory_ctx = run.memory.summary()
    err_detail = failed_task.error[:300]
    # A failed step whose prompt may already have run (Task.resume_hint) must
    # not come back as fresh work: the new plan starts by inspecting state.
    may_have_run = (
        "\n  The failed task's last attempt may already have run part of its "
        "work: plan a first step that inspects the current state, and do not "
        "restate that work as new.\n"
        if failed_task.resume_hint
        else ""
    )
    replan_spec = (
        "You are a planning agent. A task in the pipeline failed.\n"
        "Re-plan ONLY the remaining work. Do not repeat completed tasks.\n"
        "Address the failure cause in your new plan.\n\n"
        f"## Original Specification\n\n{run.spec_content}\n\n"
        f"## Completed Tasks\n{completed_summary}\n\n"
        f"## Failed Task\n- \u274c {failed_task.title}: {err_detail}\n{may_have_run}\n"
        f"{memory_ctx}\n\nRe-plan the REMAINING work."
    )
    new_tasks = await self._decompose(replan_spec, run.work_dir, run.task_id)
    if not new_tasks:
        run.status = "failed"
        run.error = f"Re-plan failed after task {failed_task.index}"
        return False
    base_idx = len(run.tasks)
    for i, task in enumerate(new_tasks, 1):
        task.depends_on = [d + base_idx for d in task.depends_on]
        task.index = base_idx + i
    run.tasks.extend(new_tasks)
    await self._workflow_set_plan(run, modified=True)
    task_list = "\n".join(f"  {t.index}. {t.title}" for t in new_tasks)
    await self._notify(
        "\U0001f4cb Revised plan", f"{len(new_tasks)} new task(s):\n{task_list}", run=run
    )
    history_key = await self._bound_history_key(run, f"taskrunner:run:{Path(run.spec_path).stem}")
    for task in new_tasks:
        if run.status != "running" or shutdown_event.is_set():
            break
        if self._token_budget > 0 and run.tokens_used >= self._token_budget:
            run.status = "failed"
            run.error = f"Token budget exhausted ({run.tokens_used}/{self._token_budget})"
            return False
        sk = f"{_SESSION_PREFIX}:{run.task_id}:task{task.index}"
        try:
            success = await self._execute_single_task(
                run,
                task,
                history_key,
                session_key=sk,
            )
        finally:
            try:
                await asyncio.shield(self._sessions.reset(sk))
            except (asyncio.CancelledError, Exception):
                pass
        if not success:
            return await self._try_replan(run, task)
    return True


async def _watchdog_loop(self: TaskRunner, run: Project) -> None:
    stall_notified = False
    dead_process_count = 0
    dead_process_key: str | None = None
    # The same age the executor's own global-timeout check reads.
    started = _MonotonicAge.of_run_start(run)
    progress = _MonotonicAge(run.last_task_time)
    while run.status == "running":
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            return
        if run.status != "running":
            return

        # ── Heartbeat: check if the current task's ACP process is alive ──
        step_key = f"{_SESSION_PREFIX}:{run.task_id}:task{run.current_task}"
        if step_key != dead_process_key:
            dead_process_count = 0
            dead_process_key = step_key
        try:
            alive = await self._sessions.is_provider_alive(step_key)
            if alive is not None and not alive:
                dead_process_count += 1
                logger.warning(
                    "Watchdog: ACP process dead for task %d (count %d/%d)",
                    run.current_task,
                    dead_process_count,
                    _DEAD_THRESHOLD,
                )
                if dead_process_count >= _DEAD_THRESHOLD:
                    await self._notify(
                        "💀 Watchdog: ACP process died",
                        f"Task {run.current_task} sub-agent is not running. "
                        "Resetting session to trigger recovery.",
                        run=run,
                    )
                    try:
                        await self._sessions.reset(step_key)
                    except Exception:
                        logger.debug("Watchdog heartbeat reset failed", exc_info=True)
                    dead_process_count = 0
            else:
                dead_process_count = 0
        except Exception:
            logger.debug("Watchdog heartbeat check failed", exc_info=True)
            dead_process_count = 0

        elapsed = started.age(run.started_at)
        if self._global_timeout > 0 and elapsed >= self._global_timeout:
            logger.warning("Watchdog: global timeout reached (%.0fs)", elapsed)
            return
        since_last = progress.age(run.last_task_time)
        if since_last >= _STALL_CANCEL_TIMEOUT and run.task_id not in self._stall_cancelled_ids:
            self._stall_cancelled_ids.add(run.task_id)
            logger.warning("Watchdog: stall cancel after %d min", int(since_last / 60))
            await self._notify(
                "\U0001f527 Watchdog: cancelling stalled task",
                f"No progress in {int(since_last / 60)} min",
                run=run,
            )
            try:
                await self._sessions.reset(step_key)
            except Exception:
                logger.debug("Watchdog reset failed", exc_info=True)
        elif since_last >= _STALL_TIMEOUT and not stall_notified:
            stall_notified = True
            logger.warning("Watchdog: no task progress in %d min", int(since_last / 60))
            await self._notify(
                "\u26a0\ufe0f Task may be stalled",
                f"No task completed in {int(since_last / 60)} min. Current: task {run.current_task}",
                run=run,
            )
        if since_last < _STALL_TIMEOUT:
            stall_notified = False
            self._stall_cancelled_ids.discard(run.task_id)

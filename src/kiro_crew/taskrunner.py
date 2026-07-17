"""Autonomous task runner — orchestrator module.

Delegates to: task_models, task_planner, task_executor, task_reporter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from kiro_crew import git_coord, shutdown_event
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.llm_helpers import stream_and_collect_json
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.task_executor import (
    build_task_prompt,
    execute_single_task,
    run_tests,
)
from kiro_crew.task_executor import self_review as self_review_fn

# ── Re-exports (preserve public API) ──
from kiro_crew.task_models import (  # noqa: F401
    DEFAULT_TOKEN_BUDGET,
    MAX_RECOVERIES,
    MAX_REPLAN,
    MAX_RETRIES,
    MAX_TOTAL_TASKS,
    PROGRESS_FILE,
    SESSION_PREFIX,
    STALL_CANCEL_TIMEOUT,
    STALL_TIMEOUT,
    NotifyCallback,
    Project,
    Task,
    TaskStatus,
    WorkingMemory,
)
from kiro_crew.task_planner import (
    auto_name,
    decompose,
    decompose_yaml,
    group_parallel_tasks,
    normalize_cross_group_deps,
    parse_tasks,
)
from kiro_crew.task_planner import plan_to_chat_context as _planner_plan_to_chat_context
from kiro_crew.task_planner import (
    update_plan_tasks,
)
from kiro_crew.task_reporter import (
    build_resume_context,
    build_status,
    format_completion_summary,
    load_checkpoint,
    notify,
    save_progress,
)

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog, HistoryConsolidator
    from kiro_crew.learn import LessonStore
    from kiro_crew.providers.base import LLMEvent
    from kiro_crew.session import SessionManager

from kiro_crew.learn import Lesson

logger = logging.getLogger(__name__)

# ── Backward-compat re-exports ──
Step = Task
StepStatus = TaskStatus
TaskRun = Project

_MAX_REPLAN = MAX_REPLAN
_MAX_TOTAL_TASKS = MAX_TOTAL_TASKS
_MAX_PARALLEL_TASKS = 3  # max tasks executing concurrently in a parallel group
_MAX_CONCURRENT_TASKS = 3  # max simultaneous task runs
_SESSION_PREFIX = SESSION_PREFIX
_STALL_TIMEOUT = STALL_TIMEOUT
_STALL_CANCEL_TIMEOUT = STALL_CANCEL_TIMEOUT
_DEFAULT_TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET
_HEARTBEAT_INTERVAL = 30  # watchdog checks process liveness every 30s
_DEAD_THRESHOLD = 2  # consecutive dead checks before fail-fast reset
_RESULT_MEM_CAP = 4000  # truncate task.result in memory after step completes


def _decompose_yaml_with_audit(yaml_content: str, task_id: str) -> list[Task]:
    """Decompose YAML with SEL audit logging."""
    try:
        tasks = decompose_yaml(yaml_content)
        sel().log_tool_invocation(
            session_key="dashboard", source="taskrunner",
            tool_name="decompose_yaml", outcome="ok",
            metadata={"task_id": task_id, "task_count": len(tasks)},
        )
        return tasks
    except Exception as exc:
        sel().log_tool_invocation(
            session_key="dashboard", source="taskrunner",
            tool_name="decompose_yaml", outcome="error",
            metadata={"task_id": task_id, "error": str(exc)},
        )
        raise


class TaskRunner:
    """Autonomous spec executor — decomposes and runs tasks."""

    def __init__(
        self,
        sessions: SessionManager,
        context_builder: ContextBuilder | None = None,
        on_notify: NotifyCallback | None = None,
        auto_test: bool = True,
        auto_commit: bool = False,
        work_dir: Path | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        lesson_store: LessonStore | None = None,
        fresh: bool = False,
        global_timeout: float = 0.0,
        token_budget: int = _DEFAULT_TOKEN_BUDGET,
        on_approval: Callable[[Task], Awaitable[bool]] | None = None,
        max_parallel_steps: int = _MAX_PARALLEL_TASKS,
    ) -> None:
        self._sessions = sessions
        self._ctx = context_builder
        self._on_notify = on_notify
        self._auto_test = auto_test
        self._auto_commit = auto_commit
        self._work_dir = work_dir or Path.cwd()
        self._test_cmd: list[str] | None = None
        self._conversation_log = conversation_log
        self._consolidator = consolidator
        self._lesson_store = lesson_store
        self._fresh = fresh
        self._global_timeout = global_timeout
        self._token_budget = token_budget
        self._on_approval = on_approval
        self._max_parallel_steps = max(1, max_parallel_steps)
        self._runs: dict[str, Project] = {}
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._plan_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None
        self._stall_cancelled_ids: set[str] = set()
        self._agent: str = ""
        self._load_runs()

    @property
    def current_run(self) -> Project | None:
        if not self._runs:
            return None
        return list(self._runs.values())[-1]

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks.values())

    @staticmethod
    def _auto_name(spec_content: str, spec_path: str = "") -> str:
        return auto_name(spec_content, spec_path)

    def _resolve_task(self, ref: str) -> Project | None:
        if ref in self._runs:
            return self._runs[ref]
        matches = [r for r in self._runs.values() if r.name == ref]
        return matches[-1] if matches else None

    @staticmethod
    def _normalize_cross_group_deps(tasks: list[Task]) -> list[Task]:
        return normalize_cross_group_deps(tasks)

    @staticmethod
    def _group_parallel_tasks(
        tasks: list[Task],
        completed_indices: set[int] | None = None,
    ) -> list[list[Task]]:
        return group_parallel_tasks(tasks, completed_indices)

    def _parse_tasks(self, text: str) -> list[Task]:
        return parse_tasks(text)

    # ── Plan Mode ──

    async def plan(
        self,
        input_text: str = "",
        source: str = "text",
        spec_path: str = "",
        agent: str = "",
    ) -> Project:
        self._agent = agent
        if source == "file":
            p = Path(spec_path)
            if not p.exists():
                raise FileNotFoundError(f"Spec not found: {spec_path}")
            content = p.read_text(encoding="utf-8").strip()
            if not content:
                raise ValueError("Spec file is empty")
            decompose_input = spec_content = original_input = content
        elif source in ("spec", "yaml"):
            if not input_text.strip():
                raise ValueError("Input text is empty")
            decompose_input = spec_content = original_input = input_text
        else:
            if not input_text.strip():
                raise ValueError("Input text is empty")
            decompose_input = original_input = input_text
            spec_content = ""

        task_id = f"plan_{int(time.time())}"
        task_dir = self._work_dir / f"plan_{task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        run = Project(
            spec_path=spec_path or "",
            spec_content=spec_content,
            original_input=original_input,
            source=source,
            status="planned",
            task_id=task_id,
            work_dir=str(task_dir),
            name=auto_name(spec_content or original_input, spec_path),
        )
        if source == "yaml":
            run.tasks = _decompose_yaml_with_audit(decompose_input, task_id)
        else:
            try:
                run.tasks = await asyncio.wait_for(
                    self._decompose(decompose_input, run.work_dir, task_id),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                raise ValueError("Planning timed out. Try simplifying.")
            except asyncio.CancelledError:
                raise ValueError("Planning was cancelled.")
        if not run.tasks:
            raise ValueError("Could not generate a plan. Try rephrasing.")
        self._runs[task_id] = run
        self._persist_runs()
        return run

    def cancel_plan(self) -> None:
        if self._plan_task and not self._plan_task.done():
            self._plan_task.cancel()

    def update_plan(self, task_id: str, tasks: list[dict]) -> Project:
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        if run.status in ("running", "cancelling"):
            raise ValueError(f"Cannot update plan while {run.status}")
        result = update_plan_tasks(run, tasks)
        self._persist_runs()
        return result

    def update_task(self, task_id: str, index: int, updates: dict) -> dict:
        """Update a single PENDING task in-place without resetting the run."""
        run = self._resolve_task(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        task = next((t for t in run.tasks if t.index == index), None)
        if not task:
            raise ValueError(f"Task {index} not found")
        if task.status != TaskStatus.PENDING:
            raise ValueError(f"Can only edit pending tasks (status={task.status.value})")
        # Validate all fields before applying any mutations
        changes: dict = {}
        if "title" in updates:
            t = updates["title"]
            if not isinstance(t, str) or not t.strip():
                raise ValueError("title must be a non-empty string")
            if len(t) > 500:
                raise ValueError("title too long")
            changes["title"] = t.strip()
        if "description" in updates:
            d = updates["description"]
            if not isinstance(d, str):
                raise ValueError("description must be a string")
            if len(d) > 5000:
                raise ValueError("description too long")
            changes["description"] = d
        if "depends_on" in updates:
            deps = updates["depends_on"]
            if not isinstance(deps, list):
                raise ValueError("depends_on must be a list")
            changes["depends_on"] = [int(d) for d in deps if isinstance(d, (int, float)) and 0 < int(d) < index]
        if "requires_approval" in updates:
            changes["requires_approval"] = bool(updates["requires_approval"])
        if "force_approval" in updates:
            # force_approval is intentionally mutable — the user who sets it is the same user
            # who can remove it. There's no cross-principal security boundary; it's a personal
            # workflow gate, not an access control mechanism. The gate re-triggers on resume
            # regardless, so removing it is an explicit user decision.
            changes["force_approval"] = bool(updates["force_approval"])
        # Apply atomically
        for key, value in changes.items():
            setattr(task, key, value)
        self._persist_runs()
        return {"index": task.index, "title": task.title, "description": task.description, "depends_on": task.depends_on, "requires_approval": task.requires_approval, "force_approval": task.force_approval}

    def execute_plan(self, task_id: str, agent: str = "", fresh: bool = False) -> str:
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        restartable = {"planned", "paused", "cancelled", "failed"}
        if run.status not in restartable:
            raise ValueError(f"Run {task_id} is not in a startable state (status={run.status})")

        # Guard: limit concurrent running tasks — check BEFORE mutating state
        active = sum(1 for t in self._tasks.values() if not t.done())
        if active >= _MAX_CONCURRENT_TASKS:
            raise ValueError(
                f"Too many concurrent tasks ({active}/{_MAX_CONCURRENT_TASKS}). "
                "Cancel or wait for a running task to finish."
            )

        if run.status in ("paused", "cancelled", "failed"):
            for t in run.tasks:
                if fresh or t.status not in (TaskStatus.PASSED, TaskStatus.SKIPPED):
                    t.status = TaskStatus.PENDING
                    t.error = ""
                    t.result = ""
                    t.attempts = 0
            run.error = ""
            run.replan_count = 0
            run.status = "planned"
            self._persist_runs()

        self._agent = agent
        history_key = f"taskrunner:run:{task_id}"

        async def _execute() -> None:
            watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
            try:
                run.status = "running"
                run.started_at = run.last_task_time = time.time()
                self._persist_runs()  # persist immediately so crash recovery works
                try:
                    await git_coord.init_workspace(run)
                except Exception:
                    logger.debug("Git init failed for plan execution", exc_info=True)
                save_progress(run)
                task_list = "\n".join(f"  {t.index}. {t.title}" for t in run.tasks)
                await self._notify(
                    "\U0001f680 Executing plan",
                    f"{len(run.tasks)} task(s):\n{task_list}",
                    run=run,
                )
                watchdog_task = asyncio.create_task(self._watchdog_loop(run))
                await self._execute_tasks(run, history_key)
                if run.status == "running":
                    run.status = "completed"
                    await self._notify(
                        "\u2705 Task completed",
                        format_completion_summary(run),
                        run=run,
                    )
            except asyncio.CancelledError:
                if run.status != "pausing":
                    run.status = "cancelling"
                self._reset_incomplete_tasks(run)
            except Exception as exc:
                logger.exception("Plan execution error")
                run.status = "failed"
                run.error = str(exc)
                await self._notify("\u274c Task error", str(exc), run=run)
            finally:
                try:
                    await asyncio.shield(self._cleanup_run_sessions(run))
                except asyncio.CancelledError:
                    pass  # shield was cancelled but cleanup completed
                # Finalize cancel status after cleanup
                if run.status in ("cancelling", "pausing"):
                    run.status = "paused" if run.status == "pausing" else "cancelled"
                run.finished_at = time.time()
                save_progress(run)
                self._persist_runs()
                if run.branch_name:
                    try:
                        await git_coord.finalize(run)
                    except Exception:
                        logger.debug("Git finalize failed", exc_info=True)
                if watchdog_task and not watchdog_task.done():
                    watchdog_task.cancel()
                if self._consolidator:
                    self._consolidator.maybe_consolidate(history_key)
                self._tasks.pop(task_id, None)

        self._tasks[task_id] = asyncio.create_task(_execute())
        return task_id

    def plan_to_chat_context(self, task_id: str) -> str:
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        return _planner_plan_to_chat_context(run)

    # ── Core Execution ──

    async def run(
        self, spec_path: str | Path, task_id: str = "", name: str = "", source: str = ""
    ) -> Project:
        spec_path = Path(spec_path)
        if not spec_path.exists():
            raise FileNotFoundError(f"Spec not found: {spec_path}")
        spec_content = spec_path.read_text(encoding="utf-8").strip()
        if not spec_content:
            raise ValueError("Spec file is empty")
        if not task_id:
            task_id = f"{spec_path.stem}_{int(time.time())}"
        task_dir = self._work_dir / spec_path.stem
        task_dir.mkdir(parents=True, exist_ok=True)
        run = Project(
            spec_path=str(spec_path),
            spec_content=spec_content,
            started_at=time.time(),
            last_task_time=time.time(),
            status="running",
            source=source,
        )
        run.task_id = task_id
        run.name = name or auto_name(spec_content, str(spec_path))
        run.work_dir = str(task_dir)
        self._runs[task_id] = run
        self._persist_runs()  # persist immediately so crash recovery works
        watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
        history_key = f"taskrunner:run:{spec_path.stem}"
        try:
            await self._notify("\U0001f680 Task started", f"Spec: `{spec_path.name}`", run=run)
            if source == "yaml":
                run.tasks = _decompose_yaml_with_audit(spec_content, task_id)
            elif not source and spec_path.suffix in (".yaml", ".yml"):
                try:
                    run.tasks = _decompose_yaml_with_audit(spec_content, task_id)
                except (ValueError, KeyError):
                    logger.warning("YAML spec %s is not in workflow format; falling back to LLM decomposition", spec_path.name)
                    run.tasks = await self._decompose(spec_content, run.work_dir, task_id)
            else:
                run.tasks = await self._decompose(spec_content, run.work_dir, task_id)
            if not run.tasks:
                run.status = "failed"
                run.error = "Failed to decompose spec into tasks"
                await self._notify("\u274c Task failed", run.error, run=run)
                return run
            self._persist_runs()  # persist tasks so resume works after crash
            try:
                await git_coord.init_workspace(run)
            except Exception as exc:
                logger.warning("Git coordination init failed: %s", exc)
            checkpoint = load_checkpoint(spec_path)
            skipped = 0
            if checkpoint and not self._fresh:
                for task in run.tasks:
                    if task.title.lower().strip() in checkpoint:
                        task.status = TaskStatus.PASSED
                        skipped += 1
                    else:
                        break
                if skipped:
                    resume_ctx = build_resume_context(
                        [t for t in run.tasks if t.status == TaskStatus.PASSED]
                    )
                    logger.info("Resuming: %d/%d tasks done", skipped, len(run.tasks))
                    await self._notify(
                        "\U0001f504 Resuming", f"{skipped}/{len(run.tasks)} done", run=run
                    )
                    pending = [t for t in run.tasks if t.status == TaskStatus.PENDING]
                    if pending:
                        pending[0].description = resume_ctx + "\n\n" + pending[0].description
            save_progress(run)
            task_list = "\n".join(f"  {t.index}. {t.title}" for t in run.tasks)
            await self._notify(
                "\U0001f4cb Plan ready", f"{len(run.tasks)} task(s):\n{task_list}", run=run
            )
            watchdog_task = asyncio.create_task(self._watchdog_loop(run))
            await self._execute_tasks(run, history_key)
            if run.status == "running":
                run.status = "completed"
                await self._notify("\u2705 Task completed", format_completion_summary(run), run=run)
        except asyncio.CancelledError:
            if run.status != "pausing":
                run.status = "cancelling"
            self._reset_incomplete_tasks(run)
        except Exception as exc:
            logger.exception("Task runner error")
            run.status = "failed"
            run.error = str(exc)
            await self._notify("\u274c Task error", str(exc), run=run)
        finally:
            try:
                await asyncio.shield(self._cleanup_run_sessions(run))
            except asyncio.CancelledError:
                pass  # shield was cancelled but cleanup completed
            if run.status in ("cancelling", "pausing"):
                run.status = "paused" if run.status == "pausing" else "cancelled"
            run.finished_at = time.time()
            save_progress(run)
            self._persist_runs()
            if run.branch_name:
                try:
                    await git_coord.finalize(run)
                except Exception:
                    logger.debug("Git finalize failed", exc_info=True)
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
            if self._consolidator:
                self._consolidator.maybe_consolidate(history_key)
        return run

    async def _execute_tasks(self, run: Project, history_key: str) -> None:
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
            if self._global_timeout > 0 and (time.time() - run.started_at) >= self._global_timeout:
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
                    success = await self._execute_single_task(
                        run, task, history_key, session_key=sk
                    )
                finally:
                    try:
                        await asyncio.shield(self._sessions.reset(sk))
                    except (asyncio.CancelledError, Exception):
                        pass
                if not success:
                    revised = await self._try_replan(run, task)
                    if not revised and run.status == "running":
                        run.status = "failed"
                        clean_err, _ = redact_exfiltration_urls(task.error or "")
                        clean_err, _ = redact_credentials(clean_err)
                        run.error = f"Task {task.index} failed: {clean_err}"
                    return
                if task.result and len(task.result) > _RESULT_MEM_CAP:
                    task.result = task.result[:_RESULT_MEM_CAP]
                self._persist_runs()  # persist after each task so crash recovery preserves progress
            else:
                titles = ", ".join(t.title for t in resolved)
                await self._notify(
                    "\u26a1 Parallel group", f"Running {len(resolved)} tasks: {titles}", run=run
                )
                # Batch large groups to avoid resource exhaustion
                results: list[bool | BaseException] = []
                try:
                    for batch_start in range(0, len(resolved), _MAX_PARALLEL_TASKS):
                        batch = resolved[batch_start : batch_start + _MAX_PARALLEL_TASKS]
                        batch_results = await asyncio.gather(
                            *(
                                self._execute_single_task(
                                    run,
                                    t,
                                    history_key,
                                    session_key=f"{_SESSION_PREFIX}:{run.task_id}:task{t.index}",
                                )
                                for t in batch
                            ),
                            return_exceptions=True,
                        )
                        results.extend(batch_results)
                finally:
                    # Reset sessions even if CancelledError interrupts the batch loop
                    for t in resolved:
                        try:
                            await asyncio.shield(
                                self._sessions.reset(
                                    f"{_SESSION_PREFIX}:{run.task_id}:task{t.index}"
                                )
                            )
                        except (asyncio.CancelledError, Exception):
                            pass
                failed_task = None
                for task, result in zip(resolved, results):
                    if isinstance(result, Exception) or not result:
                        failed_task = task
                        break
                if failed_task:
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
                self._persist_runs()  # persist after parallel group so crash recovery preserves progress

    async def _build_task_prompt(self, run: Project, task: Task, attempt: int = 1) -> str:
        """Delegate to standalone build_task_prompt for backward compat."""
        work_dir = Path(run.work_dir) if run.work_dir else Path.cwd()
        return await build_task_prompt(run, task, attempt, work_dir)

    async def self_review(self, run: Project, task: Task, session_key: str = "") -> bool:
        """Delegate to standalone self_review for backward compat."""
        return await self_review_fn(run, task, self._sessions, self._agent, session_key=session_key)

    async def _execute_single_task(
        self,
        run: Project,
        task: Task,
        history_key: str = "",
        session_key: str = "",
    ) -> bool:
        return await execute_single_task(
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
            work_dir=self._work_dir,
            log_task_fn=self._log_task,
            extract_lesson_fn=self._extract_lesson,
            session_key=session_key,
        )

    async def _try_replan(self, run: Project, failed_task: Task) -> bool:
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
        replan_spec = (
            "You are a planning agent. A task in the pipeline failed.\n"
            "Re-plan ONLY the remaining work. Do not repeat completed tasks.\n"
            "Address the failure cause in your new plan.\n\n"
            f"## Original Specification\n\n{run.spec_content}\n\n"
            f"## Completed Tasks\n{completed_summary}\n\n"
            f"## Failed Task\n- \u274c {failed_task.title}: {err_detail}\n\n"
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
        task_list = "\n".join(f"  {t.index}. {t.title}" for t in new_tasks)
        await self._notify(
            "\U0001f4cb Revised plan", f"{len(new_tasks)} new task(s):\n{task_list}", run=run
        )
        history_key = f"taskrunner:run:{Path(run.spec_path).stem}"
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

    def start_background(
        self, spec_path: str | Path, agent: str = "", name: str = "", source: str = ""
    ) -> str:
        active = sum(1 for t in self._tasks.values() if not t.done())
        if active >= _MAX_CONCURRENT_TASKS:
            raise ValueError(
                f"Too many concurrent tasks ({active}/{_MAX_CONCURRENT_TASKS}). "
                "Cancel or wait for a running task to finish."
            )

        completed = [
            tid for tid, r in self._runs.items() if r.status in ("completed", "failed", "cancelled")
        ]
        # Always purge completed cron runs; keep last 10 others
        cron_done = [tid for tid in completed if self._runs[tid].source == "cron"]
        for tid in cron_done:
            self._runs.pop(tid, None)
            self._stall_cancelled_ids.discard(tid)
        other_done = [tid for tid in completed if tid in self._runs]
        for tid in other_done[:-10]:
            self._runs.pop(tid, None)
            self._stall_cancelled_ids.discard(tid)
        task_id = f"{Path(spec_path).stem}_{int(time.time())}"
        self._agent = agent
        try:
            from kiro_crew.hooks import validate_file_path

            safe_sp = validate_file_path(str(spec_path))
            if safe_sp:
                with open(safe_sp, encoding="utf-8") as f:
                    early_content = f.read(4000).strip()
            else:
                early_content = ""
        except Exception:
            early_content = ""
        self._runs[task_id] = Project(
            spec_path=str(spec_path),
            spec_content=early_content,
            task_id=task_id,
            name=name or Path(spec_path).stem,
            status="planning",
            started_at=time.time(),
            source=source,
        )
        self._persist_runs()  # persist planning state for crash recovery

        async def _wrapped() -> None:
            try:
                await self.run(spec_path, task_id=task_id, name=name, source=source)
            except Exception as exc:
                logger.exception("start_background task %s failed", task_id)
                placeholder = self._runs.get(task_id)
                if placeholder and placeholder.status == "planning":
                    placeholder.status = "failed"
                    placeholder.error = str(exc)
                    self._persist_runs()
            finally:
                self._tasks.pop(task_id, None)

        self._tasks[task_id] = asyncio.create_task(_wrapped())
        return task_id

    @staticmethod
    def _reset_incomplete_tasks(run: Project) -> None:
        """Mark in_progress/pending/reviewing tasks as cancelled (or keep pending if pausing)."""
        pausing = run.status == "pausing"
        for task in run.tasks:
            if task.status == TaskStatus.IN_PROGRESS:
                task.status = TaskStatus.PENDING if pausing else TaskStatus.CANCELLED
            elif task.status in (TaskStatus.PENDING, TaskStatus.REVIEWING):
                if not pausing:
                    task.status = TaskStatus.CANCELLED

    async def _cleanup_run_sessions(self, run: Project) -> None:
        """Cancel in-flight ops then kill sessions for a specific run only."""
        prefix = f"{_SESSION_PREFIX}:{run.task_id}:"
        keys = [k for k in list(self._sessions._sessions) if k.startswith(prefix)]
        if not keys:
            return
        logger.info("Cleaning up %d sessions for run %s", len(keys), run.task_id)
        failed_keys: list[str] = []
        for key in keys:
            try:
                await self._sessions.cancel_current(key)
            except Exception:
                logger.debug("cancel_current failed for %s", key, exc_info=True)
        await asyncio.sleep(0.5)
        for key in keys:
            try:
                self._sessions.release(key)
            except Exception:
                pass
            try:
                await self._sessions.reset(key)
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning("reset failed for session %s: %s", key, exc)
                failed_keys.append(key)
        if failed_keys:
            run.error = f"Cancel cleanup failed for {len(failed_keys)} session(s)"
            logger.warning(
                "cleanup_run_sessions: %d session(s) failed to reset for %s",
                len(failed_keys),
                run.task_id,
            )

    def delete_run(self, task_id: str) -> bool:
        run = self._runs.get(task_id)
        if not run:
            return False
        if run.status == "running":
            run.status = "cancelling"
        bg_task = self._tasks.pop(task_id, None)
        if bg_task and not bg_task.done():
            bg_task.cancel()
        self._runs.pop(task_id, None)
        self._stall_cancelled_ids.discard(task_id)
        self._persist_runs()
        try:
            from kiro_crew.sel import sel

            sel().log_tool_invocation(
                session_key="dashboard",
                source="taskrunner",
                tool_name="delete_run",
                outcome="deleted",
                metadata={"task_id": task_id, "status": run.status, "source": run.source},
            )
        except Exception:
            logger.debug("SEL audit failed for delete_run %s", task_id)
        return True

    def cancel(self, task_id: str | None = None) -> None:
        """Cancel running tasks. Sets status to 'cancelling'; the finally block
        in run()/retry_from_task() handles actual cleanup and final status."""
        if task_id:
            matches = [r for r in self._runs.values() if r.name == task_id]
            keys = [r.task_id for r in matches] if matches else [task_id]
            for key in keys:
                run = self._runs.get(key)
                if run and run.status == "running":
                    run.status = "cancelling"
                t = self._tasks.get(key)
                if t and not t.done():
                    t.cancel()
        else:
            for run in self._runs.values():
                if run.status == "running":
                    run.status = "cancelling"
            for t in self._tasks.values():
                if not t.done():
                    t.cancel()

    def pause(self, task_id: str) -> None:
        """Pause a running task. Sets status to 'pausing'; the finally block sets 'paused'."""
        run = self._runs.get(task_id)
        if not run:
            matches = [r for r in self._runs.values() if r.name == task_id]
            if matches:
                run = matches[0]
        if not run or run.status != "running":
            return
        run.status = "pausing"
        t = self._tasks.get(run.task_id)
        if t and not t.done():
            t.cancel()

    def retry_from_task(self, task_id: str, from_task: int, agent: str = "") -> str:
        run = self._resolve_task(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        if run.status == "running":
            raise ValueError("Cannot retry a running task")
        if run.status in ("cancelling", "pausing"):
            raise ValueError("Cannot retry while cancel is in progress")
        for task in run.tasks:
            if task.index >= from_task:
                task.status = TaskStatus.PENDING
                task.error = ""
                task.result = ""
                task.attempts = 0
        run.status = "running"
        run.error = ""
        run.finished_at = 0.0
        run.started_at = run.last_task_time = time.time()
        self._persist_runs()  # persist immediately so crash recovery works
        self._agent = agent
        history_key = f"taskrunner:run:{Path(run.spec_path).stem}"

        async def _retry() -> None:
            watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
            try:
                if run.branch_name and not Path(run.work_dir).exists():
                    try:
                        await git_coord.init_workspace(run)
                    except Exception:
                        logger.debug("Git re-init on retry failed", exc_info=True)
                await self._notify("\U0001f504 Retrying", f"From task {from_task}", run=run)
                watchdog_task = asyncio.create_task(self._watchdog_loop(run))
                await self._execute_tasks(run, history_key)
                if run.status == "running":
                    run.status = "completed"
                    passed = sum(1 for t in run.tasks if t.status == TaskStatus.PASSED)
                    await self._notify(
                        "\u2705 Task completed", f"{passed}/{len(run.tasks)} passed", run=run
                    )
            except asyncio.CancelledError:
                if run.status != "pausing":
                    run.status = "cancelling"
                self._reset_incomplete_tasks(run)
            except Exception as exc:
                logger.exception("Retry failed")
                run.status = "failed"
                run.error = str(exc)
            finally:
                try:
                    await asyncio.shield(self._cleanup_run_sessions(run))
                except asyncio.CancelledError:
                    pass  # shield was cancelled but cleanup completed
                if run.status in ("cancelling", "pausing"):
                    run.status = "paused" if run.status == "pausing" else "cancelled"
                run.finished_at = time.time()
                save_progress(run)
                self._persist_runs()
                if watchdog_task and not watchdog_task.done():
                    watchdog_task.cancel()
                self._tasks.pop(task_id, None)

        self._tasks[task_id] = asyncio.create_task(_retry())
        return task_id

    # ── Decomposition (delegates to task_planner) ──

    async def _decompose(
        self,
        spec: str,
        work_dir: str = "",
        task_id: str = "",
    ) -> list[Task]:
        return await decompose(
            spec,
            self._sessions,
            self._ctx,
            work_dir=work_dir or str(self._work_dir),
            task_id=task_id,
            agent=self._agent,
        )

    # ── Notifications ──

    async def _notify(self, title: str, body: str, run: Project | None = None) -> None:
        await notify(title, body, run=run, callback=self._on_notify)

    # ── History Integration ──

    def _log_task(self, history_key: str, run: Project, task: Task) -> None:
        if not self._conversation_log:
            return
        try:
            spec_name = Path(run.spec_path).name if run.spec_path else run.task_id
            user_msg = f"[Task: {spec_name}] Task {task.index}: {task.title}"
            self._conversation_log.append(history_key, "user", user_msg)
            result_summary = task.result[:2000] if task.result else "Task completed."
            self._conversation_log.append(history_key, "assistant", result_summary)
        except Exception:
            logger.debug("Failed to log task to conversation history", exc_info=True)

    # ── Learn from Failures ──

    async def _extract_lesson(self, task: Task, run: Project | None = None) -> None:
        if not self._lesson_store:
            return
        try:
            prompt = (
                "A task failed after multiple attempts.\n\n"
                f'Task: "{task.title}"\n'
                f'Error: "{task.error[:500]}"\n\n'
                "Extract a concise lesson. Format as JSON:\n"
                '{"rule": "what to always do", '
                '"negative": "what to never do", '
                '"category": "tool"}\n\n'
                "Respond with ONLY valid JSON."
            )
            result = await self._call_llm_for_lesson(prompt)
            if not result or "rule" not in result:
                return
            rule = result["rule"]
            category = result.get("category", "tool")
            negative = result.get("negative")
            if self._consolidator and self._consolidator._vector_store:
                # write_lesson embeds via blocking urllib (Ollama); offload to
                # keep the gateway event loop responsive (same pattern as
                # dashboard/handlers/cron.py api_lessons_create).
                await run_in_embed_pool(
                    self._consolidator._vector_store.write_lesson,
                    rule, category, negative, "task_runner",
                )
            else:
                self._lesson_store.save(
                    Lesson(
                        ts=datetime.now(tz=timezone.utc).isoformat(),
                        rule=rule,
                        category=category,
                        negative=negative,
                    )
                )
            logger.info("Lesson extracted from task %d: %s", task.index, rule)
            if run:
                run.lessons_learned.append(rule)
            await self._notify("\U0001f4dd Lesson learned", rule)
        except Exception:
            logger.debug("Lesson extraction failed", exc_info=True)

    async def _call_llm_for_lesson(self, prompt: str) -> dict | None:
        session_key = BACKGROUND_KEY
        try:
            client, _is_new, _resumed = await self._sessions.get_or_create(
                session_key,
                agent=self._agent or None,
            )
            return await stream_and_collect_json(client, prompt)
        except Exception:
            logger.debug("LLM lesson extraction call failed", exc_info=True)
            return None
        finally:
            self._sessions.release(session_key)
            await self._sessions.recycle_background()

    # ── Task Watchdog ──

    async def _watchdog_loop(self, run: Project) -> None:
        stall_notified = False
        dead_process_count = 0
        dead_process_key: str | None = None
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

            now = time.time()
            elapsed = now - run.started_at
            if self._global_timeout > 0 and elapsed >= self._global_timeout:
                logger.warning("Watchdog: global timeout reached (%.0fs)", elapsed)
                return
            since_last = now - run.last_task_time
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
            if run.last_task_time > now - _STALL_TIMEOUT:
                stall_notified = False
                self._stall_cancelled_ids.discard(run.task_id)

    # ── Test Verification ──

    async def _run_tests(self) -> tuple[bool, str]:
        if not self._test_cmd:
            return True, "no test command configured"
        return await run_tests(self._test_cmd, self._work_dir)

    # ── Runs Persistence ──

    _RUNS_FILE = "runs.json"

    def _runs_path(self) -> Path:
        return self._work_dir / self._RUNS_FILE

    def _persist_runs(self) -> None:
        data = []
        for run in self._runs.values():
            if run.source == "cron":
                continue
            if run.status in ("planning", "planned", "running", "cancelling", "pausing", "paused", "completed", "failed", "cancelled"):
                data.append(
                    {
                        "task_id": run.task_id,
                        "name": run.name,
                        "spec_path": run.spec_path,
                        "status": run.status,
                        "started_at": run.started_at,
                        "finished_at": run.finished_at,
                        "error": run.error,
                        "tokens_used": run.tokens_used,
                        "replan_count": run.replan_count,
                        "work_dir": run.work_dir,
                        "original_input": run.original_input,
                        "source": run.source,
                        "spec_content": run.spec_content,
                        "task_details": [
                            {
                                "index": t.index,
                                "title": t.title,
                                "description": t.description,
                                "depends_on": t.depends_on,
                                "requires_approval": t.requires_approval,
                                "force_approval": t.force_approval,
                                "status": t.status.value,
                                "error": t.error or "",
                                "result": (t.result or "")[:2000],
                                "attempts": t.attempts,
                            }
                            for t in run.tasks
                        ],
                    }
                )
        try:
            self._runs_path().write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            logger.debug("Failed to persist runs", exc_info=True)

    def _load_runs(self) -> None:
        try:
            path = self._runs_path()
            if not path.exists():
                return
            for item in json.loads(path.read_text(encoding="utf-8")):
                tasks = [
                    Task(
                        index=t["index"],
                        title=t["title"],
                        description=t.get("description", ""),
                        status=TaskStatus(t["status"]),
                        error=t.get("error", ""),
                        result=t.get("result", ""),
                        attempts=t.get("attempts", 1),
                        depends_on=t.get("depends_on", []),
                        requires_approval=t.get("requires_approval", False),
                        force_approval=t.get("force_approval", False),
                    )
                    for t in item.get("task_details", item.get("tasks", []))
                ]
                run = Project(
                    spec_path=item["spec_path"],
                    spec_content=item.get("spec_content", ""),
                    task_id=item["task_id"],
                    name=item.get("name", ""),
                    status=item["status"],
                    started_at=item.get("started_at", 0),
                    finished_at=item.get("finished_at", 0),
                    error=item.get("error", ""),
                    tokens_used=item.get("tokens_used", 0),
                    replan_count=item.get("replan_count", 0),
                    work_dir=item.get("work_dir", ""),
                    original_input=item.get("original_input", ""),
                    source=item.get("source", ""),
                    tasks=tasks,
                )
                self._runs[run.task_id] = run
                # Crash recovery: if gateway died mid-execution, mark as resumable
                if run.status in ("running", "pausing"):
                    if not run.tasks:
                        run.status = "failed"
                        run.error = "Gateway crashed before task decomposition completed — re-run to continue"
                        logger.info("Recovered crashed run %s with no tasks — marked as failed", run.task_id)
                    else:
                        run.status = "paused"
                        run.error = run.error or "Gateway crashed during execution — resume to continue"
                        for t in run.tasks:
                            if t.status == TaskStatus.IN_PROGRESS:
                                t.status = TaskStatus.PENDING
                                t.attempts = max(0, t.attempts - 1)
                        logger.info("Recovered crashed run %s — marked as resumable", run.task_id)
                elif run.status == "planning":
                    run.status = "failed"
                    run.error = "Gateway crashed during planning — re-plan to continue"
                    logger.info("Recovered crashed planning run %s — marked as failed", run.task_id)
                elif run.status == "cancelling":
                    run.status = "cancelled"
                    run.error = run.error or "Gateway crashed during cancellation"
                    for t in run.tasks:
                        if t.status == TaskStatus.IN_PROGRESS:
                            t.status = TaskStatus.CANCELLED
                            t.attempts = max(0, t.attempts - 1)
                        elif t.status in (TaskStatus.PENDING, TaskStatus.REVIEWING):
                            t.status = TaskStatus.CANCELLED
                    logger.info("Recovered crashed cancelling run %s — marked as cancelled", run.task_id)
        except Exception:
            logger.debug("Failed to load runs", exc_info=True)

    def _save_progress(self, run: Project) -> None:
        save_progress(run)

    def _load_checkpoint(self, spec_path: Path) -> set[str] | None:
        return load_checkpoint(spec_path)

    def _build_resume_context(self, completed: list[Task]) -> str:
        return build_resume_context(completed)

    def status(self) -> dict:
        return build_status(self._runs, self._tasks, self._agent)

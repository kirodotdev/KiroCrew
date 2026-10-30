"""Every way a task run starts, resumes or stops.

Start admission (one start per id, refused once the gateway closes admission, the
concurrent-run cap without the task queue), the foreground ``run`` and
``start_background`` drivers with their finally-block teardown, the resumable-workspace
guard ``execute_plan`` and ``retry_from_task`` share, and the operator controls (cancel,
pause, delete). ``execute_plan`` and ``retry_from_task`` stay ``TaskRunner`` methods in
``taskrunner.py``, where mypy keeps the narrowing their background closures read; a new
entry point that starts or stops a run still goes here.

Instance fields written: ``_agent``, ``_run_session_keys``, ``_runs``,
``_stall_cancelled_ids``, ``_start_ids_in_flight``, ``_tasks``.

Instance fields read: ``_consolidator``, ``_ctx``, ``_fresh``, ``_sessions``,
``_start_lock``, ``_task_admission``, ``_work_dir``, ``_workspace_dir``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew import git_coord
from kiro_crew.execution_context import ExecutionContext
from kiro_crew.task_models import Project, TaskStatus
from kiro_crew.task_planner import auto_name
from kiro_crew.task_reporter import (
    build_resume_context,
    format_completion_summary,
    load_checkpoint,
    save_progress,
)
from kiro_crew.taskrunner_parts.run_inputs import (
    _UNATTENDED_SOURCES,
    _decompose_yaml_with_audit,
    _read_spec_prefix,
    _read_spec_text,
    _resolve_workspace_dir,
)
from kiro_crew.taskrunner_parts.run_scope import _SESSION_PREFIX
from kiro_crew.taskrunner_parts.run_steps import _MonotonicAge
from kiro_crew.workflow_memory import capture_admission_execution

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


def _observe_background_completion(task: asyncio.Task[None]) -> None:
    """Retrieve failures even after bookkeeping drops the task; awaiting still raises."""
    if not task.cancelled():
        error = task.exception()
        if error is not None:
            # Private task details belong to the scoped run error, never this callback.
            logger.error("TaskRunner background completion failed (%s)", type(error).__name__)


_MAX_CONCURRENT_TASKS = 3  # max simultaneous task runs


def _admission_closed(self: TaskRunner) -> bool:
    """Read the gateway shutdown gate without yielding."""
    return getattr(self._sessions, "admission_closed", False) is True


async def _reserve_start(self: TaskRunner, task_id: str) -> None:
    """Reserve one start atomically against the shared admission gate."""
    async with self._start_lock:
        self._require_workflow_ready()
        if self._admission_closed():
            raise ValueError("gateway admission is closed")
        if task_id in self._start_ids_in_flight:
            raise ValueError("Task is already starting")
        prior = self._tasks.get(task_id)
        if prior is not None and not prior.done():
            raise ValueError("Cannot start while the previous run is still finishing")
        self._start_ids_in_flight.add(task_id)


def _release_start(self: TaskRunner, task_id: str) -> None:
    self._start_ids_in_flight.discard(task_id)


async def run(
    self: TaskRunner,
    spec_path: str | Path,
    task_id: str = "",
    name: str = "",
    source: str = "",
    workspace_dir: str = "",
    auto_approve: bool = False,
    input_content: str | None = None,
) -> Project:
    self._require_workflow_ready()
    spec_path = Path(spec_path)
    if input_content is not None:
        spec_content = input_content.strip()
    else:
        # The spec path is caller-supplied and may sit on a slow mount, so the
        # existence probe runs in a worker like the read below it.
        if not await asyncio.to_thread(spec_path.exists):
            raise FileNotFoundError(f"Spec not found: {spec_path}")
        # The whole file goes into the LLM prompt, the persisted run and the
        # review context, so it is read through the same descriptor gate as
        # the planning prefix. A refusal fails the run: proceeding on a
        # refused spec is what puts an aliased sensitive file's bytes there.
        gated = await asyncio.to_thread(_read_spec_text, str(spec_path), None)
        if gated is None:
            raise PermissionError(f"Spec file refused by the file gate: {spec_path}")
        spec_content = gated
    if not spec_content:
        raise ValueError("Spec file is empty")
    if not task_id:
        task_id = f"{spec_path.stem}_{int(time.time())}"
    self._refresh_from_config()
    _override = _resolve_workspace_dir(workspace_dir)
    _effective_ws = _override or self._workspace_dir
    task_dir = Path(_effective_ws) if _effective_ws else self._work_dir / spec_path.stem
    await asyncio.to_thread(task_dir.mkdir, parents=True, exist_ok=True)
    existing = self._runs.get(task_id)
    run = Project(
        spec_path=str(spec_path),
        spec_content=spec_content,
        started_at=time.time(),
        last_task_time=time.time(),
        status="running",
        source=source,
        workflow_run_id=existing.workflow_run_id if existing else "",
        workflow_id=existing.workflow_id if existing else "",
        workflow_slug=existing.workflow_slug if existing else "",
        workflow_revision=existing.workflow_revision if existing else 0,
        derived_from_workflow_id=existing.derived_from_workflow_id if existing else "",
        derived_from_revision=existing.derived_from_revision if existing else 0,
        execution_context=existing.execution_context if existing else self._capture_execution(),
    )
    # The global timeout counts from here; the watchdog and the executor
    # both read this one age of it, so they cannot disagree after a step.
    _MonotonicAge.of_run_start(run, fresh=True)
    run.task_id = task_id
    run.name = name or auto_name(spec_content, str(spec_path))
    run.work_dir = str(task_dir)
    await self._grant_run_trust(run, bool(auto_approve))
    self._runs[task_id] = run
    watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
    history_key = await self._bound_history_key(run, f"taskrunner:run:{spec_path.stem}")
    try:
        await self._workflow_begin(run)
        await self._workflow_rebind(run)
        await self._apersist_runs()  # persist immediately so crash recovery works
        await self._notify("\U0001f680 Task started", f"Spec: `{spec_path.name}`", run=run)
        if source == "yaml":
            run.tasks = _decompose_yaml_with_audit(
                spec_content, task_id, source=source, spec_name=spec_path.name
            )
        elif spec_path.suffix in (".yaml", ".yml"):
            # The suffix decides the decomposer, not the caller: a cron- or
            # MCP-sourced workflow spec must decompose deterministically too.
            try:
                run.tasks = _decompose_yaml_with_audit(
                    spec_content, task_id, source=source, spec_name=spec_path.name
                )
            except (ValueError, KeyError):
                # Deny by default for UNATTENDED callers (cron/MCP): nobody is
                # watching, so an invalid spec fails with the audit trail above
                # rather than degrading to the unaudited LLM decomposer.
                # Attended callers (chat, dashboard, CLI) keep the fallback —
                # an operator is present to see the plan, and `/task run
                # <file>.yaml` on a non-workflow YAML worked before the gate.
                if source in _UNATTENDED_SOURCES:
                    raise
                logger.warning(
                    "YAML spec %s is not in workflow format; falling back to LLM decomposition",
                    spec_path.name,
                )
                run.tasks = await self._decompose(spec_content, run.work_dir, task_id)
        else:
            run.tasks = await self._decompose(spec_content, run.work_dir, task_id)
        if not run.tasks:
            run.status = "failed"
            run.error = "Failed to decompose spec into tasks"
            await self._notify("\u274c Task failed", run.error, run=run)
            return run
        await self._workflow_set_plan(run)
        await self._apersist_runs()  # persist tasks so resume works after crash
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
        await self._taskq_begin_run(run)
        watchdog_task = asyncio.create_task(self._watchdog_loop(run))
        await self._execute_tasks(run, history_key)
        if run.status == "running":
            run.status = "completed"
            run.finished_at = time.time()
            await self._apersist_runs()
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
        try:
            await self._apersist_runs()
            await self._taskq_end_run(run)
            if run.branch_name:
                try:
                    await git_coord.finalize(run)
                except Exception:
                    logger.debug("Git finalize failed", exc_info=True)
            await self._workflow_finalize(run)
            if self._consolidator:
                self._consolidator.maybe_consolidate(history_key)
        finally:
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
    return run


async def start_background(
    self: TaskRunner,
    spec_path: str | Path,
    agent: str = "",
    name: str = "",
    source: str = "",
    workspace_dir: str = "",
    auto_approve: bool = False,
    *,
    session_key: str = "",
    execution_context: ExecutionContext | None = None,
    input_content: str | None = None,
) -> str:
    """Plan and execute *spec_path* in the background; returns the task id.

    ``session_key`` is keyword-only and optional so every existing caller is
    unchanged. It names the conversation this run was started FROM and is
    handed to the notify sink, which is what lets a stall-worthy notice (an
    approval request, a denial) reach the surface the operator started the
    task on rather than one hard-wired destination.
    """
    execution = await capture_admission_execution(
        self._ctx,
        session_key,
        execution_context=execution_context,
        capture_fn=self._capture_execution,
    )
    if self._admission_closed():
        raise ValueError("gateway admission is closed")
    self._require_workflow_ready()
    # Validate the per-run workspace override before entering the admission
    # lock so a bad/sensitive path fails without blocking other starts.
    _resolve_workspace_dir(workspace_dir)
    self._agent = agent
    try:
        from kiro_crew.hooks import validate_file_path

        safe_sp = validate_file_path(str(spec_path))
        if input_content is not None:
            early_content = input_content[:4000]
        elif safe_sp:
            early_content = await asyncio.to_thread(
                _read_spec_prefix,
                safe_sp,
                4000,
            )
        else:
            early_content = ""
    except Exception:
        early_content = ""

    # Admission is one transaction: concurrency check, pruning, unique ID
    # allocation, placeholder persistence, and task registration. In
    # particular, do not release this lock while _apersist_runs() yields;
    # otherwise two same-spec starts can both pass the limit and overwrite
    # each other's timestamp-based ID before either appears in _tasks.
    async with self._start_lock:
        self._require_workflow_ready()
        if self._admission_closed():
            raise ValueError("gateway admission is closed")
        active = sum(1 for task in self._tasks.values() if not task.done())
        if self._task_admission is None and active >= _MAX_CONCURRENT_TASKS:
            raise ValueError(
                f"Too many concurrent tasks ({active}/{_MAX_CONCURRENT_TASKS}). "
                "Cancel or wait for a running task to finish."
            )

        completed = [
            task_id
            for task_id, run in self._runs.items()
            if run.status in ("completed", "failed", "cancelled")
        ]
        # Always purge completed cron runs; keep last 10 others.
        cron_done = [task_id for task_id in completed if self._runs[task_id].source == "cron"]
        for task_id in cron_done:
            self._runs.pop(task_id, None)
            self._stall_cancelled_ids.discard(task_id)
            self._run_session_keys.pop(task_id, None)
        other_done = [task_id for task_id in completed if task_id in self._runs]
        for task_id in other_done[:-10]:
            self._runs.pop(task_id, None)
            self._stall_cancelled_ids.discard(task_id)
            self._run_session_keys.pop(task_id, None)

        # Nanosecond IDs avoid routine same-second collisions. The guarded
        # increment is a deterministic fallback if a clock/platform returns
        # the same value for two starts.
        id_suffix = time.time_ns()
        task_id = f"{Path(spec_path).stem}_{id_suffix}"
        while (
            task_id in self._runs or task_id in self._tasks or task_id in self._start_ids_in_flight
        ):
            id_suffix += 1
            task_id = f"{Path(spec_path).stem}_{id_suffix}"
        self._start_ids_in_flight.add(task_id)

        try:
            self._runs[task_id] = Project(
                spec_path=str(spec_path),
                spec_content=early_content,
                task_id=task_id,
                name=name or Path(spec_path).stem,
                status="planning",
                started_at=time.time(),
                source=source,
                auto_approve=bool(auto_approve),
                execution_context=execution,
            )
            await self._bind_run_execution(
                self._runs[task_id], f"{_SESSION_PREFIX}:{task_id}:runtime"
            )
            if session_key:
                self._run_session_keys[task_id] = session_key
            await self._workflow_begin(self._runs[task_id])
            persist_task = asyncio.create_task(self._apersist_runs())
            try:
                await asyncio.shield(persist_task)  # durable before background execution
            except BaseException:
                # The off-thread atomic write cannot be cancelled once it
                # starts. Drain it before the outer rollback persists the
                # empty post-removal snapshot, or this stale planning row
                # can land after cleanup and reappear on restart.
                await asyncio.shield(persist_task)
                raise

            async def _wrapped() -> None:
                try:
                    await self.run(
                        spec_path,
                        task_id=task_id,
                        name=name,
                        source=source,
                        workspace_dir=workspace_dir,
                        auto_approve=auto_approve,
                        **({"input_content": input_content} if input_content is not None else {}),
                    )
                except Exception as exc:
                    logger.exception("start_background task %s failed", task_id)
                    placeholder = self._runs.get(task_id)
                    if placeholder and placeholder.status == "planning":
                        placeholder.status = "failed"
                        placeholder.error = str(exc)
                        await self._apersist_runs()
                finally:
                    self._tasks.pop(task_id, None)

            self._tasks[task_id] = asyncio.create_task(_wrapped())
            self._tasks[task_id].add_done_callback(_observe_background_completion)
            return task_id
        except BaseException:
            rollback_run = self._runs.pop(task_id, None)
            self._run_session_keys.pop(task_id, None)
            if rollback_run is not None:
                delete_task = asyncio.create_task(self._workflow_delete_link(rollback_run))
                try:
                    await asyncio.shield(delete_task)
                except asyncio.CancelledError:
                    await asyncio.shield(delete_task)
                except Exception:
                    logger.warning(
                        "Failed to remove cancelled background workflow %s",
                        task_id,
                        exc_info=True,
                    )
                cleanup_persist = asyncio.create_task(self._apersist_runs())
                try:
                    await asyncio.shield(cleanup_persist)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup_persist)
                except Exception:
                    logger.warning(
                        "Failed to persist cancelled background start %s",
                        task_id,
                        exc_info=True,
                    )
            raise
        finally:
            self._release_start(task_id)


async def delete_run(self: TaskRunner, task_id: str) -> bool:
    self._require_workflow_ready()
    run = self._runs.get(task_id)
    if not run:
        return False
    if run.status == "running":
        run.status = "cancelling"
    bg_task = self._tasks.pop(task_id, None)
    if bg_task and not bg_task.done():
        bg_task.cancel()
    self._runs.pop(task_id, None)
    origin = self._run_session_keys.get(task_id)
    try:
        await self._apersist_runs()
    except BaseException:
        self._runs.setdefault(task_id, run)
        raise
    self._stall_cancelled_ids.discard(task_id)
    if self._run_session_keys.get(task_id) == origin:
        self._run_session_keys.pop(task_id, None)
    await self._workflow_delete_link(run)
    try:
        # Resolved from ``kiro_crew.sel`` at call time, not through the
        # module-level binding, so a substituted SEL factory is observed.
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


def cancel(self: TaskRunner, task_id: str | None = None, *, exact: bool = False) -> None:
    """Cancel running tasks. Sets status to 'cancelling'; the finally block
    in run()/retry_from_task() handles actual cleanup and final status."""
    if task_id:
        matches = [] if exact else [r for r in self._runs.values() if r.name == task_id]
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


def pause(self: TaskRunner, task_id: str) -> None:
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


async def _ensure_resumable_workspace(self: TaskRunner, run: Project, verb: str) -> bool:
    """Validate (and if possible recover) the worktree of a resumed run.

    Directory-exists alone is not enough: ``git worktree remove``
    deregisters and deletes in separate steps, so an interrupted
    ``finalize()`` (or the worktree being removed out from under the run
    some other way) can leave the directory present but not registered as
    a git worktree -- resuming against it would silently dispatch every
    remaining step against a non-git directory while still reporting them
    completed. Returns True when the run may proceed. On unrecoverable
    loss the run is failed closed (persisted and notified) and False is
    returned; the caller must return without dispatching any steps.
    """
    if not run.branch_name or await git_coord.workspace_is_valid(run):
        return True
    if await git_coord.reinit_workspace_for_retry(run):
        return True
    run.status = "failed"
    run.error = (
        "Task Runner workspace worktree was lost and " f"could not be restored before {verb}"
    )
    run.finished_at = time.time()
    await self._apersist_runs()
    await self._notify(
        f"\u274c {verb.capitalize()} failed",
        "Workspace could not be restored",
        run=run,
    )
    return False

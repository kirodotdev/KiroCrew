"""Autonomous task runner — orchestrator module.

``TaskRunner`` is defined here and binds its members from the ``taskrunner_parts``
owner modules; it delegates to task_models, task_planner, task_executor, task_reporter.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import sys as _sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew import git_coord, shutdown_event  # noqa: F401
from kiro_crew.atomic_write import atomic_write  # noqa: F401
from kiro_crew.config import live  # noqa: F401
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.memory_sections import lesson_extraction_enabled  # noqa: F401
from kiro_crew.execution_context import (  # noqa: F401
    ExecutionContext,
    bind_session_execution,
    capture_session_execution,
    execution_from_record,
)
from kiro_crew.executors import run_in_embed_pool  # noqa: F401
from kiro_crew.hooks import safe_read_file_bytes_nolink, validate_file_path  # noqa: F401
from kiro_crew.llm_helpers import stream_and_collect_json  # noqa: F401
from kiro_crew.safety_override import safety_override  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.session import BACKGROUND_KEY  # noqa: F401
from kiro_crew.subagent import compute_memory_sized_parallel_cap  # noqa: F401
from kiro_crew.task_executor import (  # noqa: F401
    build_task_prompt,
    execute_single_task,
)
from kiro_crew.task_executor import self_review as self_review_fn  # noqa: F401

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
    Project,
    Task,
    TaskStatus,
    WorkingMemory,
)
from kiro_crew.task_planner import (  # noqa: F401
    auto_name,
    decompose,
    decompose_yaml,
    group_parallel_tasks,
    normalize_cross_group_deps,
    parse_tasks,
)
from kiro_crew.task_planner import (  # noqa: F401
    plan_to_chat_context as _planner_plan_to_chat_context,
)
from kiro_crew.task_planner import (  # noqa: F401
    plan_to_yaml,
    update_plan_tasks,
)
from kiro_crew.task_reporter import (  # noqa: F401  (NotifyCallback re-exported)
    NotifyCallback,
    build_resume_context,
    build_status,
    format_completion_summary,
    load_checkpoint,
    notify,
    save_progress,
)
from kiro_crew.workflow_memory import (  # noqa: F401
    TaskSnapshotError,
    capture_admission_execution,
    capture_execution,
)

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog, HistoryConsolidator
    from kiro_crew.learn import LessonStore
    from kiro_crew.providers.base import LLMEvent
    from kiro_crew.session import SessionManager
    from kiro_crew.taskq.adapters import runner as _runner_adapter

from kiro_crew import _facade_mirror
from kiro_crew.learn import Lesson  # noqa: F401
from kiro_crew.start_priority import StartPriority  # noqa: F401
from kiro_crew.taskrunner_parts import plan_mode as _plan_mode
from kiro_crew.taskrunner_parts import run_inputs as _run_inputs
from kiro_crew.taskrunner_parts import run_lifecycle as _run_lifecycle
from kiro_crew.taskrunner_parts import run_registry as _run_registry
from kiro_crew.taskrunner_parts import run_reporting as _run_reporting
from kiro_crew.taskrunner_parts import run_scope as _run_scope
from kiro_crew.taskrunner_parts import run_steps as _run_steps
from kiro_crew.taskrunner_parts import taskq_bridge as _taskq_bridge
from kiro_crew.taskrunner_parts import workflow_port as _workflow_port
from kiro_crew.taskrunner_parts.run_inputs import _resolve_workspace_dir
from kiro_crew.taskrunner_parts.run_lifecycle import (
    _MAX_CONCURRENT_TASKS,
    _observe_background_completion,
)
from kiro_crew.taskrunner_parts.run_steps import _DEFAULT_TOKEN_BUDGET, _MonotonicAge
from kiro_crew.taskrunner_parts.workflow_port import WorkflowRunPublisher

logger = logging.getLogger(__name__)


# ── Backward-compat re-exports ──
Step = Task
StepStatus = TaskStatus
TaskRun = Project


class TaskRunner:
    """Autonomous spec executor — decomposes and runs tasks."""

    # Each member below is the function of that name in the owner module its group's
    # header names (``kiro_crew.taskrunner_parts``), bound here by name.
    # ── Run inputs: the live taskrunner.* config (run_inputs) ──
    _clamp_parallel_steps = staticmethod(_run_inputs._clamp_parallel_steps)
    _refresh_from_config = _run_inputs._refresh_from_config
    # ── Run scope: sessions, identity, trust, teardown (run_scope) ──
    _capture_execution = _run_scope._capture_execution
    _bind_run_execution = _run_scope._bind_run_execution
    _bound_history_key = _run_scope._bound_history_key
    _grant_run_trust = _run_scope._grant_run_trust
    _reset_incomplete_tasks = staticmethod(_run_scope._reset_incomplete_tasks)
    _cleanup_run_sessions = _run_scope._cleanup_run_sessions
    _release_run_runtime = _run_scope._release_run_runtime
    # ── Workflow history publication (workflow_port) ──
    _workflow_initialization_error: str
    defer_workflow_attachment = _workflow_port.defer_workflow_attachment
    _require_workflow_ready = _workflow_port._require_workflow_ready
    attach_workflow_service = _workflow_port.attach_workflow_service
    _workflow_begin = _workflow_port._workflow_begin
    _workflow_set_plan = _workflow_port._workflow_set_plan
    _mark_workflow_adapted = staticmethod(_workflow_port._mark_workflow_adapted)
    _workflow_persist_replacement = _workflow_port._workflow_persist_replacement
    _workflow_rebind = _workflow_port._workflow_rebind
    _workflow_finalize = _workflow_port._workflow_finalize
    _workflow_delete_link = _workflow_port._workflow_delete_link
    # ── Durable task queue (taskq_bridge) ──
    attach_task_admission = _taskq_bridge.attach_task_admission
    adopt_task_rows = _taskq_bridge.adopt_task_rows
    _taskq_lane_inputs = _taskq_bridge._taskq_lane_inputs
    _taskq_begin_run = _taskq_bridge._taskq_begin_run
    _taskq_row_state = _taskq_bridge._taskq_row_state
    _taskq_end_run = _taskq_bridge._taskq_end_run
    _taskq_admit_step = _taskq_bridge._taskq_admit_step
    # ── Runs registry and checkpoints (run_registry) ──
    _RUNS_FILE = "runs.json"
    _runs_path = _run_registry._runs_path
    _persist_runs = _run_registry._persist_runs
    _serialize_runs = _run_registry._serialize_runs
    _next_persist_seq = _run_registry._next_persist_seq
    _commit_snapshot = _run_registry._commit_snapshot
    _apersist_runs = _run_registry._apersist_runs
    _load_runs = _run_registry._load_runs
    _save_progress = _run_registry._save_progress
    _load_checkpoint = _run_registry._load_checkpoint
    _build_resume_context = _run_registry._build_resume_context
    status = _run_registry.status
    # ── Notifications, history and lessons (run_reporting) ──
    _notify = _run_reporting._notify
    _log_task = _run_reporting._log_task
    _extract_lesson = _run_reporting._extract_lesson
    _call_llm_for_lesson = _run_reporting._call_llm_for_lesson
    # ── Step execution, replanning, watchdog (run_steps) ──
    _execute_tasks = _run_steps._execute_tasks
    _build_task_prompt = _run_steps._build_task_prompt
    self_review = _run_steps.self_review
    _execute_single_task = _run_steps._execute_single_task
    _try_replan = _run_steps._try_replan
    _watchdog_loop = _run_steps._watchdog_loop
    # ── Plan mode (plan_mode) ──
    _auto_name = staticmethod(_plan_mode._auto_name)
    _resolve_task = _plan_mode._resolve_task
    _normalize_cross_group_deps = staticmethod(_plan_mode._normalize_cross_group_deps)
    _group_parallel_tasks = staticmethod(_plan_mode._group_parallel_tasks)
    _parse_tasks = _plan_mode._parse_tasks
    plan = _plan_mode.plan
    start_workflow_definition = _plan_mode.start_workflow_definition
    cancel_plan = _plan_mode.cancel_plan
    update_plan = _plan_mode.update_plan
    update_task = _plan_mode.update_task
    plan_to_chat_context = _plan_mode.plan_to_chat_context
    _decompose = _plan_mode._decompose
    # ── Run lifecycle: start, resume, stop (run_lifecycle) ──
    _admission_closed = _run_lifecycle._admission_closed
    _reserve_start = _run_lifecycle._reserve_start
    _release_start = _run_lifecycle._release_start
    run = _run_lifecycle.run
    start_background = _run_lifecycle.start_background
    delete_run = _run_lifecycle.delete_run
    cancel = _run_lifecycle.cancel
    pause = _run_lifecycle.pause
    _ensure_resumable_workspace = _run_lifecycle._ensure_resumable_workspace

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
        max_parallel_steps: int | None = None,
        workspace_dir: str = "",
        workflow_service: WorkflowRunPublisher | None = None,
        test_cmd: list[str] | None = None,
        require_verification: bool = False,
    ) -> None:
        self._sessions = sessions
        self._ctx = context_builder
        self._on_notify = on_notify
        self._auto_test = auto_test
        self._auto_commit = auto_commit
        # Configured target folder for all executions. When set, every
        # run operates directly in this folder instead of a per-run scratch dir,
        # so the workflow works on the intended location rather than a path it
        # creates for itself. Empty = legacy per-run workspace behavior.
        # Security note: this folder becomes the cwd for autonomous, LLM-driven
        # task execution, so _resolve_workspace_dir rejects credential/secret
        # locations (canonicalized first to defeat traversal/symlink bypasses).
        self._workspace_dir = _resolve_workspace_dir(workspace_dir)
        if self._workspace_dir:
            self._work_dir = Path(self._workspace_dir)
        else:
            self._work_dir = work_dir or Path.cwd()
        # What the constructor was handed, so a config write that later reverts
        # ``taskrunner.workspace_dir`` restores exactly this (see _refresh_from_config).
        self._ctor_workspace_dir = self._workspace_dir
        self._ctor_work_dir = self._work_dir
        # The operator's taskrunner.test_command argv; never inferred from the project.
        self._test_cmd: list[str] | None = list(test_cmd) if test_cmd else None
        self._require_verification = require_verification
        self._conversation_log = conversation_log
        self._consolidator = consolidator
        self._lesson_store = lesson_store
        self._fresh = fresh
        self._global_timeout = global_timeout
        self._token_budget = token_budget
        self._on_approval = on_approval
        # Concurrency cap for parallel task groups.
        # ``compute_memory_sized_parallel_cap`` is the host-safe ceiling (host
        # memory over the per-agent cost, clamped to ``[3,
        # agent.subagent_auto_max]``) -- it exists to prevent OOM, because no
        # per-start memory floor prices a TaskRunner step the way it prices a
        # subagent start, so it is always the upper bound. A positive
        # ``taskrunner.max_parallel_steps`` may only lower it (intentional
        # throttling for cost / rate-limits); ``0`` (or unset) means "use the
        # computed ceiling". An explicit value can never raise concurrency above
        # the host-safe maximum.
        try:
            cfg: KiroCrewConfig | None = KiroCrewConfig.load()
        except Exception:
            cfg = None
        self._max_parallel_steps = self._clamp_parallel_steps(max_parallel_steps, cfg)
        # The ``taskrunner.*`` values the gateway constructed this runner from.
        # Each run entry re-reads config and adopts a field whose config value has
        # MOVED since this baseline (a hot write from any writer), while a field
        # the config never changed keeps the constructor's argument -- so a test
        # or embedder passing explicit values is not overridden by a config.json
        # that never mentioned them. See ``_refresh_from_config``.
        self._config_baseline: tuple[int, str] | None = (
            (int(cfg.taskrunner.max_parallel_steps), str(cfg.taskrunner.workspace_dir))
            if cfg is not None
            else None
        )
        self._ctor_max_parallel_steps = max_parallel_steps
        self._runs: dict[str, Project] = {}
        # Serialize registry writes and enforce monotonic ordering. Snapshots
        # are always built on the event-loop thread (see _serialize_runs), so
        # an older snapshot whose offloaded write lands late must not clobber a
        # newer one: _commit_snapshot skips any write whose sequence is behind
        # what has already been persisted.
        self._persist_lock = threading.Lock()
        self._persist_seq = 0  # last sequence handed out (event-loop thread only)
        self._persist_written = 0  # highest sequence persisted (lock-guarded)
        self._persist_failed = 0  # newer failures must not be overwritten by stale workers
        self._snapshot_recovery_incomplete = False
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._start_lock = asyncio.Lock()
        self._start_ids_in_flight: set[str] = set()
        self._plan_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None
        self._stall_cancelled_ids: set[str] = set()
        # task_id -> the conversation the run was started FROM, so a notification
        # can be routed back to the surface the operator is actually watching.
        # Deliberately in memory only and deliberately not on ``Project``: it is
        # a routing hint for the lifetime of one process, and a persisted channel
        # key would outlive the binding it names and send a restart's first
        # notice into a conversation that may no longer resolve.
        self._run_session_keys: dict[str, str] = {}
        # Optional publication port into the shared workflow history. TaskRunner
        # remains the owner of planning/execution semantics; this port only
        # mirrors lifecycle and progress for one unified management surface.
        self._workflow_service = workflow_service
        self._workflow_initializing = False
        self._agent: str = ""
        # Durable task queue (``taskq``): attached by the gateway once the
        # SubagentManager's store is open. None keeps the legacy behaviour --
        # a fixed run cap and no rows. See ``attach_task_admission``.
        self._task_admission: _runner_adapter.RunnerAdmission | None = None
        self._run_handles: dict[str, _runner_adapter.Admitted] = {}
        self._adopt_inflight: asyncio.Task[Any] | None = None
        self._load_runs()

    @property
    def task_admission(self) -> "_runner_adapter.RunnerAdmission | None":
        return self._task_admission

    @property
    def current_run(self) -> Project | None:
        if not self._runs:
            return None
        return list(self._runs.values())[-1]

    @property
    def running(self) -> bool:
        return bool(self._start_ids_in_flight) or any(not t.done() for t in self._tasks.values())

    async def execute_plan(
        self,
        task_id: str,
        agent: str = "",
        fresh: bool = False,
        workspace_dir: str = "",
        auto_approve: bool = False,
    ) -> str:
        self._require_workflow_ready()
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        restartable = {"planned", "paused", "cancelled", "failed"}
        if run.status not in restartable:
            raise ValueError(f"Run {task_id} is not in a startable state (status={run.status})")
        # The status flips terminal BEFORE the prior run's finally-block
        # finishes -- git finalize (which removes the worktree) runs after
        # the terminal status is persisted. A restart accepted in that window
        # validates a workspace the prior finalizer is about to delete, and
        # then executes against a missing directory. The background task
        # handle is the honest signal: refuse while it is still running.
        # (Same guard as retry_from_task.)
        prior = self._tasks.get(task_id)
        if prior is not None and not prior.done():
            raise ValueError("Cannot restart while the previous run is still finishing")

        await self._reserve_start(task_id)

        try:
            # Optional per-run workspace override: only applied to a run that has NOT
            # begun yet (status "planned"). A resumed run (paused/cancelled/failed) keeps
            # its original work_dir, so re-targeting the folder can't orphan work already
            # produced there (files/commits, git worktree state). The path is still
            # resolved+validated below regardless of status (audit/sensitive-path guard).
            self._refresh_from_config()
            _override = _resolve_workspace_dir(workspace_dir)
            if _override and run.status == "planned":
                run.work_dir = _override

            # Guard: limit concurrent running tasks — check BEFORE mutating state.
            # With the task queue attached the cap is the lane's: excess steps
            # queue instead of the run being refused.
            active = sum(1 for t in self._tasks.values() if not t.done())
            if self._task_admission is None and active >= _MAX_CONCURRENT_TASKS:
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
                await self._apersist_runs()

            await self._grant_run_trust(run, bool(auto_approve))
            await self._apersist_runs()

            self._agent = agent
            history_key = await self._bound_history_key(run, f"taskrunner:run:{task_id}")
        except BaseException:
            self._release_start(task_id)
            raise

        async def _execute() -> None:
            watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
            # Pessimistic from entry for a run that already owns a worktree:
            # the finally-block finalize() force-removes run.worktree_path,
            # and until _ensure_resumable_workspace positively identifies
            # that path as this run's own worktree, deleting it could destroy
            # an unrelated directory. The flag is assigned before the first
            # await, so no cancellation anywhere in this coroutine can reach
            # the finally with an unvalidated workspace still marked safe.
            # A planned first run starts False: its worktree is created by
            # this invocation's init_workspace, so its identity is not in
            # question.
            workspace_lost = bool(run.branch_name)
            try:
                run.status = "running"
                run.started_at = run.last_task_time = time.time()
                # The global timeout counts from here; the watchdog and the executor
                # both read this one age of it, so they cannot disagree after a step.
                _MonotonicAge.of_run_start(run, fresh=True)
                await self._workflow_rebind(run)
                await self._apersist_runs()  # persist immediately so crash recovery works
                if run.branch_name:
                    # A restart of a run that once had a worktree (paused /
                    # cancelled / failed) resumes against it exactly like a
                    # retry does, so it shares the retry path's guard: a lost
                    # worktree is recovered or the run fails closed.
                    #
                    if not await self._ensure_resumable_workspace(run, "restart"):
                        return
                    workspace_lost = False
                else:
                    # First initialisation of a planned run: git is
                    # best-effort and its failure is non-fatal (the run
                    # continues without git coordination).
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
                await self._taskq_begin_run(run)
                watchdog_task = asyncio.create_task(self._watchdog_loop(run))
                await self._execute_tasks(run, history_key)
                if run.status == "running":
                    run.status = "completed"
                    run.finished_at = time.time()
                    await self._apersist_runs()
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
                try:
                    await self._apersist_runs()
                    await self._taskq_end_run(run)
                    if run.branch_name and not workspace_lost:
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
                    self._tasks.pop(task_id, None)

        try:
            self._tasks[task_id] = asyncio.create_task(_execute())
            self._tasks[task_id].add_done_callback(_observe_background_completion)
        finally:
            self._release_start(task_id)
        return task_id

    async def retry_from_task(self, task_id: str, from_task: int, agent: str = "") -> str:
        self._require_workflow_ready()
        run = self._resolve_task(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        # _resolve_task accepts a run NAME as well as the canonical id, but
        # self._tasks is keyed by the canonical id. Canonicalize before any
        # lookup, or a name-addressed retry misses the prior background-task
        # handle, bypasses the finishing guard below, and races the prior
        # run's finalizer (which is about to remove the worktree).
        task_id = run.task_id
        if run.status == "running":
            raise ValueError("Cannot retry a running task")
        if run.status in ("cancelling", "pausing"):
            raise ValueError("Cannot retry while cancel is in progress")
        # The status flips terminal BEFORE the prior run's finally-block
        # finishes -- git finalize (which removes the worktree) runs after
        # the terminal status is persisted. A retry accepted in that window
        # validates a workspace the prior finalizer is about to delete, and
        # then executes against a missing directory. The background task
        # handle is the honest signal: refuse while it is still running.
        prior = self._tasks.get(task_id)
        if prior is not None and not prior.done():
            raise ValueError("Cannot retry while the previous run is still finishing")

        await self._reserve_start(task_id)
        try:
            # Resolve fallible prerequisites before resetting results. After the
            # acknowledged snapshot there must be no await before task handoff.
            history_key = await self._bound_history_key(
                run, f"taskrunner:run:{Path(run.spec_path).stem}"
            )
            previous_run = (
                run.status,
                run.error,
                run.finished_at,
                run.started_at,
                run.last_task_time,
            )
            previous_tasks = [
                (task, task.status, task.error, task.result, task.verification, task.attempts)
                for task in run.tasks
                if task.index >= from_task
            ]
            try:
                for task, *_ in previous_tasks:
                    task.status = TaskStatus.PENDING
                    task.error = ""
                    task.result = ""
                    task.attempts = 0
                run.status = "running"
                run.error = ""
                run.finished_at = 0.0
                run.started_at = run.last_task_time = time.time()
                # The global timeout counts from here; the watchdog and the executor
                # both read this one age of it, so they cannot disagree after a step.
                _MonotonicAge.of_run_start(run, fresh=True)
                await self._apersist_runs()
            except BaseException:
                # Persistence drains its worker even on repeated cancellation.
                # Restore in place: callers may retain the Project/Task objects.
                # Restore live state until a later snapshot succeeds.
                run.status, run.error, run.finished_at, run.started_at, run.last_task_time = (
                    previous_run
                )
                for task, status, error, result, verification, attempts in previous_tasks:
                    task.status, task.error, task.result = status, error, result
                    task.verification, task.attempts = verification, attempts
                raise
            self._agent = agent
        except BaseException:
            self._release_start(task_id)
            raise

        async def _retry() -> None:
            watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
            try:
                await self._workflow_rebind(run)
                if not await self._ensure_resumable_workspace(run, "retry"):
                    return
                await self._notify("\U0001f504 Retrying", f"From task {from_task}", run=run)
                await self._taskq_begin_run(run)
                watchdog_task = asyncio.create_task(self._watchdog_loop(run))
                await self._execute_tasks(run, history_key)
                if run.status == "running":
                    run.status = "completed"
                    run.finished_at = time.time()
                    await self._apersist_runs()
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
                try:
                    await self._apersist_runs()
                    await self._taskq_end_run(run)
                    await self._workflow_finalize(run)
                finally:
                    if watchdog_task and not watchdog_task.done():
                        watchdog_task.cancel()
                    self._tasks.pop(task_id, None)

        try:
            self._tasks[task_id] = asyncio.create_task(_retry())
            self._tasks[task_id].add_done_callback(_observe_background_completion)
        finally:
            self._release_start(task_id)
        return task_id


# The bodies of TaskRunner's members, and the module-level names this module serves,
# live in the taskrunner_parts owners. ``_facade_mirror`` keeps the patch seams whole: a
# write of ``kiro_crew.taskrunner.<name>`` reaches every owner that binds the same object.
#: The owner modules, lowest layer first.
_OWNERS: tuple[str, ...] = (
    "kiro_crew.taskrunner_parts.run_inputs",
    "kiro_crew.taskrunner_parts.run_scope",
    "kiro_crew.taskrunner_parts.workflow_port",
    "kiro_crew.taskrunner_parts.taskq_bridge",
    "kiro_crew.taskrunner_parts.run_registry",
    "kiro_crew.taskrunner_parts.run_reporting",
    "kiro_crew.taskrunner_parts.run_steps",
    "kiro_crew.taskrunner_parts.plan_mode",
    "kiro_crew.taskrunner_parts.run_lifecycle",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: The module-level names this module defines in its owners, served through the mirror.
_MOVED_SURFACE = frozenset(
    {
        "WorkflowInitializing",
        "_DEAD_THRESHOLD",
        "_HEARTBEAT_INTERVAL",
        "_MAX_PARALLEL_TASKS",
        "_MAX_REPLAN",
        "_MAX_TOTAL_TASKS",
        "_RESULT_MEM_CAP",
        "_SESSION_PREFIX",
        "_STALL_CANCEL_TIMEOUT",
        "_STALL_TIMEOUT",
        "_UNATTENDED_SOURCES",
        "_WORKFLOW_RESULT_SUMMARY_CAP",
        "_auto_approve_scope",
        "_decompose_yaml_with_audit",
        "_read_spec_prefix",
        "_read_spec_text",
    }
)
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__],
        _OWNERS,
        excluded=(),
        surface=frozenset(globals()) | _MOVED_SURFACE,
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.taskrunner_parts.run_inputs import (  # noqa: F401
        _MAX_PARALLEL_TASKS,
        _UNATTENDED_SOURCES,
        _decompose_yaml_with_audit,
        _read_spec_prefix,
        _read_spec_text,
    )
    from kiro_crew.taskrunner_parts.run_scope import (  # noqa: F401
        _SESSION_PREFIX,
        _auto_approve_scope,
    )
    from kiro_crew.taskrunner_parts.run_steps import (  # noqa: F401
        _DEAD_THRESHOLD,
        _HEARTBEAT_INTERVAL,
        _MAX_REPLAN,
        _MAX_TOTAL_TASKS,
        _RESULT_MEM_CAP,
        _STALL_CANCEL_TIMEOUT,
        _STALL_TIMEOUT,
        _WORKFLOW_RESULT_SUMMARY_CAP,
    )
    from kiro_crew.taskrunner_parts.workflow_port import (  # noqa: F401
        WorkflowInitializing,
    )

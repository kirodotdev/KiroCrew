"""Autonomous task runner — orchestrator module.

Delegates to: task_models, task_planner, task_executor, task_reporter.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol

from kiro_crew import git_coord, shutdown_event
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.llm_helpers import stream_and_collect_json
from kiro_crew.safety_override import safety_override
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.subagent import compute_max_subagents
from kiro_crew.task_executor import (
    build_task_prompt,
    execute_single_task,
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
    Project,
    ReviewFixAuditEvent,
    ReviewFixDependencyGroup,
    ReviewFixGroupState,
    ReviewFixMetadata,
    ReviewFixState,
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
_MAX_PARALLEL_TASKS = 3  # ctor fallback default when compute_max_subagents fails; live cap is self._max_parallel_steps
_MAX_CONCURRENT_TASKS = 3  # max simultaneous task runs
_SESSION_PREFIX = SESSION_PREFIX
_STALL_TIMEOUT = STALL_TIMEOUT
_STALL_CANCEL_TIMEOUT = STALL_CANCEL_TIMEOUT
_DEFAULT_TOKEN_BUDGET = DEFAULT_TOKEN_BUDGET
_HEARTBEAT_INTERVAL = 30  # watchdog checks process liveness every 30s
_DEAD_THRESHOLD = 2  # consecutive dead checks before fail-fast reset
_RESULT_MEM_CAP = 4000  # truncate task.result in memory after step completes
_WORKFLOW_RESULT_SUMMARY_CAP = 120


class WorkflowRunPublisher(Protocol):
    """Narrow publication port from TaskRunner into shared workflow history."""

    async def begin_host_run(
        self,
        *,
        name: str,
        source: str = "",
        source_format: str,
        task_id: str = "",
        driver: str,
        author: str = "",
        session_key: str = "",
        capabilities: tuple[str, ...] = (),
        workflow_id: str = "",
        workflow_slug: str = "",
        workflow_revision: int = 0,
        derived_from_workflow_id: str = "",
        derived_from_revision: int = 0,
    ) -> str: ...

    async def phase(self, run_id: str, title: str) -> None: ...

    async def set_source(
        self,
        run_id: str,
        source: str,
        *,
        source_format: str = "",
        clear_definition: bool = False,
    ) -> bool: ...

    async def step(
        self,
        run_id: str,
        index: int,
        title: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> None: ...

    async def pause(self, run_id: str) -> bool: ...

    async def rebind(self, run_id: str, task: asyncio.Task[Any], *, task_id: str = "") -> bool: ...

    async def finish(self, run_id: str, result: Any) -> None: ...

    async def fail(self, run_id: str, error: str, *, where: str = "host") -> None: ...

    async def cancel_host_run(self, run_id: str, reason: str = "cancelled") -> None: ...

    async def delete_run(self, run_id: str) -> bool: ...

    def status(self, run_id: str) -> dict[str, Any] | None: ...


def _auto_approve_scope(task_id: str) -> str:
    """SafetyOverride scope key holding a run's per-run auto-approve grant.

    The live, TTL-bounded, audited grant lives in the SafetyOverride singleton
    (see ``activate_scoped``); ``Project.auto_approve`` is only the UI intent
    flag. Enforcement reads ``safety_override().is_scope_active(scope)``.
    """
    return f"{_SESSION_PREFIX}:{task_id}:autoapprove"


def _resolve_workspace_dir(raw: str) -> str:
    """Canonicalize a user-supplied workspace_dir and reject sensitive paths.

    Expands ``~`` and resolves symlinks + ``..`` traversal BEFORE the
    ``is_sensitive_path`` check, so a value like ``/tmp/../../home/user/.aws``
    or a symlink pointing at ``~/.ssh`` cannot slip past by presenting a
    non-sensitive-looking spelling. Returns the resolved absolute path, or an
    empty string when ``raw`` is blank. Raises ``ValueError`` for
    sensitive/credential locations.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    resolved = str(Path(raw).expanduser().resolve())
    if is_sensitive_path(resolved):
        # Security-relevant permission decision — audit before rejecting so a
        # probe for workspace_dir bypass vectors leaves a trace in the SEL log.
        try:
            sel().log_tool_invocation(
                session_key="taskrunner",
                source="taskrunner",
                tool_name="workspace_dir_validate",
                outcome="denied",
                metadata={"raw": raw, "resolved": resolved, "reason": "sensitive_path"},
            )
        except Exception:
            logger.debug("SEL audit for workspace_dir rejection failed", exc_info=True)
        raise ValueError(
            "workspace_dir resolves to a sensitive/credential path and was " f"rejected: {raw!r}"
        )
    # Accepting a workspace_dir authorizes LLM-driven autonomous execution in
    # that directory — a permission decision, so audit the "allowed" outcome too
    # (mirrors the "denied" branch above). Audit failure must not block the run.
    try:
        sel().log_tool_invocation(
            session_key="taskrunner",
            source="taskrunner",
            tool_name="workspace_dir_validate",
            outcome="allowed",
            metadata={"raw": raw, "resolved": resolved},
        )
    except Exception:
        logger.debug("SEL audit for workspace_dir acceptance failed", exc_info=True)
    return resolved


def _read_spec_prefix(path: str, max_chars: int) -> str:
    """Read and normalize a bounded spec prefix on a worker thread."""
    with open(path, encoding="utf-8") as spec_file:
        return spec_file.read(max_chars).strip()


def _decompose_yaml_with_audit(yaml_content: str, task_id: str) -> list[Task]:
    """Decompose YAML with SEL audit logging."""
    try:
        tasks = decompose_yaml(yaml_content)
        sel().log_tool_invocation(
            session_key="dashboard",
            source="taskrunner",
            tool_name="decompose_yaml",
            outcome="ok",
            metadata={"task_id": task_id, "task_count": len(tasks)},
        )
        return tasks
    except Exception as exc:
        sel().log_tool_invocation(
            session_key="dashboard",
            source="taskrunner",
            tool_name="decompose_yaml",
            outcome="error",
            metadata={"task_id": task_id, "error": str(exc)},
        )
        raise


class ReviewFixConflict(ValueError):
    """Raised when a review-fix mutation is based on stale durable state."""

    code = "stale_task_state"

    def __init__(self, run: Project, reason: str) -> None:
        self.task_id = run.task_id
        self.current_revision = run.revision
        self.current_state = run.review_fix.state.value if run.review_fix else ""
        self.current_group_revisions = {
            group.group_id: group.revision
            for group in (run.review_fix.groups if run.review_fix else [])
        }
        super().__init__(reason)


# Two edges here exist only so a FAILED START can be undone: RUNNING →
# AWAITING_GROUP_CONFIRMATION / BLOCKED_VALIDATION (and REREVIEWING → PUSHED) are
# the "execution never actually began" returns used by start_rolled_back, not
# states a run may normally leave RUNNING for.
_REVIEW_FIX_TRANSITIONS: dict[ReviewFixState, set[ReviewFixState]] = {
    ReviewFixState.DRAFT: {
        ReviewFixState.PLANNING,
        ReviewFixState.BLOCKED_DIRTY_OVERLAP,
        ReviewFixState.BLOCKED_MODEL_RESOLUTION,
        ReviewFixState.FAILED,
    },
    ReviewFixState.PLANNING: {
        ReviewFixState.AWAITING_GROUP_CONFIRMATION,
        ReviewFixState.BLOCKED_MODEL_RESOLUTION,
        ReviewFixState.FAILED,
    },
    ReviewFixState.AWAITING_GROUP_CONFIRMATION: {
        ReviewFixState.RUNNING,
        ReviewFixState.PLANNING,
        ReviewFixState.FAILED,
        ReviewFixState.DONE,
    },
    ReviewFixState.RUNNING: {
        ReviewFixState.AWAITING_VALIDATION,
        ReviewFixState.PAUSED,
        ReviewFixState.BLOCKED_MODEL_RESOLUTION,
        ReviewFixState.BLOCKED_VALIDATION,
        ReviewFixState.AWAITING_GROUP_CONFIRMATION,
        ReviewFixState.FAILED,
    },
    ReviewFixState.AWAITING_VALIDATION: {
        ReviewFixState.READY_TO_APPLY,
        ReviewFixState.BLOCKED_VALIDATION,
        ReviewFixState.RUNNING,
        ReviewFixState.DONE,
    },
    ReviewFixState.READY_TO_APPLY: {ReviewFixState.AWAITING_COMMIT, ReviewFixState.DONE},
    ReviewFixState.AWAITING_COMMIT: {ReviewFixState.COMMITTED, ReviewFixState.DONE},
    ReviewFixState.COMMITTED: {ReviewFixState.AWAITING_PUSH, ReviewFixState.DONE},
    ReviewFixState.AWAITING_PUSH: {ReviewFixState.PUSHED, ReviewFixState.DONE},
    ReviewFixState.PUSHED: {ReviewFixState.REREVIEWING, ReviewFixState.DONE},
    ReviewFixState.REREVIEWING: {ReviewFixState.DONE, ReviewFixState.FAILED, ReviewFixState.PUSHED},
    ReviewFixState.BLOCKED_MODEL_RESOLUTION: {
        ReviewFixState.PLANNING,
        ReviewFixState.AWAITING_GROUP_CONFIRMATION,
        ReviewFixState.RUNNING,
        ReviewFixState.DONE,
    },
    ReviewFixState.BLOCKED_DIRTY_OVERLAP: {ReviewFixState.PLANNING, ReviewFixState.DONE},
    ReviewFixState.BLOCKED_VALIDATION: {
        ReviewFixState.RUNNING,
        ReviewFixState.AWAITING_VALIDATION,
        ReviewFixState.DONE,
    },
    ReviewFixState.PAUSED: {ReviewFixState.RUNNING, ReviewFixState.DONE},
    ReviewFixState.FAILED: {ReviewFixState.RUNNING, ReviewFixState.PLANNING, ReviewFixState.DONE},
    ReviewFixState.DONE: set(),
}


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
        max_parallel_steps: int | None = None,
        workspace_dir: str = "",
        workflow_service: WorkflowRunPublisher | None = None,
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
        self._test_cmd: list[str] | None = None
        self._conversation_log = conversation_log
        self._consolidator = consolidator
        self._lesson_store = lesson_store
        self._fresh = fresh
        self._global_timeout = global_timeout
        self._token_budget = token_budget
        self._on_approval = on_approval
        # Concurrency cap for parallel task groups. ``compute_max_subagents`` is
        # the host-safe ceiling (derived from ``agent.subagent_auto_max`` and
        # clamped to host memory/CPU headroom) — it exists to prevent OOM, so it
        # is always the upper bound. A positive ``taskrunner.max_parallel_steps``
        # may only lower it (intentional throttling for cost / rate-limits);
        # ``0`` (or unset) means "use the computed ceiling". An explicit value can
        # never raise concurrency above the host-safe maximum.
        try:
            auto_cap = compute_max_subagents(KiroCrewConfig.load())
        except Exception:
            auto_cap = _MAX_PARALLEL_TASKS
        auto_cap = max(1, auto_cap)
        if max_parallel_steps and max_parallel_steps >= 1:
            self._max_parallel_steps = min(max_parallel_steps, auto_cap)
        else:
            self._max_parallel_steps = auto_cap
        self._runs: dict[str, Project] = {}
        # Serialize registry writes and enforce monotonic ordering. Snapshots
        # are always built on the event-loop thread (see _serialize_runs), so
        # an older snapshot whose offloaded write lands late must not clobber a
        # newer one: _commit_snapshot skips any write whose sequence is behind
        # what has already been persisted.
        self._persist_lock = threading.Lock()
        self._persist_seq = 0  # last sequence handed out (event-loop thread only)
        self._persist_written = 0  # highest sequence persisted (lock-guarded)
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._start_lock = asyncio.Lock()
        self._plan_ids_in_flight: set[str] = set()
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

    def attach_workflow_service(self, service: WorkflowRunPublisher | None) -> None:
        """Attach the shared workflow publication port after gateway startup."""
        self._workflow_service = service

    async def _workflow_begin(
        self, run: Project, *, source: str = "", persist_link: bool = False
    ) -> None:
        service = self._workflow_service
        if service is None or run.workflow_run_id:
            return
        try:
            run.workflow_run_id = await service.begin_host_run(
                name=run.name or run.task_id,
                source=source,
                source_format="task-plan",
                task_id=run.task_id,
                driver="taskrunner",
                session_key=self._run_session_keys.get(run.task_id, ""),
                capabilities=("pause", "cancel", "retry", "save", "delete"),
                workflow_id=run.workflow_id,
                workflow_slug=run.workflow_slug,
                workflow_revision=run.workflow_revision,
                derived_from_workflow_id=run.derived_from_workflow_id,
                derived_from_revision=run.derived_from_revision,
            )
            if persist_link:
                persist_task = asyncio.create_task(self._apersist_runs())
                try:
                    await asyncio.shield(persist_task)
                except BaseException:
                    # Once begin_host_run returns, cancellation must not strand
                    # its durable record without the owning project link.
                    await asyncio.shield(persist_task)
                    raise
            await service.phase(run.workflow_run_id, "Planning")
        except Exception:
            logger.warning("TaskRunner workflow publication failed to start", exc_info=True)

    async def _workflow_set_plan(
        self,
        run: Project,
        *,
        pause: bool = False,
        source: str | None = None,
        modified: bool = False,
    ) -> None:
        if not run.tasks:
            return
        rendered_source = plan_to_yaml(run.tasks) if source is None else source
        if modified:
            self._mark_workflow_adapted(run)
        service = self._workflow_service
        if service is None or not run.workflow_run_id:
            return
        try:
            if modified:
                await service.set_source(
                    run.workflow_run_id,
                    rendered_source,
                    source_format="task-plan",
                    clear_definition=True,
                )
            else:
                await service.set_source(
                    run.workflow_run_id,
                    rendered_source,
                    source_format="task-plan",
                )
            if pause:
                await service.pause(run.workflow_run_id)
        except Exception:
            logger.warning("TaskRunner workflow plan publication failed", exc_info=True)

    @staticmethod
    def _mark_workflow_adapted(run: Project) -> None:
        """Replace exact saved-revision provenance with durable ancestry."""
        if run.workflow_id and run.workflow_revision and not run.derived_from_workflow_id:
            run.derived_from_workflow_id = run.workflow_id
            run.derived_from_revision = run.workflow_revision
        run.workflow_id = ""
        run.workflow_slug = ""
        run.workflow_revision = 0

    async def _workflow_persist_replacement(self, run: Project, *, source: str) -> bool:
        """Create and durably link a replacement shared workflow run."""
        run.workflow_run_id = ""
        await self._workflow_begin(run, source=source, persist_link=True)
        return bool(run.workflow_run_id)

    async def _workflow_rebind(self, run: Project) -> None:
        service = self._workflow_service
        task = asyncio.current_task()
        if service is None or task is None:
            return
        try:
            snapshot = service.status(run.workflow_run_id) if run.workflow_run_id else None
            if not snapshot:
                if not await self._workflow_persist_replacement(
                    run, source=plan_to_yaml(run.tasks) if run.tasks else ""
                ):
                    return
            if not run.workflow_run_id:
                return
            if not await service.rebind(run.workflow_run_id, task, task_id=run.task_id):
                if not await self._workflow_persist_replacement(
                    run, source=plan_to_yaml(run.tasks) if run.tasks else ""
                ):
                    return
                await service.rebind(run.workflow_run_id, task, task_id=run.task_id)
            await service.phase(run.workflow_run_id, "Execution")
        except Exception:
            logger.warning("TaskRunner workflow publication failed to bind", exc_info=True)

    async def _workflow_finalize(self, run: Project) -> None:
        service = self._workflow_service
        if service is None or not run.workflow_run_id:
            return
        try:
            result = {"task_id": run.task_id, "status": run.status}
            if run.status == "completed":
                await service.finish(run.workflow_run_id, result)
            elif run.status == "paused":
                await service.pause(run.workflow_run_id)
            elif run.status == "cancelled":
                await service.cancel_host_run(run.workflow_run_id, run.error or "cancelled")
            elif run.status == "failed":
                await service.fail(
                    run.workflow_run_id, run.error or "TaskRunner failed", where="taskrunner"
                )
        except Exception:
            logger.warning("TaskRunner workflow publication failed to finalize", exc_info=True)

    async def _workflow_delete_link(self, run: Project) -> None:
        service = self._workflow_service
        if service is None or not run.workflow_run_id:
            return
        try:
            await service.delete_run(run.workflow_run_id)
        except Exception:
            logger.debug(
                "delete linked workflow run failed for %s",
                run.workflow_run_id,
                exc_info=True,
            )

    @staticmethod
    def _auto_name(spec_content: str, spec_path: str = "") -> str:
        return auto_name(spec_content, spec_path)

    def _resolve_task(self, ref: str) -> Project | None:
        if ref in self._runs:
            return self._runs[ref]
        matches = [r for r in self._runs.values() if r.name == ref]
        return matches[-1] if matches else None

    def get_review_fix(self, task_id: str) -> Project:
        """Return a review-fix run or raise a stable not-found error."""
        run = self._resolve_task(task_id)
        if not run or not run.review_fix or run.execution_mode != "review_fix":
            raise ValueError(f"Review-fix task {task_id} not found")
        return run

    async def create_review_fix(
        self,
        metadata: ReviewFixMetadata,
        *,
        task_id: str = "",
        name: str = "",
        spec_path: str = "",
        spec_content: str = "",
        source: str = "code_review_sage",
        work_dir: str = "",
        tasks: list[Task] | None = None,
    ) -> Project:
        """Create and durably register a review-fix run in draft state."""
        task_id = task_id or f"review_fix_{time.time_ns()}"
        if task_id in self._runs:
            raise ValueError(f"Task {task_id} already exists")
        now = time.time()
        metadata = copy.deepcopy(metadata)
        metadata.state = ReviewFixState.DRAFT
        metadata.revision = 0
        metadata.created_at = metadata.created_at or now
        metadata.updated_at = now
        run = Project(
            spec_path=spec_path,
            spec_content=spec_content,
            tasks=list(tasks or []),
            status="planned",
            task_id=task_id,
            name=name or task_id,
            source=source,
            work_dir=work_dir,
            review_fix=metadata,
            revision=0,
            execution_mode="review_fix",
            commit_policy="manual_group",
        )
        self._runs[task_id] = run
        await self._apersist_runs()
        return run

    async def mutate_review_fix(
        self,
        task_id: str,
        *,
        expected_revision: int,
        action: str,
        mutate: Callable[[ReviewFixMetadata], None],
        to_state: ReviewFixState | None = None,
        expected_state: ReviewFixState | str | None = None,
        group_id: str = "",
        expected_group_revision: int | None = None,
        expected_target_fingerprint: str = "",
    ) -> Project:
        """Apply one revision-checked review-fix mutation and persist it.

        The mutator runs against a deep copy. If a state, group, or target CAS
        check fails, the live Project remains untouched and no audit entry is
        emitted.
        """
        run = self.get_review_fix(task_id)
        metadata = run.review_fix
        assert metadata is not None
        if run.revision != expected_revision or metadata.revision != expected_revision:
            raise ReviewFixConflict(run, "task revision is stale")
        if expected_state is not None:
            expected_value = (
                expected_state.value
                if isinstance(expected_state, ReviewFixState)
                else str(expected_state)
            )
            if metadata.state.value != expected_value:
                raise ReviewFixConflict(run, "task state is stale")
        if (
            expected_target_fingerprint
            and metadata.target.dirty_fingerprint != expected_target_fingerprint
        ):
            raise ReviewFixConflict(run, "target fingerprint is stale")
        group = None
        if group_id:
            group = next((item for item in metadata.groups if item.group_id == group_id), None)
            if group is None:
                raise ValueError(f"Review-fix group {group_id} not found")
            if expected_group_revision is not None and group.revision != expected_group_revision:
                raise ReviewFixConflict(run, "group revision is stale")

        next_state = to_state or metadata.state
        if (
            next_state != metadata.state
            and next_state not in _REVIEW_FIX_TRANSITIONS[metadata.state]
        ):
            raise ValueError(
                f"Invalid review-fix transition: {metadata.state.value} -> {next_state.value}"
            )
        candidate = copy.deepcopy(metadata)
        mutate(candidate)
        previous_state = candidate.state
        candidate.state = next_state
        new_revision = max(run.revision, metadata.revision) + 1
        candidate.revision = new_revision
        candidate.updated_at = time.time()
        candidate.audit_log.append(
            ReviewFixAuditEvent(
                action=action,
                from_state=previous_state.value,
                to_state=next_state.value,
                revision=new_revision,
                timestamp=candidate.updated_at,
                details={"group_id": group_id} if group_id else {},
            )
        )
        candidate.audit_log = candidate.audit_log[-100:]
        run.review_fix = candidate
        run.revision = new_revision
        await self._apersist_runs()
        return run

    @staticmethod
    def review_fix_group(run: Project, group_id: str) -> ReviewFixDependencyGroup:
        """Find a persisted group without exposing mutable lookup internals."""
        if not run.review_fix:
            raise ValueError("Run is not a review-fix task")
        group = next((item for item in run.review_fix.groups if item.group_id == group_id), None)
        if group is None:
            raise ValueError(f"Review-fix group {group_id} not found")
        return group

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
        workspace_dir: str = "",
        workflow_name: str = "",
        workflow_id: str = "",
        workflow_slug: str = "",
        workflow_revision: int = 0,
        workflow_source: str = "",
        session_key: str = "",
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

        _override = _resolve_workspace_dir(workspace_dir)
        async with self._start_lock:
            id_suffix = time.time_ns()
            task_id = f"plan_{id_suffix}"
            while (
                task_id in self._runs
                or task_id in self._tasks
                or task_id in self._plan_ids_in_flight
            ):
                id_suffix += 1
                task_id = f"plan_{id_suffix}"
            self._plan_ids_in_flight.add(task_id)
        _effective_ws = _override or self._workspace_dir
        owns_task_dir = not _effective_ws
        task_dir = Path(_effective_ws) if _effective_ws else self._work_dir / f"plan_{task_id}"
        created_task_dir = False
        try:
            if owns_task_dir:
                task_dir.mkdir(parents=True, exist_ok=False)
                created_task_dir = True
            else:
                task_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            pass
        except BaseException:
            self._plan_ids_in_flight.discard(task_id)
            raise
        run = Project(
            spec_path=spec_path or "",
            spec_content=spec_content,
            original_input=original_input,
            source=source,
            status="planned",
            task_id=task_id,
            work_dir=str(task_dir),
            name=workflow_name or auto_name(spec_content or original_input, spec_path),
            workflow_id=workflow_id,
            workflow_slug=workflow_slug,
            workflow_revision=workflow_revision,
        )
        try:
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
        except Exception:
            # Only the default plan directory belongs to this attempt. A caller's
            # workspace is an input and must survive a rejected plan unchanged.
            if created_task_dir:
                try:
                    task_dir.rmdir()
                except OSError:
                    logger.warning("Failed to remove rejected plan directory %s", task_dir)
            self._plan_ids_in_flight.discard(task_id)
            raise
        if session_key:
            self._run_session_keys[task_id] = session_key
        committed = False
        try:
            await self._workflow_begin(run, source=workflow_source)
            await self._workflow_set_plan(
                run,
                pause=True,
                source=workflow_source or None,
            )
            self._runs[task_id] = run
            persist_task = asyncio.create_task(self._apersist_runs())
            try:
                await asyncio.shield(persist_task)
            except BaseException:
                # The off-thread atomic write cannot be cancelled once it has
                # started. Drain it before rollback so its stale snapshot cannot
                # land after the project and linked workflow have been removed.
                await asyncio.shield(persist_task)
                raise
            committed = True
            return run
        except BaseException:
            if not committed:
                if self._runs.get(task_id) is run:
                    self._runs.pop(task_id, None)
                self._run_session_keys.pop(task_id, None)
                cleanup_persist = asyncio.create_task(self._apersist_runs())
                try:
                    await asyncio.shield(cleanup_persist)
                except asyncio.CancelledError:
                    await asyncio.shield(cleanup_persist)
                except Exception:
                    logger.warning(
                        "Failed to persist cancelled plan removal for %s",
                        task_id,
                        exc_info=True,
                    )
                await asyncio.shield(self._workflow_delete_link(run))
                if created_task_dir:
                    try:
                        task_dir.rmdir()
                    except OSError:
                        logger.warning("Failed to remove cancelled plan directory %s", task_dir)
            raise
        finally:
            self._plan_ids_in_flight.discard(task_id)

    async def start_workflow_definition(
        self,
        definition: dict,
        *,
        input_text: str = "",
        author: str = "",
        session_key: str = "",
    ) -> dict[str, str]:
        """Execute one saved task-plan revision through the existing TaskRunner."""
        del author  # reserved for a future TaskRunner attribution surface
        run = await self.plan(
            str(definition.get("source", "")),
            source="yaml",
            workflow_name=str(definition.get("name", "")),
            workflow_id=str(definition.get("id", "")),
            workflow_slug=str(definition.get("slug", "")),
            workflow_revision=int(definition.get("revision") or 0),
            workflow_source=str(definition.get("source", "")),
            session_key=session_key,
        )
        run.original_input = input_text
        await self._apersist_runs()
        try:
            await self.execute_plan(run.task_id)
        except ValueError as exc:
            # Planning owns a durable TaskRunner project and shared workflow run
            # before execution admission is checked. A rejected saved invocation
            # must relinquish both identities so its chat caller can report the
            # rejection without leaving an inert run behind.
            await self.delete_run(run.task_id)
            return {"error": str(exc)}
        return {"task_id": run.task_id, "run_id": run.workflow_run_id}

    def cancel_plan(self) -> None:
        if self._plan_task and not self._plan_task.done():
            self._plan_task.cancel()

    async def update_plan(self, task_id: str, tasks: list[dict]) -> Project:
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        if run.status in ("running", "cancelling"):
            raise ValueError(f"Cannot update plan while {run.status}")
        previous_source = plan_to_yaml(run.tasks) if run.tasks else ""
        result = update_plan_tasks(run, tasks)
        await self._workflow_set_plan(
            run,
            pause=run.status == "planned",
            modified=plan_to_yaml(run.tasks) != previous_source,
        )
        await self._apersist_runs()
        return result

    async def update_task(self, task_id: str, index: int, updates: dict) -> dict:
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
            changes["depends_on"] = [
                int(d) for d in deps if isinstance(d, (int, float)) and 0 < int(d) < index
            ]
        if "requires_approval" in updates:
            changes["requires_approval"] = bool(updates["requires_approval"])
        if "force_approval" in updates:
            # force_approval is intentionally mutable — the user who sets it is the same user
            # who can remove it. There's no cross-principal security boundary; it's a personal
            # workflow gate, not an access control mechanism. The gate re-triggers on resume
            # regardless, so removing it is an explicit user decision.
            changes["force_approval"] = bool(updates["force_approval"])
        previous_source = plan_to_yaml(run.tasks)
        # Apply atomically
        for key, value in changes.items():
            setattr(task, key, value)
        await self._workflow_set_plan(
            run,
            pause=run.status == "planned",
            modified=plan_to_yaml(run.tasks) != previous_source,
        )
        await self._apersist_runs()
        return {
            "index": task.index,
            "title": task.title,
            "description": task.description,
            "depends_on": task.depends_on,
            "requires_approval": task.requires_approval,
            "force_approval": task.force_approval,
        }

    async def execute_plan(
        self,
        task_id: str,
        agent: str = "",
        fresh: bool = False,
        workspace_dir: str = "",
        auto_approve: bool = False,
    ) -> str:
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        restartable = {"planned", "paused", "cancelled", "failed"}
        if run.status not in restartable:
            raise ValueError(f"Run {task_id} is not in a startable state (status={run.status})")

        # Optional per-run workspace override: only applied to a run that has NOT
        # begun yet (status "planned"). A resumed run (paused/cancelled/failed) keeps
        # its original work_dir, so re-targeting the folder can't orphan work already
        # produced there (files/commits, git worktree state). The path is still
        # resolved+validated below regardless of status (audit/sensitive-path guard).
        _override = _resolve_workspace_dir(workspace_dir)
        if _override and run.status == "planned":
            run.work_dir = _override

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
            await self._apersist_runs()

        self._grant_run_trust(run, bool(auto_approve))
        await self._apersist_runs()

        self._agent = agent
        history_key = f"taskrunner:run:{task_id}"

        async def _execute() -> None:
            watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
            try:
                run.status = "running"
                run.started_at = run.last_task_time = time.time()
                await self._workflow_rebind(run)
                await self._apersist_runs()  # persist immediately so crash recovery works
                if run.execution_mode == "review_fix":
                    if not run.work_dir or not Path(run.work_dir).is_dir():
                        raise ValueError("review-fix candidate worktree is unavailable")
                    run.git_enabled = True
                    if run.review_fix and run.review_fix.git.candidate_branch:
                        run.branch_name = run.review_fix.git.candidate_branch
                else:
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
                    if run.execution_mode == "review_fix":
                        await self.mutate_review_fix(
                            task_id,
                            expected_revision=run.revision,
                            action="execution_complete",
                            expected_state=ReviewFixState.RUNNING,
                            to_state=ReviewFixState.AWAITING_VALIDATION,
                            mutate=lambda metadata: setattr(metadata, "blocked_reason", ""),
                        )
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
                if run.execution_mode == "review_fix" and run.review_fix:
                    try:
                        error_message = str(exc)[:2000]
                        await self.mutate_review_fix(
                            task_id,
                            expected_revision=run.revision,
                            expected_state=ReviewFixState.RUNNING,
                            action="execution_failed",
                            to_state=ReviewFixState.FAILED,
                            mutate=lambda metadata: setattr(
                                metadata, "blocked_reason", error_message
                            ),
                        )
                    except Exception:
                        logger.debug("review-fix failure state update failed", exc_info=True)
                await self._notify("\u274c Task error", str(exc), run=run)
            finally:
                try:
                    await asyncio.shield(self._cleanup_run_sessions(run))
                except asyncio.CancelledError:
                    pass  # shield was cancelled but cleanup completed
                # Finalize cancel status after cleanup
                if run.status in ("cancelling", "pausing"):
                    run.status = "paused" if run.status == "pausing" else "cancelled"
                if (
                    run.execution_mode == "review_fix"
                    and run.review_fix
                    and run.review_fix.state is ReviewFixState.RUNNING
                    and run.status == "paused"
                ):
                    try:
                        await self.mutate_review_fix(
                            task_id,
                            expected_revision=run.revision,
                            expected_state=ReviewFixState.RUNNING,
                            action="execution_paused",
                            to_state=ReviewFixState.PAUSED,
                            mutate=lambda metadata: None,
                        )
                    except Exception:
                        logger.debug("review-fix pause state update failed", exc_info=True)
                run.finished_at = time.time()
                save_progress(run)
                await self._apersist_runs()
                if run.branch_name and run.execution_mode != "review_fix":
                    try:
                        await git_coord.finalize(run)
                    except Exception:
                        logger.debug("Git finalize failed", exc_info=True)
                await self._workflow_finalize(run)
                if watchdog_task and not watchdog_task.done():
                    watchdog_task.cancel()
                if self._consolidator:
                    self._consolidator.maybe_consolidate(history_key)
                self._tasks.pop(task_id, None)

        self._tasks[task_id] = asyncio.create_task(_execute())
        return task_id

    async def execute_review_fix(
        self,
        task_id: str,
        *,
        agent: str = "",
        fresh: bool = False,
        auto_approve: bool = False,
    ) -> str:
        """Run confirmed review-fix tasks in their retained candidate worktree."""
        run = self.get_review_fix(task_id)
        metadata = run.review_fix
        assert metadata is not None
        allowed = {
            ReviewFixState.AWAITING_GROUP_CONFIRMATION,
            ReviewFixState.PAUSED,
            ReviewFixState.FAILED,
            ReviewFixState.BLOCKED_VALIDATION,
            ReviewFixState.BLOCKED_MODEL_RESOLUTION,
        }
        if metadata.state not in allowed:
            raise ValueError(f"Review-fix task is not executable in state {metadata.state.value}")
        if metadata.state is ReviewFixState.AWAITING_GROUP_CONFIRMATION and any(
            group.state is not ReviewFixGroupState.CONFIRMED for group in metadata.groups
        ):
            raise ValueError("Review-fix grouping must be confirmed before execution")
        if not metadata.model.resolved_model_id:
            await self.mutate_review_fix(
                task_id,
                expected_revision=run.revision,
                action="model_resolution_required",
                expected_state=metadata.state,
                to_state=ReviewFixState.BLOCKED_MODEL_RESOLUTION,
                mutate=lambda current: setattr(
                    current, "blocked_reason", "a concrete model pin is required"
                ),
            )
            raise ValueError("Review-fix model resolution is unavailable")

        prior_state = metadata.state
        try:
            await self.mutate_review_fix(
                task_id,
                expected_revision=run.revision,
                action="start_execution",
                expected_state=prior_state,
                to_state=ReviewFixState.RUNNING,
                mutate=lambda current: setattr(current, "blocked_reason", ""),
            )
            run = self.get_review_fix(task_id)
            metadata = run.review_fix
            assert metadata is not None
            if run.status not in {"planned", "paused", "cancelled", "failed"}:
                run.status = "planned"
            run.work_dir = metadata.git.candidate_worktree_path or run.work_dir
            run.branch_name = metadata.git.candidate_branch or run.branch_name
            run.git_enabled = True
            await self._apersist_runs()
            return await self.execute_plan(
                task_id,
                agent=agent,
                fresh=fresh,
                workspace_dir=run.work_dir,
                auto_approve=auto_approve,
            )
        except Exception:
            # execute_plan can reject before its background task exists (planner
            # failure, concurrency cap), and nothing else would then move the run
            # out of RUNNING — which every later gate treats as "work in flight".
            await self._rollback_review_fix_start(task_id, prior_state)
            raise

    async def _rollback_review_fix_start(self, task_id: str, prior_state: ReviewFixState) -> None:
        """Undo a start that failed between the RUNNING transition and launch.

        Best effort by contract: the caller is about to re-raise the original
        failure, so a rollback that itself loses a race (another actor already
        moved the run) is logged, never allowed to mask it. Restoring only from
        the exact state we transitioned to keeps a concurrent, legitimate move
        out of RUNNING from being clobbered.
        """
        try:
            current = self.get_review_fix(task_id)
            if current.review_fix is None or current.review_fix.state is not ReviewFixState.RUNNING:
                return
            await self.mutate_review_fix(
                task_id,
                expected_revision=current.revision,
                expected_state=ReviewFixState.RUNNING,
                action="start_rolled_back",
                to_state=prior_state,
                mutate=lambda metadata: None,
            )
        except Exception:
            logger.warning("review-fix start rollback failed for %s", task_id, exc_info=True)

    def plan_to_chat_context(self, task_id: str) -> str:
        run = self._runs.get(task_id)
        if not run:
            raise ValueError(f"Run {task_id} not found")
        return _planner_plan_to_chat_context(run)

    # ── Core Execution ──

    async def run(
        self,
        spec_path: str | Path,
        task_id: str = "",
        name: str = "",
        source: str = "",
        workspace_dir: str = "",
        auto_approve: bool = False,
    ) -> Project:
        spec_path = Path(spec_path)
        if not spec_path.exists():
            raise FileNotFoundError(f"Spec not found: {spec_path}")
        spec_content = spec_path.read_text(encoding="utf-8").strip()
        if not spec_content:
            raise ValueError("Spec file is empty")
        if not task_id:
            task_id = f"{spec_path.stem}_{int(time.time())}"
        _override = _resolve_workspace_dir(workspace_dir)
        _effective_ws = _override or self._workspace_dir
        task_dir = Path(_effective_ws) if _effective_ws else self._work_dir / spec_path.stem
        task_dir.mkdir(parents=True, exist_ok=True)
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
        )
        run.task_id = task_id
        run.name = name or auto_name(spec_content, str(spec_path))
        run.work_dir = str(task_dir)
        self._grant_run_trust(run, bool(auto_approve))
        self._runs[task_id] = run
        watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
        history_key = f"taskrunner:run:{spec_path.stem}"
        try:
            await self._workflow_begin(run)
            await self._workflow_rebind(run)
            await self._apersist_runs()  # persist immediately so crash recovery works
            await self._notify("\U0001f680 Task started", f"Spec: `{spec_path.name}`", run=run)
            if source == "yaml":
                run.tasks = _decompose_yaml_with_audit(spec_content, task_id)
            elif not source and spec_path.suffix in (".yaml", ".yml"):
                try:
                    run.tasks = _decompose_yaml_with_audit(spec_content, task_id)
                except (ValueError, KeyError):
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
            await self._apersist_runs()
            if run.branch_name:
                try:
                    await git_coord.finalize(run)
                except Exception:
                    logger.debug("Git finalize failed", exc_info=True)
            await self._workflow_finalize(run)
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
                await self._apersist_runs()  # persist after each task so crash recovery preserves progress
            else:
                titles = ", ".join(t.title for t in resolved)
                await self._notify(
                    "\u26a1 Parallel group", f"Running {len(resolved)} tasks: {titles}", run=run
                )
                # Bound concurrency with a semaphore sized by the configurable
                # `taskrunner.max_parallel_steps` knob (self._max_parallel_steps),
                # not a hardcoded batch size. All ready tasks are dispatched at once
                # and the semaphore caps how many run simultaneously, so a slow task
                # no longer stalls a whole fixed-size batch. The knob is the single
                # place to lift concurrency (capped by compute_max_subagents ceiling).
                results: list[bool | BaseException] = []
                sem = asyncio.Semaphore(self._max_parallel_steps)

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
                await self._apersist_runs()  # persist after parallel group so crash recovery preserves progress

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
        service = self._workflow_service
        if service is not None and run.workflow_run_id:
            try:
                await service.step(run.workflow_run_id, task.index, task.title, status="running")
            except Exception:
                logger.debug("TaskRunner workflow step start publication failed", exc_info=True)
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
            # executing elsewhere edits/tests the wrong project). Mirrors
            # _build_task_prompt's resolution above.
            work_dir=Path(run.work_dir) if run.work_dir else self._work_dir,
            log_task_fn=self._log_task,
            extract_lesson_fn=self._extract_lesson,
            session_key=session_key,
        )
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
        await self._workflow_set_plan(run, modified=True)
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

    async def start_background(
        self,
        spec_path: str | Path,
        agent: str = "",
        name: str = "",
        source: str = "",
        workspace_dir: str = "",
        auto_approve: bool = False,
        *,
        session_key: str = "",
    ) -> str:
        """Plan and execute *spec_path* in the background; returns the task id.

        ``session_key`` is keyword-only and optional so every existing caller is
        unchanged. It names the conversation this run was started FROM and is
        handed to the notify sink, which is what lets a stall-worthy notice (an
        approval request, a denial) reach the surface the operator started the
        task on rather than one hard-wired destination.
        """
        # Validate the per-run workspace override before entering the admission
        # lock so a bad/sensitive path fails without blocking other starts.
        _resolve_workspace_dir(workspace_dir)
        self._agent = agent
        try:
            from kiro_crew.hooks import validate_file_path

            safe_sp = validate_file_path(str(spec_path))
            if safe_sp:
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
            active = sum(1 for task in self._tasks.values() if not task.done())
            if active >= _MAX_CONCURRENT_TASKS:
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
            while task_id in self._runs or task_id in self._tasks:
                id_suffix += 1
                task_id = f"{Path(spec_path).stem}_{id_suffix}"

            self._runs[task_id] = Project(
                spec_path=str(spec_path),
                spec_content=early_content,
                task_id=task_id,
                name=name or Path(spec_path).stem,
                status="planning",
                started_at=time.time(),
                source=source,
                auto_approve=bool(auto_approve),
            )
            if session_key:
                self._run_session_keys[task_id] = session_key
            await self._workflow_begin(self._runs[task_id])
            try:
                await self._apersist_runs()  # durable before background execution
            except BaseException:
                try:
                    await asyncio.shield(self._workflow_delete_link(self._runs[task_id]))
                finally:
                    self._runs.pop(task_id, None)
                    self._run_session_keys.pop(task_id, None)
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
            await self._release_run_runtime(run)
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
        # Kill the run's shared AcpRuntime after its per-step sessions are torn
        # down (one kiro-cli process for the whole run).
        await self._release_run_runtime(run)

    def _grant_run_trust(self, run: Project, enabled: bool) -> None:
        """Single owner of per-run trust — sets the persisted UI intent flag AND
        the authoritative SafetyOverride scoped grant together, so the two
        representations can never diverge at a call site. Enable activates an
        audited, TTL-bounded scoped grant; disable revokes it.
        """
        run.auto_approve = bool(enabled)
        scope = _auto_approve_scope(run.task_id)
        if run.auto_approve:
            safety_override().activate_scoped(scope, source="dashboard")
        else:
            safety_override().deactivate_scope(scope)

    async def _release_run_runtime(self, run: Project) -> None:
        """Kill the run's shared AcpRuntime once (idempotent) at run teardown.

        The task runner routes every step (decompose/tasks/self_review/replan)
        onto one run-scoped runtime keyed ``{prefix}:{task_id}:runtime`` via
        ``SessionManager.open_task_session``; this frees that process exactly
        once on any termination path (success/fail/cancel). No-op if absent.
        """
        try:
            await self._sessions.release_subagent_runtime(
                f"{_SESSION_PREFIX}:{run.task_id}:runtime"
            )
        except Exception:
            logger.debug("release run runtime failed for %s", run.task_id, exc_info=True)
        # Revoke the per-run auto-approve grant so trust never outlives the run.
        try:
            safety_override().deactivate_scope(_auto_approve_scope(run.task_id))
        except Exception:
            logger.debug("deactivate auto-approve scope failed for %s", run.task_id, exc_info=True)

    async def delete_run(self, task_id: str) -> bool:
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
        self._run_session_keys.pop(task_id, None)
        await self._apersist_runs()
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

    async def retry_from_task(self, task_id: str, from_task: int, agent: str = "") -> str:
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
        await self._apersist_runs()  # persist immediately so crash recovery works
        self._agent = agent
        history_key = f"taskrunner:run:{Path(run.spec_path).stem}"

        async def _retry() -> None:
            watchdog_task: asyncio.Task | None = None  # type: ignore[type-arg]
            try:
                await self._workflow_rebind(run)
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
                await self._apersist_runs()
                await self._workflow_finalize(run)
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
        # The originating conversation is per-run, so it is resolved from the run
        # rather than passed at each of the ~20 call sites. A notification with no
        # run attached (a lesson learned, say) carries no conversation and falls
        # back to the sink's own default destination.
        session_key = self._run_session_keys.get(run.task_id, "") if run else ""
        await notify(title, body, run=run, callback=self._on_notify, session_key=session_key)

    # ── History Integration ──

    def _log_task(self, history_key: str, run: Project, task: Task) -> None:
        if not self._conversation_log:
            return
        spec_name = Path(run.spec_path).name if run.spec_path else run.task_id
        user_msg = f"[Task: {spec_name}] Task {task.index}: {task.title}"
        result_summary = task.result[:2000] if task.result else "Task completed."
        log = self._conversation_log

        def _do() -> None:
            # Both appends run on ONE worker thread so they persist IN ORDER —
            # two independent append_off_loop dispatches could interleave on the
            # default executor and reorder the transcript — and take the patient
            # off-loop cross-process lock acquire path.
            log.append(history_key, "user", user_msg)
            log.append(history_key, "assistant", result_summary)

        # _log_task is invoked from async task_executor code running ON the
        # event loop. A direct append there hits _locked's fail-fast on-loop
        # path (raises HistoryLockTimeout under benign contention, silently
        # swallowed) and risks a synchronous disk write on the loop. Offload to
        # a worker thread so it takes the blocking off-loop acquire and can
        # neither stall the loop nor drop the write under contention.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            try:
                _do()
            except Exception:
                logger.debug("Failed to log task to conversation history", exc_info=True)
            return

        def _report(fut: "asyncio.Future[None]") -> None:
            exc = fut.exception()
            if exc is not None:
                logger.debug("Failed to log task to conversation history: %r", exc)

        loop.run_in_executor(None, _do).add_done_callback(_report)

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
                    rule,
                    category,
                    negative,
                    "task_runner",
                )
            else:
                # Offloaded for the same reason as write_lesson above, and now
                # necessarily so: LessonStore locks per PATH, so instances in
                # different components share one lock. A dashboard writer holding
                # it across its read-and-rewrite would stall this loop if save()
                # ran here. The lock is what makes the write atomic, so the fix is
                # to move the caller off the loop rather than to weaken it.
                await asyncio.to_thread(
                    self._lesson_store.save,
                    Lesson(
                        ts=datetime.now(tz=timezone.utc).isoformat(),
                        rule=rule,
                        category=category,
                        negative=negative,
                    ),
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

    # ── Runs Persistence ──

    _RUNS_FILE = "runs.json"

    def _runs_path(self) -> Path:
        return self._work_dir / self._RUNS_FILE

    def _persist_runs(self) -> None:
        # Synchronous compatibility helper for internal/off-loop callers and
        # focused persistence tests. Production mutation APIs await
        # _apersist_runs so the fsync-backed atomic write never blocks the
        # gateway event loop.
        self._commit_snapshot(self._next_persist_seq(), self._serialize_runs())

    def _serialize_runs(self) -> str:
        """Serialize the runs registry to a JSON string.

        MUST be called on the thread that owns ``_runs`` (the event loop).
        Iterating the live registry in a worker thread while the loop mutates
        it can raise ``RuntimeError: dictionary changed size during iteration``
        or capture a torn snapshot, so persistence always snapshots here first
        and offloads only the byte-level write.
        """
        data = []
        for run in self._runs.values():
            if run.source == "cron":
                continue
            if run.status in (
                "planning",
                "planned",
                "running",
                "cancelling",
                "pausing",
                "paused",
                "completed",
                "failed",
                "cancelled",
            ):
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
                        # Git-workspace identity. `work_dir` alone is NOT enough:
                        # init_workspace() OVERWRITES it with the worktree path,
                        # so a restored run pointed at a worktree while every
                        # field that says which worktree it is -- and whether git
                        # coordination is even on -- came back at its default.
                        # git_coord reads all six after a restart.
                        "branch_name": run.branch_name,
                        "base_branch": run.base_branch,
                        "worktree_path": run.worktree_path,
                        "repo_root": run.repo_root,
                        "git_enabled": run.git_enabled,
                        "commit_hashes": run.commit_hashes,
                        # Produced once by `_extract_lesson` during the run and
                        # NOT recomputable: the lesson text is separately
                        # durable in the lesson store, but that corpus is
                        # global and keyed by category, so this per-run
                        # attribution exists only here. Two consumers read it
                        # after a restart -- the status payload
                        # (task_reporter.build_status) and the to-chat
                        # continuation prompt.
                        "lessons_learned": run.lessons_learned,
                        "original_input": run.original_input,
                        "source": run.source,
                        "spec_content": run.spec_content,
                        "auto_approve": run.auto_approve,
                        "workflow_run_id": run.workflow_run_id,
                        "workflow_id": run.workflow_id,
                        "workflow_slug": run.workflow_slug,
                        "workflow_revision": run.workflow_revision,
                        "derived_from_workflow_id": run.derived_from_workflow_id,
                        "derived_from_revision": run.derived_from_revision,
                        **(
                            {
                                "review_fix": run.review_fix.to_dict(),
                                "revision": run.revision,
                                "execution_mode": run.execution_mode,
                                "commit_policy": run.commit_policy,
                            }
                            if run.review_fix is not None
                            else {}
                        ),
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
        return json.dumps(data)

    def _next_persist_seq(self) -> int:
        # Handed out on the event-loop thread only (both _persist_runs and the
        # pre-offload part of _apersist_runs run there), so the bump needs no
        # lock — it establishes the causal order of snapshots.
        self._persist_seq += 1
        return self._persist_seq

    def _commit_snapshot(self, seq: int, payload: str) -> None:
        # Serialize concurrent writers and enforce monotonic ordering under the
        # lock: a stale snapshot (older seq) whose offloaded write is scheduled
        # late must never overwrite a newer one that already landed.
        with self._persist_lock:
            if seq < self._persist_written:
                return
            try:
                # Atomic write: serialize to a temp file in the same dir, fsync,
                # then os.replace onto the final path so a crash/kill/full-disk
                # mid-write can never leave a truncated registry that
                # _load_runs would otherwise have to discard.
                atomic_write(self._runs_path(), payload, fsync=True)
            except OSError:
                logger.debug("Failed to persist runs", exc_info=True)
                return
            self._persist_written = seq

    async def _apersist_runs(self) -> None:
        """Persist the runs registry without blocking the event loop.

        The JSON snapshot is built synchronously on THIS (event-loop) thread —
        which owns ``_runs`` — so we never iterate the live registry in a
        worker while the loop mutates it (that could raise ``dictionary
        changed size during iteration`` or capture a torn snapshot). Only the
        blocking, fsync-backed atomic write is offloaded to a worker thread, so
        a slow/full disk can't stall the loop, while a per-snapshot sequence
        number preserves write ordering. Every production mutation API awaits
        this method before returning, preserving durability without blocking
        unrelated gateway work.
        """
        seq = self._next_persist_seq()
        payload = self._serialize_runs()
        await asyncio.to_thread(self._commit_snapshot, seq, payload)

    def _load_runs(self) -> None:
        path = self._runs_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # No registry yet — seed a fresh one rather than treating a
            # missing file as an error.
            return
        except OSError:
            # File exists but is unreadable (permission error, transient
            # sharing violation, etc.). _load_runs is called from
            # TaskRunner.__init__, so raising here would prevent the runner —
            # and potentially the gateway — from starting. Log loudly and
            # start with an empty in-memory registry without touching the file
            # on disk (so a later, successful read can still recover it).
            logger.error(
                "Failed to read runs registry %s; starting with an empty "
                "registry (file left untouched)",
                path,
                exc_info=True,
            )
            return
        try:
            items = json.loads(raw)
        except (ValueError, OSError) as exc:
            # Never silently discard run state on a corrupt/truncated file:
            # surface the corruption loudly and preserve the bad file as a
            # sidecar for recovery instead of returning an empty registry.
            bak = path.with_suffix(path.suffix + ".corrupt")
            logger.error(
                "Runs registry %s is corrupt (%s); preserved as %s, starting "
                "with an empty registry",
                path,
                exc,
                bak,
            )
            try:
                path.replace(bak)
            except OSError:
                logger.warning("Failed to preserve corrupt runs registry", exc_info=True)
            return
        try:
            for item in items:
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
                _worktree_path = item.get("worktree_path", "")
                review_fix_raw = item.get("review_fix")
                review_fix = (
                    ReviewFixMetadata.from_dict(review_fix_raw)
                    if isinstance(review_fix_raw, Mapping)
                    else None
                )
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
                    branch_name=item.get("branch_name", ""),
                    base_branch=item.get("base_branch", ""),
                    worktree_path=_worktree_path,
                    repo_root=item.get("repo_root", ""),
                    # An entry written before the git identity was persisted
                    # carries none of these, and `git_enabled` defaults to True
                    # -- the one combination that must not survive: git ops
                    # enabled while nothing records WHICH worktree they target.
                    # Absent an explicit value, enable git coordination only
                    # when the worktree location is actually known; otherwise
                    # fall back to the documented "task continues without git
                    # coordination" behaviour instead of committing into a path
                    # no longer identified.
                    git_enabled=bool(item.get("git_enabled", bool(_worktree_path))),
                    commit_hashes=list(item.get("commit_hashes", [])),
                    lessons_learned=list(item.get("lessons_learned", [])),
                    original_input=item.get("original_input", ""),
                    source=item.get("source", ""),
                    tasks=tasks,
                    auto_approve=item.get("auto_approve", False),
                    workflow_run_id=item.get("workflow_run_id", ""),
                    workflow_id=item.get("workflow_id", ""),
                    workflow_slug=item.get("workflow_slug", ""),
                    workflow_revision=int(item.get("workflow_revision") or 0),
                    derived_from_workflow_id=item.get("derived_from_workflow_id", ""),
                    derived_from_revision=int(item.get("derived_from_revision") or 0),
                    review_fix=review_fix,
                    revision=int(
                        item.get("revision", review_fix.revision if review_fix else 0) or 0
                    ),
                    execution_mode=item.get(
                        "execution_mode", "review_fix" if review_fix else "standard"
                    ),
                    commit_policy=item.get(
                        "commit_policy", "manual_group" if review_fix else "per_task"
                    ),
                )
                self._runs[run.task_id] = run
                # Compensating control: never let per-run trust silently survive a
                # gateway restart. A run recovered from an active state had its
                # auto-approve granted for a live, attended launch; after a crash the
                # user must re-affirm trust on resume (execute_plan re-applies it from
                # the dashboard toggle).
                if run.status in ("running", "pausing", "cancelling"):
                    run.auto_approve = False
                    safety_override().deactivate_scope(_auto_approve_scope(run.task_id))
                # Crash recovery: if gateway died mid-execution, mark as resumable
                if run.status in ("running", "pausing"):
                    if not run.tasks:
                        run.status = "failed"
                        run.error = "Gateway crashed before task decomposition completed — re-run to continue"
                        logger.info(
                            "Recovered crashed run %s with no tasks — marked as failed", run.task_id
                        )
                    else:
                        run.status = "paused"
                        run.error = (
                            run.error or "Gateway crashed during execution — resume to continue"
                        )
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
                    logger.info(
                        "Recovered crashed cancelling run %s — marked as cancelled", run.task_id
                    )
        except Exception:
            # A structural error while rebuilding an individual run (not a
            # parse failure — that is handled above) is surfaced loudly and
            # leaves the runs already loaded intact, rather than silently
            # discarding the whole registry.
            logger.error("Failed to deserialize a run from registry %s", path, exc_info=True)

    def _save_progress(self, run: Project) -> None:
        save_progress(run)

    def _load_checkpoint(self, spec_path: Path) -> set[str] | None:
        return load_checkpoint(spec_path)

    def _build_resume_context(self, completed: list[Task]) -> str:
        return build_resume_context(completed)

    def status(self) -> dict:
        return build_status(self._runs, self._tasks, self._agent)

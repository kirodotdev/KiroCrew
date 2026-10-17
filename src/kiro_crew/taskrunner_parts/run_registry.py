"""The task-run registry: ``runs.json`` snapshots, their restore, and the per-run checkpoints.

Snapshots are built on the event loop and written off it in a fenced order, so an older
snapshot never lands over a newer one; a restart restores every run and settles the ones a
crash interrupted. The ``TASK_PROGRESS.md`` checkpoints and the status view read the same
registry. A new persisted run field is written and read back here.

Instance fields written: ``_persist_failed``, ``_persist_seq``, ``_persist_written``,
``_runs``, ``_snapshot_recovery_incomplete``.

Instance fields read: ``_agent``, ``_persist_lock``, ``_tasks``, ``_work_dir``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.atomic_write import atomic_write
from kiro_crew.execution_context import capture_session_execution, execution_from_record
from kiro_crew.safety_override import safety_override
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_reporter import (
    build_resume_context,
    build_status,
    load_checkpoint,
    save_progress,
)
from kiro_crew.taskrunner_parts.run_scope import _SESSION_PREFIX, _auto_approve_scope
from kiro_crew.workflow_memory import TaskSnapshotError

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


def _runs_path(self: TaskRunner) -> Path:
    return self._work_dir / self._RUNS_FILE


def _persist_runs(self: TaskRunner) -> None:
    # Synchronous compatibility helper for internal/off-loop callers and
    # focused persistence tests. Production mutation APIs await
    # _apersist_runs so the fsync-backed atomic write never blocks the
    # gateway event loop.
    try:
        self._commit_snapshot(self._next_persist_seq(), self._serialize_runs())
    except TaskSnapshotError:
        pass  # Legacy synchronous callers do not acknowledge durable admission.


def _serialize_runs(self: TaskRunner) -> str:
    """Serialize the runs registry to a JSON string.

    MUST be called on the thread that owns ``_runs`` (the event loop).
    Iterating the live registry in a worker thread while the loop mutates
    it can raise ``RuntimeError: dictionary changed size during iteration``
    or capture a torn snapshot, so persistence always snapshots here first
    and offloads only the byte-level write.
    """
    data: list[dict] = []
    for run in self._runs.values():
        if run.source == "cron" or (
            run.execution_context and run.execution_context.memory_mode != "persistent"
        ):
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
                    **(
                        {"execution_context": run.execution_context.to_record()}
                        if run.execution_context
                        else {}
                    ),
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
                            # Durable so an ambiguous-delivery resume hint set
                            # on a crash-recovery retry survives a gateway
                            # restart; without it a restart in that window
                            # would restore the task to a verbatim replay of a
                            # possibly-executed step.
                            "resume_hint": t.resume_hint or "",
                            "verification": t.verification,
                        }
                        for t in run.tasks
                    ],
                }
            )
    return json.dumps(data)


def _next_persist_seq(self: TaskRunner) -> int:
    # Handed out on the event-loop thread only (both _persist_runs and the
    # pre-offload part of _apersist_runs run there), so the bump needs no
    # lock — it establishes the causal order of snapshots.
    self._persist_seq += 1
    return self._persist_seq


def _commit_snapshot(self: TaskRunner, seq: int, payload: str) -> None:
    # Serialize concurrent writers and enforce monotonic ordering under the
    # lock: a stale snapshot (older seq) whose offloaded write is scheduled
    # late must never overwrite a newer one that already landed.
    with self._persist_lock:
        if self._snapshot_recovery_incomplete:
            raise TaskSnapshotError(
                "Task snapshot recovery incomplete; restart after storage recovers"
            )
        if seq < self._persist_written:
            return
        if seq < self._persist_failed:
            raise TaskSnapshotError("A newer task snapshot failed; retry the operation")
        try:
            # Atomic write: serialize to a temp file in the same dir, fsync,
            # then os.replace onto the final path so a crash/kill/full-disk
            # mid-write can never leave a truncated registry that
            # _load_runs would otherwise have to discard.
            from kiro_crew.workflow_memory import WorkflowMemoryError, write_task_snapshot

            write_task_snapshot(self._runs_path(), payload, writer=atomic_write)
        except (OSError, ValueError, WorkflowMemoryError) as exc:
            self._persist_failed = max(self._persist_failed, seq)
            logger.warning("Failed to persist task snapshot (%s)", type(exc).__name__)
            raise TaskSnapshotError(
                "Task snapshot persistence failed; retry the operation"
            ) from None
        self._persist_written = seq


async def _apersist_runs(self: TaskRunner) -> None:
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
    worker = asyncio.create_task(asyncio.to_thread(self._commit_snapshot, seq, payload))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            if not cancelled:
                raise
    if cancelled:
        # Retrieve a late write error, but preserve the caller's cancellation.
        if not worker.cancelled():
            worker.exception()
        raise asyncio.CancelledError
    worker.result()


def _load_runs(self: TaskRunner) -> None:
    path = self._runs_path()
    try:
        from kiro_crew.workflow_memory import read_task_registry

        try:
            raw = read_task_registry(path)
        except TaskSnapshotError:
            self._snapshot_recovery_incomplete = True
            logger.error("Task snapshot recovery incomplete; writes require a restart")
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
        self._snapshot_recovery_incomplete = True
        logger.error(
            "Failed to read runs registry %s; starting with an empty "
            "registry (file left untouched)",
            path,
            exc_info=True,
        )
        return
    try:
        items = json.loads(raw)
    except ValueError as exc:
        # Never silently discard run state on a corrupt/truncated file:
        # surface the corruption loudly and preserve the bad file as a
        # sidecar for recovery instead of returning an empty registry.
        bak = path.with_suffix(path.suffix + ".corrupt")
        logger.error(
            "Runs registry %s is corrupt (%s); preserved as %s, starting " "with an empty registry",
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
        from kiro_crew.workflow_memory import read_task_snapshot

        legacy: list[dict] = []
        items = json.loads(read_task_snapshot(path, public_payload=raw, legacy_references=legacy))
    except Exception as exc:
        self._snapshot_recovery_incomplete = True
        logger.error("Failed to read task snapshot (%s)", type(exc).__name__)
        return
    if legacy:
        # A row of exactly the pre-release shape is left out of the restored
        # runs rather than refusing the registry: its payload in the hidden
        # sidecar is never read, so the task cannot resume. Writes are not
        # fenced by it, so the next snapshot rewrites the registry without
        # it; a restart that finds the same rows again logs this once more.
        logger.warning(
            "Left out %d task record(s) from a 0.7.0 pre-release (%s); their private "
            "payloads were not read and those tasks cannot resume. Re-create them to "
            "run them again.",
            len(legacy),
            ", ".join(row["task_id"] for row in legacy),
        )
    for item in items:
        try:
            execution_context = execution_from_record(item, required=False)
            if execution_context is None and any(
                key in item for key in ("member_id", "memory_store", "memory_mode")
            ):
                # Legacy task snapshots may predate canonical execution
                # records. Recover named V1 routing from this run's own
                # runtime metadata, never from the current gateway session
                # (which would silently select Global after a restart).
                task_id = item.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    raise TaskSnapshotError("Task run has no stable identity")
                runtime_key = f"{_SESSION_PREFIX}:{task_id}:runtime"
                from kiro_crew.history import ConversationLog

                metadata, readable = ConversationLog().get_metadata_status(runtime_key)
                if not readable or not isinstance(metadata, dict):
                    raise TaskSnapshotError(
                        "Task run execution identity is unreadable; Global was not used"
                    )
                if not metadata or not any(
                    key in metadata for key in ("execution_context", "memory_store")
                ):
                    raise TaskSnapshotError(
                        "Task run execution identity is unavailable; Global was not used"
                    )
                execution_context = capture_session_execution(runtime_key)
            tasks = [
                Task(
                    index=t["index"],
                    title=t["title"],
                    description=t.get("description", ""),
                    status=TaskStatus(t["status"]),
                    error=t.get("error", ""),
                    result=t.get("result", ""),
                    resume_hint=t.get("resume_hint", ""),
                    verification=str(t.get("verification", "")),
                    attempts=t.get("attempts", 1),
                    depends_on=t.get("depends_on", []),
                    requires_approval=t.get("requires_approval", False),
                    force_approval=t.get("force_approval", False),
                )
                for t in item.get("task_details", item.get("tasks", []))
            ]
            _worktree_path = item.get("worktree_path", "")
            run = Project(
                spec_path=item["spec_path"],
                spec_content=item.get("spec_content", ""),
                execution_context=execution_context,
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
                # that is not identified.
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
            )
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
                    run.error = (
                        "Gateway crashed before task decomposition completed — re-run to continue"
                    )
                    logger.info(
                        "Recovered crashed run %s with no tasks — marked as failed", run.task_id
                    )
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
                logger.info(
                    "Recovered crashed cancelling run %s — marked as cancelled", run.task_id
                )
            self._runs[run.task_id] = run
        except Exception as exc:
            self._snapshot_recovery_incomplete = True
            logger.error("Failed to deserialize a task snapshot row (%s)", type(exc).__name__)


def _save_progress(self: TaskRunner, run: Project) -> None:
    save_progress(run)


def _load_checkpoint(self: TaskRunner, spec_path: Path) -> set[str] | None:
    return load_checkpoint(spec_path)


def _build_resume_context(self: TaskRunner, completed: list[Task]) -> str:
    return build_resume_context(completed)


def status(self: TaskRunner) -> dict:
    return build_status(self._runs, self._tasks, self._agent)

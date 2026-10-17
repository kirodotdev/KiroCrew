"""What belongs to one task run: its session namespace, identity, trust and teardown.

Every session a run opens is keyed under ``_SESSION_PREFIX:<task_id>:``; this module binds
the run's execution identity onto those keys and picks its history key, owns the per-run
auto-approve grant (the persisted intent flag and the SafetyOverride scope together), and
tears the run's sessions and shared runtime down on every exit path. New per-run state that
must be bound at start or released at the end goes here.

Instance fields written: none.

Instance fields read: ``_ctx``, ``_sessions``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.execution_context import ExecutionContext, bind_session_execution
from kiro_crew.safety_override import safety_override
from kiro_crew.task_models import SESSION_PREFIX, Project, TaskStatus
from kiro_crew.workflow_memory import capture_execution

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


_SESSION_PREFIX = SESSION_PREFIX


def _auto_approve_scope(task_id: str) -> str:
    """SafetyOverride scope key holding a run's per-run auto-approve grant.

    The live, TTL-bounded, audited grant lives in the SafetyOverride singleton
    (see ``activate_scoped``); ``Project.auto_approve`` is only the UI intent
    flag. Enforcement reads ``safety_override().is_scope_active(scope)``.
    """
    return f"{_SESSION_PREFIX}:{task_id}:autoapprove"


def _capture_execution(self: TaskRunner, session_key: str = "") -> ExecutionContext:
    execution = capture_execution(session_key)
    modes = getattr(self._ctx, "_session_memory_modes", None)
    if isinstance(modes, dict):
        execution = execution.with_mode(modes.get(session_key, "persistent"))
    return execution


async def _bind_run_execution(self: TaskRunner, run: Project, session_key: str) -> None:
    if run.execution_context is None:
        run.execution_context = self._capture_execution()
    execution = run.execution_context
    await asyncio.to_thread(bind_session_execution, session_key, execution)
    modes = getattr(self._ctx, "_session_memory_modes", None)
    if isinstance(modes, dict):
        modes[session_key] = execution.memory_mode


def _reset_incomplete_tasks(run: Project) -> None:
    """Mark in_progress/pending/reviewing tasks as cancelled (or keep pending if pausing)."""
    pausing = run.status == "pausing"
    for task in run.tasks:
        if task.status == TaskStatus.IN_PROGRESS:
            task.status = TaskStatus.PENDING if pausing else TaskStatus.CANCELLED
        elif task.status in (TaskStatus.PENDING, TaskStatus.REVIEWING):
            if not pausing:
                task.status = TaskStatus.CANCELLED


async def _cleanup_run_sessions(self: TaskRunner, run: Project) -> None:
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
            # Cancel cleanup: the run is over, so each step conversation ends and
            # takes its sub-agent runs with it.
            await self._sessions.reset(key, ends_conversation=True)
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


async def _grant_run_trust(self: TaskRunner, run: Project, enabled: bool) -> None:
    """Single owner of per-run trust — sets the persisted UI intent flag AND
    the authoritative SafetyOverride scoped grant together, so the two
    representations can never diverge at a call site. Enable activates an
    audited, TTL-bounded scoped grant; disable revokes it.

    Async, and both halves are offloaded: each writes a SEL event, and arming
    additionally consults the ``approval_modes`` policy. Both callers are async
    methods, so running either inline put that filesystem work on the gateway's
    event loop.

    The flag is set FROM the activation result, never ahead of it. Arming can
    now be refused -- an ``approval_modes`` deny of ``yolo`` disables scoped
    grants too -- and assigning the flag first meant a refused arm still
    persisted and reported ``auto_approve: True`` with no authoritative grant
    behind it. That is the exact divergence this function exists to prevent,
    so the refusal has to reach the flag.
    """
    scope = _auto_approve_scope(run.task_id)
    if enabled:
        result = await asyncio.to_thread(safety_override().activate_scoped, scope, "dashboard")
        run.auto_approve = bool(result.active)
    else:
        await asyncio.to_thread(safety_override().deactivate_scope, scope)
        run.auto_approve = False


async def _release_run_runtime(self: TaskRunner, run: Project) -> None:
    """Kill the run's shared AcpRuntime once (idempotent) at run teardown.

    The task runner routes every step (decompose/tasks/self_review/replan)
    onto one run-scoped runtime keyed ``{prefix}:{task_id}:runtime`` via
    ``SessionManager.open_task_session``; this frees that process exactly
    once on any termination path (success/fail/cancel). No-op if absent.
    """
    try:
        await self._sessions.release_subagent_runtime(f"{_SESSION_PREFIX}:{run.task_id}:runtime")
    except Exception:
        logger.debug("release run runtime failed for %s", run.task_id, exc_info=True)
    # Revoke the per-run auto-approve grant so trust never outlives the run.
    try:
        safety_override().deactivate_scope(_auto_approve_scope(run.task_id))
    except Exception:
        logger.debug("deactivate auto-approve scope failed for %s", run.task_id, exc_info=True)


async def _bound_history_key(self: TaskRunner, run: Project, legacy_key: str) -> str:
    runtime_key = f"{_SESSION_PREFIX}:{run.task_id}:runtime"
    await self._bind_run_execution(run, runtime_key)
    execution = run.execution_context
    history_key = (
        f"taskrunner:run:{run.task_id}"
        if execution and (execution.member_id or execution.memory_mode != "persistent")
        else legacy_key
    )
    await self._bind_run_execution(run, history_key)
    return history_key

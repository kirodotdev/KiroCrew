"""The ``TaskRunner`` side of the shared workflow-history port.

``WorkflowRunPublisher`` is the narrow port into the workflow service and
``WorkflowInitializing`` the refusal raised while the gateway has not attached it yet. The
functions here hold new work until attachment and mirror a run's lifecycle onto its shared
workflow run (begin, plan source, rebind, finalize, delete), best-effort once attached. A
new lifecycle event the workflow history should record goes here.

Instance fields written: ``_workflow_initialization_error``, ``_workflow_initializing``,
``_workflow_service``.

Instance fields read: ``_run_session_keys``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.execution_context import ExecutionContext
from kiro_crew.task_models import Project
from kiro_crew.task_planner import plan_to_yaml
from kiro_crew.workflow_memory import TaskSnapshotError

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


class WorkflowInitializing(RuntimeError):
    """Task mutations are unavailable until workflow attachment."""

    def __init__(self, message: str, *, code: str = "workflow_initializing") -> None:
        super().__init__(message)
        self.code = code


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
        execution_context: ExecutionContext | None = None,
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


# Named by the path callers import them from, which is what tracebacks and error
# chains render as ``module.qualname``.
WorkflowInitializing.__module__ = "kiro_crew.taskrunner"
WorkflowRunPublisher.__module__ = "kiro_crew.taskrunner"


def defer_workflow_attachment(self: TaskRunner, *, failed: bool = False) -> None:
    """Hold new work until attachment; a failed host requires a restart."""
    self._workflow_initializing = True
    self._workflow_initialization_error = (
        "Workflow initialization failed; restart the gateway." if failed else ""
    )


def _require_workflow_ready(self: TaskRunner) -> None:
    if self._workflow_initializing:
        error = getattr(self, "_workflow_initialization_error", "")
        if error:
            raise WorkflowInitializing(error, code="workflow_initialization_failed")
        raise WorkflowInitializing("Task runner is initializing workflows; retry shortly.")


def attach_workflow_service(self: TaskRunner, service: WorkflowRunPublisher | None) -> None:
    """Release admission with a ready port, or explicit standalone fallback."""
    self._workflow_service = service
    self._workflow_initializing = False


async def _workflow_begin(
    self: TaskRunner, run: Project, *, source: str = "", persist_link: bool = False
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
            execution_context=run.execution_context,
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
    except TaskSnapshotError:
        raise
    except Exception:
        logger.warning("TaskRunner workflow publication failed to start", exc_info=True)


async def _workflow_set_plan(
    self: TaskRunner,
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


def _mark_workflow_adapted(run: Project) -> None:
    """Replace exact saved-revision provenance with durable ancestry."""
    if run.workflow_id and run.workflow_revision and not run.derived_from_workflow_id:
        run.derived_from_workflow_id = run.workflow_id
        run.derived_from_revision = run.workflow_revision
    run.workflow_id = ""
    run.workflow_slug = ""
    run.workflow_revision = 0


async def _workflow_persist_replacement(self: TaskRunner, run: Project, *, source: str) -> bool:
    """Create and durably link a replacement shared workflow run."""
    run.workflow_run_id = ""
    await self._workflow_begin(run, source=source, persist_link=True)
    return bool(run.workflow_run_id)


async def _workflow_rebind(self: TaskRunner, run: Project) -> None:
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


async def _workflow_finalize(self: TaskRunner, run: Project) -> None:
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


async def _workflow_delete_link(self: TaskRunner, run: Project) -> None:
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

"""Plan mode: turning input into a planned project and editing it before it runs.

Decomposing a spec, YAML or free text into steps, starting a saved workflow definition,
editing a planned project's steps, and the plan views the dashboard and chat read. A new
way to build or edit a plan goes here.

Instance fields written: ``_agent``, ``_run_session_keys``, ``_runs``,
``_start_ids_in_flight``.

Instance fields read: ``_ctx``, ``_plan_task``, ``_sessions``, ``_start_lock``, ``_tasks``,
``_work_dir``, ``_workspace_dir``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.execution_context import ExecutionContext
from kiro_crew.start_priority import StartPriority
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_planner import (
    auto_name,
    decompose,
    group_parallel_tasks,
    normalize_cross_group_deps,
    parse_tasks,
)
from kiro_crew.task_planner import plan_to_chat_context as _planner_plan_to_chat_context
from kiro_crew.task_planner import (
    plan_to_yaml,
    update_plan_tasks,
)
from kiro_crew.taskrunner_parts.run_inputs import (
    _decompose_yaml_with_audit,
    _read_spec_text,
    _resolve_workspace_dir,
)
from kiro_crew.taskrunner_parts.run_scope import _SESSION_PREFIX
from kiro_crew.workflow_memory import capture_admission_execution

if TYPE_CHECKING:
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


def _auto_name(spec_content: str, spec_path: str = "") -> str:
    return auto_name(spec_content, spec_path)


def _resolve_task(self: TaskRunner, ref: str) -> Project | None:
    if ref in self._runs:
        return self._runs[ref]
    matches = [r for r in self._runs.values() if r.name == ref]
    return matches[-1] if matches else None


def _normalize_cross_group_deps(tasks: list[Task]) -> list[Task]:
    return normalize_cross_group_deps(tasks)


def _group_parallel_tasks(
    tasks: list[Task],
    completed_indices: set[int] | None = None,
) -> list[list[Task]]:
    return group_parallel_tasks(tasks, completed_indices)


def _parse_tasks(self: TaskRunner, text: str) -> list[Task]:
    return parse_tasks(text)


async def plan(
    self: TaskRunner,
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
    execution_context: ExecutionContext | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> Project:
    execution = await capture_admission_execution(
        self._ctx,
        session_key,
        execution_context=execution_context,
        capture_fn=self._capture_execution,
    )
    self._require_workflow_ready()
    self._agent = agent
    if source == "file":
        p = Path(spec_path)
        if not p.exists():
            raise FileNotFoundError(f"Spec not found: {spec_path}")
        content = await asyncio.to_thread(_read_spec_text, str(p), None)
        if content is None:
            raise PermissionError(f"Spec file refused by the file gate: {spec_path}")
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

    self._refresh_from_config()
    _override = _resolve_workspace_dir(workspace_dir)
    async with self._start_lock:
        self._require_workflow_ready()
        if self._admission_closed():
            raise ValueError("gateway admission is closed")
        id_suffix = time.time_ns()
        task_id = f"plan_{id_suffix}"
        while (
            task_id in self._runs or task_id in self._tasks or task_id in self._start_ids_in_flight
        ):
            id_suffix += 1
            task_id = f"plan_{id_suffix}"
        self._start_ids_in_flight.add(task_id)
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
        self._start_ids_in_flight.discard(task_id)
        raise
    try:
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
            execution_context=execution,
        )
        await self._bind_run_execution(run, f"{_SESSION_PREFIX}:{task_id}:runtime")
        if source == "yaml":
            run.tasks = _decompose_yaml_with_audit(
                decompose_input,
                task_id,
                source=source,
                spec_name=Path(spec_path).name if spec_path else "",
            )
        else:
            try:
                run.tasks = await asyncio.wait_for(
                    self._decompose(
                        decompose_input, run.work_dir, task_id, start_priority=start_priority
                    ),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                raise ValueError("Planning timed out. Try simplifying.")
            except asyncio.CancelledError:
                raise ValueError("Planning was cancelled.")
        if not run.tasks:
            raise ValueError("Could not generate a plan. Try rephrasing.")
    except BaseException:
        # Only the default plan directory belongs to this attempt. A caller's
        # workspace is an input and must survive a rejected plan unchanged.
        if created_task_dir:
            try:
                task_dir.rmdir()
            except OSError:
                logger.warning("Failed to remove rejected plan directory %s", task_dir)
        self._start_ids_in_flight.discard(task_id)
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
        self._start_ids_in_flight.discard(task_id)


async def start_workflow_definition(
    self: TaskRunner,
    definition: dict,
    *,
    input_text: str = "",
    author: str = "",
    session_key: str = "",
    execution_context: ExecutionContext | None = None,
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
        execution_context=execution_context,
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


def cancel_plan(self: TaskRunner) -> None:
    if self._plan_task and not self._plan_task.done():
        self._plan_task.cancel()


async def update_plan(self: TaskRunner, task_id: str, tasks: list[dict]) -> Project:
    self._require_workflow_ready()
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


async def update_task(self: TaskRunner, task_id: str, index: int, updates: dict) -> dict:
    """Update a single PENDING task in-place without resetting the run."""
    self._require_workflow_ready()
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


def plan_to_chat_context(self: TaskRunner, task_id: str) -> str:
    run = self._runs.get(task_id)
    if not run:
        raise ValueError(f"Run {task_id} not found")
    return _planner_plan_to_chat_context(run)


async def _decompose(
    self: TaskRunner,
    spec: str,
    work_dir: str = "",
    task_id: str = "",
    *,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> list[Task]:
    return await decompose(
        spec,
        self._sessions,
        self._ctx,
        work_dir=work_dir or str(self._work_dir),
        task_id=task_id,
        agent=self._agent,
        start_priority=start_priority,
    )

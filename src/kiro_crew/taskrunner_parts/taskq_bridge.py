"""How ``TaskRunner`` runs and steps become durable task-queue (``taskq``) rows.

Attaching the queue's admission, adopting the rows a dead gateway left behind, accepting
and claiming a run's container row and settling it from the final status, and admitting
each step through its lane before the step opens a session (``taskq.md`` § Runner
adapters). New queue behaviour for task runs goes here.

Instance fields written: ``_adopt_inflight``, ``_run_handles``, ``_task_admission``.

Instance fields read: ``_agent``, ``_run_session_keys``, ``_runs``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.task_models import PROGRESS_FILE, Project, Task

if TYPE_CHECKING:
    from kiro_crew.taskq.adapters import runner as _runner_adapter
    from kiro_crew.taskrunner import TaskRunner


# The facade's logger: records keep the name ``kiro_crew.taskrunner``.
logger = logging.getLogger("kiro_crew.taskrunner")


def attach_task_admission(
    self: TaskRunner, admission: "_runner_adapter.RunnerAdmission | None"
) -> None:
    """Route every step through the shared task queue and its lane.

    With an admission attached: a run is a ``taskrunner:<id>`` row, each
    step a child row admitted through ``RunnerAdmission.admit`` (deferred
    under memory pressure, bounded by the effective cap, claimed under a
    lease), and the run cap ``_MAX_CONCURRENT_TASKS`` does not refuse --
    excess steps queue in the lane instead. Rows a dead incarnation left
    behind are adopted on the running loop (``adopt_task_rows``).

    The sweep is armed only when the admission already HAS a store, as the
    workflow service's own attach does: the store getter is live, so a task
    armed while the store was still opening would otherwise adopt on the
    loop pass after it lands, concurrently with the sweep the gateway arms
    at that boundary -- two sweeps over one set of rows.
    """
    self._task_admission = admission
    if admission is None or admission.store is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    self._adopt_inflight = loop.create_task(self.adopt_task_rows())


async def adopt_task_rows(self: TaskRunner) -> "_runner_adapter.AdoptReport | None":
    """Resume the runs whose rows the boot reconciler left ``awaiting_adapter``.

    A run row that is safe to retry (``params.safe_retry`` -- the run is
    git-coordinated, so its checkpoint is the last committed step) is
    resumed through ``execute_plan``, which re-runs only the steps that
    did not PASS. One that is not safe stays ``paused`` for a human and
    its row is settled ``unknown_side_effect``. A safe row that
    ``RECOVERY_MAX_ATTEMPTS`` earlier gateway lives already resumed is
    settled ``failed`` instead of being resumed again: the run stays
    ``paused`` and a notice says that the limit counts every gateway stop,
    not only a crash. A row that was only ever ACCEPTED (``queued``: the
    crash landed while it waited for a lane slot) is settled ``cancelled``
    -- a resume is a NEW row, so nothing is lost, and no dispatcher exists
    for this kind to pick the old one up. One sweep at a time: a call that
    overlaps the sweep ``attach_task_admission`` started joins it instead
    of adopting the same rows twice.
    """
    inflight = self._adopt_inflight
    if inflight is not None and not inflight.done() and inflight is not asyncio.current_task():
        return await inflight
    admission = self._task_admission
    store = admission.store if admission is not None else None
    if store is None:
        return None
    from kiro_crew.taskq import model as _taskq_model
    from kiro_crew.taskq.adapters import runner as _runner_adapter

    resumable: list[str] = []

    def _resume(rec: _taskq_model.TaskRecord) -> bool:
        run_id = str(rec.params.get("task_id") or "")
        if not run_id:
            run_id = rec.id[len(_runner_adapter.TASKRUNNER_ID_PREFIX) :]
        run = self._runs.get(run_id)
        if run is None or run.status not in ("paused", "planned"):
            return False
        if run.execution_context is not None and run.execution_context.memory_mode != "persistent":
            return False
        resumable.append(run_id)
        return True

    report = await asyncio.to_thread(
        _runner_adapter.adopt_orphaned_rows,
        store,
        kinds=(_taskq_model.KIND_TASKRUNNER_STEP,),
        resume=_resume,
    )
    for run_id in resumable:
        try:
            await self.execute_plan(run_id)
        except ValueError as exc:
            logger.warning("taskq adopt: could not resume run %s: %s", run_id, exc)
    for rec_id in report.unknown_side_effect:
        if ":task" in rec_id:
            continue
        run = self._runs.get(rec_id[len(_runner_adapter.TASKRUNNER_ID_PREFIX) :])
        if run is not None and run.status == "paused":
            await self._notify(
                "\u23f8\ufe0f Run not auto-resumed",
                "The interrupted step may have had a side effect (no git worktree "
                "to checkpoint against). Review the workspace and resume manually.",
                run=run,
            )
    for rec_id in report.lost_owner_limit:
        # ``owner_of`` drops the ``~N`` of the row a manual resume opens once the
        # run's earlier row has ended, which slicing the prefix off would keep.
        owner = _runner_adapter.owner_of(rec_id)
        run = self._runs.get(owner[1]) if owner is not None else None
        if run is not None and run.status == "paused":
            await self._notify(
                "\u23f8\ufe0f Run not auto-resumed",
                f"This run was resumed automatically {_taskq_model.RECOVERY_MAX_ATTEMPTS} "
                "times after the gateway stopped, and the gateway stopped again before it "
                "finished, so it is not resumed again. The limit counts every gateway stop, "
                "a routine restart as much as a crash. Steps that passed are kept: resume "
                "the run by hand to continue it.",
                run=run,
            )
    return report


def _taskq_lane_inputs(self: TaskRunner, run: Project) -> tuple[str, str]:
    return self._run_session_keys.get(run.task_id, ""), str(run.source or "")


async def _taskq_begin_run(self: TaskRunner, run: Project) -> None:
    """Accept + claim the run's container row (no lane slot; steps take those)."""
    if run.execution_context is not None and run.execution_context.memory_mode != "persistent":
        return
    admission = self._task_admission
    if admission is None or admission.store is None:
        return
    from kiro_crew.taskq import model as _taskq_model
    from kiro_crew.taskq.adapters import runner as _runner_adapter

    session_key, source = self._taskq_lane_inputs(run)
    row_id = _runner_adapter.run_task_id(run.task_id)
    try:
        rec = await admission.accept_async(
            kind=_taskq_model.KIND_TASKRUNNER_STEP,
            task_id=row_id,
            session_key=session_key,
            source=source,
            params={
                "task_id": run.task_id,
                "name": run.name,
                "spec_path": run.spec_path,
                "steps": len(run.tasks),
                _runner_adapter.PARAM_SAFE_RETRY: bool(run.branch_name),
            },
            workspace=run.work_dir or None,
            scope_ref={"auto_approve": False, "agent": self._agent},
            side_effect_class=_taskq_model.SIDE_EFFECT_UNKNOWN,
        )
    except _runner_adapter.RunnerAdmissionRefused as exc:
        logger.warning("taskq: run row for %s not accepted: %s", run.task_id, exc)
        return
    if rec is None:
        return
    handle = await admission.claim_only_async(
        rec.id, kind=rec.kind, lane=_runner_adapter.lane_for(session_key, source)
    )
    if handle is None:
        return
    if not await handle.running_async({"phase": "executing", "steps": len(run.tasks)}):
        # THE ROW'S STATE DECIDES, not the refusal. A refused mark on this
        # container row is not the sibling case that fails the unit
        # (``run_steps._execute_single_task``, ``workflows.agent_pool``): a STEP row
        # holds a lane slot and must reach a WAITING state, which ``starting``
        # cannot, while this row holds no slot, enters no wait, and settles from
        # ``starting`` because ``TRANSITIONS[STARTING]`` carries every active
        # terminal -- so an uncommitted mark under a live row costs the run its
        # progress marker and nothing else. What the refusal CAN mean is that
        # another incarnation's reconcile already gave the row an outcome, and a
        # plan must never execute under a row that has one, so the state is read
        # and only a terminal one ends the start.
        state = await self._taskq_row_state(rec.id)
        if state is not None and state in _taskq_model.TERMINAL:
            raise _runner_adapter.RunnerTaskCancelled(f"{rec.id} is {state}; the run did not start")
        logger.warning(
            "taskq: run row %s did not take the running mark (state=%s); "
            "the run proceeds and the row settles from %s",
            rec.id,
            state,
            _taskq_model.STARTING,
        )
    self._run_handles[run.task_id] = handle


async def _taskq_row_state(self: TaskRunner, row_id: str) -> str | None:
    """The row's state read on the store's writer thread; None when unreadable.

    Unreadable and absent answer the same, because both leave the caller with
    no evidence that the row ended: a read that could not be taken must never
    be the reason an accepted run is refused.
    """
    admission = self._task_admission
    store = admission.store if admission is not None else None
    if store is None:
        return None
    from kiro_crew.taskq.store import TaskStoreUnavailable

    try:
        state = await store.run(store.state_of, row_id)
    except TaskStoreUnavailable:
        logger.debug("taskq: state read for %s failed", row_id, exc_info=True)
        return None
    return str(state) if isinstance(state, str) else None


async def _taskq_end_run(self: TaskRunner, run: Project) -> None:
    handle = self._run_handles.pop(run.task_id, None)
    if handle is None:
        return
    if run.status == "completed":
        ref = str(Path(run.work_dir) / PROGRESS_FILE) if run.work_dir else None
        await handle.done_async(result_ref=ref)
    elif run.status == "failed":
        await handle.fail_async(run.error or "run failed")
    else:
        # ``paused`` / ``cancelled`` are operator decisions: this execution
        # is over and a later resume is a NEW row. Never left ``recovering``,
        # or the adopter would restart what the operator stopped.
        await handle.cancel_async(f"run {run.status}")


async def _taskq_admit_step(
    self: TaskRunner, run: Project, task: Task
) -> "_runner_adapter.Admitted | None":
    """Persist the step as a child row and wait for its lane slot."""
    admission = self._task_admission
    if admission is None:
        return None
    if run.execution_context is not None and run.execution_context.memory_mode != "persistent":
        admission = admission.in_memory()
    from kiro_crew.taskq import model as _taskq_model
    from kiro_crew.taskq.adapters import runner as _runner_adapter

    session_key, source = self._taskq_lane_inputs(run)
    lane = _runner_adapter.lane_for(session_key, source)
    parent = self._run_handles.get(run.task_id)
    row_id = _runner_adapter.step_task_id(run.task_id, task.index)
    if admission.store is not None:
        try:
            rec = await admission.accept_async(
                kind=_taskq_model.KIND_TASKRUNNER_STEP,
                task_id=row_id,
                session_key=session_key,
                source=source,
                params={
                    "task_id": run.task_id,
                    "index": task.index,
                    "title": task.title[:200],
                    _runner_adapter.PARAM_SAFE_RETRY: bool(run.branch_name),
                },
                workspace=run.work_dir or None,
                scope_ref={"auto_approve": bool(run.auto_approve), "agent": self._agent},
                side_effect_class=_taskq_model.SIDE_EFFECT_UNKNOWN,
                parent_id=parent.task_id if parent is not None else None,
            )
        except _runner_adapter.RunnerAdmissionRefused:
            # Nothing accepted: the step fails closed rather than running
            # off the record. ONE writer records the verdict on the task --
            # ``_execute_single_task``'s refusal arm, which catches an admit
            # refusal by the same name -- so a message written here would
            # only be the one it overwrites.
            raise
        if rec is not None:
            row_id = rec.id
    return await admission.admit(
        row_id, kind=_taskq_model.KIND_TASKRUNNER_STEP, lane=lane, session_key=session_key
    )

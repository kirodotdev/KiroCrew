"""The restart sweep never resumes a run an operator cancelled while the sweep was running."""

from __future__ import annotations

from pathlib import Path

import pytest
import test_taskrunner_taskq as taskq
from test_taskrunner_taskq import _gateway_lives_that_resumed, _next_gateway_life, _run

from kiro_crew.taskrunner import Step

clock = taskq.clock  # the fixture, made visible to this module


@pytest.mark.asyncio
async def test_a_run_cancelled_during_the_sweep_stays_cancelled(tmp_path: Path, clock) -> None:
    path = tmp_path / "tasks.db"
    for run_id in ("a", "b"):
        _gateway_lives_that_resumed(path, clock, f"taskrunner:{run_id}", run_id, 0)
    store, provider, runner, _notices = _next_gateway_life(tmp_path, path, clock)
    try:
        for run_id in ("a", "b"):
            run = _run(tmp_path, Step(index=1, title="x", description="d"), task_id=run_id)
            run.status = "paused"
            runner._runs[run_id] = run
        real_execute_plan = runner.execute_plan
        started: list[str] = []
        cancelled: list[str] = []

        async def execute_plan(task_id: str, **kwargs):
            if not started:  # the first run is still being set up: the operator cancels the other
                started.append(task_id)
                other = "b" if task_id == "a" else "a"
                assert await runner.cancel_paused(other) is True
                cancelled.append(other)
                return task_id
            return await real_execute_plan(task_id, **kwargs)

        runner.execute_plan = execute_plan

        await runner.adopt_task_rows()

        assert runner._runs[cancelled[0]].status == "cancelled"
        assert cancelled[0] not in runner._tasks and not provider.calls
        # Its adopted row is ended too, not left ``recovering`` for the gateway's life.
        assert store.get(f"taskrunner:{cancelled[0]}").state == "cancelled"
    finally:
        store.close()

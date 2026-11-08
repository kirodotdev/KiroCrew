"""A plan a Shared Crew guest wrote runs as the guest's, whoever starts it.

Its stored text is cut before it reaches a prompt (``[:200]``, ``[:2000]``,
``[:3000]``), so no check on the guest's words can foresee every path a cut forms.
The plan carries the mark instead (``Project.guest_origin``), and every run and
chat turn built from it reads images only through the attachment stores.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiro_crew import guest_prompt_origin
from kiro_crew import taskrunner as taskrunner_module
from kiro_crew.task_models import Project, Task
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.testing.wait import finished


def _runner(tmp_path) -> TaskRunner:
    sessions = MagicMock()
    sessions.admission_closed = False
    return TaskRunner(sessions=sessions, work_dir=tmp_path, auto_test=False)


def _plan(tmp_path, *, task_id: str = "t1") -> Project:
    return Project(
        spec_path="",
        spec_content="",
        tasks=[Task(1, "step", "step")],
        status="running",
        task_id=task_id,
        work_dir=str(tmp_path),
    )


def test_a_plan_built_in_a_guest_turn_carries_the_mark(tmp_path) -> None:
    token = guest_prompt_origin.bind(True)
    try:
        guest_plan = _plan(tmp_path)
    finally:
        guest_prompt_origin.reset(token)
    assert guest_plan.guest_origin is True
    assert _plan(tmp_path).guest_origin is False


@pytest.mark.asyncio
async def test_the_mark_survives_a_restart(tmp_path) -> None:
    runner = _runner(tmp_path)
    plan = _plan(tmp_path)
    plan.status = "planned"
    plan.guest_origin = True
    runner._runs[plan.task_id] = plan
    await runner._apersist_runs()

    restored = _runner(tmp_path)
    assert restored._runs[plan.task_id].guest_origin is True


@pytest.mark.asyncio
@pytest.mark.parametrize("guest_wrote_it", [True, False])
async def test_a_plan_runs_as_whoever_wrote_it_whoever_starts_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch, guest_wrote_it: bool
) -> None:
    seen: list[bool] = []

    async def step(*, run, task, agent, **_kwargs):
        seen.append(guest_prompt_origin.current() is not None)
        return True

    async def review(*_args, **_kwargs):
        return True

    monkeypatch.setattr(taskrunner_module, "execute_single_task", step)
    monkeypatch.setattr(taskrunner_module, "self_review_fn", review)
    runner = _runner(tmp_path)
    plan = _plan(tmp_path)
    plan.status = "planned"
    plan.guest_origin = guest_wrote_it
    runner._runs[plan.task_id] = plan
    # Started outside any guest's request: the owner's Execute, a restart's resume.
    assert guest_prompt_origin.current() is None
    await runner.execute_plan(plan.task_id)
    await finished(runner._tasks[plan.task_id], "the plan's run")
    assert seen == [guest_wrote_it]
    assert guest_prompt_origin.current() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("guest_wrote_it", [True, False])
async def test_a_plan_opened_in_chat_is_the_guest_s_turn_when_a_guest_wrote_it(
    tmp_path, guest_wrote_it: bool
) -> None:
    from kiro_crew.dashboard.handlers.taskrunner import _plan_chat_task

    seen: list[bool] = []

    async def turn() -> None:
        seen.append(guest_prompt_origin.current() is not None)

    plan = _plan(tmp_path)
    plan.guest_origin = guest_wrote_it
    await _plan_chat_task(plan, turn())
    assert seen == [guest_wrote_it]
    assert guest_prompt_origin.current() is None

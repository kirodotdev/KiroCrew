"""A task run keeps the agent it was planned or started with, whatever else the runner does.

One ``TaskRunner`` serves the whole gateway, so the agent belongs to the run: another run
starting, a gateway restart, a retry or a resume from the Projects page must not change
the agent a run's steps open their sessions with. Real ``TaskRunner`` and real step
execution; only the session manager is faked, and it records the agent of every session
opened (``taskrunner.md`` § Concurrent Tasks, § Pause / Resume, § Crash Recovery).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_taskrunner_coverage_helpers import _body, _request, _state

from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_retry
from kiro_crew.learn import LessonStore
from kiro_crew.providers.base import LLMEvent
from kiro_crew.session import BACKGROUND_KEY
from kiro_crew.task_models import TaskStatus
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.testing.wait import await_until

#: Two steps, the second after the first, so a step opens after the run has started.
_PLAN = json.dumps(
    [
        {"title": "first", "description": "do the first part"},
        {"title": "second", "description": "do the second part", "depends_on": [1]},
    ]
)


@pytest.fixture(autouse=True)
def _no_self_review(_floor_monkeypatch):
    # The post-step review is its own turn on its own session; these tests read the
    # sessions the STEPS open.
    _floor_monkeypatch.setattr("kiro_crew.task_executor.self_review", AsyncMock(return_value=True))


def _provider(text: str) -> MagicMock:
    provider = MagicMock()

    async def _stream(_message: str):
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


class _Sessions:
    """The session manager: records ``(session_key, agent)`` for every session opened.

    A decomposition session streams :data:`_PLAN`; a step session streams ``done``.
    :meth:`hold` parks the opening of one session key until the test releases it.
    """

    def __init__(self) -> None:
        self.opened: list[tuple[str, str | None]] = []
        self.held: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
        manager = MagicMock()
        manager._lock = asyncio.Lock()
        manager._sessions = {}
        manager.get_pid = MagicMock(return_value=None)
        manager.release = MagicMock()
        manager.reset = AsyncMock()
        manager.record_success = MagicMock()
        manager.record_failure = AsyncMock()
        manager.check_context_usage = MagicMock()
        manager.release_subagent_runtime = AsyncMock()
        manager.close_all = AsyncMock()
        manager.recycle_background = AsyncMock()
        manager.get_or_create = self._open
        manager.open_task_session = self._open_task_session
        self.manager = manager

    def hold(self, session_key: str) -> tuple[asyncio.Event, asyncio.Event]:
        """``(entered, release)`` for *session_key*: set on open, awaited before it returns."""
        events = (asyncio.Event(), asyncio.Event())
        self.held[session_key] = events
        return events

    async def _open(self, session_key: str, agent=None, cwd=None, **_kw):
        self.opened.append((session_key, agent))
        if session_key in self.held:
            entered, release = self.held[session_key]
            entered.set()
            await release.wait()
        text = _PLAN if session_key.endswith(":decompose") else "done"
        return _provider(text), True, False

    async def _open_task_session(self, _parent_key, session_key, *, agent=None, cwd=None, **_kw):
        return await self._open(session_key, agent=agent, cwd=cwd)

    def step_agents(self, task_id: str) -> list[str | None]:
        """The agent of each step session *task_id* opened, in order."""
        prefix = f"taskrunner:{task_id}:task"
        return [agent for key, agent in self.opened if key.startswith(prefix)]


def _runner(tmp_path: Path, sessions: _Sessions, **kwargs) -> TaskRunner:
    return TaskRunner(sessions=sessions.manager, auto_test=False, work_dir=tmp_path, **kwargs)


async def _finished(runner: TaskRunner, task_id: str) -> None:
    await await_until(runner._tasks[task_id], what=f"run {task_id} to finish")


@pytest.mark.asyncio
async def test_planning_a_run_uses_its_agent(tmp_path: Path) -> None:
    sessions = _Sessions()
    runner = _runner(tmp_path, sessions)

    run = await runner.plan("build the thing", agent="alpha")

    assert sessions.opened == [(f"taskrunner:{run.task_id}:decompose", "alpha")]
    assert run.agent == "alpha"


@pytest.mark.asyncio
async def test_a_run_started_while_another_runs_keeps_its_agent(tmp_path: Path) -> None:
    sessions = _Sessions()
    runner = _runner(tmp_path, sessions)
    first = await runner.plan("build A", agent="alpha")
    second = await runner.plan("build B", agent="beta")
    entered, release = sessions.hold(f"taskrunner:{first.task_id}:task1")
    try:
        await runner.execute_plan(first.task_id, agent="alpha")
        await await_until(entered, what="the first run's step 1 to open its session")

        await runner.execute_plan(second.task_id, agent="beta")
        await _finished(runner, second.task_id)
    finally:
        release.set()
    await _finished(runner, first.task_id)

    assert (first.status, second.status) == ("completed", "completed")
    assert sessions.step_agents(first.task_id) == ["alpha", "alpha"]
    assert sessions.step_agents(second.task_id) == ["beta", "beta"]


@pytest.mark.asyncio
async def test_a_run_resumed_after_a_restart_keeps_its_agent(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _Sessions())
    run = await runner.plan("build A", agent="alpha")
    run.tasks[0].status = TaskStatus.PASSED
    run.status = "running"  # the gateway stopped during step 2
    await runner._apersist_runs()

    sessions = _Sessions()
    restarted = _runner(tmp_path, sessions)
    resumed = restarted._runs[run.task_id]
    assert resumed.status == "paused"

    # What the adoption sweep (``adopt_task_rows``) calls for a resumable run.
    await restarted.execute_plan(run.task_id)
    await _finished(restarted, run.task_id)

    assert resumed.status == "completed"
    assert sessions.step_agents(run.task_id) == ["alpha"]
    assert resumed.agent == "alpha"


@pytest.mark.asyncio
async def test_retry_keeps_the_run_agent_after_another_run_started(tmp_path: Path) -> None:
    sessions = _Sessions()
    runner = _runner(tmp_path, sessions)
    failed = await runner.plan("build A", agent="alpha")
    failed.status = "failed"
    failed.tasks[0].status = TaskStatus.PASSED
    failed.tasks[1].status = TaskStatus.FAILED
    other = await runner.plan("build B", agent="beta")
    await runner.execute_plan(other.task_id, agent="beta")
    await _finished(runner, other.task_id)

    response = await api_taskrunner_retry(
        _request(
            _state(runner),
            match_info={"task_id": failed.task_id},
            json_body={"from_step": 2},
        )
    )
    assert _body(response) == {"ok": True, "task_id": failed.task_id}
    await _finished(runner, failed.task_id)

    assert failed.status == "completed"
    assert sessions.step_agents(failed.task_id) == ["alpha"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [("planned", "beta"), ("paused", "alpha"), ("cancelled", "alpha"), ("failed", "alpha")],
)
async def test_execute_plan_switches_the_agent_of_a_planned_run_only(
    tmp_path: Path, status: str, expected: str
) -> None:
    sessions = _Sessions()
    runner = _runner(tmp_path, sessions)
    run = await runner.plan("build A", agent="alpha")
    run.status = status

    # The Projects page sends its agent selector with Execute and with Resume alike.
    await runner.execute_plan(run.task_id, agent="beta")
    await _finished(runner, run.task_id)

    assert sessions.step_agents(run.task_id) == [expected, expected]
    assert run.agent == expected


@pytest.mark.asyncio
async def test_execute_plan_without_an_agent_keeps_the_planned_one(tmp_path: Path) -> None:
    sessions = _Sessions()
    runner = _runner(tmp_path, sessions)
    run = await runner.plan("build A", agent="alpha")

    await runner.execute_plan(run.task_id)
    await _finished(runner, run.task_id)

    assert sessions.step_agents(run.task_id) == ["alpha", "alpha"]


@pytest.mark.asyncio
async def test_a_failed_step_lesson_uses_its_run_agent(tmp_path: Path) -> None:
    sessions = _Sessions()
    runner = _runner(tmp_path, sessions, lesson_store=LessonStore(base_dir=tmp_path / "lessons"))
    run = await runner.plan("build A", agent="alpha")
    await runner.plan("build B", agent="beta")
    step = run.tasks[0]
    step.error = "boom"

    await runner._extract_lesson(step, run)

    assert sessions.opened[-1] == (BACKGROUND_KEY, "alpha")


@pytest.mark.asyncio
async def test_status_reports_each_run_agent(tmp_path: Path) -> None:
    runner = _runner(tmp_path, _Sessions())
    first = await runner.plan("build A", agent="alpha")
    second = await runner.plan("build B", agent="beta")

    agents = {row["task_id"]: row["agent"] for row in runner.status()["runs"]}

    assert agents == {first.task_id: "alpha", second.task_id: "beta"}

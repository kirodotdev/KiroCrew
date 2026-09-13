"""An operator answer applied to a task-runner step is consumed only once the
step has durably completed.

``answer_input`` persists the answer on the row's ``wake`` event; a step that
is (re)dispatched reads it back (``RunnerAdmission.recorded_answer``) and
appends it to its prompt. The ``input_consumed`` marker that stops a later
rebuild from replaying it must be written AFTER the turn finished, never when
the answer is merely applied: a crash between applying the answer and the turn
completing would otherwise make the re-dispatched step ask the operator again.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import task_executor
from kiro_crew.acp.types import TurnUsage
from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
from kiro_crew.task_models import SESSION_PREFIX, Project, Task
from kiro_crew.taskq import model as m
from kiro_crew.taskq.adapters import runner as r
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitLedger, WaitRecord

_RUN_TASK_ID = "task-replay"
_SESSION_KEY = f"{SESSION_PREFIX}:{_RUN_TASK_ID}:task1"


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class _Client:
    """Fake ACP client: ``crash`` raises mid-turn, else the turn completes."""

    def __init__(self, *, crash: bool) -> None:
        self.crash = crash
        self.prompts: list[str] = []

    async def stream(self, prompt: str):
        self.prompts.append(prompt)
        await asyncio.sleep(0)
        if self.crash:
            raise asyncio.CancelledError()  # the process dies mid-turn
        yield LLMEvent(kind=EVENT_COMPLETE, usage=TurnUsage(duration_ms=0))


def _sessions(client: _Client) -> MagicMock:
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(client, True, False))
    sessions.record_success = MagicMock()
    sessions.check_context_usage = MagicMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_failure = AsyncMock()
    return sessions


def _patch_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_executor, "check_context", AsyncMock())
    monkeypatch.setattr(
        task_executor,
        "build_task_prompt",
        AsyncMock(side_effect=lambda run, task, *a, **k: task.description),
    )
    fake_config = MagicMock()
    fake_config.load.return_value = SimpleNamespace(agent=SimpleNamespace(provider="acp"))
    monkeypatch.setattr(task_executor, "KiroCrewConfig", fake_config)
    monkeypatch.setattr(task_executor, "persist_token_record_async", AsyncMock(), raising=False)


async def _step(client: _Client, task: Task, handle: r.Admitted) -> bool:
    run = Project(spec_path="spec.md", spec_content="body")
    run.task_id = _RUN_TASK_ID
    run.tasks = [task]
    run.branch_name = ""
    run.work_dir = ""
    return await task_executor.execute_task(
        run,
        task,
        _sessions(client),
        None,
        "agentX",
        None,
        False,
        None,
        "",
        AsyncMock(),
        _SESSION_KEY,
        taskq=handle,
    )


@pytest.mark.asyncio
async def test_answer_survives_a_crash_between_apply_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executor(monkeypatch)
    clock = _Clock()
    path = tmp_path / "tasks.db"
    store = TaskStore(path, window=8, clock=clock, network_fs=False).open()
    adm = r.RunnerAdmission(store, lane=r.RunnerLane(1), clock=clock)
    rec = adm.accept(
        kind=m.KIND_TASKRUNNER_STEP,
        task_id="taskrunner:r:ask",
        side_effect_class=m.SIDE_EFFECT_NONE,
    )
    handle = await adm.admit(rec.id)
    handle.running()
    # The operator answered while the row waited; the answer is on the wake event.
    store.enter_wait(
        rec.id, WaitRecord.input("q1", since=clock()).to_dict(), generation=handle.generation
    )
    assert WaitLedger(store, clock=clock).wake(
        rec.id, reason="input answered", detail={"answer": "hunter2"}
    )
    handle.generation = store.get(rec.id).generation
    assert adm.recorded_answer(rec.id) == "hunter2"

    # -- rebuild 1: the answer is applied to the prompt, then the turn CRASHES.
    crashing = _Client(crash=True)
    task = Task(index=1, title="ask", description="desc")
    with pytest.raises(asyncio.CancelledError):
        await _step(crashing, task, handle)
    assert crashing.prompts and "hunter2" in crashing.prompts[0]
    assert adm.recorded_answer(rec.id) == "hunter2", "not consumed: the turn never completed"

    # -- rebuild 2: the answer is applied again and the turn completes; only
    # now is it consumed, so a third rebuild would not replay it.
    finishing = _Client(crash=False)
    task2 = Task(index=1, title="ask", description="desc")
    assert await _step(finishing, task2, handle) is True
    assert finishing.prompts and "hunter2" in finishing.prompts[0]
    assert adm.recorded_answer(rec.id) is None
    store.close()

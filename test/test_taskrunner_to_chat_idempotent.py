"""``POST /api/taskrunner/{task_id}/to-chat`` opens one chat per completion.

Each to-chat appends the run summary and starts an agent turn told to work in the
run's directory. A repeated click for the same completion, from the same or from
another entry point, must land on the conversation already open rather than put
a second agent in that directory. A retry is a new completion and gets its own
chat.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import taskrunner as handlers
from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_to_chat
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.taskrunner import Step, StepStatus, TaskRun

_EXECUTION = ExecutionContext(
    "alice", MemoryStoreRef("alice-store", "alice"), "member", "kirocrew", "persistent"
)


class _Slot(SimpleNamespace):
    def append(self, role: str, text: str, css: str) -> None:
        self.messages.append((role, text))


class _State:
    """The slot registry surface the handler uses, with close support."""

    def __init__(self, runner: Any) -> None:
        self.task_runner = runner
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._slots: dict[str, _Slot] = {}
        self._minted = 0
        self.push_slots_update = MagicMock()

    def get_slot(self, name: str) -> _Slot | None:
        return self._slots.get(name)

    def get_or_create_slot(self, name: str | None = None, **kwargs: Any) -> _Slot:
        if name is not None and name in self._slots:
            return self._slots[name]
        if name is None:
            self._minted += 1
            name = f"chat-{self._minted}"
        slot = _Slot(
            key=name,
            title="",
            task=None,
            messages=[],
            linked_session_key=kwargs.get("linked_session_key", ""),
            memory_store="",
            memory_mode="",
            agent="",
            _app="",
        )
        self._slots[name] = slot
        return slot

    def close(self, name: str) -> None:
        self._slots.pop(name)


def _run(*, execution: ExecutionContext | None = _EXECUTION, status: str = "completed") -> TaskRun:
    return TaskRun(
        spec_path="/specs/build.md",
        spec_content="do the thing",
        status=status,
        task_id="t1",
        work_dir="/w",
        started_at=100.0,
        finished_at=200.0,
        execution_context=execution,
        tasks=[Step(index=1, title="a", description="", status=StepStatus.PASSED)],
    )


def _state(run: TaskRun) -> _State:
    runner = MagicMock()
    runner._runs = {run.task_id: run}
    runner.plan_to_chat_context = MagicMock(return_value="plan ctx")
    return _State(runner)


def _request(state: _State) -> web.Request:
    app = web.Application()
    app["state"] = state
    req = make_mocked_request(
        "POST", "/api/taskrunner/t1/to-chat", app=app, match_info={"task_id": "t1"}
    )
    req["app"] = ""
    req["user"] = "local-app"
    return req


async def _post(state: _State) -> str:
    resp = await api_taskrunner_to_chat(_request(state))
    assert resp.status == 200
    assert resp.body is not None
    return json.loads(bytes(resp.body))["slot"]  # type: ignore[arg-type]


async def _drain(state: _State) -> None:
    await asyncio.gather(*list(state._background_tasks))


def _summaries(slot: _Slot) -> int:
    return sum(1 for role, _ in slot.messages if role == "user")


@pytest.fixture(autouse=True)
def _hermetic():
    with (
        patch.object(handlers, "_sel", return_value=MagicMock()),
        patch.object(handlers, "bind_session_execution") as bind,
    ):
        yield bind


@pytest.fixture
def chat():
    with patch("kiro_crew.dashboard.chat._run_chat", AsyncMock()) as run_chat:
        yield run_chat


@pytest.mark.asyncio
async def test_a_repeated_click_returns_the_open_slot_with_one_summary_and_one_turn(
    chat, _hermetic
) -> None:
    state = _state(_run())
    first = await _post(state)
    await _drain(state)
    second = await _post(state)
    await _drain(state)
    assert first == second
    assert first.startswith("task-review-")
    assert _summaries(state._slots[first]) == 1
    assert chat.await_count == 1
    assert _hermetic.call_count == 1


@pytest.mark.asyncio
async def test_a_click_while_the_first_turn_runs_starts_no_turn(_hermetic) -> None:
    release = asyncio.Event()
    calls: list[str] = []

    async def slow_turn(state: Any, slot: Any, *_a: Any, **_kw: Any) -> None:
        calls.append(slot.key)
        await release.wait()

    state = _state(_run())
    with patch("kiro_crew.dashboard.chat._run_chat", new=slow_turn):
        first = await _post(state)
        await asyncio.sleep(0)
        assert state._slots[first].task is not None and not state._slots[first].task.done()
        second = await _post(state)
        release.set()
        await _drain(state)
    assert second == first
    assert calls == [first]
    assert _summaries(state._slots[first]) == 1


@pytest.mark.asyncio
async def test_concurrent_clicks_open_one_slot(chat, _hermetic) -> None:
    state = _state(_run())
    first, second = await asyncio.gather(_post(state), _post(state))
    await _drain(state)
    assert first == second
    assert len(state._slots) == 1
    assert _summaries(state._slots[first]) == 1
    assert chat.await_count == 1


@pytest.mark.asyncio
async def test_the_finish_time_restamped_after_cleanup_keeps_the_one_slot(chat, _hermetic) -> None:
    run = _run()
    state = _state(run)
    from_notification = await _post(state)
    await _drain(state)
    run.finished_at = 205.0
    after_cleanup = await _post(state)
    await _drain(state)
    assert after_cleanup == from_notification
    assert len(state._slots) == 1
    assert _summaries(state._slots[from_notification]) == 1
    assert chat.await_count == 1
    assert _hermetic.call_count == 1


@pytest.mark.asyncio
async def test_a_click_on_the_outcome_after_a_click_mid_run_opens_the_outcome_chat(chat) -> None:
    run = _run(status="running")
    run.finished_at = 0.0
    run.tasks[0].status = StepStatus.IN_PROGRESS
    state = _state(run)
    mid_run = await _post(state)
    await _drain(state)
    run.status, run.finished_at = "failed", 200.0
    run.tasks[0].status, run.tasks[0].error = StepStatus.FAILED, "boom"
    outcome = await _post(state)
    await _drain(state)
    assert outcome != mid_run
    assert "diagnose the root cause" in state._slots[outcome].messages[0][1]
    assert chat.await_count == 2
    run.finished_at = 205.0
    assert await _post(state) == outcome
    assert chat.await_count == 2


@pytest.mark.asyncio
async def test_a_cancel_settling_during_cleanup_keeps_the_one_slot(chat) -> None:
    run = _run(status="cancelling")
    state = _state(run)
    during_cleanup = await _post(state)
    run.status, run.finished_at = "cancelled", 205.0
    after_cleanup = await _post(state)
    await _drain(state)
    assert after_cleanup == during_cleanup
    assert chat.await_count == 1


@pytest.mark.asyncio
async def test_a_retry_completion_opens_a_new_slot(chat) -> None:
    run = _run()
    state = _state(run)
    first = await _post(state)
    run.started_at = 300.0
    second = await _post(state)
    await _drain(state)
    assert second != first
    assert _summaries(state._slots[second]) == 1
    assert chat.await_count == 2


@pytest.mark.asyncio
async def test_the_plan_chat_and_the_review_chat_are_separate_slots(chat) -> None:
    run = _run(status="planned")
    state = _state(run)
    plan = await _post(state)
    run.status = "completed"
    review = await _post(state)
    await _drain(state)
    assert plan != review
    assert state._slots[plan].title == "Plan: t1"
    assert state._slots[review].title == "Review: build"


@pytest.mark.asyncio
async def test_a_closed_tab_is_opened_again_with_the_review_turn(chat) -> None:
    state = _state(_run())
    first = await _post(state)
    state.close(first)
    second = await _post(state)
    await _drain(state)
    assert _summaries(state._slots[second]) == 1
    assert chat.await_count == 2


@pytest.mark.asyncio
async def test_a_run_without_an_execution_context_is_idempotent_too(chat, _hermetic) -> None:
    run = _run(execution=None)
    state = _state(run)
    first = await _post(state)
    second = await _post(state)
    await _drain(state)
    assert first == second == "chat-1"
    assert _summaries(state._slots[first]) == 1
    assert chat.await_count == 1
    _hermetic.assert_not_called()
    run.finished_at = 205.0
    assert await _post(state) == "chat-1"
    run.started_at = 300.0
    assert await _post(state) == "chat-2"
    await _drain(state)


@pytest.mark.asyncio
async def test_a_failed_open_lets_the_next_click_open_the_slot(chat, _hermetic) -> None:
    state = _state(_run())
    _hermetic.side_effect = [OSError("session record is unreadable"), None]
    with pytest.raises(OSError):
        await _post(state)
    assert state._slots == {}
    opened = await _post(state)
    await _drain(state)
    assert _summaries(state._slots[opened]) == 1
    assert chat.await_count == 1

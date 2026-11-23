"""Stop-reason consistency on the task runner path (``task_executor``).

A step whose turn ends in a persistent tool stall is failed, one that stalls once
and then succeeds passes on retry, and an ``end_turn`` step passes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from subagent_stop_reason_helpers import _STALL_EVIDENCE
from test_taskrunner import _make_mock_sessions as _make_taskrunner_sessions
from turn_harness import TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_CLASS_FAILED,
    STOP_REASON_END_TURN,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    AcpEvent,
    StopClass,
    classify_stop_reason,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import (
    CONN_RECOVERY_PREFIX,
    STALE_RECOVERY_PREFIX,
    TOOL_STALL_RECOVERY_PREFIX,
)
from kiro_crew.providers.base import LLMEvent
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner


def _taskrunner_provider(stop_reason: str, text: str = "half") -> MagicMock:
    provider = MagicMock()
    calls: list[str] = []

    async def _stream(message: str):
        calls.append(message)
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete", stop_reason=stop_reason, text=_STALL_EVIDENCE)

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    provider.calls = calls
    return provider


@pytest.mark.asyncio
async def test_taskrunner_persistent_tool_stall_step_is_failed(tmp_path: Path):
    sessions = _make_taskrunner_sessions()
    provider = _taskrunner_provider(STOP_REASON_TOOL_STALL)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Test step", description="desc")
    run.tasks = [step]

    success = await runner._execute_single_task(run, step)

    assert success is False
    assert step.status == StepStatus.FAILED
    assert step.result == "half"  # the partial is kept on the task
    sessions.record_success.assert_not_called()
    # Bounded: the existing retry ladder (incl. same-error loop detection) ran.
    assert 1 < len(provider.calls) <= 5
    # Every retry prompt names the stall and continues from it, never a bare re-run.
    for retry_prompt in provider.calls[1:]:
        assert "retry attempt" in retry_prompt
        assert "stalled" in retry_prompt and STOP_REASON_TOOL_STALL in retry_prompt


@pytest.mark.asyncio
async def test_taskrunner_tool_stall_then_success_passes_on_retry(tmp_path: Path):
    sessions = _make_taskrunner_sessions()
    provider = MagicMock()
    calls: list[str] = []

    async def _stream(message: str):
        calls.append(message)
        if len(calls) == 1:
            yield LLMEvent(kind="text_chunk", text="half")
            yield LLMEvent(
                kind="complete", stop_reason=STOP_REASON_TOOL_STALL, text=_STALL_EVIDENCE
            )
        else:
            yield LLMEvent(kind="text_chunk", text="whole")
            yield LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN)

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Test step", description="desc")
    run.tasks = [step]

    assert await runner._execute_single_task(run, step) is True
    assert step.status == StepStatus.PASSED and step.error == ""
    assert step.result == "whole"
    assert "retry attempt 2" in calls[1] and "stalled" in calls[1]


@pytest.mark.asyncio
async def test_taskrunner_end_turn_step_passes(tmp_path: Path):
    sessions = _make_taskrunner_sessions()
    provider = _taskrunner_provider(STOP_REASON_END_TURN, text="done")
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Test step", description="desc")
    run.tasks = [step]
    assert await runner._execute_single_task(run, step) is True
    assert step.status == StepStatus.PASSED and step.result == "done"
    assert provider.calls[0] and "retry attempt" not in provider.calls[0]


# ── 5. The main chat uses the same table (no private spelling) ────────


#: A transport death: the shared table calls the generic ``error:`` family retryable.
_PROCESS_EXITED = "error: process exited"


def _stalled_turns(count: int, stop_reason: str) -> TurnScript:
    """*count* chat turns on one slot, each ending in *stop_reason*."""
    stall = (
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="half"),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason, text="idle_secs=600"),
    )
    script = None
    for _ in range(count):
        script = TurnScript(events=stall, message="continue", user_row=False, then=script)
    assert script is not None
    return script


def _requeued(record: TurnRecord, prefix: str) -> bool:
    return any(str(arg).startswith(prefix) for call in record.successors for arg in call.args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "prefix"),
    [
        (STOP_REASON_TOOL_STALL, TOOL_STALL_RECOVERY_PREFIX),
        (STOP_REASON_STALE_RECOVER, STALE_RECOVERY_PREFIX),
    ],
)
async def test_chat_turn_bounds_stall_retries_by_the_shared_budget(
    monkeypatch, stop_reason, prefix
):
    """The main chat retries a tool stall or a stale turn exactly
    ``STOP_RECOVERY_MAX_RETRIES`` times, the shared budget, then stops queueing:
    no private retry literal."""
    budget = 2  # below the shipped value, so a private literal of 3 retries more
    monkeypatch.setattr(chat_runner, "STOP_RECOVERY_MAX_RETRIES", budget)
    record: TurnRecord | None = await run_turn(_stalled_turns(budget + 3, stop_reason))
    retried = []
    while record is not None:
        assert record.stop_reason == stop_reason
        retried.append(_requeued(record, prefix))
        record = record.then
    assert retried == [True] * budget + [False] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("table_retries", [True, False])
async def test_chat_turn_classifies_its_completion_through_the_shared_table(
    monkeypatch, table_retries
):
    """The main chat re-queues an ``error:`` completion when, and only when, the
    shared ``classify_stop_reason`` table says it is retryable. A private
    ``startswith("error:")`` spelling would re-queue whatever the table said."""
    asked: list[str] = []

    def _table(stop_reason, **kwargs):
        asked.append(stop_reason)
        if stop_reason == _PROCESS_EXITED and not table_retries:
            return StopClass(STOP_CLASS_FAILED, stop_reason)
        return classify_stop_reason(stop_reason, **kwargs)

    monkeypatch.setattr(chat_runner, "classify_stop_reason", _table)
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="half"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=_PROCESS_EXITED),
            ]
        )
    )
    assert _PROCESS_EXITED in asked
    assert record.stop_reason == _PROCESS_EXITED
    assert _requeued(record, CONN_RECOVERY_PREFIX) is table_retries


def test_subagent_run_has_no_private_stop_reason_mapping():
    import kiro_crew.subagent_manager.run as run_mod

    # run.py binds most of its members from sibling ``run_*`` owners: read the family.
    facade = Path(run_mod.__file__)
    family = [facade, *sorted(facade.parent.glob("run_*.py"))]
    src = "\n".join(path.read_text(encoding="utf-8") for path in family)
    assert 'startswith("error:")' not in src
    assert "classify_stop_reason(" in src

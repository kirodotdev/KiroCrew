"""Workflow retention applies to task admission as well as run checkpoints."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kiro_crew.taskq import model
from kiro_crew.taskq.adapters.runner import RunnerAdmission, RunnerLane
from kiro_crew.taskq.store import TaskStore
from kiro_crew.workflows import service

CANARY = "TRANSIENT_WORKFLOW_BODY"


def _runner(monkeypatch, admission, mode, agent, run_id="retention"):
    monkeypatch.setattr(service, "build_agent_fn", lambda *args, **kwargs: agent)
    workflows = service.WorkflowService(sessions=object(), persist=False, pool_agents=False)
    workflows.attach_task_admission(admission)
    scope = SimpleNamespace(memory_mode=mode, validate=AsyncMock())
    return workflows._runner(run_id, session_key="chat:owner", memory_scope=scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
async def test_workflow_taskq_retains_only_persistent_calls(tmp_path, monkeypatch, mode, outcome):
    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    admission = RunnerAdmission(store, lane=RunnerLane(1), require_store=True)

    async def agent(prompt, opts):
        assert admission.lane.running == 1
        if outcome == "failed":
            raise RuntimeError(CANARY)
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return prompt

    try:
        runner = _runner(monkeypatch, admission, mode, agent)
        if outcome == "done":
            assert await runner._agent_fn(CANARY, {"session": CANARY}) == CANARY
        else:
            expected = RuntimeError if outcome == "failed" else asyncio.CancelledError
            with pytest.raises(expected):
                await runner._agent_fn(CANARY, {"session": CANARY})
        assert admission.lane.running == admission.lane.waiting == 0
        rows = store.list_rows(kind=model.KIND_WORKFLOW_AGENT)
        if mode == "persistent":
            assert len(rows) == 1 and rows[0].state == outcome
            assert rows[0].params["session"] == CANARY
        else:
            assert rows == []
            assert store.events("workflow:retention:agent1") == []
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_workflow_keeps_pressure_and_adaptive_cap(tmp_path, monkeypatch, mode):
    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    decision = SimpleNamespace(admitted=False, reason="memory pressure")
    deferred = asyncio.Event()
    released = asyncio.Event()

    async def sleep(delay):
        deferred.set()
        await released.wait()

    admission = RunnerAdmission(
        store, lane=RunnerLane(1), pressure=lambda: decision, sleep=sleep, require_store=True
    )
    agent = AsyncMock(return_value="complete")
    task = None
    try:
        runner = _runner(monkeypatch, admission, mode, agent)
        task = asyncio.create_task(runner._agent_fn(CANARY, {}))
        await asyncio.wait_for(deferred.wait(), timeout=2)
        agent.assert_not_awaited()
        assert store.list_rows() == []
        admission.lane.set_effective_cap(0)
        decision.admitted = True
        released.set()
        for _ in range(100):
            if admission.lane.waiting:
                break
            await asyncio.sleep(0.01)
        assert admission.lane.waiting == 1
        agent.assert_not_awaited()
        admission.lane.set_effective_cap(1)
        assert await asyncio.wait_for(task, timeout=2) == "complete"
        agent.assert_awaited_once()
        assert store.list_rows() == []
        assert admission.lane.running == 0
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_workflow_dependency_retry_never_reaches_durable_coordinator(
    tmp_path, monkeypatch, mode
):
    from kiro_crew.taskq.dependency import KIND_RATE_LIMITED, SIGNAL_ATTR, DependencySignal

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    sleeps = []
    calls = []

    async def sleep(delay):
        assert admission.lane.running == 0
        sleeps.append(delay)

    coordinator = SimpleNamespace(report=Mock(side_effect=AssertionError("durable report")))
    admission = RunnerAdmission(
        store, lane=RunnerLane(1), coordinator=coordinator, clock=lambda: 100, sleep=sleep
    )

    async def agent(prompt, opts):
        calls.append(prompt)
        if len(calls) == 1:
            error = RuntimeError(CANARY)
            setattr(
                error,
                SIGNAL_ATTR,
                DependencySignal(
                    kind=KIND_RATE_LIMITED,
                    dependency_scope="provider",
                    source="test",
                    retry_at=130,
                    detail=CANARY,
                ),
            )
            raise error
        return "complete"

    try:
        runner = _runner(monkeypatch, admission, mode, agent)
        assert await runner._agent_fn(CANARY, {}) == "complete"
        assert calls == [CANARY, CANARY] and sleeps == [30]
        coordinator.report.assert_not_called()
        assert store.list_rows() == []
        assert admission.lane.running == admission.lane.waiting == 0
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_workflow_shares_live_lane_and_releases_cancelled_wait(
    tmp_path, monkeypatch, mode
):
    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    admission = RunnerAdmission(store, lane=RunnerLane(1))
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def agent(prompt, opts):
        entered.set()
        await finish.wait()
        return prompt

    running = waiting = None
    try:
        persistent = _runner(monkeypatch, admission, "persistent", agent, "persistent")
        transient = _runner(monkeypatch, admission, mode, agent, "transient")
        running = asyncio.create_task(persistent._agent_fn("persistent", {}))
        await asyncio.wait_for(entered.wait(), timeout=2)
        waiting = asyncio.create_task(transient._agent_fn(CANARY, {}))
        for _ in range(100):
            if admission.lane.waiting:
                break
            await asyncio.sleep(0.01)
        assert admission.lane.running == admission.lane.waiting == 1
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert admission.lane.waiting == 0
        finish.set()
        assert await running == "persistent"
        assert admission.lane.running == 0
        assert [row.id for row in store.list_rows()] == ["workflow:persistent:agent1"]
    finally:
        finish.set()
        for task in (waiting, running):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (waiting, running) if task is not None), return_exceptions=True
        )
        store.close()

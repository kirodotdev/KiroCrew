"""S2-04: one child execution per task and truthful continuation state."""
from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.agent_runtime import (
    AgentRegistry, AgentResult, AgentRuntime, AgentSpec, AgentTaskBusyError,
)


def _one_task():
    registry = AgentRegistry()
    plan_id, _ = registry.begin_plan(
        "crew-member-session",
        (AgentSpec("worker", "inspect", tool_policy="read_only"),),
    )
    task = registry.task_for_role(plan_id, "worker")
    assert task is not None
    return registry, task


def test_runtime_refuses_duplicate_submit_but_allows_new_generation_after_completion():
    registry, task = _one_task()
    runtime = AgentRuntime(registry, max_workers=2)
    entered, release = threading.Event(), threading.Event()
    calls = []
    try:
        def long_runner(t):
            calls.append(t.task_id)
            entered.set()
            assert release.wait(3)
            return AgentResult(status="completed", summary="first")

        pending = runtime.submit(task, long_runner)
        assert entered.wait(2)
        assert runtime.is_active(task.task_id)
        with pytest.raises(AgentTaskBusyError):
            runtime.submit(task, long_runner)
        release.set()
        assert pending.result(timeout=3).summary == "first"
        assert calls == [task.task_id]
        registry.resume(task.task_id)
        assert runtime.submit(task, lambda _: AgentResult(status="completed", summary="second")).result(timeout=3).summary == "second"
    finally:
        release.set()
        runtime.shutdown(wait=True)


def test_concurrent_resumes_keep_one_execution_and_queue_the_later_message(tmp_path):
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    session = Session("crew-child", str(tmp_path), "model")
    _, tasks = session.agent_registry.begin_plan(
        session.session_id, (AgentSpec("worker", "inspect", tool_policy="read_only"),),
    )
    task = tasks[0]
    entered, release = threading.Event(), threading.Event()
    results = []
    # Replace only the actual slow execution. The new admission/queue wrapper
    # remains real, and no provider/network request is made by this test.
    original = server._resume_agent_task_once
    try:
        def slow_once(_session, _task_id, *, message=""):
            entered.set()
            assert release.wait(3)
            return AgentResult(status="completed", summary="original finished")
        server._resume_agent_task_once = slow_once
        thread = threading.Thread(target=lambda: results.append(server._resume_agent_task(session, task.task_id)))
        thread.start()
        assert entered.wait(2)
        followup = server._resume_agent_task(session, task.task_id, message="change the direction")
        assert followup.status == "running"
        assert session.agent_registry.consume_messages(task.task_id) == ("change the direction",)
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert results[0].status == "completed"
    finally:
        release.set()
        server._resume_agent_task_once = original
        server._shutdown_agent_runtime(session)
        server._executor.shutdown(wait=True)


def test_running_resume_rpc_returns_queued_status_instead_of_second_execution(tmp_path):
    output = io.StringIO()
    server = AcpServer(stdin=io.StringIO(), stdout=output)
    session = Session("crew-child", str(tmp_path), "model")
    _, tasks = session.agent_registry.begin_plan(
        session.session_id, (AgentSpec("worker", "inspect", tool_policy="read_only"),),
    )
    task = tasks[0]
    session.agent_registry.transition(task.task_id, "running")
    server._put_session(session)
    try:
        server._handle_agent_resume_request(
            35, {"sessionId": session.session_id, "taskId": task.task_id, "message": "extra guidance"}
        )
        response = json.loads(output.getvalue().splitlines()[-1])
        assert response["id"] == 35
        assert response["result"]["inProgress"] is True
        assert response["result"]["messageQueued"] is True
        assert response["result"]["result"]["status"] == "running"
        assert task.generation == 0
        assert session.agent_registry.consume_messages(task.task_id) == ("extra guidance",)
    finally:
        server._shutdown_agent_runtime(session)
        server._executor.shutdown(wait=True)


def test_cancelled_running_task_does_not_accept_new_continuation(tmp_path):
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    session = Session("crew-child", str(tmp_path), "model")
    _, tasks = session.agent_registry.begin_plan(
        session.session_id, (AgentSpec("worker", "inspect", tool_policy="read_only"),),
    )
    task = tasks[0]
    session.agent_registry.transition(task.task_id, "running")
    session.agent_registry.cancel(task.task_id)
    try:
        result = server._resume_agent_task(session, task.task_id, message="do not revive")
        assert result.status == "cancelled"
        assert session.agent_registry.consume_messages(task.task_id) == ()
    finally:
        server._shutdown_agent_runtime(session)
        server._executor.shutdown(wait=True)


def test_queued_child_cancel_reports_cancelled_not_failed():
    """A cancelled future waiting for a worker is not a failed model request."""
    registry = AgentRegistry()
    plan, _ = registry.begin_plan("s", (
        AgentSpec("worker", "busy", name="busy", tool_policy="read_only"),
        AgentSpec("worker", "waiting", name="waiting", tool_policy="read_only"),
    ))
    busy = registry.task_for_name(plan, "busy")
    waiting = registry.task_for_name(plan, "waiting")
    assert busy is not None and waiting is not None
    runtime = AgentRuntime(registry, max_workers=1)
    entered, release = threading.Event(), threading.Event()
    finished = []
    try:
        first = runtime.submit(busy, lambda t: (
            entered.set(), release.wait(3), AgentResult(status="completed")
        )[-1])
        assert entered.wait(2)
        def run_second():
            finished.extend(runtime.run_group((waiting,), lambda _t: AgentResult(status="completed")))
        thread = threading.Thread(target=run_second)
        thread.start()
        # Its queued Future is registered as active even though it has not run.
        import time
        deadline = time.monotonic() + 2
        while not runtime.is_active(waiting.task_id) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert runtime.is_active(waiting.task_id)
        assert runtime.cancel(waiting.task_id)
        release.set()
        first.result(timeout=3)
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert finished[0][1].status == "cancelled"
        assert registry.get(waiting.task_id).status == "cancelled"
    finally:
        release.set()
        runtime.shutdown(wait=True)

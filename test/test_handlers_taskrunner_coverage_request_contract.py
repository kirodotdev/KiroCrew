"""What every task-runner handler takes from a request, whatever its route.

A JSON body that is not an object is a 400 on every converted handler; a
member's file input, inline plan, from-chat plan and to-chat hand-off carry the
caller's captured execution context, not identity fields from the body, and a
restricted (incognito or temporary) inline start keeps its input in memory;
only the owner cancels the shared planning; and an exact cancel stops the task
it names, not another run whose name collides. Shared helpers live in
``handlers_taskrunner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _body,
    _chat_state,
    _request,
    _runner,
    _state,
    _stub_sel,
)

from kiro_crew.dashboard.handlers.taskrunner import (
    api_taskrunner_cancel,
    api_taskrunner_execute_plan,
    api_taskrunner_from_chat,
    api_taskrunner_plan,
    api_taskrunner_plan_cancel,
    api_taskrunner_refine,
    api_taskrunner_refine_answer,
    api_taskrunner_rename,
    api_taskrunner_retry,
    api_taskrunner_start,
    api_taskrunner_to_chat,
    api_taskrunner_update_plan,
    api_taskrunner_update_task,
)
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.taskrunner import TaskRun


class TestNonObjectBodiesAcrossConvertedHandlers:
    """Every converted handler answers 400, never 5xx, on a non-object body.

    ``[]`` / ``"s"`` / ``5`` / ``true`` / ``null`` are all VALID JSON, so
    ``request.json()`` returned them and the ``.get()`` each handler performs
    next raised ``AttributeError`` from OUTSIDE the parse ``try`` -- a 500 for
    what is really malformed client input. Enumerated rather than
    one test per handler so a handler that loses the guard fails by
    construction; the cap decision for each of these sites is recorded in
    ``_CAP_REGISTER`` in ``test_json_object_body_guard.py``.
    """

    _HANDLERS = [
        (api_taskrunner_start, {}),
        (api_taskrunner_cancel, {}),
        (api_taskrunner_rename, {"task_id": "t1"}),
        (api_taskrunner_update_task, {"task_id": "t1", "index": "0"}),
        (api_taskrunner_retry, {"task_id": "t1"}),
        (api_taskrunner_plan, {}),
        (api_taskrunner_update_plan, {"task_id": "t1"}),
        (api_taskrunner_execute_plan, {"task_id": "t1"}),
        (api_taskrunner_from_chat, {}),
        (api_taskrunner_refine, {}),
        (api_taskrunner_refine_answer, {}),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [[], "a string", 5, 1.5, True, None], ids=repr)
    @pytest.mark.parametrize(
        "handler,match_info", _HANDLERS, ids=lambda v: getattr(v, "__name__", "")
    )
    async def test_non_object_body_is_400_not_500(
        self, handler: Any, match_info: dict[str, str], payload: Any, tmp_path: Path
    ) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(spec_path="s.md", spec_content="s", task_id="t1")
        req = _request(_state(runner), json_body=payload, match_info=match_info, body_present=True)
        resp = await handler(req)
        assert resp.status == 400, f"{handler.__name__} on {payload!r}: expected 400"
        assert _body(resp)["code"] == "body_not_object", handler.__name__


def _member_execution(mode="persistent"):
    return ExecutionContext(
        "alice", MemoryStoreRef("alice-store", "alice"), "member", "kirocrew", mode
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [api_taskrunner_start, api_taskrunner_plan])
async def test_member_file_input_uses_ordinary_path_validation(tmp_path, handler):
    runner = _runner(tmp_path)
    runner._capture_execution.return_value = _member_execution()
    runner.plan.return_value = TaskRun("", "", task_id="new-plan")
    spec = tmp_path / "task.md"
    spec.write_text("work", encoding="utf-8")
    request = _request(
        _state(runner), json_body={"source": "file", "spec": str(spec)}, session="dashboard:alice"
    )
    request["internal_auth"] = True
    response = await handler(request)
    assert response.status == 200
    method = runner.start_background if handler is api_taskrunner_start else runner.plan
    assert method.await_args.kwargs["execution_context"] == _member_execution()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_restricted_inline_start_keeps_input_in_memory(tmp_path, mode):
    runner = _runner(tmp_path)
    runner._capture_execution.return_value = _member_execution(mode)
    response = await api_taskrunner_start(
        _request(_state(runner), json_body={"spec": "__inline__:restricted body"})
    )
    assert response.status == 200
    assert runner.start_background.await_args.kwargs["input_content"] == "restricted body"
    assert not list(tmp_path.rglob("TASK_*.md"))


@pytest.mark.asyncio
async def test_inline_plan_uses_authenticated_origin_and_captured_context(tmp_path):
    runner = _runner(tmp_path)
    runner._capture_execution.return_value = _member_execution()
    runner.plan.return_value = TaskRun("", "", task_id="new-plan")
    request = _request(
        _state(runner),
        session="dashboard:alice",
        json_body={"input": "plan", "session_key": "dashboard:bob", "memory_store": "bob-store"},
    )
    request["internal_auth"] = True
    response = await api_taskrunner_plan(request)
    assert response.status == 200
    assert runner.plan.await_args.kwargs["session_key"] == "dashboard:alice"
    assert runner.plan.await_args.kwargs["execution_context"] == _member_execution()


@pytest.mark.asyncio
async def test_from_chat_owns_context_before_runtime_binding(tmp_path):
    runner = _runner(tmp_path)
    execution = _member_execution()
    runner._capture_execution.return_value = execution
    runner.update_plan.side_effect = lambda task_id, steps: runner._runs[task_id]
    request = _request(
        _state(runner),
        session="dashboard:alice",
        json_body={"steps": [{"title": "step"}], "session_key": "dashboard:bob"},
    )
    request["internal_auth"] = True
    response = await api_taskrunner_from_chat(request)
    assert response.status == 200
    task_id = _body(response)["task_id"]
    run = runner._runs[task_id]
    assert run.execution_context == execution
    runner._bind_run_execution.assert_awaited_once_with(run, f"taskrunner:{task_id}:runtime")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["planned", "completed"])
async def test_task_to_chat_uses_owning_record_without_runtime_session(tmp_path, status):
    from kiro_crew.execution_context import read_session_execution

    runner = _runner(tmp_path)
    execution = _member_execution()
    runner._runs["member-run"] = TaskRun(
        "s.md", "material", task_id="member-run", status=status, execution_context=execution
    )
    runner.plan_to_chat_context.return_value = "plan"
    state = _chat_state(runner)
    slot = state.get_or_create_slot.return_value

    def create(*args, **kwargs):
        assert read_session_execution(kwargs["linked_session_key"]) == execution
        assert kwargs["memory_mode"] == execution.memory_mode
        return slot

    state.get_or_create_slot.side_effect = create
    slot.append.side_effect = lambda *args: (
        None if slot.memory_store == "alice-store" else pytest.fail("unbound append")
    )
    with patch("kiro_crew.dashboard.chat._run_chat", AsyncMock()) as chat:
        response = await api_taskrunner_to_chat(
            _request(state, match_info={"task_id": "member-run"})
        )
        # flake-ok: the background task runs the patched AsyncMock _run_chat and ends on its first pass
        await asyncio.gather(*list(state._background_tasks))
    assert response.status == 200
    chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_internal_caller_cannot_cancel_shared_planning(tmp_path):
    runner = _runner(tmp_path)
    request = _request(_state(runner))
    request["internal_auth"] = True
    response = await api_taskrunner_plan_cancel(request)
    assert response.status == 403
    runner.cancel_plan.assert_not_called()


@pytest.mark.asyncio
async def test_owner_can_cancel_shared_planning(tmp_path):
    runner = _runner(tmp_path)
    response = await api_taskrunner_plan_cancel(_request(_state(runner)))
    assert response.status == 200
    runner.cancel_plan.assert_called_once_with()


def test_exact_task_cancel_does_not_follow_another_runs_colliding_name():
    from kiro_crew.taskrunner import TaskRunner

    runner = object.__new__(TaskRunner)
    own = TaskRun(spec_path="own", spec_content="", task_id="own-id", status="running")
    peer = TaskRun(
        spec_path="peer", spec_content="", task_id="peer-id", name="own-id", status="running"
    )
    runner._runs = {"own-id": own, "peer-id": peer}
    runner._tasks = {}
    runner.cancel("own-id", exact=True)
    assert own.status == "cancelling"
    assert peer.status == "running"

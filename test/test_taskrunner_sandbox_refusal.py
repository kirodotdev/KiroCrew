"""A host that cannot sandbox a task run's planning session: one line, stored nowhere.

The launcher refuses with ``SandboxUnavailableError`` (no sandbox backend on this host)
or ``SandboxCeilingUnsealable`` (a governance ceiling that cannot be sealed), and each
one's own text names the host's remedy, the machine owner's runbook. ``decompose`` lets
the refusal through. A run (``run``, ``execute_plan``, ``retry_from_task``, a replan in
any of them) reads it as an empty plan: ``run.error``, its notices and every record built
from them carry the ordinary text, and the traceback is logged at DEBUG only. The
refusal's own text reaches two live readers alone: ``kirocrew run``'s stderr line
(``live_error``) and the plan route's answer to the machine owner.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _body,
    _request,
    _runner,
    _state,
    _stub_sel,
)
from taskrunner_helpers import _make_mock_sessions

from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_plan, api_taskrunner_status
from kiro_crew.sandbox import SandboxCeilingUnsealable, SandboxUnavailableError
from kiro_crew.task_planner import PlanSandboxRefused, decompose
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner
from kiro_crew.taskrunner_parts.run_reporting import live_error
from kiro_crew.testing.wait import default_timeout

_NO_BACKEND = "Sandbox backend unavailable and allow_unsandboxed_exec is not set. Run X."
_UNSEALABLE = "Cannot seal computer_use.json: a dangling symlink sits there. Remove it."
_REFUSED_MESSAGE = "the host refused a sandbox"
_NO_RUN_PLAN = "Failed to decompose spec into tasks"
_NO_PLAN = "Could not generate a plan. Try rephrasing."

_REFUSALS = [
    pytest.param(
        lambda: SandboxUnavailableError(_NO_BACKEND, kind="no_backend", detail="userns denied"),
        _NO_BACKEND,
        id="no-sandbox-backend",
    ),
    pytest.param(lambda: SandboxCeilingUnsealable(_UNSEALABLE), _UNSEALABLE, id="unsealable"),
]


def _sessions_refusing(exc: BaseException) -> MagicMock:
    sessions = MagicMock()

    async def _open_task_session(*_a, **_k):
        raise exc

    sessions.open_task_session = _open_task_session
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _assert_stored_nowhere(text: str, root: Path, notify: AsyncMock, caplog) -> None:
    """*text* is in no file under *root* (runs.json, TASK_PROGRESS.md) or the per-test data
    home (the SEL audit log, the workflow and admission stores), no notice, and no log
    record above DEBUG; its traceback is in the DEBUG refusal record alone."""
    home = Path(os.environ.get("KIROCREW_HOME", "")) if os.environ.get("KIROCREW_HOME") else None
    for base in (root, home):
        if base is None or not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                assert text not in path.read_text(encoding="utf-8", errors="replace"), path
    assert not [call for call in notify.await_args_list if text in str(call.args)]
    loud = [r for r in caplog.records if r.levelno > logging.DEBUG]
    assert not [r for r in loud if r.exc_info or text in r.getMessage()]
    assert [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and r.exc_info and _REFUSED_MESSAGE in r.getMessage()
    ]


def _git_stubs():
    return (
        patch("kiro_crew.git_coord.init_workspace", AsyncMock()),
        patch("kiro_crew.git_coord.finalize", AsyncMock(return_value="")),
        patch("kiro_crew.git_coord.get_state_summary", AsyncMock(return_value="")),
    )


@pytest.mark.parametrize(("make", "text"), _REFUSALS)
def test_decompose_lets_a_sandbox_refusal_through(make, text, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    sessions = _sessions_refusing(make())
    with pytest.raises((SandboxUnavailableError, SandboxCeilingUnsealable)) as caught:
        asyncio.run(decompose("spec", sessions, ctx=None, task_id="t1"))
    assert str(caught.value) == text
    # The decompose session is still released and reset.
    sessions.release.assert_called_once_with("taskrunner:t1:decompose")
    sessions.reset.assert_awaited_once_with("taskrunner:t1:decompose")
    assert not [r for r in caplog.records if r.levelno > logging.DEBUG and r.exc_info]


def test_any_other_decompose_failure_is_still_an_empty_plan() -> None:
    sessions = _sessions_refusing(RuntimeError("the backend went away"))
    assert asyncio.run(decompose("spec", sessions, ctx=None, task_id="t1")) == []


async def _refused_run(
    make, tmp_path: Path, notify: AsyncMock, spec_name: str = "TASK.md"
) -> tuple[TaskRunner, TaskRun]:
    """A real run (and the real decompose) whose planning session's start is refused.

    A ``.yaml`` *spec_name* that is not workflow-shaped takes ``run()``'s other call site,
    the attended fallback to the LLM decomposer."""
    spec = tmp_path / spec_name
    spec.write_text("# Say hello" if spec_name.endswith(".md") else "Say hello\n", encoding="utf-8")
    sessions = _make_mock_sessions()
    sessions.get_or_create = AsyncMock(side_effect=make())
    runner = TaskRunner(
        sessions=sessions, auto_test=False, work_dir=tmp_path / "work", on_notify=notify
    )
    runner._taskq_end_run = AsyncMock()  # type: ignore[method-assign]
    init, finalize, summary = _git_stubs()
    with init, finalize, summary:
        run = await runner.run(spec, task_id="r1", source="dashboard")
    return runner, run


@pytest.mark.asyncio
@pytest.mark.parametrize(("make", "text"), _REFUSALS)
@pytest.mark.parametrize("spec_name", ["TASK.md", "TASK.yaml"], ids=["markdown", "yaml-fallback"])
async def test_a_refused_run_stores_the_ordinary_error_and_the_cli_reads_the_refusal(
    make, text, spec_name: str, tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    notify = AsyncMock()
    runner, run = await _refused_run(make, tmp_path, notify, spec_name)
    assert run.status == "failed"
    assert run.error == _NO_RUN_PLAN
    # ``task_reporter.notify`` leads each title with the run's label.
    label = run.name or Path(run.spec_path).stem
    assert notify.await_args_list[-1].args[:2] == (f"[{label}] Error: Task failed", _NO_RUN_PLAN)
    assert cast(AsyncMock, runner._taskq_end_run).await_args_list[-1].args[0].error == _NO_RUN_PLAN
    _assert_stored_nowhere(text, tmp_path, notify, caplog)
    # The task that awaited the run (``kirocrew run``) reads the refusal's own text.
    assert live_error(run) == text


@pytest.mark.asyncio
@pytest.mark.parametrize(("make", "text"), _REFUSALS)
async def test_a_later_different_error_is_shown_as_it_is(make, text, tmp_path: Path) -> None:
    _runner_obj, run = await _refused_run(make, tmp_path, AsyncMock())
    run.error = "Task 1 failed: the step failed"
    assert live_error(run) == "Task 1 failed: the step failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(("make", "text"), _REFUSALS)
async def test_a_replan_refused_inside_run_stores_the_ordinary_error_and_the_cli_reads_it(
    make, text, tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    notify = AsyncMock()
    runner = _runner_whose_replan_is_refused(tmp_path / "work", make(), notify)
    first_plan = [Step(index=1, title="A", description="d")]
    refused = runner._decompose

    async def _decompose(*a, **k):  # type: ignore[no-untyped-def]
        nonlocal first_plan
        if first_plan:
            plan, first_plan = first_plan, []
            return plan
        return await refused(*a, **k)

    runner._decompose = _decompose  # type: ignore[method-assign]
    spec = tmp_path / "TASK.md"
    spec.write_text("# Say hello", encoding="utf-8")
    init, finalize, summary = _git_stubs()
    with init, finalize, summary:
        run = await runner.run(spec, task_id="r1", source="dashboard")
    assert run.status == "failed"
    assert run.error == "Re-plan failed after task 1"
    _assert_stored_nowhere(text, tmp_path, notify, caplog)
    # The replan ran in this task, so the CLI's reader sees the refusal.
    assert live_error(run) == text


def _runner_whose_replan_is_refused(tmp_path: Path, exc: BaseException, notify) -> TaskRunner:
    runner = TaskRunner(
        sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path, on_notify=notify
    )

    async def _step(run, task, history_key="", session_key=""):  # type: ignore[no-untyped-def]
        task.status = StepStatus.FAILED
        task.error = "the step failed"
        return False

    async def _decompose(*_a, **_k):  # type: ignore[no-untyped-def]
        raise exc

    runner._execute_single_task = _step  # type: ignore[method-assign]
    runner._decompose = _decompose  # type: ignore[method-assign]
    runner._taskq_end_run = AsyncMock()  # type: ignore[method-assign]
    return runner


def _seed(runner: TaskRunner, tmp_path: Path, status: str) -> TaskRun:
    run = TaskRun(
        spec_path=str(tmp_path / "TASK.md"),
        spec_content="# Say hello",
        status=status,
        task_id="p1",
        work_dir=str(tmp_path / "p1"),
    )
    run.tasks = [Step(index=1, title="A", description="d")]
    runner._runs["p1"] = run
    return run


async def _drive_replan(runner: TaskRunner, tmp_path: Path, driver: str) -> TaskRun:
    run = _seed(runner, tmp_path, "planned" if driver == "execute_plan" else "failed")
    init, finalize, summary = _git_stubs()
    with init, finalize, summary:
        if driver == "execute_plan":
            await runner.execute_plan("p1")
        else:
            runner._ensure_resumable_workspace = AsyncMock(return_value=True)  # type: ignore[method-assign]
            await runner.retry_from_task("p1", 1)
        await asyncio.wait_for(runner._tasks["p1"], timeout=default_timeout())
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("driver", ["execute_plan", "retry_from_task"])
@pytest.mark.parametrize(("make", "text"), _REFUSALS)
async def test_a_refused_replan_stores_the_ordinary_error(
    make, text, driver: str, tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    notify = AsyncMock()
    runner = _runner_whose_replan_is_refused(tmp_path, make(), notify)
    run = await _drive_replan(runner, tmp_path, driver)
    assert run.status == "failed"
    assert run.error == "Re-plan failed after task 1"
    end_run = cast(AsyncMock, runner._taskq_end_run)
    assert end_run.await_args_list[-1].args[0].error == "Re-plan failed after task 1"
    _assert_stored_nowhere(text, tmp_path, notify, caplog)
    # The run's own background task held the refusal; this task never saw it.
    assert live_error(run) == "Re-plan failed after task 1"


@pytest.mark.asyncio
@pytest.mark.parametrize(("make", "text"), _REFUSALS)
async def test_plan_raises_the_ordinary_error_carrying_the_refusal(
    make, text, tmp_path: Path
) -> None:
    runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)

    async def _decompose(*_a, **_k):  # type: ignore[no-untyped-def]
        raise make()

    runner._decompose = _decompose  # type: ignore[method-assign]
    with pytest.raises(PlanSandboxRefused) as caught:
        await runner.plan("say hello", source="text")
    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == _NO_PLAN
    assert caught.value.refusal == text


async def _admitted(work, **_kw):  # type: ignore[no-untyped-def]
    return await work


_CALLERS = ["machine-owner", "teammate", "app-token", "internal-secret"]


def _caller_request(runner, caller: str):  # type: ignore[no-untyped-def]
    """A request from *caller*; the teammate passes the owner gate as the owner does."""
    request = _request(
        _state(runner),
        json_body={"input": "x"},
        request_app="some-app" if caller == "app-token" else "",
    )
    if caller == "teammate":
        request["user"] = "teammate-1"
    if caller == "internal-secret":
        del request["app"]  # an X-Internal-Secret caller carries no app key
        request["internal_auth"] = True
    return request


async def _answer(runner, caller: str, route=api_taskrunner_plan):  # type: ignore[no-untyped-def]
    with (
        patch("kiro_crew.shared_crew.turn_gate.run_admitted_or_raise", _admitted),
        patch(
            "kiro_crew.shared_crew.teammate_access.teammate_passes_owner_gate",
            return_value=caller == "teammate",
        ),
    ):
        return await route(_caller_request(runner, caller))


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", _CALLERS)
async def test_the_plan_route_shows_the_refusal_to_the_machine_owner_alone(
    tmp_path: Path, caller: str
) -> None:
    runner = _runner(tmp_path)
    runner.plan = AsyncMock(side_effect=PlanSandboxRefused(_NO_PLAN, _NO_BACKEND))
    resp = await _answer(runner, caller)
    assert resp.status == 400
    assert _body(resp)["error"] == (_NO_BACKEND if caller == "machine-owner" else _NO_PLAN)


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", _CALLERS)
async def test_the_plan_route_keeps_any_other_value_error_for_every_caller(
    tmp_path: Path, caller: str
) -> None:
    runner = _runner(tmp_path)
    runner.plan = AsyncMock(side_effect=ValueError(_NO_PLAN))
    resp = await _answer(runner, caller)
    assert resp.status == 400
    assert _body(resp)["error"] == _NO_PLAN


@pytest.mark.asyncio
@pytest.mark.parametrize(("make", "text"), _REFUSALS)
async def test_the_run_list_shows_the_ordinary_error(make, text, tmp_path: Path) -> None:
    # The status handler does not branch on its caller, so one caller stands for all.
    runner, _run = await _refused_run(make, tmp_path, AsyncMock())
    resp = await _answer(runner, "machine-owner", route=api_taskrunner_status)
    row = _body(resp)["runs"][0]
    assert row["error"] == _NO_RUN_PLAN
    assert text not in resp.text

"""Task projects own member routing across asynchronous work and restarts."""

import asyncio
import json
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_taskrunner_coverage_helpers import _body, _request, _state

import kiro_crew.taskrunner as taskrunner_module
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_retry
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    read_session_execution,
)
from kiro_crew.history import ConversationLog
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.task_models import PROGRESS_FILE, Project, Task, TaskStatus
from kiro_crew.task_reporter import save_progress
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.testing.wait import await_until


def member_execution(member="alpha", mode="persistent"):
    return ExecutionContext(
        member, MemoryStoreRef("memory-" + member, member), "member", "kirocrew", mode
    )


def template_execution(template="custom-agent"):
    """The carrier a chat on *template* binds (``session_agent_selection``)."""
    return ExecutionContext(
        None, MemoryStoreRef("default"), "template", template, "persistent", "", template
    )


def member_bound_execution(kind, template, member="alpha"):
    """A carrier bound to *member*: its own chat, or its crewmate worker's."""
    return ExecutionContext(member, MemoryStoreRef("memory-" + member, member), kind, template)


@pytest.fixture(autouse=True)
def _user_level_custom_agents():
    """The custom agents these chats run are user-level specs, as dashboard-made ones are."""
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    for name in ("custom-agent", "other-agent"):
        (agents / f"{name}.json").write_text(json.dumps({"name": name}), encoding="utf-8")


def make_runner(tmp_path):
    sessions = MagicMock()
    sessions.admission_closed = False
    return TaskRunner(sessions=sessions, work_dir=tmp_path, auto_test=False)


def _discard_scheduled_coroutine(coro):
    """Own the background worker without running it; the test drives the steps."""
    coro.close()
    return MagicMock()


def record_session_agents(monkeypatch):
    """Record, per run, the agent each step, review and decompose session opens on.

    The three openers are faked at the names the runner calls, so no session is
    opened and no step runs: what is recorded is the ``agent`` the runner hands
    each one for that run.
    """
    seen = defaultdict(list)

    async def step(*, run, task, agent, **_kwargs):
        seen[run.task_id].append(("step", agent))
        return True

    async def review(run, task, sessions, agent, *, session_key="", ctx=None):
        seen[run.task_id].append(("review", agent))
        return True

    async def decompose(spec, sessions, ctx, *, work_dir, task_id, agent, start_priority):
        seen[task_id].append(("decompose", agent))
        return [Task(1, "step", "step")]

    monkeypatch.setattr(taskrunner_module, "execute_single_task", step)
    monkeypatch.setattr(taskrunner_module, "self_review_fn", review)
    monkeypatch.setattr(taskrunner_module, "decompose", decompose)
    return seen


async def run_remaining_work(runner, run):
    """What a run does after its start: a step, its review and a re-plan."""
    task = Task(len(run.tasks) + 1, "step", "step")
    await runner._execute_single_task(run, task)
    await runner.self_review(run, task)
    # A re-plan hands the run's explicit agent, as ``_try_replan`` does.
    await runner._decompose("re-plan", run.work_dir, run.task_id, agent=run.agent)


def all_as(agent):
    return [("step", agent), ("review", agent), ("decompose", agent)]


def write_spec(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# Step 1\nsearch the code", encoding="utf-8")
    return spec


CARRIER_CASES = [
    pytest.param(template_execution(), "", "custom-agent", id="template-carrier"),
    pytest.param(template_execution(), "picked", "picked", id="explicit-agent-wins"),
    pytest.param(template_execution("kirocrew"), "", "", id="default-chat"),
    pytest.param(template_execution("project-only-agent"), "", "", id="project-only-template"),
    pytest.param(member_bound_execution("member", "member-template"), "", "", id="member-carrier"),
    pytest.param(
        member_bound_execution("template", MATE_WORKER_AGENT),
        "",
        "",
        id="crewmate-worker-carrier",
    ),
    pytest.param(None, "", "", id="no-origin"),
]


@pytest.mark.parametrize(("carrier", "explicit", "expected"), CARRIER_CASES)
@pytest.mark.asyncio
async def test_a_plan_decomposes_and_runs_as_the_starting_chat_s_agent(
    tmp_path, monkeypatch, carrier, explicit, expected
):
    runner = make_runner(tmp_path)
    seen = record_session_agents(monkeypatch)
    session_key = ""
    if carrier is not None:
        session_key = "source-chat"
        bind_session_execution(session_key, carrier)

    run = await runner.plan("a plan", agent=explicit, session_key=session_key)
    with patch("kiro_crew.taskrunner.asyncio.create_task", _discard_scheduled_coroutine):
        await runner.execute_plan(run.task_id, agent=explicit)
    await run_remaining_work(runner, run)

    assert seen[run.task_id] == [("decompose", expected), *all_as(expected)]
    # The inherited agent is resolved per session; ``run.agent`` holds only a pick.
    assert runner.status()["agent"] == explicit


@pytest.mark.asyncio
async def test_a_background_start_runs_as_the_starting_chat_s_agent(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    seen = record_session_agents(monkeypatch)
    bind_session_execution("source-chat", template_execution())

    async def fake_run(_spec_path, *, task_id, **_kwargs):
        await run_remaining_work(runner, runner._runs[task_id])

    monkeypatch.setattr(runner, "run", fake_run)

    task_id = await runner.start_background(write_spec(tmp_path), session_key="source-chat")
    await await_until(runner._tasks[task_id], what="the background run")

    assert seen[task_id] == all_as("custom-agent")
    assert runner.status()["agent"] == ""


@pytest.mark.asyncio
async def test_overlapping_runs_from_different_chats_each_keep_their_own_agent(
    tmp_path, monkeypatch
):
    """A start from one chat never moves a run another chat started onto its agent."""
    runner = make_runner(tmp_path)
    seen = record_session_agents(monkeypatch)
    bind_session_execution("default-chat", template_execution("kirocrew"))
    bind_session_execution("custom-chat", template_execution())
    spec = write_spec(tmp_path)
    started = defaultdict(asyncio.Event)
    proceed = defaultdict(asyncio.Event)

    async def fake_run(_spec_path, *, task_id, **_kwargs):
        run = runner._runs[task_id]
        await run_remaining_work(runner, run)
        started[task_id].set()
        await proceed[task_id].wait()
        await run_remaining_work(runner, run)

    monkeypatch.setattr(runner, "run", fake_run)

    async def start(**kwargs):
        task_id = await runner.start_background(spec, **kwargs)
        await await_until(started[task_id], what=f"{task_id} doing its first work")
        return task_id

    async def finish(task_id):
        proceed[task_id].set()
        await await_until(runner._tasks[task_id], what=f"{task_id} finishing")

    default_run = await start(session_key="default-chat")
    custom_run = await start(session_key="custom-chat")
    await finish(default_run)
    # A dashboard start names no origin, so it captures the default floor.
    dashboard_run = await start()
    await finish(custom_run)
    await finish(dashboard_run)

    assert seen[default_run] == all_as("") * 2
    assert seen[custom_run] == all_as("custom-agent") * 2
    assert seen[dashboard_run] == all_as("") * 2


def failed_run(task_id, execution):
    run = Project(
        f"{task_id}.md", "saved plan", task_id=task_id, status="failed", execution_context=execution
    )
    run.tasks = [Task(1, "step", "step", status=TaskStatus.FAILED)]
    return run


async def retry_as_the_dashboard_does(runner, task_id):
    """Retry through ``api_taskrunner_retry`` with the body the dashboard sends.

    The body names only ``from_step``, and the handler passes no agent, so
    each run keeps the agent it had.
    """
    request = _request(_state(runner), match_info={"task_id": task_id}, json_body={"from_step": 1})
    with (
        patch("kiro_crew.dashboard.handlers.taskrunner._sel"),
        patch("kiro_crew.taskrunner.asyncio.create_task", _discard_scheduled_coroutine),
    ):
        response = await api_taskrunner_retry(request)
    assert _body(response) == {"ok": True, "task_id": task_id}


@pytest.mark.asyncio
async def test_a_dashboard_retry_keeps_each_run_on_its_own_chat_s_agent(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    seen = record_session_agents(monkeypatch)
    runs = [
        failed_run("custom-run", template_execution()),
        failed_run("default-run", template_execution("kirocrew")),
        failed_run("other-run", template_execution("other-agent")),
    ]
    for run in runs:
        runner._runs[run.task_id] = run

    for run in runs:
        await retry_as_the_dashboard_does(runner, run.task_id)
        await run_remaining_work(runner, run)

    assert seen["custom-run"] == all_as("custom-agent")
    assert seen["default-run"] == all_as("")
    assert seen["other-run"] == all_as("other-agent")


@pytest.mark.parametrize("entry", ["execute_plan", "retry_from_task"])
@pytest.mark.asyncio
async def test_a_resumed_run_keeps_the_agent_its_persisted_carrier_names(
    tmp_path, monkeypatch, entry
):
    runner = make_runner(tmp_path)
    seen = record_session_agents(monkeypatch)
    run = failed_run("saved", template_execution())
    if entry == "execute_plan":
        run.status = "planned"
    runner._runs[run.task_id] = run
    await runner._apersist_runs()
    restored = make_runner(tmp_path)

    if entry == "execute_plan":
        with patch("kiro_crew.taskrunner.asyncio.create_task", _discard_scheduled_coroutine):
            await restored.execute_plan(run.task_id)
    else:
        await retry_as_the_dashboard_does(restored, run.task_id)
    await run_remaining_work(restored, restored._runs[run.task_id])

    assert seen[run.task_id] == all_as("custom-agent")


@pytest.mark.asyncio
async def test_plan_captures_member_before_admission_and_survives_parent_change(
    tmp_path, monkeypatch
):
    runner = make_runner(tmp_path)
    alpha = member_execution()
    bind_session_execution("source-chat", alpha)
    runner._decompose = AsyncMock(return_value=[Task(1, "test", "test")])
    captured = asyncio.Event()
    refresh = runner._refresh_from_config

    def refreshed():
        refresh()
        captured.set()

    monkeypatch.setattr(runner, "_refresh_from_config", refreshed)
    await runner._start_lock.acquire()
    pending = asyncio.create_task(runner.plan("a plan", session_key="source-chat"))
    try:
        await await_until(captured, what="the plan capturing its execution context")
        await asyncio.to_thread(
            bind_session_execution,
            "source-chat",
            member_execution("beta"),
            replace_existing=True,
        )
    except BaseException:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        raise
    finally:
        runner._start_lock.release()
    run = await await_until(pending, what="the plan finishing")
    assert run.execution_context == alpha
    assert read_session_execution(f"taskrunner:{run.task_id}:runtime") == alpha
    persisted = json.loads(runner._runs_path().read_text(encoding="utf-8"))
    assert persisted[0]["execution_context"] == alpha.to_record()


@pytest.mark.asyncio
async def test_restored_project_rebinds_without_origin_or_runtime_lookup(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    alpha = member_execution()
    run = Project("", "saved plan", task_id="restored", status="planned", execution_context=alpha)
    runner._runs[run.task_id] = run
    await runner._apersist_runs()
    restored = make_runner(tmp_path)
    restored_run = restored._runs[run.task_id]
    assert restored_run.execution_context == alpha
    monkeypatch.setattr(
        restored, "_capture_execution", MagicMock(side_effect=AssertionError("origin reread"))
    )
    history_key = await restored._bound_history_key(restored_run, "global-key")
    assert history_key == "taskrunner:run:restored"
    assert read_session_execution(history_key) == alpha
    assert read_session_execution("taskrunner:restored:runtime") == alpha


def test_legacy_run_recovers_runtime_identity_without_global_fallback(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    legacy = ExecutionContext(None, MemoryStoreRef("legacy-v1"), "template", "kirocrew")
    runtime_key = "taskrunner:legacy-v1-run:runtime"
    monkeypatch.setattr(
        ConversationLog,
        "get_metadata_status",
        lambda _self, key: (
            ({"memory_store": "legacy-v1"}, True) if key == runtime_key else ({}, True)
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.taskrunner.capture_session_execution",
        lambda key: legacy if key == runtime_key else None,
    )
    runner._runs_path().write_text(
        json.dumps(
            [
                {
                    "task_id": "legacy-v1-run",
                    "spec_path": "legacy.md",
                    "status": "paused",
                    "memory_store": "legacy-v1",
                }
            ]
        ),
        encoding="utf-8",
    )

    restored = make_runner(tmp_path)

    assert restored._runs["legacy-v1-run"].execution_context == legacy


def test_legacy_run_without_runtime_identity_is_not_rebound_to_global(tmp_path, monkeypatch):
    runner = make_runner(tmp_path)
    monkeypatch.setattr(ConversationLog, "get_metadata_status", lambda _self, _key: ({}, True))
    runner._runs_path().write_text(
        json.dumps(
            [
                {
                    "task_id": "legacy-without-identity",
                    "spec_path": "legacy.md",
                    "status": "paused",
                    "memory_store": "legacy-v1",
                }
            ]
        ),
        encoding="utf-8",
    )

    restored = make_runner(tmp_path)

    assert not restored._runs
    assert restored._snapshot_recovery_incomplete


@pytest.mark.parametrize("mode", ["incognito", "temporary"])
@pytest.mark.asyncio
async def test_restricted_project_does_not_persist_body_or_learn(tmp_path, mode):
    runner = make_runner(tmp_path)
    runner._conversation_log = MagicMock()
    runner._call_llm_for_lesson = AsyncMock()
    run = Project(
        str(tmp_path / "input.md"),
        "private body",
        task_id="restricted",
        status="planned",
        execution_context=member_execution(mode=mode),
    )
    task = Task(1, "private title", "private description", result="private result")
    run.tasks = [task]
    runner._runs[run.task_id] = run
    await runner._apersist_runs()
    save_progress(run)
    runner._log_task("history", run, task)
    await runner._extract_lesson(task, run)
    assert json.loads(runner._runs_path().read_text(encoding="utf-8")) == []
    assert not (tmp_path / PROGRESS_FILE).exists()
    runner._conversation_log.append.assert_not_called()
    runner._call_llm_for_lesson.assert_not_awaited()

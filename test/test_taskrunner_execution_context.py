"""Task projects own member routing across asynchronous work and restarts."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    read_session_execution,
)
from kiro_crew.history import ConversationLog
from kiro_crew.task_models import PROGRESS_FILE, Project, Task
from kiro_crew.task_reporter import save_progress
from kiro_crew.taskrunner import TaskRunner


def member_execution(member="alpha", mode="persistent"):
    return ExecutionContext(
        member, MemoryStoreRef("memory-" + member, member), "member", "kirocrew", mode
    )


def make_runner(tmp_path):
    sessions = MagicMock()
    sessions.admission_closed = False
    return TaskRunner(sessions=sessions, work_dir=tmp_path, auto_test=False)


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
        await asyncio.wait_for(captured.wait(), timeout=5)
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
    run = await asyncio.wait_for(pending, timeout=5)
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

"""A step that declares a status file is judged by what it reported, not by its turn ending.

A YAML workflow agent names ``status_file``; when the step's turn lands, a
``"status": "failed"`` there (or a file the step did not write) fails the step and its
run, with no retry and no replan, so the steps that depend on it never start. The
declaration and the run's originating conversation survive a gateway restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from conftest import make_escaping_link
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_planner import (
    decompose_yaml,
    plan_to_chat_context,
    plan_to_yaml,
    update_plan_tasks,
)
from kiro_crew.taskrunner import TaskRunner

_CURRENT = re.compile(r"^## Current Task \(\d+/\d+\)\n(.*?)(?=^## |\Z)", re.S | re.M)

_SPEC = """
agents:
  taskei:
    description: Read Taskei tasks
    status_file: out/taskei.json
    prompt: Read the team's tasks and write out/taskei.json with a status.
  triage:
    description: Triage the sources
    depends_on: [taskei]
    prompt: Read out/taskei.json and write triage.json.
"""


def _event(kind: str, text: str = ""):
    from kiro_crew.providers.base import LLMEvent

    return LLMEvent(kind=kind, text=text)


def _sessions(on_step_turn) -> MagicMock:
    """Mock sessions; a step turn (not a review turn) calls *on_step_turn* with its title."""
    provider = MagicMock()

    async def _stream(msg: str):
        current = _CURRENT.search(msg)
        if current is not None:
            on_step_turn(current.group(1).strip().splitlines()[0].strip("* "))
        yield _event("text_chunk", "done")
        yield _event("complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    s = MagicMock()
    s._lock = asyncio.Lock()
    s._sessions = {}
    s.get_or_create = AsyncMock(return_value=(provider, True, False))
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    s.record_failure = AsyncMock()
    s.check_context_usage = MagicMock()

    async def _open_task_session(
        _parent, key, *, agent=None, cwd=None, approval_policy="", start_priority=None
    ):
        return await s.get_or_create(key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    return s


def _runner_and_run(tmp_path: Path, status: dict | None) -> tuple[TaskRunner, Project, list]:
    """The spec's two steps on a runner whose taskei turn writes *status* (None: nothing)."""
    turns: list[str] = []

    def _turn(title: str) -> None:
        turns.append(title)
        if title == "Read Taskei tasks" and status is not None:
            out = tmp_path / "out"
            out.mkdir(exist_ok=True)
            (out / "taskei.json").write_text(json.dumps(status), encoding="utf-8")

    runner = TaskRunner(sessions=_sessions(_turn), auto_test=False, work_dir=tmp_path)
    runner._decompose = AsyncMock(return_value=[])  # type: ignore[method-assign]
    run = Project(
        spec_path=str(tmp_path / "standup.yaml"),
        spec_content=_SPEC,
        tasks=decompose_yaml(_SPEC),
        status="running",
        task_id="standup_1",
        work_dir=str(tmp_path),
    )
    runner._runs[run.task_id] = run
    return runner, run, turns


class TestDeclaredInTheSpec:
    def test_a_yaml_agent_may_declare_its_status_file(self) -> None:
        tasks = decompose_yaml(_SPEC)
        assert [t.status_file for t in tasks] == ["out/taskei.json", ""]

    @pytest.mark.parametrize(
        "value",
        ["/etc/passwd", "../outside.json", "out/../../x.json", "C:\\x.json", "", "  ", 3, True],
    )
    def test_a_status_file_outside_the_work_dir_or_not_a_path_is_refused(self, value) -> None:
        spec = {"agents": {"taskei": {"prompt": "p", "status_file": value}}}
        with pytest.raises(ValueError, match="status_file"):
            decompose_yaml(json.dumps(spec))

    def test_the_declaration_round_trips_through_an_exported_plan(self) -> None:
        tasks = decompose_yaml(plan_to_yaml(decompose_yaml(_SPEC)))
        assert [t.status_file for t in tasks] == ["out/taskei.json", ""]


class TestAPlanEditKeepsTheDeclarationOnItsStep:
    """``update_plan_tasks`` keeps ``status_file`` on its own step, never on a position."""

    @staticmethod
    def _failed_run() -> Project:
        return Project(
            spec_path="standup.yaml",
            spec_content=_SPEC,
            tasks=decompose_yaml(_SPEC),
            status="failed",
            task_id="standup_1",
        )

    @staticmethod
    def _chat_items(run: Project) -> list[dict]:
        ctx = plan_to_chat_context(run)
        body = ctx.split("## Tasks JSON\n", 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
        return json.loads(body)

    @staticmethod
    def _editor_items(run: Project) -> list[dict]:
        """``ProjectDetailPage.handleSaveTask``'s shape: every step in place, no ``status_file``."""
        return [
            {
                "title": t.title,
                "description": t.description,
                "depends_on": t.depends_on,
                "requires_approval": t.requires_approval,
            }
            for t in run.tasks
        ]

    def test_the_chat_json_names_status_file_on_every_step(self) -> None:
        items = self._chat_items(self._failed_run())
        assert [item["status_file"] for item in items] == ["out/taskei.json", ""]

    def test_a_step_inserted_ahead_in_the_chat_json_does_not_take_the_declaration(self) -> None:
        run = self._failed_run()
        items = self._chat_items(run)
        # The new step carries no status_file key, as an LLM's new step need not.
        items.insert(0, {"title": "Refresh the token", "description": "log in again"})

        updated = update_plan_tasks(run, items)

        assert [(t.title, t.status_file) for t in updated.tasks] == [
            ("Refresh the token", ""),
            ("Read Taskei tasks", "out/taskei.json"),
            ("Triage the sources", ""),
        ]

    def test_a_step_removed_from_the_chat_json_does_not_pass_its_declaration_on(self) -> None:
        run = self._failed_run()

        updated = update_plan_tasks(run, self._chat_items(run)[1:])

        assert [(t.title, t.status_file) for t in updated.tasks] == [("Triage the sources", "")]

    @pytest.mark.parametrize("new_title", ["Read the team's Taskei tasks", "Triage the sources"])
    def test_a_step_the_editor_renames_keeps_its_declaration(self, new_title: str) -> None:
        run = self._failed_run()
        items = self._editor_items(run)
        items[0]["title"] = new_title  # also to the other step's title

        updated = update_plan_tasks(run, items)

        assert [(t.title, t.status_file) for t in updated.tasks] == [
            (new_title, "out/taskei.json"),
            ("Triage the sources", ""),
        ]

    @pytest.mark.parametrize("cleared", [None, ""])
    def test_an_item_that_names_it_sets_or_clears_it(self, cleared) -> None:
        run = self._failed_run()
        items = self._chat_items(run)
        items[0]["status_file"] = cleared
        items[1]["status_file"] = "triage.json"

        updated = update_plan_tasks(run, items)

        assert [t.status_file for t in updated.tasks] == ["", "triage.json"]

    def test_an_edit_naming_a_path_outside_the_work_dir_is_refused(self) -> None:
        run = self._failed_run()
        items = self._chat_items(run)
        items[1]["status_file"] = "../outside.json"

        with pytest.raises(ValueError, match="status_file"):
            update_plan_tasks(run, items)


class TestADeclaredFailureFailsTheStepAndStopsTheRun:
    @pytest.mark.asyncio
    async def test_a_status_file_that_says_failed_fails_the_step_and_its_dependents_never_run(
        self, tmp_path: Path
    ) -> None:
        runner, run, turns = _runner_and_run(
            tmp_path, {"source": "taskei", "status": "failed", "reason": "401 Unauthorized"}
        )

        await runner._execute_tasks(run, "taskrunner:run:standup")

        taskei, triage = run.tasks
        assert turns == ["Read Taskei tasks"]  # one turn: no retry, and triage never ran
        assert taskei.status == TaskStatus.FAILED
        assert taskei.error == "The step reported failure in out/taskei.json: 401 Unauthorized"
        assert triage.status == TaskStatus.PENDING
        assert not (tmp_path / "triage.json").exists()
        assert run.status == "failed"
        assert run.error == f"Task 1 failed: {taskei.error}"
        runner._decompose.assert_not_awaited()  # not re-planned around

    @pytest.mark.asyncio
    async def test_a_status_file_the_step_did_not_write_fails_it(self, tmp_path: Path) -> None:
        runner, run, turns = _runner_and_run(tmp_path, None)

        await runner._execute_tasks(run, "taskrunner:run:standup")

        assert turns == ["Read Taskei tasks"]
        assert run.tasks[0].status == TaskStatus.FAILED
        assert "did not write its status file out/taskei.json" in run.tasks[0].error
        assert run.status == "failed"

    @pytest.mark.asyncio
    async def test_any_other_status_lets_the_step_pass_and_the_run_go_on(
        self, tmp_path: Path
    ) -> None:
        runner, run, turns = _runner_and_run(tmp_path, {"status": "ok", "tasks": []})

        await runner._execute_tasks(run, "taskrunner:run:standup")

        assert turns == ["Read Taskei tasks", "Triage the sources"]
        assert [t.status for t in run.tasks] == [TaskStatus.PASSED, TaskStatus.PASSED]
        assert run.status == "running"


class TestReadingTheFile:
    """``declared_failure`` alone; imported per test so the rest of the module collects
    on a tree without it."""

    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            ('{"status": "FAILED", "error": "no token"}', "reported failure in s.json: no token"),
            ('{"status": "failed"}', "reported failure in s.json"),
            ("not json", "is not valid JSON"),
            ('["failed"]', "is not a JSON object"),
            ('{"status": "partial"}', ""),
            ("{}", ""),
        ],
    )
    def test_what_the_file_says(self, tmp_path: Path, content: str, expected: str) -> None:
        from kiro_crew.task_status_file import declared_failure

        (tmp_path / "s.json").write_text(content, encoding="utf-8")
        got = declared_failure(tmp_path, "s.json")
        assert (expected in got) if expected else got == ""

    def test_a_file_too_large_to_read_fails_the_step(self, tmp_path: Path) -> None:
        from kiro_crew.task_status_file import STATUS_FILE_MAX_BYTES, declared_failure

        (tmp_path / "s.json").write_bytes(b" " * (STATUS_FILE_MAX_BYTES + 1))
        assert "is larger than" in declared_failure(tmp_path, "s.json")

    def test_a_link_out_of_the_work_dir_is_not_followed(self, tmp_path: Path) -> None:
        from kiro_crew.task_status_file import declared_failure

        outside = tmp_path / "outside"
        outside.mkdir()
        # A passing report: only the containment check fails the step.
        (outside / "secret.py").write_text('{"status": "ok"}', encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        through_link = make_escaping_link(work, outside)

        got = declared_failure(work, through_link)

        assert got == f"Status file {through_link} is outside the step's work directory"

    def test_a_credential_file_in_a_home_work_dir_is_refused_unread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.task_status_file import declared_failure

        home = tmp_path / "home"
        (home / ".aws").mkdir(parents=True)
        # Content that passes the step if it were read: only the refusal fails it.
        (home / ".aws" / "credentials").write_text('{"status": "ok"}', encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        got = declared_failure(home, ".aws/credentials")

        assert got == "Status file .aws/credentials could not be read safely"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
    def test_a_fifo_with_no_writer_is_refused_without_blocking(self, tmp_path: Path) -> None:
        from kiro_crew.task_status_file import declared_failure

        fifo = tmp_path / "s.json"
        os.mkfifo(fifo)
        got: list[str] = []
        reader = threading.Thread(
            target=lambda: got.append(declared_failure(tmp_path, "s.json")), daemon=True
        )
        reader.start()
        reader.join(timeout=30)
        if reader.is_alive():
            # An open() waiting for a writer: give it one so the thread ends, then fail.
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            reader.join(timeout=30)
            pytest.fail("reading the status file blocked on a FIFO with no writer")
        assert got == ["Status file s.json could not be read safely"]


class TestARestartKeepsTheDeclarationAndTheConversation:
    def test_runs_json_carries_the_status_file_and_the_origin_session_key(
        self, tmp_path: Path
    ) -> None:
        runner = TaskRunner(sessions=_sessions(lambda _t: None), work_dir=tmp_path)
        run = Project(
            spec_path=str(tmp_path / "standup.yaml"),
            spec_content=_SPEC,
            tasks=decompose_yaml(_SPEC),
            status="running",
            task_id="standup_1",
            work_dir=str(tmp_path),
        )
        runner._runs[run.task_id] = run
        runner._run_session_keys[run.task_id] = "dashboard:chat-1"
        runner._persist_runs()

        restarted = TaskRunner(sessions=_sessions(lambda _t: None), work_dir=tmp_path)

        again = restarted._runs["standup_1"]
        assert again.status == "paused"  # crash recovery, as before
        assert [t.status_file for t in again.tasks] == ["out/taskei.json", ""]
        assert restarted._run_session_keys == {"standup_1": "dashboard:chat-1"}

    def test_a_run_with_no_conversation_restores_none(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_sessions(lambda _t: None), work_dir=tmp_path)
        runner._runs["r1"] = Project(
            spec_path="",
            spec_content="s",
            tasks=[Task(index=1, title="x", description="d")],
            status="completed",
            task_id="r1",
        )
        runner._persist_runs()

        restarted = TaskRunner(sessions=_sessions(lambda _t: None), work_dir=tmp_path)
        assert restarted._run_session_keys == {}

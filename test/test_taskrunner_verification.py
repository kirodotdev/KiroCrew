"""TaskRunner verification is its own outcome, separate from execution.

A step's ``verification`` is ``passed``, ``failed``, ``skipped`` or
``not_configured``, and only ``passed`` counts as verified: the operator's
``taskrunner.test_command`` ran and exited 0. A command that could not start,
zero collected tests, a timeout, a cancellation or a truncated failing log
never reaches ``passed``. ``taskrunner.require_verification`` turns every other
verdict into a failed step, and blocks a run that has no command to run.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import unittest.mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import task_executor
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.task_models import MAX_RETRIES
from kiro_crew.task_reporter import build_status, format_completion_summary
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner


@pytest.fixture(autouse=True)
def _unwrapped_spawn(_floor_monkeypatch):
    """Run the test command unwrapped: these cases pin verdicts, not sandbox availability."""

    async def _plain(argv, **_kw):
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
        return list(argv), env, None

    _floor_monkeypatch.setattr(task_executor, "sandboxed_spawn_argv_async", _plain)


def _event(kind: str, text: str = ""):
    from kiro_crew.providers.base import LLMEvent

    return LLMEvent(kind=kind, text=text)


def _sessions(on_turn=None) -> MagicMock:
    """Mock sessions whose agent turn calls *on_turn* (the step's edit) and says done."""
    provider = MagicMock()

    async def _stream(msg: str):
        if on_turn is not None:
            on_turn()
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


def _run(tmp_path: Path, *steps: Step) -> TaskRun:
    return TaskRun(
        spec_path=str(tmp_path / "t.md"),
        spec_content="s",
        status="running",
        tasks=list(steps) or [Step(index=1, title="Impl", description="d")],
    )


def _checker(tmp_path: Path, body: str) -> list[str]:
    """An operator test command: a script that logs each run to ran.log, then *body*."""
    script = tmp_path / "check.py"
    script.write_text(
        "import pathlib, sys\n"
        "with open('ran.log', 'a', encoding='utf-8') as fh:\n"
        "    fh.write('x')\n" + body,
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _runs_of(tmp_path: Path) -> int:
    log = tmp_path / "ran.log"
    return len(log.read_text(encoding="utf-8")) if log.exists() else 0


def _spy_run_tests(monkeypatch) -> list:
    """Record what the REAL run_tests returned on each call."""
    seen: list = []
    real = task_executor.run_tests

    async def _spy(test_cmd, work_dir):
        result = await real(test_cmd, work_dir)
        seen.append(result)
        return result

    monkeypatch.setattr(task_executor, "run_tests", _spy)
    return seen


_NEEDS_GOOD_IMPL = (
    "if pathlib.Path('impl.txt').read_text(encoding='utf-8') != 'good':\n"
    "    print('AssertionError: impl.txt is not good')\n"
    "    sys.exit(1)\n"
)

# A sandbox wrapper (here env) runs ahead of the program only on POSIX.
_MISSING_PROGRAMS = ["absolute path", "relative path"] + (
    ["name behind env"] if os.name == "posix" else []
)


class TestWiring:
    """The operator's persisted command reaches the step and really runs."""

    def test_config_json_carries_the_test_command_and_policy(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "config.json"
        argv = ["python", "-m", "pytest", "-q"]
        cfg_file.write_text(
            json.dumps({"taskrunner": {"test_command": argv, "require_verification": True}}),
            encoding="utf-8",
        )
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            unittest.mock.patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.taskrunner.test_command == argv
        assert cfg.taskrunner.require_verification is True

    @pytest.mark.parametrize("raw", ["pytest -q", ["pytest", 3], ["", "-q"], {"argv": "x"}])
    def test_a_malformed_test_command_is_not_configured(self, tmp_path: Path, raw) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"taskrunner": {"test_command": raw}}), encoding="utf-8")
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            unittest.mock.patch(
                "kiro_crew.config.loader.config_local_path",
                return_value=tmp_path / "config.local.json",
            ),
        ):
            cfg = KiroCrewConfig.load()
        assert cfg.taskrunner.test_command == []

    @pytest.mark.asyncio
    async def test_a_configured_command_really_runs(self, tmp_path: Path) -> None:
        runner = TaskRunner(
            sessions=_sessions(),
            auto_test=True,
            work_dir=tmp_path,
            test_cmd=_checker(tmp_path, "sys.exit(0)\n"),
        )
        step = Step(index=1, title="Impl", description="d")
        assert await runner._execute_single_task(_run(tmp_path, step), step)
        assert _runs_of(tmp_path) == 1
        assert step.status == StepStatus.PASSED
        assert step.verification == "passed"


class TestRealVerdicts:
    """A real command's exit status is the verdict; a failing one is retried, bounded."""

    @pytest.mark.asyncio
    async def test_a_deliberate_defect_fails_verification(self, tmp_path: Path) -> None:
        runner = TaskRunner(
            sessions=_sessions(lambda: (tmp_path / "impl.txt").write_text("bad", encoding="utf-8")),
            auto_test=True,
            work_dir=tmp_path,
        )
        runner._test_cmd = _checker(tmp_path, _NEEDS_GOOD_IMPL)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.status == StepStatus.FAILED
        assert step.verification == "failed"
        assert _runs_of(tmp_path) == MAX_RETRIES

    @pytest.mark.asyncio
    async def test_a_genuine_repair_passes_the_unchanged_command(self, tmp_path: Path) -> None:
        turns = {"n": 0}

        def _edit() -> None:
            turns["n"] += 1
            impl = "bad" if turns["n"] == 1 else "good"
            (tmp_path / "impl.txt").write_text(impl, encoding="utf-8")

        runner = TaskRunner(sessions=_sessions(_edit), auto_test=True, work_dir=tmp_path)
        runner._test_cmd = _checker(tmp_path, _NEEDS_GOOD_IMPL)
        step = Step(index=1, title="Impl", description="d")
        assert await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "passed"
        assert step.attempts == 2
        assert _runs_of(tmp_path) == 2

    @pytest.mark.asyncio
    async def test_zero_collected_tests_are_not_passed(self, tmp_path: Path, monkeypatch) -> None:
        ini = tmp_path / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        runner = TaskRunner(sessions=_sessions(), auto_test=True, work_dir=tmp_path)
        runner._test_cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(ini),
            "--rootdir",
            str(tmp_path),
        ]
        seen = _spy_run_tests(monkeypatch)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "failed"
        assert seen[0][0] is False and "no tests ran" in seen[0][1]

    @pytest.mark.asyncio
    async def test_a_truncated_failing_log_is_not_passed(self, tmp_path: Path, monkeypatch) -> None:
        body = "print('1 failed'); print('x' * 5000); print('1 passed'); sys.exit(1)\n"
        runner = TaskRunner(sessions=_sessions(), auto_test=True, work_dir=tmp_path)
        runner._test_cmd = _checker(tmp_path, body)
        seen = _spy_run_tests(monkeypatch)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "failed"
        # The kept tail claims "1 passed" and the failure line was cut; the exit status decided.
        assert "1 passed" in seen[0][1] and "1 failed" not in seen[0][1]

    @pytest.mark.asyncio
    async def test_a_missing_executable_reports_no_success(self, tmp_path) -> None:
        # The same verdict on every platform, with the spawn shim in place on POSIX.
        ok, output = await task_executor.run_tests([str(tmp_path / "no-such-test-tool")], tmp_path)
        assert ok is None
        assert "not verified" in output

    @pytest.mark.asyncio
    async def test_a_spawn_that_raises_not_found_reports_no_success(
        self, tmp_path, monkeypatch
    ) -> None:
        # Windows has no spawn shim: its CreateProcess raises for a missing program.
        spawn = AsyncMock(side_effect=FileNotFoundError(2, "No such file or directory", "t"))
        monkeypatch.setattr(task_executor, "create_subprocess_limited", spawn)
        ok, output = await task_executor.run_tests([sys.executable, "-V"], tmp_path)
        assert ok is None
        assert "not verified" in output
        spawn.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["missing", "a file"])
    async def test_a_work_dir_that_is_missing_or_a_file_is_never_a_pass(
        self, tmp_path: Path, kind
    ) -> None:
        work_dir = tmp_path / "gone"
        if kind == "a file":
            work_dir.write_text("", encoding="utf-8")
        ok, output = await task_executor.run_tests([sys.executable, "-V"], work_dir)
        assert ok is None
        assert "not verified" in output

    @pytest.mark.asyncio
    @pytest.mark.parametrize("required", [False, True], ids=["optional", "required"])
    @pytest.mark.parametrize("program", _MISSING_PROGRAMS)
    async def test_a_missing_program_is_skipped_after_one_agent_turn(
        self, tmp_path, monkeypatch, program, required
    ) -> None:
        # On POSIX the shim, or a sandbox wrapper such as env, execs the program
        # in the child, where a missing one is only an exit status (the shim's 127).
        argv0 = {
            "absolute path": str(tmp_path / "no-such-test-tool"),
            "relative path": "bin/no-such-test-tool",
            "name behind env": "no-such-test-tool",
        }[program]
        if program == "name behind env":
            env_bin = shutil.which("env")
            assert env_bin is not None

            async def _wrapped(argv, **_kw):
                env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
                return [env_bin, *argv], env, None

            monkeypatch.setattr(task_executor, "sandboxed_spawn_argv_async", _wrapped)
        # Count the step's own turns; a step that passes also gets a review turn.
        monkeypatch.setattr(task_executor, "self_review", AsyncMock(return_value=True))
        turns: list[str] = []
        runner = TaskRunner(
            sessions=_sessions(lambda: turns.append("turn")),
            auto_test=True,
            work_dir=tmp_path,
            test_cmd=[argv0, "-q"],
            require_verification=required,
        )
        step = Step(index=1, title="Impl", description="d")
        ok = await runner._execute_single_task(_run(tmp_path, step), step)
        assert (turns, step.attempts) == (["turn"], 1)
        assert step.verification == "skipped"
        assert ok == (not required)
        assert step.status == (StepStatus.FAILED if required else StepStatus.PASSED)

    @pytest.mark.parametrize("program", ["check-tool", "bin/check-tool"], ids=["name", "path"])
    def test_a_program_the_child_would_find_passes_the_check(self, tmp_path, program) -> None:
        # Resolved as the spawn resolves it: a relative PATH entry and a relative
        # path both from the work directory, not the gateway's cwd.
        tool = tmp_path / "bin" / "check-tool"
        tool.parent.mkdir()
        tool.write_text("", encoding="utf-8")
        tool.chmod(0o755)
        env = {"PATH": "bin"}
        assert task_executor._test_command_start_problem([program], tmp_path, env) == ""


def _stub_runner(tmp_path: Path, result, *, auto_test=True, cmd=("t",), required=False):
    runner = TaskRunner(sessions=_sessions(), auto_test=auto_test, work_dir=tmp_path)
    runner._test_cmd = list(cmd) or None
    runner._require_verification = required

    async def _run_tests(test_cmd, work_dir):
        if isinstance(result, BaseException):
            raise result
        return result

    return runner, _run_tests


class TestVerdicts:
    """Each way a command can end maps to one verdict; only exit 0 is passed."""

    @pytest.mark.asyncio
    async def test_exit_zero_is_passed(self, tmp_path: Path, monkeypatch) -> None:
        runner, fake = _stub_runner(tmp_path, (True, "1 passed"))
        monkeypatch.setattr(task_executor, "run_tests", fake)
        step = Step(index=1, title="Impl", description="d")
        assert await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "passed"

    @pytest.mark.asyncio
    async def test_a_command_that_could_not_start_is_skipped(self, tmp_path, monkeypatch) -> None:
        runner, fake = _stub_runner(tmp_path, (None, "test command could not start"))
        monkeypatch.setattr(task_executor, "run_tests", fake)
        step = Step(index=1, title="Impl", description="d")
        assert await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.status == StepStatus.PASSED  # optional: today's behavior
        assert step.verification == "skipped"

    @pytest.mark.asyncio
    async def test_a_timeout_is_failed(self, tmp_path: Path, monkeypatch) -> None:
        runner, fake = _stub_runner(tmp_path, (False, "Test timed out after 5400s"))
        monkeypatch.setattr(task_executor, "run_tests", fake)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "failed"

    @pytest.mark.asyncio
    async def test_no_command_is_not_configured(self, tmp_path: Path) -> None:
        runner, _ = _stub_runner(tmp_path, None, cmd=())
        step = Step(index=1, title="Impl", description="d")
        assert await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "not_configured"

    @pytest.mark.asyncio
    async def test_no_test_flag_reports_skipped(self, tmp_path: Path, monkeypatch) -> None:
        runner, fake = _stub_runner(tmp_path, (True, "never run"), auto_test=False)
        monkeypatch.setattr(task_executor, "run_tests", AsyncMock(side_effect=fake))
        step = Step(index=1, title="Impl", description="d")
        assert await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "skipped"
        task_executor.run_tests.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancelled_run_keeps_no_earlier_verdict(self, tmp_path, monkeypatch) -> None:
        runner, fake = _stub_runner(tmp_path, asyncio.CancelledError())
        monkeypatch.setattr(task_executor, "run_tests", fake)
        step = Step(index=1, title="Impl", description="d")
        step.verification = "passed"  # an earlier attempt's verdict
        with pytest.raises(asyncio.CancelledError):
            await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == ""


class TestRequiredVerification:
    """Required verification accepts only passed, and blocks a run with nothing to run."""

    @pytest.mark.asyncio
    async def test_a_command_that_could_not_start_fails_the_step(
        self, tmp_path, monkeypatch
    ) -> None:
        runner, fake = _stub_runner(tmp_path, (None, "could not start"), required=True)
        monkeypatch.setattr(task_executor, "run_tests", fake)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.status == StepStatus.FAILED
        assert step.verification == "skipped"
        assert "required" in step.error
        assert step.attempts == 1  # no agent retry can make the verifier start

    @pytest.mark.asyncio
    async def test_no_command_fails_the_step(self, tmp_path: Path) -> None:
        runner, _ = _stub_runner(tmp_path, None, cmd=(), required=True)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "not_configured"
        assert "required" in step.error

    @pytest.mark.asyncio
    async def test_no_runnable_command_blocks_the_run_before_any_step(self, tmp_path) -> None:
        runner, _ = _stub_runner(tmp_path, None, cmd=(), required=True)
        runner._execute_single_task = AsyncMock(side_effect=AssertionError("no step may run"))
        runner._notify = AsyncMock()
        run = _run(tmp_path, Step(index=1, title="A", description="d"))
        await runner._execute_tasks(run, "hk")
        assert run.status == "failed"
        assert "taskrunner.require_verification" in run.error
        assert run.tasks[0].status == StepStatus.PENDING
        runner._notify.assert_awaited_once()
        assert runner._notify.await_args.args[1] == run.error


class TestRecordAndReport:
    """The verdict survives a restart, and the summary counts only passed as verified."""

    def test_the_verdict_survives_a_restart(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=MagicMock(), auto_test=True, work_dir=tmp_path)
        step = Step(index=0, title="step one", description="do it", status=StepStatus.PASSED)
        step.verification = "not_configured"
        run = TaskRun(
            spec_path="/tmp/TASK.md",
            spec_content="# demo",
            task_id="t1",
            name="demo run",
            status="paused",
            source="text",
            work_dir="/tmp",
            tasks=[step],
        )
        runner._runs[run.task_id] = run
        runner._persist_runs()
        reloaded = TaskRunner(sessions=MagicMock(), auto_test=True, work_dir=tmp_path)
        reloaded._load_runs()
        assert reloaded._runs["t1"].tasks[0].verification == "not_configured"

    def test_the_summary_counts_only_passed_as_verified(self, tmp_path: Path) -> None:
        steps = [
            Step(index=i, title=f"S{i}", description="d", status=StepStatus.PASSED)
            for i in (1, 2, 3)
        ]
        for s, verdict in zip(steps, ("passed", "skipped", "not_configured")):
            s.verification = verdict
        run = _run(tmp_path, *steps)
        run.started_at = 1.0
        summary = format_completion_summary(run)
        assert "1/3 verified" in summary
        assert "1 skipped" in summary and "1 not configured" in summary


class TestARequeuedStepKeepsNoEarlierVerdict:
    """A step set back to pending has no verdict for its next run, on every reset path."""

    @staticmethod
    def _failed_run(tmp_path: Path) -> tuple[TaskRunner, TaskRun]:
        sessions = _sessions()
        sessions.admission_closed = False
        runner = TaskRunner(sessions=sessions, auto_test=True, work_dir=tmp_path)
        steps = [
            Step(index=1, title="kept", description="d", status=StepStatus.PASSED),
            Step(index=2, title="redo", description="d", status=StepStatus.FAILED),
            Step(index=3, title="was green", description="d", status=StepStatus.PASSED),
        ]
        for step, verdict in zip(steps, ("passed", "failed", "passed")):
            step.verification = verdict
        run = TaskRun(
            spec_path=str(tmp_path / "t.md"),
            spec_content="s",
            status="failed",
            task_id="plan_1",
            name="demo",
            work_dir=str(tmp_path),
            tasks=steps,
        )
        runner._runs["plan_1"] = run
        return runner, run

    @pytest.mark.asyncio
    async def test_retry_from_task_clears_the_verdict_of_every_requeued_step(
        self, tmp_path
    ) -> None:
        runner, run = self._failed_run(tmp_path)
        with (
            unittest.mock.patch.object(TaskRunner, "_execute_tasks", AsyncMock()),
            unittest.mock.patch.object(TaskRunner, "_watchdog_loop", AsyncMock()),
        ):
            async with asyncio.timeout(30):
                started = await runner.retry_from_task("plan_1", 2)
                await runner._tasks[started]
        assert [s.verification for s in run.tasks] == ["passed", "", ""]
        persisted = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
        assert [t["verification"] for t in persisted[0]["task_details"]] == ["passed", "", ""]

    @pytest.mark.asyncio
    async def test_a_fresh_restart_clears_every_verdict(self, tmp_path) -> None:
        runner, run = self._failed_run(tmp_path)
        with (
            unittest.mock.patch.object(TaskRunner, "_execute_tasks", AsyncMock()),
            unittest.mock.patch.object(TaskRunner, "_watchdog_loop", AsyncMock()),
            unittest.mock.patch.object(
                TaskRunner, "_ensure_resumable_workspace", AsyncMock(return_value=True)
            ),
        ):
            async with asyncio.timeout(30):
                started = await runner.execute_plan("plan_1", fresh=True)
                await runner._tasks[started]
        assert [s.verification for s in run.tasks] == ["", "", ""]

    @pytest.mark.asyncio
    async def test_a_failed_save_restores_the_verdicts_with_the_rest(self, tmp_path) -> None:
        runner, run = self._failed_run(tmp_path)
        runner._apersist_runs = AsyncMock(side_effect=OSError("disk full"))
        with pytest.raises(OSError):
            async with asyncio.timeout(30):
                await runner.retry_from_task("plan_1", 2)
        assert [s.verification for s in run.tasks] == ["passed", "failed", "passed"]
        assert [s.status for s in run.tasks] == [
            StepStatus.PASSED,
            StepStatus.FAILED,
            StepStatus.PASSED,
        ]

    def test_a_pause_clears_the_verdict_of_the_step_it_requeues(self, tmp_path: Path) -> None:
        # A step on its second attempt still carries the first attempt's verdict.
        step = Step(index=1, title="A", description="d", status=StepStatus.IN_PROGRESS)
        step.verification = "failed"
        run = _run(tmp_path, step)
        run.status = "pausing"
        TaskRunner._reset_incomplete_tasks(run)
        assert step.status == StepStatus.PENDING
        assert step.verification == ""

    def test_crash_recovery_clears_the_verdict_of_the_step_it_requeues(self, tmp_path) -> None:
        record = {
            "task_id": "r1",
            "name": "r1",
            "spec_path": "spec.md",
            "status": "running",
            "source": "text",
            "task_details": [
                {
                    "index": 1,
                    "title": "one",
                    "description": "d",
                    "status": "in_progress",
                    "attempts": 2,
                    "verification": "failed",
                }
            ],
        }
        (tmp_path / "runs.json").write_text(json.dumps([record]), encoding="utf-8")
        runner = TaskRunner(sessions=MagicMock(), auto_test=True, work_dir=tmp_path)
        step = runner._runs["r1"].tasks[0]
        assert step.status == StepStatus.PENDING
        assert step.verification == ""

    @pytest.mark.asyncio
    async def test_a_denied_approval_leaves_a_pending_step_without_a_verdict(
        self, tmp_path
    ) -> None:
        runner = TaskRunner(
            sessions=_sessions(),
            auto_test=True,
            work_dir=tmp_path,
            on_approval=AsyncMock(return_value=False),
        )
        step = Step(index=1, title="A", description="d", requires_approval=True)
        step.verification = "failed"
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.status == StepStatus.PENDING
        assert step.verification == ""

    @pytest.mark.parametrize("status", [StepStatus.PENDING, StepStatus.IN_PROGRESS])
    def test_setting_a_step_pending_or_running_clears_its_verdict(self, status) -> None:
        # The one rule behind every reset path (retry, resume, restart, pause,
        # crash recovery, approval denial) and every attempt start.
        step = Step(index=1, title="A", description="d", status=StepStatus.FAILED)
        step.verification = "failed"
        step.status = status
        assert step.verification == ""

    @pytest.mark.parametrize("status", [StepStatus.PASSED, StepStatus.FAILED, StepStatus.REVIEWING])
    def test_any_other_status_keeps_the_verdict(self, status) -> None:
        step = Step(index=1, title="A", description="d", status=StepStatus.IN_PROGRESS)
        step.verification = "passed"
        step.status = status
        assert step.verification == "passed"

    def test_a_loaded_record_keeps_its_stored_verdict(self) -> None:
        # The rule runs on assignment after construction, not while a record loads.
        step = Step(
            index=1, title="A", description="d", status=StepStatus.FAILED, verification="failed"
        )
        assert step.verification == "failed"

    @pytest.mark.asyncio
    async def test_a_retry_attempt_starts_without_the_previous_attempts_verdict(
        self, tmp_path, monkeypatch
    ) -> None:
        step = Step(index=1, title="Impl", description="d")
        seen: list[str] = []
        runner, fake = _stub_runner(tmp_path, (False, "boom"))
        runner._sessions = _sessions(lambda: seen.append(step.verification))
        monkeypatch.setattr(task_executor, "run_tests", fake)
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert seen == [""] * MAX_RETRIES
        assert step.verification == "failed"


class TestClaimsTheOtherCasesLeaveOpen:
    def test_the_status_payload_carries_each_steps_verdict(self, tmp_path: Path) -> None:
        step = Step(index=1, title="S", description="d", status=StepStatus.PASSED)
        step.verification = "skipped"
        run = _run(tmp_path, step)
        rows = build_status({run.task_id: run}, {})["runs"][0]["task_details"]
        assert [row["verification"] for row in rows] == ["skipped"]

    @pytest.mark.asyncio
    async def test_required_with_tests_turned_off_blocks_the_run_before_any_step(
        self, tmp_path: Path
    ) -> None:
        runner, _ = _stub_runner(tmp_path, None, auto_test=False, cmd=("t",), required=True)
        runner._execute_single_task = AsyncMock(side_effect=AssertionError("no step may run"))
        runner._notify = AsyncMock()
        run = _run(tmp_path, Step(index=1, title="A", description="d"))
        await runner._execute_tasks(run, "hk")
        assert run.status == "failed"
        assert run.tasks[0].status == StepStatus.PENDING

    @pytest.mark.asyncio
    async def test_required_still_retries_a_failing_verdict_to_the_bound(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        runner, fake = _stub_runner(tmp_path, (False, "boom"), required=True)
        monkeypatch.setattr(task_executor, "run_tests", fake)
        step = Step(index=1, title="Impl", description="d")
        assert not await runner._execute_single_task(_run(tmp_path, step), step)
        assert step.verification == "failed"
        assert step.attempts == MAX_RETRIES

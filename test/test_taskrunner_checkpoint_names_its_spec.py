"""A task run resumes only from its own spec's progress file.

``save_progress`` writes ``TASK_PROGRESS.md`` beside the spec, so every spec in one
directory shares one progress file, and records which spec wrote it on its
``**Spec:**`` line. ``load_checkpoint`` must not hand one spec's completed steps to
another spec: the runner marks every task whose title matches the checkpoint as
PASSED, so a second spec in the same directory would skip a step it never ran.

The real ``TaskRunner.run`` is driven against a tmp_path home with the model calls
faked: ``_decompose`` returns a fixed plan and ``_execute_tasks`` records which tasks
it was asked to run. ``git_coord`` is faked, so no git command runs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from test_taskrunner_v2_scenarios import _make_provider, _mock_sessions

from kiro_crew.task_reporter import load_checkpoint
from kiro_crew.taskrunner import Step, StepStatus, TaskRunner


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    """Pin the data home and workspace to ``tmp_path`` through the isolation
    floor's own ``MonkeyPatch``: a test's ``monkeypatch.undo()`` cannot lift
    these pins, so no path here ever resolves to the operator's real home."""
    for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
        (tmp_path / sub).mkdir()
        _floor_monkeypatch.setenv(var, str(tmp_path / sub))


async def _run(tmp_path: Path, spec: Path, plan: list[Step], *, fresh: bool = False) -> list[str]:
    sessions = _mock_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_make_provider(), True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path, fresh=fresh)
    executed: list[str] = []

    async def _exec(run, history_key):  # type: ignore[no-untyped-def]
        for step in run.tasks:
            if step.status == StepStatus.PENDING:
                executed.append(step.title)
                step.status = StepStatus.PASSED

    async def _git_init(run):  # type: ignore[no-untyped-def]
        run.branch_name = ""

    with (
        patch.object(runner, "_decompose", return_value=plan),
        patch.object(runner, "_execute_tasks", side_effect=_exec),
        patch("kiro_crew.taskrunner.git_coord") as git,
    ):
        git.init_workspace = _git_init
        git.finalize = AsyncMock(return_value="")
        await runner.run(spec)
    return executed


def _plan(second: str) -> list[Step]:
    return [
        Step(index=1, title="Explore the codebase", description="read the code"),
        Step(index=2, title=second, description="build it"),
    ]


def _specs(tmp_path: Path) -> tuple[Path, Path]:
    specs = tmp_path / "specs"
    specs.mkdir()
    spec_a = specs / "feature-a.md"
    spec_a.write_text("# Feature A\nBuild the reports API.", encoding="utf-8")
    spec_b = specs / "feature-b.md"
    spec_b.write_text("# Feature B\nAdd the billing migration.", encoding="utf-8")
    return spec_a, spec_b


@pytest.mark.asyncio
async def test_a_second_spec_in_the_same_folder_runs_every_step(tmp_path):
    spec_a, spec_b = _specs(tmp_path)
    await _run(tmp_path, spec_a, _plan("Write the reports API"))
    ran_b = await _run(tmp_path, spec_b, _plan("Add the billing migration"))
    assert (
        "Explore the codebase" in ran_b
    ), f"feature-b's first step was skipped as done from feature-a's progress file: ran {ran_b}"


@pytest.mark.asyncio
async def test_control_the_same_spec_still_resumes_from_its_own_progress(tmp_path):
    spec_a, _ = _specs(tmp_path)
    await _run(tmp_path, spec_a, _plan("Write the reports API"))
    assert await _run(tmp_path, spec_a, _plan("Write the reports API")) == []


@pytest.mark.asyncio
async def test_control_fresh_still_ignores_the_checkpoint(tmp_path):
    spec_a, _ = _specs(tmp_path)
    await _run(tmp_path, spec_a, _plan("Write the reports API"))
    rerun = await _run(tmp_path, spec_a, _plan("Write the reports API"), fresh=True)
    assert rerun == ["Explore the codebase", "Write the reports API"]


def test_control_a_progress_file_without_a_spec_line_still_parses(tmp_path):
    spec = tmp_path / "old.md"
    (tmp_path / "TASK_PROGRESS.md").write_text(
        "# Task Progress\n\n## Tasks\n- ✅ **Step 1:** Old step title (attempts: 2)\n",
        encoding="utf-8",
    )
    assert load_checkpoint(spec) == {"old step title"}


def test_a_progress_file_naming_another_spec_is_no_checkpoint(tmp_path):
    (tmp_path / "TASK_PROGRESS.md").write_text(
        "# Task Progress\n**Spec:** `other.md`\n\n## Tasks\n- ✅ **Task 1:** Explore\n",
        encoding="utf-8",
    )
    assert load_checkpoint(tmp_path / "mine.md") is None
    assert load_checkpoint(tmp_path / "other.md") == {"explore"}

"""A cron project_path that could never fire is refused at save, not at fire.

``_validate_project_path``'s first three gates are absolute / non-sensitive /
existing-directory. A directory that CONTAINS the Kiro Crew data home passes all
three — ``is_sensitive_path`` answers the *inside* direction, so an ancestor of
the data home is not itself sensitive — so the job saved, and then every fire
raised from the fail-closed macOS voice-runtime spawn guard
(``assert_voice_runtime_outside_agent_workspace``), spending an auto-pause strike
each time until the job auto-paused. The four sibling directory-choice surfaces
(``chat_folders._folder_project_overlap_denied``, the chat project endpoint,
``set_project``, ``session_directive_apply``) already pre-flight this through the
same shared scan; cron did not.

Two halves, pulling opposite ways, and both are pinned here: the refusal must
fire on macOS, and it must NOT fire anywhere else — every spawn-time guard
early-returns off darwin, so refusing off darwin would delete a configuration
that works today to prevent a harm that cannot happen there.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew.cron import CronService


@pytest.fixture
def overlapping_project(tmp_path: Path, monkeypatch):
    """A project directory that lexically contains the voice runtime.

    The runtime paths are stubbed rather than resolved for real so the scan is
    genuinely exercised against a synthetic tree instead of the developer's own
    data home.
    """
    project = tmp_path / "proj"
    runtime = project / "data" / "run" / "voice-runtime"
    runtime.mkdir(parents=True)
    monkeypatch.setattr(sandbox_mod, "_voice_runtime_sandbox_paths", lambda: (str(runtime),))
    return project


def _svc(tmp_path: Path) -> CronService:
    svc = CronService(base_dir=tmp_path / "cron_home")
    svc._load()
    return svc


class TestADirectoryThatContainsTheDataHome:
    def test_is_refused_at_save_on_macos(self, tmp_path, overlapping_project, monkeypatch):
        # The pre-flight is darwin-gated to match the spawn-time guard it
        # mirrors, so pin the platform for the refusal path.
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        svc = _svc(tmp_path)
        with pytest.raises(ValueError, match="overlaps Kiro Crew's protected voice runtime"):
            svc.add_job(
                name="test",
                message="hello",
                every_secs=300,
                project_path=str(overlapping_project),
            )
        # A rejected create must not leave an orphaned job on disk, matching
        # the sibling nonexistent-path refusal.
        assert svc.list_jobs(include_disabled=True) == []

    def test_the_refusal_names_no_path(self, tmp_path, overlapping_project, monkeypatch):
        """The surface's contract is that a rejected project_path never rides
        back out in the body, so its safety does not depend on the owner gate's
        ordering. The shared guard message the four sibling pickers surface
        verbatim names BOTH absolute paths; this one names neither."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        svc = _svc(tmp_path)
        with pytest.raises(ValueError) as caught:
            svc.add_job(
                name="test",
                message="hello",
                every_secs=300,
                project_path=str(overlapping_project),
            )
        message = str(caught.value)
        assert str(overlapping_project) not in message
        assert "voice-runtime" not in message
        # It still ends on the same remedy the guard gives, so the two read alike.
        assert "Pick a project subdirectory that does not contain the Kiro Crew data home." in (
            message
        )

    def test_the_denial_is_audited(self, tmp_path, overlapping_project, monkeypatch):
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        svc = _svc(tmp_path)
        with patch("kiro_crew.cron.sel") as sel_mod:
            with pytest.raises(ValueError):
                svc.add_job(
                    name="test",
                    message="hello",
                    every_secs=300,
                    project_path=str(overlapping_project),
                )
        calls = [c for c in sel_mod.sel().log_api_access.call_args_list]
        assert calls, "the refusal must leave an audit record like the sensitive-path one"
        kwargs = calls[-1].kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["error"] == "voice runtime overlap"
        assert kwargs["resources"] == str(overlapping_project.resolve())

    def test_off_darwin_the_same_directory_still_saves(
        self, tmp_path, overlapping_project, monkeypatch
    ):
        """The opposite-pulling half: the spawn guard early-returns off darwin,
        so this configuration fires fine on Linux/Windows and must keep saving.
        A refusal here would remove a working setup with macOS-worded copy."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "linux")
        svc = _svc(tmp_path)
        job = svc.add_job(
            name="test",
            message="hello",
            every_secs=300,
            project_path=str(overlapping_project),
        )
        assert job.project_path == str(overlapping_project.resolve())

    def test_a_sibling_directory_still_saves_on_macos(self, tmp_path, monkeypatch):
        """The refusal is scoped to an actual overlap, not to project binding."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        runtime = tmp_path / "home" / "crew" / "run" / "voice-runtime"
        runtime.mkdir(parents=True)
        monkeypatch.setattr(sandbox_mod, "_voice_runtime_sandbox_paths", lambda: (str(runtime),))
        project = tmp_path / "elsewhere"
        project.mkdir()
        svc = _svc(tmp_path)
        job = svc.add_job(
            name="test",
            message="hello",
            every_secs=300,
            project_path=str(project),
        )
        assert job.project_path == str(project.resolve())


class TestTheAsyncValidatorInheritsIt:
    @pytest.mark.asyncio
    async def test_the_off_loop_path_refuses_the_same_directory(
        self, tmp_path, overlapping_project, monkeypatch
    ):
        """The check lives in the SYNC validator, which the async one wraps in
        ``asyncio.to_thread`` — so the loop-side create surfaces inherit it with
        no blocking call on the loop, and the sync locked create chokepoint
        (``_build_job``) is covered by the same edit rather than diverging."""
        monkeypatch.setattr(sandbox_mod.sys, "platform", "darwin")
        svc = _svc(tmp_path)
        with pytest.raises(ValueError, match="overlaps Kiro Crew's protected voice runtime"):
            await svc.add_job_async(
                name="test",
                message="hello",
                every_secs=300,
                project_path=str(overlapping_project),
            )
        assert svc.list_jobs(include_disabled=True) == []

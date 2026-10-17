"""A gateway running from a git checkout is named at boot and guarded in the skill.

Two halves of one guard. ``kiro_crew.live_checkout`` logs a boot WARNING when the
gateway's package sits in a git work tree. The worktree-dev skill's
``live_checkout_guard.py`` refuses (exit 30) a HEAD-moving git command in the
checkout a live gateway runs from, and lets a linked worktree through.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from skill_script_helpers import load_skill_script

from kiro_crew import live_checkout

GUARD = (
    Path(__file__).resolve().parents[1]
    / "src/kiro_crew/builtin_skills/kirocrew-dev/kirocrew-worktree-dev/scripts/live_checkout_guard.py"
)


@pytest.fixture
def guard():
    return load_skill_script("live_checkout_guard_under_test", GUARD)


def _clone(tmp_path: Path) -> Path:
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "src" / "kiro_crew").mkdir(parents=True)
    return clone


def _worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../clone/.git/worktrees/wt\n")
    return wt


def _run_dir(tmp_path: Path, pid: int, launcher: str) -> Path:
    run = tmp_path / "home" / "run"
    run.mkdir(parents=True)
    (run / "gateway-5476.pid").write_text(f"{pid}\n")
    (run / "gateway-5476.bin").write_text(launcher)
    return run


class TestBootWarning:
    def test_a_source_checkout_that_is_a_work_tree_warns_with_the_root(self, tmp_path, caplog):
        clone = _clone(tmp_path)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.live_checkout"):
            root = live_checkout.warn_if_running_from_git_checkout(clone)
        assert root == clone
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert str(clone) in warnings[0].getMessage()

    def test_a_linked_worktree_counts_as_a_work_tree(self, tmp_path):
        wt = _worktree(tmp_path)
        assert live_checkout.warn_if_running_from_git_checkout(wt) == wt

    def test_an_installed_copy_does_not_warn(self, monkeypatch, caplog):
        monkeypatch.setattr(live_checkout, "_source_checkout_root", lambda: None)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.live_checkout"):
            assert live_checkout.warn_if_running_from_git_checkout() is None
        assert not caplog.records

    def test_a_source_tree_without_git_does_not_warn(self, tmp_path, caplog):
        plain = tmp_path / "plain"
        plain.mkdir()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.live_checkout"):
            assert live_checkout.warn_if_running_from_git_checkout(plain) is None
        assert not caplog.records


class TestSkillGuard:
    def test_the_live_gateways_checkout_is_refused(self, guard, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clone = _clone(tmp_path)
        run = _run_dir(tmp_path, os.getpid(), str(clone / ".venv/bin/kirocrew"))
        code, message = guard.check(clone / "src", run)
        assert code == guard.EXIT_REFUSED == 30
        assert "worktree add" in message
        assert str(clone) in message

    def test_a_linked_worktree_of_it_is_allowed(self, guard, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clone = _clone(tmp_path)
        wt = _worktree(tmp_path)
        run = _run_dir(tmp_path, os.getpid(), str(clone / ".venv/bin/kirocrew"))
        code, _ = guard.check(wt, run)
        assert code == guard.EXIT_SAFE

    @pytest.mark.skipif(
        not Path("/proc").is_dir(), reason="without /proc a recorded pid counts as live"
    )
    def test_a_dead_gateways_record_is_ignored(self, guard, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        clone = _clone(tmp_path)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        run = _run_dir(tmp_path, proc.pid, str(clone / ".venv/bin/kirocrew"))
        code, _ = guard.check(clone, run)
        assert code == guard.EXIT_SAFE

    @pytest.mark.skipif(not Path("/proc/self/cwd").exists(), reason="needs /proc/<pid>/cwd")
    def test_the_gateways_working_directory_is_also_a_source(self, guard, tmp_path, monkeypatch):
        clone = _clone(tmp_path)
        monkeypatch.chdir(clone)
        run = _run_dir(tmp_path, os.getpid(), "")
        code, _ = guard.check(clone, run)
        assert code == guard.EXIT_REFUSED

    def test_no_run_directory_is_safe(self, guard, tmp_path):
        code, _ = guard.check(_clone(tmp_path), tmp_path / "missing")
        assert code == guard.EXIT_SAFE

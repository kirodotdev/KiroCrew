"""The auto-improvement spine :class:`Gate`: the edit-allowlist fence and RED-base staging.

The allowlist is a hard fence: a git error must reject the candidate rather than read
as an empty diff, and a rename must report its old path as well as its new one, so an
off-limits file cannot be moved past the check. The bug track's RED base is the base
source plus only the candidate's new test. It must revert the source fix, drop a
linked worktree's copied gitlink file, and be removed after the gate runs. (Copying
symlinks as links is pinned by ``test_dogfood_learnings_clone.py``.)

Every test runs against a real throwaway git repo in ``tmp_path``. The profile's
collaborators (allowlist, build gate, bug runner) and the RED/GREEN ``BugGate`` are
fakes, so no build or test command runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import gate as gate_mod
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import (
    BUG_ERROR,
    BUG_FAILED_BUILD,
    BUG_FILED,
    BugGateResult,
    BugReproducingTest,
    Candidate,
    GateResult,
    Proposal,
)
from kiro_crew.apps.builtins.auto_improvement.spine.gate import Gate, self_clone_src
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_SRC_BASE = "def f():\n    return 1  # bug\n"
_SRC_FIX = "def f():\n    return 2\n"


@pytest.fixture(autouse=True)
def _isolated_git(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """No host git config, and a fixed identity so the gate's snapshot commit works.

    Patched through the isolation floor's own ``MonkeyPatch``, not the shared one, so a
    test body's ``monkeypatch.undo()`` cannot drop these pins.
    """
    _floor_monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    _floor_monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    _floor_monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        _floor_monkeypatch.setenv(f"{key}_NAME", "T")
        _floor_monkeypatch.setenv(f"{key}_EMAIL", "t@example.com")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, **UTF8_TEXT
    ).stdout.strip()


@pytest.fixture()
def worktree(tmp_path: Path) -> tuple[Path, str]:
    """A candidate worktree at its base commit: ``src/pkg.py`` (buggy) and one old test."""
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "test").mkdir()
    (wt / "src" / "pkg.py").write_text(_SRC_BASE)
    (wt / "test" / "test_old.py").write_text("def test_old():\n    pass\n")
    _git(wt, "init", "-q", "-b", "main")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    return wt, _git(wt, "rev-parse", "HEAD")


def _apply_fix(wt: Path) -> None:
    """The candidate's diff: fix the source, add a new source file and a reproducing test."""
    (wt / "src" / "pkg.py").write_text(_SRC_FIX)
    (wt / "src" / "helper.py").write_text("X = 1\n")
    (wt / "test" / "test_bug_f.py").write_text("def test_f():\n    assert True\n")


def _proposal(wt: Path) -> Proposal:
    candidate = Candidate(
        kind="bug",
        target="src/pkg.py::f",
        reproducing_test=BugReproducingTest(test_id="test/test_bug_f.py::test_f"),
    )
    return Proposal(
        cand_id="c1", candidate=candidate, worktree=wt, branch="cand/c1", description="d"
    )


class _Allows:
    """A plain name-only allowlist: rejects anything under ``forbidden``."""

    def __init__(self, forbidden: str = "nope/") -> None:
        self.forbidden = forbidden
        self.seen: list[list[str]] = []

    def allows(self, paths: list[str]) -> tuple[bool, list[str]]:
        self.seen.append(list(paths))
        bad = [p for p in paths if p.startswith(self.forbidden)]
        return not bad, bad


class _AllowsChanges(_Allows):
    """A status-aware allowlist: may add a test, never modify an existing one."""

    def __init__(self) -> None:
        super().__init__()
        self.changes: list[list[tuple[str, str]]] = []

    def allows_changes(self, status_paths: list[tuple[str, str]]) -> tuple[bool, list[str]]:
        self.changes.append(list(status_paths))
        bad = [p for s, p in status_paths if p.startswith("test/") and s != "A"]
        return not bad, bad


def _profile(allowlist: Any, **extra: Any) -> Any:
    return SimpleNamespace(id="p1", edit_allowlist=allowlist, **extra)


class TestAllowlistFence:
    def test_a_git_error_rejects_instead_of_passing_an_empty_diff(self, worktree) -> None:
        wt, _base = worktree
        allowlist = _Allows()
        res = Gate().check_allowlist(
            profile=_profile(allowlist), proposal=_proposal(wt), base_sha="0" * 40
        )
        assert res.passed is False
        assert res.detail.startswith("allowlist check failed (git error): git diff failed")
        assert allowlist.seen == []  # nothing was inspected, so nothing may pass

    def test_untracked_new_files_are_inspected(self, worktree) -> None:
        wt, base = worktree
        (wt / "nope").mkdir()
        (wt / "nope" / "secret.py").write_text("x = 1\n")
        allowlist = _Allows()
        res = Gate().check_allowlist(
            profile=_profile(allowlist), proposal=_proposal(wt), base_sha=base
        )
        assert (res.passed, res.detail) == (False, "off-limits paths: nope/secret.py")
        assert allowlist.seen == [["nope/secret.py"]]

    def test_a_rename_reports_the_old_path_too(self, worktree) -> None:
        # Moving an off-limits file into an allowed place must not hide where it came from.
        wt, base = worktree
        (wt / "nope").mkdir()
        (wt / "nope" / "guarded.py").write_text("GUARDED = True\n" * 5)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "guarded")
        base = _git(wt, "rev-parse", "HEAD")
        _git(wt, "mv", "nope/guarded.py", "src/guarded.py")
        allowlist = _Allows()
        res = Gate().check_allowlist(
            profile=_profile(allowlist), proposal=_proposal(wt), base_sha=base
        )
        assert allowlist.seen == [["nope/guarded.py", "src/guarded.py"]]
        assert (res.passed, res.detail) == (False, "off-limits paths: nope/guarded.py")

    def test_a_status_aware_allowlist_tells_an_added_test_from_an_edited_one(
        self, worktree
    ) -> None:
        wt, base = worktree
        _apply_fix(wt)
        (wt / "test" / "test_old.py").write_text("def test_old():\n    assert False\n")
        allowlist = _AllowsChanges()
        res = Gate().check_allowlist(
            profile=_profile(allowlist), proposal=_proposal(wt), base_sha=base
        )
        assert sorted(allowlist.changes[0]) == [
            ("A", "src/helper.py"),
            ("A", "test/test_bug_f.py"),
            ("M", "src/pkg.py"),
            ("M", "test/test_old.py"),
        ]
        assert allowlist.seen == []  # the name-only fallback is not consulted
        assert (res.passed, res.detail) == (False, "off-limits paths: test/test_old.py")

    def test_an_allowed_diff_passes(self, worktree) -> None:
        wt, base = worktree
        _apply_fix(wt)
        res = Gate().check_allowlist(
            profile=_profile(_AllowsChanges()), proposal=_proposal(wt), base_sha=base
        )
        assert (res.passed, res.detail) == (True, "allowlist ok")


class _BuildGate:
    def __init__(self, *, passed: bool = True) -> None:
        self.passed = passed
        self.calls: list[tuple[Path, Path]] = []

    def build_and_test(self, *, worktree: Path, src: Path) -> GateResult:
        self.calls.append((worktree, src))
        return GateResult(passed=self.passed, commit_sha="from-build", detail="built")


class TestPerfRun:
    def test_an_off_limits_edit_never_reaches_the_build(self, worktree) -> None:
        wt, base = worktree
        (wt / "nope").mkdir()
        (wt / "nope" / "x.py").write_text("x = 1\n")
        build = _BuildGate()
        res = Gate().run(
            profile=_profile(_Allows(), build_gate=build), proposal=_proposal(wt), base_sha=base
        )
        assert res.passed is False
        assert build.calls == []

    def test_the_edit_is_snapshotted_and_its_sha_carried_into_the_result(self, worktree) -> None:
        wt, base = worktree
        _apply_fix(wt)
        build = _BuildGate()
        res = Gate().run(
            profile=_profile(_Allows(), build_gate=build), proposal=_proposal(wt), base_sha=base
        )
        head = _git(wt, "rev-parse", "HEAD")
        assert head != base
        assert _git(wt, "status", "--porcelain") == ""  # the edit is committed
        assert (res.passed, res.commit_sha) == (True, head)
        assert build.calls == [(wt, wt / "src")]

    def test_a_failed_snapshot_commit_keeps_the_build_sha(self, worktree) -> None:
        wt, base = worktree
        _apply_fix(wt)
        # A held index lock makes `git add` and `git commit` fail the same way on every OS.
        (wt / ".git" / "index.lock").write_text("")
        res = Gate().run(
            profile=_profile(_Allows(), build_gate=_BuildGate()),
            proposal=_proposal(wt),
            base_sha=base,
        )
        assert _git(wt, "rev-parse", "HEAD") == base  # no snapshot was made
        assert res.commit_sha == "from-build"


class _FakeBugGate:
    """Records what the gate hands the RED/GREEN check, including the staged base tree."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.base_files: dict[str, str] = {}
        self.base_has_git = False

    def run(self, **kwargs: Any) -> BugGateResult:
        self.kwargs = kwargs
        root = Path(kwargs["base_src"]).parent
        self.base_has_git = os.path.lexists(root / ".git")
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root).as_posix()
            if rel == ".git" or rel.startswith(".git/"):
                continue  # the fallback hands over the worktree itself, git dir and all
            if p.is_file():
                self.base_files[rel] = p.read_text()
        return BugGateResult(passed=True, reason=BUG_FILED)


class TestBugRun:
    def test_an_off_limits_bug_fix_is_a_failed_gate(self, worktree) -> None:
        wt, base = worktree
        (wt / "nope").mkdir()
        (wt / "nope" / "x.py").write_text("x = 1\n")
        fake = _FakeBugGate()
        res = Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows(), bug_runner=object()), proposal=_proposal(wt), base_sha=base
        )
        assert (res.passed, res.reason, res.build_ok) == (False, BUG_FAILED_BUILD, False)
        assert res.detail == "off-limits: off-limits paths: nope/x.py"
        assert fake.kwargs == {}

    def test_a_profile_without_a_bug_runner_cannot_drive_the_bug_track(self, worktree) -> None:
        wt, base = worktree
        _apply_fix(wt)
        fake = _FakeBugGate()
        res = Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows()), proposal=_proposal(wt), base_sha=base
        )
        assert (res.passed, res.reason) == (False, BUG_ERROR)
        assert "profile p1 has no bug_runner" in res.detail
        assert fake.kwargs == {}

    def test_the_red_base_keeps_the_new_test_and_reverts_the_fix(self, worktree) -> None:
        wt, base = worktree
        _apply_fix(wt)
        runner = object()
        fake = _FakeBugGate()
        proposal = _proposal(wt)
        res = Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows(), bug_runner=runner), proposal=proposal, base_sha=base
        )
        redbase = wt.parent / "wt__redbase"
        assert res.reason == BUG_FILED
        assert fake.kwargs["runner"] is runner
        assert fake.kwargs["candidate"] is proposal.candidate
        assert fake.kwargs["cand_src"] == wt / "src"
        assert fake.kwargs["base_src"] == redbase / "src"
        assert fake.base_files["src/pkg.py"] == _SRC_BASE  # the bug is back
        assert "src/helper.py" not in fake.base_files  # a file the fix added is gone
        assert "test_f" in fake.base_files["test/test_bug_f.py"]  # the new test stays
        assert fake.base_has_git is False  # git cannot reach the candidate's index
        # The staged tree is removed afterwards; the candidate worktree is untouched.
        assert not redbase.exists()
        assert (wt / "src" / "pkg.py").read_text() == _SRC_FIX

    def test_a_linked_worktree_red_base_drops_its_gitlink_file(self, worktree) -> None:
        # A linked worktree's `.git` is a FILE pointing at the shared git dir; a copy of it
        # would let git in the RED base write the candidate's own index.
        main, _base = worktree
        wt = main.parent / "linked"
        _git(main, "worktree", "add", "-q", "-b", "cand", str(wt))
        base = _git(wt, "rev-parse", "HEAD")
        assert (wt / ".git").is_file()
        _apply_fix(wt)
        fake = _FakeBugGate()
        res = Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows(), bug_runner=object()), proposal=_proposal(wt), base_sha=base
        )
        assert res.reason == BUG_FILED
        assert fake.kwargs["base_src"] == main.parent / "linked__redbase" / "src"
        assert fake.base_has_git is False
        assert fake.base_files["src/pkg.py"] == _SRC_BASE

    def test_with_nothing_to_revert_the_worktree_src_is_used_and_kept(self, worktree) -> None:
        wt, base = worktree
        fake = _FakeBugGate()
        Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows(), bug_runner=object()), proposal=_proposal(wt), base_sha=base
        )
        assert fake.kwargs["base_src"] == wt / "src"
        assert (wt / "src" / "pkg.py").is_file()
        assert not (wt.parent / "wt__redbase").exists()

    def test_a_staging_failure_falls_back_to_the_worktree_src(
        self, worktree, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wt, base = worktree
        _apply_fix(wt)

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(gate_mod.shutil, "copytree", _boom)
        fake = _FakeBugGate()
        res = Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows(), bug_runner=object()), proposal=_proposal(wt), base_sha=base
        )
        assert res.reason == BUG_FILED
        assert fake.kwargs["base_src"] == wt / "src"
        assert (wt / "src" / "pkg.py").read_text() == _SRC_FIX  # never deleted

    def test_a_stale_red_base_from_an_earlier_cycle_is_replaced(self, worktree) -> None:
        wt, base = worktree
        _apply_fix(wt)
        stale = wt.parent / "wt__redbase"
        (stale / "src").mkdir(parents=True)
        (stale / "src" / "leftover.py").write_text("old\n")
        fake = _FakeBugGate()
        Gate(bug_gate=fake).run_bug(  # type: ignore[arg-type]
            profile=_profile(_Allows(), bug_runner=object()), proposal=_proposal(wt), base_sha=base
        )
        assert "src/leftover.py" not in fake.base_files
        assert not stale.exists()


def test_self_clone_src_is_the_parent_clone_src(tmp_path: Path) -> None:
    proposal = _proposal(tmp_path / "clone" / "worktrees" / "c1")
    assert self_clone_src(proposal) == tmp_path / "clone" / "src"

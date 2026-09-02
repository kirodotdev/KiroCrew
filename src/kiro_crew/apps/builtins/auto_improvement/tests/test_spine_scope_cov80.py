"""Diff-scope degradation — the three ways ``scoped_relpaths`` answers "unscoped".

``None`` and ``set()`` are NOT interchangeable here: ``None`` widens the edit fence to
the whole repository, ``set()`` narrows it to nothing. The module's own docstring
records a regression where a valid-but-empty diff collapsed into ``None`` and silently
un-fenced the run, so each way of reaching ``None`` (blank ref, git raising, git
exiting non-zero) is pinned separately, and the empty-but-successful diff is pinned as
a set. ``git`` is injected via the ``runner`` seam — no subprocess runs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kiro_crew.apps.builtins.auto_improvement.spine.scope import in_scope, scoped_relpaths


def _proc(*, returncode: int = 0, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class TestUnscopedDegradations:
    def test_a_blank_base_ref_is_unscoped_without_shelling_git(self, tmp_path: Path) -> None:
        """No ref means the operator set no scope, so git must not even be invoked."""
        calls: list[list[str]] = []

        def _runner(argv, **kwargs):
            calls.append(list(argv))
            return _proc()

        for blank in ("", "   ", None):
            assert scoped_relpaths(tmp_path, blank, runner=_runner) is None  # type: ignore[arg-type]
        assert calls == []

    def test_a_raising_git_degrades_to_unscoped_instead_of_crashing(self, tmp_path: Path) -> None:
        def _runner(argv, **kwargs):
            raise OSError("git not on PATH")

        assert scoped_relpaths(tmp_path, "origin/main", runner=_runner) is None

    def test_a_nonzero_git_exit_degrades_to_unscoped(self, tmp_path: Path) -> None:
        """An unresolvable base ref cannot yield a scope, so it must not yield an
        empty one — that would fence the run down to editing nothing."""
        out = scoped_relpaths(tmp_path, "no/such/ref", runner=lambda argv, **kw: _proc(returncode=128))
        assert out is None


class TestSuccessfulDiff:
    def test_a_successful_empty_diff_is_a_scope_of_nothing_not_unscoped(
        self, tmp_path: Path
    ) -> None:
        """The regression the module docstring records: ``set()`` must survive as a set."""
        out = scoped_relpaths(tmp_path, "HEAD", runner=lambda argv, **kw: _proc(stdout="\n \n"))
        assert out == set()
        assert out is not None

    def test_it_diffs_three_dot_against_the_base_in_the_clone(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def _runner(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            return _proc(stdout="src/a.py\n src/b.py \n")

        assert scoped_relpaths(tmp_path, " feat/base ", runner=_runner) == {
            "src/a.py",
            "src/b.py",
        }
        argv = seen["argv"]
        assert argv[:3] == ["git", "-C", str(tmp_path)]  # type: ignore[index]
        assert "feat/base...HEAD" in argv  # type: ignore[operator]
        assert "--name-only" in argv  # type: ignore[operator]
        assert seen["kwargs"]["timeout"] == 60  # type: ignore[index]


class TestInScopePredicate:
    def test_unscoped_admits_every_path(self) -> None:
        assert in_scope("anything/at/all.py", None) is True

    def test_membership_is_verbatim(self) -> None:
        scope = {"src/a.py"}
        assert in_scope("src/a.py", scope) is True
        assert in_scope("src/b.py", scope) is False

    def test_a_scope_of_nothing_admits_nothing(self) -> None:
        assert in_scope("src/a.py", set()) is False

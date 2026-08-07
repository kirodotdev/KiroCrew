"""Gate-suite scoping — why a monorepo candidate could never pass STAYGREEN.

The full-suite gate steps (T0 build smoke, STAYGREEN) ran the target repo's ENTIRE
suite. On this monorepo that is 21,901 tests, which cannot finish inside
``_SUITE_TIMEOUT_S`` even in parallel; a timeout returns the unparseable
``<unparsed-suite-failure>`` sentinel, which the gate must conservatively read as a
regression. So every candidate died with "suite is NOT green but reported no
identifiable failing test" no matter how good the fix.

When the operator has NARROWED the edit allowlist to a subtree, the fence already
guarantees the change touches only that subtree, so its own tests are the relevant
regression signal (28s here vs a 900s timeout). These pin that derivation, and that
it fails OPEN to whole-tree behavior whenever a scope cannot be established.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.profile import (
    PytestBugRunner,
    PytestBuildGate,
    _suite_scope_for_globs,
)


def _repo(tmp_path: Path) -> Path:
    """A src-layout repo with an app subtree that has its own tests dir."""
    root = tmp_path / "repo"
    (root / "src" / "pkg" / "apps" / "myapp" / "backend").mkdir(parents=True)
    (root / "src" / "pkg" / "apps" / "myapp" / "spine").mkdir(parents=True)
    (root / "src" / "pkg" / "apps" / "myapp" / "tests").mkdir(parents=True)
    (root / "tests").mkdir()  # the repo-wide suite
    return root


class TestSuiteScopeDerivation:
    def test_narrowed_globs_scope_to_the_nearest_tests_dir(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        scope = _suite_scope_for_globs(
            root,
            [
                "src/pkg/apps/myapp/backend/*.py",
                "src/pkg/apps/myapp/spine/*.py",
            ],
        )
        assert scope == [os.path.join("src", "pkg", "apps", "myapp", "tests")]

    def test_no_allowlist_means_no_scope(self, tmp_path: Path) -> None:
        """Unset allowlist must keep the whole-tree behavior, not gate nothing."""
        root = _repo(tmp_path)
        assert _suite_scope_for_globs(root, None) == []
        assert _suite_scope_for_globs(root, []) == []

    def test_globs_with_no_common_ancestor_widen_to_the_repo_suite(self, tmp_path: Path) -> None:
        """Disjoint edit regions share no ancestor, so the scope must widen to the
        repo-wide suite — named explicitly rather than left empty, but equivalent in
        coverage to the unscoped behavior."""
        root = _repo(tmp_path)
        assert _suite_scope_for_globs(root, ["src/*.py", "other/*.py"]) == ["tests"]

    def test_it_is_empty_when_the_repo_has_no_test_dir_at_all(self, tmp_path: Path) -> None:
        root = tmp_path / "notests"
        (root / "src" / "pkg").mkdir(parents=True)
        assert _suite_scope_for_globs(root, ["src/*.py", "other/*.py"]) == []

    def test_it_walks_up_to_the_nearest_enclosing_test_dir(self, tmp_path: Path) -> None:
        """A dir with no tests of its own widens to the nearest enclosing suite —
        each step up only ever widens coverage, so this is safe."""
        root = _repo(tmp_path)
        (root / "src" / "pkg" / "other").mkdir(parents=True)
        # Nothing at src/pkg/other or src/pkg or src → the repo-wide tests/ dir.
        assert _suite_scope_for_globs(root, ["src/pkg/other/*.py"]) == ["tests"]

    def test_no_test_dir_anywhere_fails_open_to_whole_tree(self, tmp_path: Path) -> None:
        root = tmp_path / "bare"
        (root / "mod").mkdir(parents=True)
        assert _suite_scope_for_globs(root, ["mod/*.py"]) == []

    def test_a_test_dir_named_test_is_accepted(self, tmp_path: Path) -> None:
        root = tmp_path / "r2"
        (root / "mod" / "test").mkdir(parents=True)
        assert _suite_scope_for_globs(root, ["mod/*.py"]) == [os.path.join("mod", "test")]

    def test_scope_is_repo_relative(self, tmp_path: Path) -> None:
        """It is passed as a pytest arg with cwd=repo root, so it must not be absolute."""
        scope = _suite_scope_for_globs(_repo(tmp_path), ["src/pkg/apps/myapp/backend/*.py"])
        assert scope and not Path(scope[0]).is_absolute()


class TestRunnersAcceptTheScope:
    def test_both_runners_default_to_whole_tree(self) -> None:
        assert PytestBuildGate().suite_scope == []
        assert PytestBugRunner().suite_scope == []

    def test_both_runners_store_the_scope(self) -> None:
        scope = ["src/pkg/apps/myapp/tests"]
        assert PytestBuildGate(suite_scope=scope).suite_scope == scope
        assert PytestBugRunner(suite_scope=scope).suite_scope == scope

    def test_the_scope_is_copied_not_aliased(self) -> None:
        """A caller mutating its list afterwards must not retarget the gate."""
        scope = ["a/tests"]
        runner = PytestBugRunner(suite_scope=scope)
        scope.append("b/tests")
        assert runner.suite_scope == ["a/tests"]


class TestUnresolvableScopeRefuses:
    """`scoped_relpaths` returns None for THREE different situations — blank ref, a git
    FAILURE (unresolvable ref), and a valid-but-EMPTY diff — and the caller cannot tell them
    apart. Treating all three as "no scope" let a misconfigured `scopeDiffBase` silently
    widen the edit fence from "what this branch changed" to the WHOLE repository, the
    opposite of what setting a scope is for. The profile now REFUSES an unresolvable ref
    while still allowing the legitimate empty-diff case. Raised by the GPT review.
    """

    @staticmethod
    def _repo(root: Path) -> Path:
        import os
        import subprocess

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, env=env)
        (root / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env)
        return root

    def test_an_unresolvable_scope_base_refuses_to_build_the_profile(self, tmp_path) -> None:
        import pytest

        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gp

        clone = self._repo(tmp_path / "clone")
        # Matches on the CONSEQUENCE, not the cause: the guard deliberately no longer
        # distinguishes "does not resolve" from "resolves but cannot be diffed" (both widen
        # the fence identically), so asserting the old cause-specific wording would pin a
        # distinction the code dropped on purpose.
        with pytest.raises(ValueError, match="could not be resolved to a file set"):
            gp.GitHubRepoProfile(
                clone_path=clone,
                pr_queue_dir=tmp_path / "queue",
                scope_base="origin/no-such-branch",
            )

    def test_a_resolvable_base_with_an_empty_diff_scopes_to_NOTHING(self, tmp_path) -> None:
        """base == HEAD must scope to the EMPTY SET, not fall through to unscoped.

        REPLACES an assertion that `_scope is None` here, on the reasoning that "there is
        nothing this branch changed, so scoping is moot". That reasoning let the fence widen to
        the whole repository for a base that resolves perfectly well: measured,
        `scopeDiffBase='HEAD'` returned `None` and `RepoEditAllowlist` (which checks
        `scope is not None`) then permitted every path. An empty scope is a real answer — "no
        file may be edited" — and a run that can keep nothing is strictly safer than one that
        may edit anything. It also does not raise: the ref resolved, so this is not the
        unresolvable-ref refusal case above.
        """
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import profile as gp

        clone = self._repo(tmp_path / "clone")
        prof = gp.GitHubRepoProfile(
            clone_path=clone, pr_queue_dir=tmp_path / "queue", scope_base="HEAD"
        )
        assert prof._scope == set(), "an empty diff must scope to nothing, not to the repo"
        # And the fence actually enforces it — this is the property that was broken.
        ok, offending = prof.edit_allowlist.allows(["src/anything.py"])
        assert ok is False, "an empty scope permitted an arbitrary path"
        assert offending == ["src/anything.py"]

"""The shared scope resolver must tell the two merge shapes apart.

``scripts/ratchet_scope.py`` answers "which files did THIS change touch" for the
merge-ref ratchets. Two checkout shapes both look like "HEAD is a merge" and
need opposite diffs:

* CI's ``pull_request`` merge ref: the BASE is the first parent, so
  ``HEAD^1..HEAD`` is exactly the PR's own change.
* A local ``git merge origin/main`` on a feature branch: the FEATURE tip is the
  first parent, so ``HEAD^1..HEAD`` is only what main brought in and the
  branch's own commits are invisible -- every consuming gate then under-scopes,
  and a violation added in an earlier feature commit passes locally only to red
  the PR on CI.

The resolver decides by asking git which parent the base branch can reach, so
these tests build one synthetic repo per shape and pin the attempt LABEL chosen
plus the returned path set. The three-dot fallback deliberately keeps diffing
from ``merge-base(base, HEAD)`` rather than the base tip: an unscoped gate has
already been observed reporting files the base branch merged after the baseline
was taken, and the CI-shape test locks that property by moving main after the
branch point.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS, git_command_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ratchet_scope.py"

SPEC = importlib.util.spec_from_file_location("ratchet_scope", SCRIPT)
assert SPEC and SPEC.loader
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no inherited location, templates, hooks, or identity.

    ``git_command_env()`` (the production chokepoint) strips the ``GIT_DIR``
    location family -- those must be ABSENT, and a merge over ``os.environ``
    can only add keys -- and pins the fixed-key exec vectors. On top of that,
    an inherited ``GIT_TEMPLATE_DIR`` (or a global ``init.templateDir``) would
    have its hooks COPIED into every fixture repo by ``git init`` and executed
    by the ``git commit`` below -- host-side effects from running the test
    suite -- so both template channels are pinned empty. Identity is supplied
    so a commit cannot depend on, or fall back to, the developer's global
    config, which is itself pointed at ``os.devnull``.
    """
    env = {
        **git_command_env(),
        "GIT_TEMPLATE_DIR": "",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    count = int(env["GIT_CONFIG_COUNT"])
    env[f"GIT_CONFIG_KEY_{count}"] = "init.templateDir"
    env[f"GIT_CONFIG_VALUE_{count}"] = ""
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=_fixture_git_env(),
    )
    return proc.stdout.strip()


def _commit_file(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def _repo_with_diverged_feature(tmp_path: Path) -> Path:
    """One base repo both shapes start from.

    ``main`` gains ``mainline.txt`` AFTER ``feature`` branches off with its own
    ``feature.py``, so the two sides of every merge below differ and a wrong
    parent choice shows up in the returned path set, not just the label.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main", ".")
    _commit_file(repo, "base.txt", "base")
    _git(repo, "checkout", "-b", "feature")
    _commit_file(repo, "feature.py", "the change under judgment")
    _git(repo, "checkout", "main")
    _commit_file(repo, "mainline.txt", "someone else's change, landed after the branch point")
    return repo


def _set_origin_main(repo: Path) -> None:
    # The synthetic repo has no remote; the resolver only needs the REF, so
    # point origin/main at the local main tip directly.
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The fixture's own git calls build a scrubbed env per call, but the
    # RESOLVER under test runs git with the ambient process environment: an
    # exported GIT_DIR (pytest run from a git hook, `git rebase --exec`,
    # `git bisect run`) would override its cwd=ROOT and answer for the wrong
    # repository. Delete the whole location family -- monkeypatch restores it
    # after the test -- using the same canonical list the production env
    # builder strips.
    for var in _GIT_LOCATION_VARS:
        monkeypatch.delenv(var, raising=False)
    fixture_repo = _repo_with_diverged_feature(tmp_path)
    # The module runs git with cwd=ROOT; retarget it at the synthetic repo.
    monkeypatch.setattr(scope, "ROOT", fixture_repo)
    return fixture_repo


class TestMergeShapes:
    def test_ci_merge_ref_scopes_to_the_change_only(self, repo: Path) -> None:
        # GitHub's pull_request merge ref: merge the PR INTO the base, so the
        # base tip is the first parent and origin/main can reach it.
        _set_origin_main(repo)
        _git(repo, "checkout", "--detach", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge ref", "feature")

        paths, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        # Exactly the PR's own change: mainline.txt landed on the base after
        # the branch point and must NOT be judged as part of this change.
        assert paths == {"feature.py"}

    def test_local_merge_of_main_scopes_to_the_branch_own_commits(self, repo: Path) -> None:
        # The inverted shape: `git merge origin/main` ON the feature branch
        # puts the feature tip first. HEAD^1..HEAD here is what main brought
        # in, so taking the merge diff would hide feature.py -- the defect this
        # resolver exists to avoid. The base-reachability probe must reject the
        # merge attempts and fall through to the three-dot diff.
        _set_origin_main(repo)
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync with main", "origin/main")

        paths, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert paths == {"feature.py"}

    def test_merge_made_on_main_is_still_recognised_without_a_remote(self, repo: Path) -> None:
        # A merge made ON main itself (no remote at all): the prior main tip is
        # the first parent, and the local `main` ref -- now the merge commit --
        # reaches it. The probe must accept this via the `main` fallback;
        # probing only origin/main would reject it, and `main...HEAD` then
        # diffs the merge against itself: an EMPTY scope, so every consuming
        # gate passes vacuously -- a false green in the same direction as the
        # under-scope this resolver exists to prevent.
        _git(repo, "merge", "--no-ff", "-m", "land feature", "feature")

        paths, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert paths == {"feature.py"}

    def test_unverifiable_base_falls_through_rather_than_trusting_parent_order(
        self, repo: Path
    ) -> None:
        # No origin/main at all: the reachability probe cannot verify either
        # way, and an unverified merge diff is the failure mode above. Falling
        # through is the safe direction -- here the local `main` ref still
        # answers the three-dot question correctly.
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync with main", "main")

        paths, label = scope.changed_paths()

        assert label == "main...HEAD"
        assert paths == {"feature.py"}

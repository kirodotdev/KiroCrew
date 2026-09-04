"""Regression tests for review-fix git-mutation serialization.

Two concurrent group actions at one revision both pass target-unchanged
validation and both apply to the real checkout; the CAS then rejects the
loser while its git changes remain in the tree, unrecorded (GPT review,
PR #5274, residual/crash-data-loss-corruption). ``fix_tasks`` holds
``_GIT_MUTATION_LOCK`` across inspect -> mutate -> CAS persist, so the
loser is rejected BEFORE its git mutation can run.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew import review_fix_git
from kiro_crew.apps.builtins.code_review_sage.backend import fix_tasks
from kiro_crew.task_models import ReviewFixGroupState, ReviewFixState


class _Target:
    def __init__(self, tree_version: int):
        self.tree_version = tree_version
        self.target_path = "/repo"
        self.mode = "worktree"
        self.repo_root = "/repo"
        self.head_sha = "abc123"
        self.branch_name = "main"


class _Group:
    def __init__(self):
        self.group_id = "g-1"
        self.state = ReviewFixGroupState.READY_TO_APPLY
        self.revision = 1
        self.candidate_patch_id = "p1"
        self.affected_files = ["a.py"]


class _Metadata:
    def __init__(self):
        self.state = ReviewFixState.READY_TO_APPLY
        self.target = _Target(0)
        self.git = SimpleNamespace(candidate_worktree_path="/candidate")
        self.groups = [_Group()]


class _Run:
    def __init__(self, metadata):
        self.task_id = "t1"
        self.review_fix = metadata


class _Runner:
    """CAS-faithful fake: the persisted revision is the concurrency arbiter."""

    def __init__(self, metadata):
        self._metadata = metadata
        self.revision = 1

    def review_fix_group(self, run, group_id):
        return self._metadata.groups[0]

    async def mutate_review_fix(self, task_id, *, expected_revision, **kw):
        if expected_revision != self.revision:
            raise ValueError("stale revision")
        self.revision += 1
        return SimpleNamespace(ok=True)


@pytest.mark.asyncio
async def test_concurrent_applies_serialize_before_git_mutation(monkeypatch, tmp_path):
    tree = {"version": 0}
    apply_calls: list[float] = []

    async def _inspect_target(path, mode=None):
        return _Target(tree["version"])

    def _assert_unchanged(expected, current):
        if expected.tree_version != current.tree_version:
            raise ValueError("target changed since capture")

    async def _candidate_patch(worktree, head_sha, paths):
        return SimpleNamespace(patch_id="p1", patch_text="x", paths=("a.py",), patch_path="")

    async def _write_patch(patch, patch_path):
        patch.patch_path = str(patch_path)
        return patch

    async def _apply_patch(target, patch):
        # A real git apply takes long enough that, WITHOUT the lock, the second
        # request's validation sneaks in before the first mutation lands.
        await asyncio.sleep(0.05)
        apply_calls.append(asyncio.get_running_loop().time())
        tree["version"] += 1

    monkeypatch.setattr(review_fix_git, "inspect_target", _inspect_target)
    monkeypatch.setattr(review_fix_git, "assert_target_unchanged", _assert_unchanged)
    monkeypatch.setattr(review_fix_git, "candidate_patch", _candidate_patch)
    monkeypatch.setattr(review_fix_git, "write_patch", _write_patch)
    monkeypatch.setattr(review_fix_git, "apply_patch", _apply_patch)
    monkeypatch.setattr(fix_tasks, "artifact_root", lambda metadata: tmp_path)

    metadata = _Metadata()
    runner = _Runner(metadata)
    run = _Run(metadata)

    results = await asyncio.wait_for(
        asyncio.gather(
            # Through _action — the lock lives at the dispatch site, so calling
            # _apply_group directly would bypass exactly the serialization
            # under test.
            fix_tasks._action(None, runner, run, "apply_group", {"group_id": "g-1"}, 1, "fp"),
            fix_tasks._action(None, runner, run, "apply_group", {"group_id": "g-1"}, 1, "fp"),
            return_exceptions=True,
        ),
        timeout=10,
    )

    oks = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]
    assert len(oks) == 1, f"unexpected results: {results!r}"
    # The loser is rejected by the target-unchanged check — NOT after its git
    # mutation already landed — so the tree never holds unrecorded changes.
    assert len(errors) == 1
    assert "changed" in str(errors[0])
    assert len(apply_calls) == 1

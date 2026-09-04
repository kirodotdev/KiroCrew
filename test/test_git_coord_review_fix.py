from __future__ import annotations

from pathlib import Path

import pytest
from review_fix_helpers import _git, _repo, unsandboxed_git  # noqa: F401  (autouse fixture)

from kiro_crew.review_fix_git import (
    ReviewFixGitError,
    ReviewFixPatch,
    apply_patch,
    assert_target_unchanged,
    candidate_patch,
    commit_group,
    create_candidate,
    dirty_overlap,
    dirty_paths,
    discard_candidate,
    inspect_target,
    push_preview,
    write_patch,
)
from kiro_crew.task_models import ReviewFixTargetMode


@pytest.mark.asyncio
async def test_candidate_patch_apply_and_scoped_commit_preserve_unrelated_file(tmp_path):
    repo = _repo(tmp_path)
    target = await inspect_target(repo, mode=ReviewFixTargetMode.CURRENT_BRANCH)
    candidate = await create_candidate(
        target,
        tmp_path / "candidate",
        "kirocrew/review-fix/test-1",
    )
    candidate_file = tmp_path / "candidate" / "target.txt"
    candidate_file.write_text("after\n", encoding="utf-8")

    patch = await candidate_patch(candidate.candidate_worktree_path, target.head_sha)
    assert patch.paths == ("target.txt",)
    patch = await write_patch(patch, tmp_path / "patch.diff")
    assert patch.patch_id

    await apply_patch(target, patch)
    assert (repo / "target.txt").read_text(encoding="utf-8") == "after\n"
    assert (repo / "unrelated.txt").read_text(encoding="utf-8") == "keep\n"

    commit_sha = await commit_group(repo, patch.paths, "fix: apply review finding")
    assert commit_sha
    assert "unrelated.txt" not in _git(repo, "show", "--name-only", "--format=", "HEAD").stdout
    await discard_candidate(candidate, target.repo_root)


@pytest.mark.asyncio
async def test_target_fingerprint_and_path_overlap_are_conservative(tmp_path):
    repo = _repo(tmp_path)
    clean = await inspect_target(repo)
    (repo / "unrelated.txt").write_text("local change\n", encoding="utf-8")
    dirty = await inspect_target(repo)

    assert clean.dirty_fingerprint != dirty.dirty_fingerprint
    assert dirty_overlap(dirty, ["unrelated.txt"]) == ["unrelated.txt"]
    assert dirty_overlap(dirty, ["target.txt"]) == []
    with pytest.raises(ReviewFixGitError):
        assert_target_unchanged(clean, dirty)


@pytest.mark.asyncio
async def test_candidate_is_built_from_captured_head(tmp_path):
    repo = _repo(tmp_path)
    target = await inspect_target(repo)
    (repo / "target.txt").write_text("uncommitted target\n", encoding="utf-8")
    candidate = await create_candidate(target, tmp_path / "candidate", "kirocrew/review-fix/test-2")

    assert (tmp_path / "candidate" / "target.txt").read_text(encoding="utf-8") == "before\n"
    await discard_candidate(candidate, target.repo_root)


def test_clean_path_list_rejects_unsafe_entries():
    from kiro_crew.review_fix_git import _clean_path_list

    for unsafe in ("/abs/target.txt", ".", "a/../b", "-flag"):
        with pytest.raises(ReviewFixGitError):
            _clean_path_list([unsafe])


@pytest.mark.asyncio
async def test_inspect_target_rejects_missing_and_sensitive_paths(tmp_path):
    with pytest.raises(ReviewFixGitError, match="not a directory"):
        await inspect_target(tmp_path / "missing" / "repo")
    with pytest.raises(ReviewFixGitError, match="sensitive"):
        await inspect_target(Path.home() / ".aws")


@pytest.mark.asyncio
async def test_write_patch_refuses_sensitive_paths(tmp_path):
    patch = ReviewFixPatch(patch_id="p", patch_text="diff\n", paths=("target.txt",))
    with pytest.raises(ReviewFixGitError, match="sensitive"):
        await write_patch(patch, Path.home() / ".ssh" / "review-fix.patch")


@pytest.mark.asyncio
async def test_non_ascii_paths_are_tracked_and_overlapped_raw(tmp_path):
    # `git status --porcelain` (without -z) C-quotes non-ASCII names under the
    # default core.quotePath=true, so a Thai or accented filename arrived as
    # "na\303\257ve..." and NEVER matched the task's plain-text owned path —
    # dirty_overlap missed it and validation would have clobbered the user's
    # uncommitted change. -z + --no-renames keeps the name byte-exact.
    repo = _repo(tmp_path)
    untracked = repo / "naïve ทดสอบ.py"
    untracked.write_text("local edit\n", encoding="utf-8")

    snapshot = await inspect_target(repo)
    assert "naïve ทดสอบ.py" in snapshot.untracked_paths
    assert "naïve ทดสอบ.py" in dirty_paths(snapshot)
    assert dirty_overlap(snapshot, ["naïve ทดสอบ.py"]) == ["naïve ทดสอบ.py"]


@pytest.mark.asyncio
async def test_pathspec_magic_in_a_owned_path_captures_nothing_extra(tmp_path):
    # A crafted "filename" is data, never a pattern: every consume point wraps
    # owned paths as :(literal)<path>, so :(top)** must not widen the diff to
    # the whole worktree (and likewise for stage/commit). The wrapped magic
    # path matches no real file, so the patch is EMPTY — never the tree.
    repo = _repo(tmp_path)
    target = await inspect_target(repo)
    candidate = await create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/test-magic"
    )
    (tmp_path / "candidate" / "target.txt").write_text("after\n", encoding="utf-8")

    patch = await candidate_patch(candidate.candidate_worktree_path, target.head_sha, [":(top)**"])
    # Nothing captured at all: the magic path is inert, not tree-wide.
    assert patch.patch_text == ""
    assert patch.paths == ()

    # An owned path containing glob metacharacters still matches ITSELF
    # exactly: the agent's real edit to dir[a]/x.txt is captured by the
    # literal pathspec, and nothing else.
    tricky_dir = tmp_path / "candidate" / "dir[a]"
    tricky_dir.mkdir()
    (tricky_dir / "x.txt").write_text("untouched base\n", encoding="utf-8")
    _git(candidate.candidate_worktree_path, "add", "--", ":(literal)dir[a]/x.txt")
    _git(
        candidate.candidate_worktree_path,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "add tricky",
    )
    # the fix agent edits the owned bracket-named file:
    (tricky_dir / "x.txt").write_text("after\n", encoding="utf-8")

    tricky_target = await inspect_target(candidate.candidate_worktree_path)
    tricky_patch = await candidate_patch(
        candidate.candidate_worktree_path,
        tricky_target.head_sha,
        ["dir[a]/x.txt"],  # as a glob this would ALSO match "dira/x.txt" etc.
    )
    assert tricky_patch.paths == ("dir[a]/x.txt",)
    assert "a/dir[a]/x.txt" in tricky_patch.patch_text or "dir[a]/x.txt" in tricky_patch.patch_text
    await discard_candidate(candidate, target.repo_root)


@pytest.mark.asyncio
async def test_push_preview_without_upstream_previews_against_cached_remote_ref(tmp_path):
    # A branch with no tracking ref (a new-branch candidate) used to preview
    # as EMPTY — approving it would publish unseen commits and files. The
    # preview must fall back to the branch's locally cached remote ref (no
    # network) and list what the push would actually publish.
    repo = _repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "target.txt").write_text("after\n", encoding="utf-8")
    _git(repo, "commit", "-am", "fix the finding")

    # The remote branch's last-fetched state exists locally only.
    _git(repo, "update-ref", "refs/remotes/origin/feature/fix", base_sha)

    preview = await push_preview(repo, "origin", "feature/fix")

    assert preview["upstream"] == ""
    assert preview["preview_base"] == base_sha
    # `--oneline` prefixes each subject with its short sha.
    assert len(preview["commits"]) == 1 and preview["commits"][0].endswith("fix the finding")
    assert preview["files"] == ["target.txt"]


@pytest.mark.asyncio
async def test_push_preview_with_no_local_basis_is_rejected(tmp_path):
    # Neither an upstream nor any cached remote ref: there is nothing to
    # preview against, and an empty preview would invite an unseen push.
    # Reject instead of previewing as empty.
    repo = _repo(tmp_path)

    with pytest.raises(ReviewFixGitError, match="no local basis"):
        await push_preview(repo, "origin", "feature/fix")


@pytest.mark.asyncio
async def test_candidate_patch_over_capture_cap_is_rejected_not_truncated(tmp_path):
    # communicate() buffered the ENTIRE candidate diff before any limit, so a
    # runaway agent diff OOM'd the gateway. The capture is now capped and a
    # diff beyond the cap FAILS the call: a truncated patch would break
    # `git apply` after the group pinned its patch_id, so truncation must
    # never be returned as a usable patch.
    repo = _repo(tmp_path)
    target = await inspect_target(repo)
    candidate = await create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/test-cap"
    )
    # The agent's edits land in the CANDIDATE worktree; ~9 MiB single-file
    # edit is comfortably past the 8 MiB capture cap.
    big = Path(candidate.candidate_worktree_path) / "target.txt"
    big.write_text("x" * (9 * 1024 * 1024) + "\n", encoding="utf-8")

    with pytest.raises(ReviewFixGitError, match="capture"):
        await candidate_patch(candidate.candidate_worktree_path, target.head_sha)
    await discard_candidate(candidate, target.repo_root)

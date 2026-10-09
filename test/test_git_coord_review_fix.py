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
    stage_paths,
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
async def test_inspect_target_with_untracked_nested_git_repo(tmp_path):
    # --untracked-files=all reports an untracked nested git repo as a
    # directory entry ("sub/"), which `git hash-object` refuses to hash
    # (exit 128). inspect_target must skip directory entries and hash only
    # untracked files, or every target with a vendored/nested repo is
    # unusable for review-fix.
    repo = _repo(tmp_path)
    nested = repo / "sub"
    nested.mkdir()
    _git(nested, "init")
    (nested / "file.txt").write_text("nested\n", encoding="utf-8")

    snapshot = await inspect_target(repo)
    assert "sub/" in snapshot.untracked_paths


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
    # A branch with no tracking ref (a new-branch candidate) previews as
    # EMPTY against the tracking ref alone — approving it would publish
    # unseen commits and files. The preview falls back to the branch's
    # locally cached remote ref (no network) and lists what the push would
    # actually publish.
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
@pytest.mark.parametrize(
    "filename,edits",
    [
        # G3: `git status` alone reports PATH-level dirty state ("M target.txt"),
        # so editing a file that was ALREADY modified changes its content but
        # not its status line. A status-only fingerprint would miss the second
        # edit and let assert_target_unchanged wave a stale target through.
        ("target.txt", ("first edit\n", "second edit\n")),
        # Same gap, untracked side: an untracked file's OWN content can change
        # without its "?? path" status line changing at all.
        ("new.txt", ("first draft\n", "second draft\n")),
    ],
    ids=["already-modified-file", "untracked-file"],
)
async def test_target_fingerprint_detects_a_second_content_edit(tmp_path, filename, edits):
    repo = _repo(tmp_path)
    (repo / filename).write_text(edits[0], encoding="utf-8")
    first = await inspect_target(repo)

    (repo / filename).write_text(edits[1], encoding="utf-8")
    second = await inspect_target(repo)

    assert first.dirty_fingerprint != second.dirty_fingerprint
    with pytest.raises(ReviewFixGitError, match="changed since confirmation"):
        assert_target_unchanged(first, second)


@pytest.mark.asyncio
async def test_apply_accepts_a_file_containing_a_divider_that_looks_like_a_marker(tmp_path):
    # G4/O1: a real conflict marker owns its whole line and comes as a trio
    # (<<<<<<<, =======, >>>>>>>). A plain substring scan false-positived on
    # a bare "=======" Markdown/RST heading underline or comment banner that
    # a file may legitimately contain, blocking an otherwise-clean apply.
    repo = _repo(tmp_path)
    target = await inspect_target(repo)
    candidate = await create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/test-banner"
    )
    banner = "Title\n=======\nbody\n"
    (Path(candidate.candidate_worktree_path) / "target.txt").write_text(banner, encoding="utf-8")

    patch = await candidate_patch(candidate.candidate_worktree_path, target.head_sha)
    patch = await write_patch(patch, tmp_path / "patch.diff")

    applied = await apply_patch(target, patch)

    assert applied == list(patch.paths)
    assert (repo / "target.txt").read_text(encoding="utf-8") == banner
    await discard_candidate(candidate, target.repo_root)


@pytest.mark.asyncio
async def test_apply_with_a_real_conflict_restores_owned_paths(tmp_path):
    # G4/O1: a genuine conflict must still raise, and the checkout must not
    # be left modified (holding conflict markers) while the group as a
    # whole stays unapplied.
    repo = _repo(tmp_path)
    (repo / "target.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(repo, "commit", "-am", "expand target")
    target = await inspect_target(repo)
    candidate = await create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/test-conflict"
    )
    (Path(candidate.candidate_worktree_path) / "target.txt").write_text(
        "line1\ncandidate change\nline3\n", encoding="utf-8"
    )
    patch = await candidate_patch(candidate.candidate_worktree_path, target.head_sha)
    patch = await write_patch(patch, tmp_path / "patch.diff")

    # Diverge the TARGET itself on the same line so the 3-way merge cannot
    # resolve cleanly and leaves real conflict markers.
    (repo / "target.txt").write_text("line1\ndiverged change\nline3\n", encoding="utf-8")
    _git(repo, "commit", "-am", "diverge target")
    restored_head = await inspect_target(repo)

    with pytest.raises(ReviewFixGitError, match="conflict"):
        await apply_patch(target, patch)

    assert (repo / "target.txt").read_text(encoding="utf-8") == "line1\ndiverged change\nline3\n"
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert (await inspect_target(repo)).dirty_fingerprint == restored_head.dirty_fingerprint
    await discard_candidate(candidate, target.repo_root)


@pytest.mark.asyncio
async def test_apply_conflict_on_an_added_file_restores_owned_paths(tmp_path):
    # B3: when the conflicting path is one the patch itself ADDS (never
    # tracked at HEAD), `git apply --3way` stages it as a new blob --
    # `git status --porcelain` reports it "AA" (both-added, conflicted).
    # Restore must unstage it (`git rm --cached`), not just unlink it from
    # disk, or the index keeps the staged addition though the file is gone.
    repo = _repo(tmp_path)
    target = await inspect_target(repo)
    candidate = await create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/test-new-file-conflict"
    )
    (Path(candidate.candidate_worktree_path) / "new_file.txt").write_text(
        "candidate content\n", encoding="utf-8"
    )
    _git(candidate.candidate_worktree_path, "add", "--", "new_file.txt")
    patch = await candidate_patch(candidate.candidate_worktree_path, target.head_sha)
    assert patch.paths == ("new_file.txt",)
    patch = await write_patch(patch, tmp_path / "patch.diff")

    # Independently stage (never commit) the same new path in the TARGET
    # with different content: the path stays absent at HEAD, yet the 3-way
    # merge still conflicts against the staged blob.
    (repo / "new_file.txt").write_text("target content\n", encoding="utf-8")
    _git(repo, "add", "new_file.txt")

    with pytest.raises(ReviewFixGitError, match="conflict"):
        await apply_patch(target, patch)

    assert _git(repo, "status", "--porcelain").stdout == ""
    assert not (repo / "new_file.txt").exists()
    await discard_candidate(candidate, target.repo_root)


@pytest.mark.asyncio
async def test_candidate_patch_discovers_non_ascii_filenames(tmp_path):
    # Opus finding: `git diff --name-only` without -z C-quotes a non-ASCII
    # filename under core.quotePath=true, so a Thai/accented candidate file
    # arrived quoted and never matched a task's plain-text owned path.
    repo = _repo(tmp_path)
    target = await inspect_target(repo)
    candidate = await create_candidate(
        target, tmp_path / "candidate", "kirocrew/review-fix/test-nonascii-diff"
    )
    new_file = Path(candidate.candidate_worktree_path) / "naïve ทดสอบ.py"
    new_file.write_text("x = 1\n", encoding="utf-8")
    _git(candidate.candidate_worktree_path, "add", "--", "naïve ทดสอบ.py")

    patch = await candidate_patch(candidate.candidate_worktree_path, target.head_sha)

    assert patch.paths == ("naïve ทดสอบ.py",)
    await discard_candidate(candidate, target.repo_root)


@pytest.mark.asyncio
async def test_stage_paths_discovers_non_ascii_filenames(tmp_path):
    # Opus finding: `git diff --cached --name-only` without -z has the same
    # C-quoting gap on the staged-path readback in stage_paths.
    repo = _repo(tmp_path)
    new_file = repo / "naïve ทดสอบ.py"
    new_file.write_text("x = 1\n", encoding="utf-8")

    staged = await stage_paths(repo, ["naïve ทดสอบ.py"])

    assert staged == ["naïve ทดสอบ.py"]


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


@pytest.mark.asyncio
async def test_apply_rejects_a_swapped_artifact(tmp_path, monkeypatch):
    """A swapped artifact cannot add unowned paths."""
    repo = _repo(tmp_path)

    patch_path = tmp_path / "patch.diff"
    patch_path.write_text("tampered")
    patch = ReviewFixPatch("id", "bad", ("target.txt",), str(patch_path))

    with pytest.raises(ReviewFixGitError, match="patch changed before apply"):
        await apply_patch(await inspect_target(repo), patch)

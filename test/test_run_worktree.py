# SPDX-License-Identifier: Apache-2.0
"""An isolated ``spawn_run`` worker gets a git worktree of its own, beside the repository.

``crewmate_worktree.make_run_worktree`` makes ``<parent>/<repo>-wt-<run id>``
detached at the HEAD of the checkout the task's cwd is in, only under an allowed
root; ``remove_run_worktree`` removes it only while it is clean, and
``source_path`` maps it back to the repository so a profile binding still applies.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from crewmate_worktree_helpers import (  # noqa: F401 - fixtures
    _commit_in,
    _git,
    _init,
    _not_a_worktree_reason,
    git_here_fixture,
)

from kiro_crew import crewmate_worktree as cw

RUN_A = "0123456789abcdef"
RUN_B = "fedcba9876543210"


@pytest.fixture
def projects(tmp_path, git_here):  # noqa: F811
    root = tmp_path / "projects"
    repo = root / "tasklog"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    _init(repo)
    return str(root), str(repo)


def _dest(repo: str, run_id: str) -> str:
    return os.path.join(os.path.dirname(repo), f"tasklog-wt-{run_id}")


def _registered(repo: str) -> str:
    return _git(repo, "worktree", "list", "--porcelain")


def test_run_id_shape_matches_the_run_id_length():
    from kiro_crew import subagent

    assert cw._RUN_ID_RE.pattern == "[0-9a-f]{%d}" % subagent._RUN_ID_HEX_CHARS == "[0-9a-f]{16}"


def test_made_beside_the_repository_detached_at_head_with_the_subfolder_mapped(projects):
    root, repo = projects
    head = _git(repo, "rev-parse", "HEAD")
    made = cw.make_run_worktree(os.path.join(repo, "docs"), RUN_A, [root])
    assert made.path == _dest(repo, RUN_A)
    assert made.source_cwd == os.path.join(repo, "docs")
    assert made.base == head
    assert made.cwd == os.path.join(made.path, "docs")
    assert _git(made.path, "rev-parse", "HEAD") == head
    assert _git(made.path, "branch", "--show-current") == ""  # detached
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "branch", "--show-current") == "main"


def test_uncommitted_changes_in_the_source_are_not_carried_over(projects):
    root, repo = projects
    with open(os.path.join(repo, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("local edit\n")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    assert _git(made.path, "status", "--porcelain") == ""


def test_two_run_ids_get_two_worktrees(projects):
    root, repo = projects
    a = cw.make_run_worktree(repo, RUN_A, [root])
    b = cw.make_run_worktree(repo, RUN_B, [root])
    assert a.path != b.path
    listing = _registered(repo)
    assert a.path in listing and b.path in listing


def test_the_same_run_id_twice_reuses_the_worktree(projects):
    root, repo = projects
    a = cw.make_run_worktree(repo, RUN_A, [root])
    _commit_in(a.path, "work.txt", "work")
    again = cw.make_run_worktree(a.cwd, RUN_A, [root])
    assert again.path == a.path
    assert again.cwd == a.path


def test_a_clean_worktree_is_removed_and_the_source_untouched(projects):
    root, repo = projects
    branches = _git(repo, "branch", "--list")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed and report.kept_because == "" and report.commit_count == 0
    assert not os.path.exists(made.path)
    assert made.path not in _registered(repo)
    assert _git(repo, "branch", "--list") == branches
    assert _git(repo, "status", "--porcelain") == ""


def test_a_commit_keeps_the_worktree_and_is_reported(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    _commit_in(made.path, "work.txt", "work")
    sha = _git(made.path, "rev-parse", "--short", "HEAD")
    peek = cw.remove_run_worktree(made.path, made.base, RUN_A, remove=False)
    assert not peek.removed and peek.commit_count == 1 and peek.commits[0].startswith(sha[:7])
    assert os.path.isdir(made.path)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and report.kept_because == "it has commits"
    assert report.commit_count == 1
    assert os.path.isdir(made.path)


def test_an_uncommitted_change_keeps_the_worktree(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    with open(os.path.join(made.path, "new.txt"), "w", encoding="utf-8") as fh:
        fh.write("wip\n")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and report.kept_because.startswith(
        "it holds changed, untracked or ignored files"
    )
    assert os.path.isdir(made.path)


def test_a_new_file_keeps_the_worktree_when_status_hides_untracked_files(projects):
    root, repo = projects
    _git(repo, "config", "status.showUntrackedFiles", "no")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    with open(os.path.join(made.path, "new.txt"), "w", encoding="utf-8") as fh:
        fh.write("wip\n")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and report.kept_because.startswith(
        "it holds changed, untracked or ignored files"
    )
    assert os.path.isfile(os.path.join(made.path, "new.txt"))


def test_only_an_ignored_file_keeps_the_worktree(projects):
    root, repo = projects
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("out/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    os.mkdir(os.path.join(made.path, "out"))
    with open(os.path.join(made.path, "out", "r.bin"), "w", encoding="utf-8") as fh:
        fh.write("x")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and report.kept_because.startswith(
        "it holds changed, untracked or ignored files"
    )
    assert os.path.isdir(made.path)


@pytest.mark.parametrize(
    ("flag", "edited"),
    [
        ("--assume-unchanged", True),
        ("--skip-worktree", True),
        ("--assume-unchanged", False),
        ("--skip-worktree", False),
    ],
)
def test_a_flagged_file_edited_or_deleted_keeps_the_worktree(projects, flag, edited):
    """The flag hides the edit, or the deletion, from ``status`` and ``worktree remove``."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    tracked = os.path.join(made.path, "docs", "a.md")
    _git(made.path, "update-index", flag, "docs/a.md")
    if edited:
        with open(tracked, "w", encoding="utf-8") as fh:
            fh.write("unsaved work\n")
    else:
        os.remove(tracked)
    assert _git(made.path, "status", "--porcelain") == ""  # the flag hides the change
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and "docs/a.md" in report.kept_because
    assert os.path.isdir(made.path)
    if edited:
        with open(tracked, encoding="utf-8") as fh:
            assert fh.read() == "unsaved work\n"


def test_a_file_a_sparse_checkout_leaves_out_holds_nothing(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    _git(made.path, "sparse-checkout", "set", "--no-cone", "/README.md")
    assert not os.path.exists(os.path.join(made.path, "docs", "a.md"))
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed and not os.path.exists(made.path)
    assert made.path not in _registered(repo)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_a_flagged_file_with_a_name_that_is_not_utf8_keeps_the_worktree(projects, flag):
    root, repo = projects
    name = b"draft-\xff.txt"
    try:
        with open(os.path.join(os.fsencode(repo), name), "wb") as fh:
            fh.write(b"draft\n")
    except OSError as exc:  # APFS and NTFS refuse a file name that is not UTF-8
        pytest.skip(f"this filesystem refuses a file name that is not UTF-8: {exc}")
    _git(repo, "add", os.fsdecode(name))
    _git(repo, "commit", "-q", "-m", "draft")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    tracked = os.path.join(os.fsencode(made.path), name)
    _git(made.path, "update-index", flag, os.fsdecode(name))
    with open(tracked, "wb") as fh:
        fh.write(b"unsaved work\n")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and "draft-\ufffd.txt" in report.kept_because
    report.kept_because.encode("utf-8")  # safe for a log line or a JSON answer
    with open(tracked, "rb") as fh:
        assert fh.read() == b"unsaved work\n"


def _sparse_checkout_leaves_a_file_out(path: str) -> None:
    _git(path, "sparse-checkout", "set", "--no-cone", "/README.md")


def _edit_core_ignore_stat_hides(path: str) -> None:
    _git(path, "config", "core.ignoreStat", "true")
    with open(os.path.join(path, "docs", "a.md"), "w", encoding="utf-8") as fh:
        fh.write("unsaved work\n")


def _chmod_core_file_mode_hides(path: str) -> None:
    if os.name == "nt":
        pytest.skip("Windows keeps no executable bit")
    _git(path, "config", "core.fileMode", "false")
    tracked = os.path.join(path, "docs", "a.md")
    os.chmod(tracked, os.stat(tracked).st_mode | 0o100)


@pytest.mark.parametrize(
    ("make_state", "removable"),
    [
        (_sparse_checkout_leaves_a_file_out, True),
        (_edit_core_ignore_stat_hides, False),
        (_chmod_core_file_mode_hides, False),
    ],
)
def test_a_run_worktree_gets_the_answer_the_one_removability_check_gives(
    projects, make_state, removable
):
    """The prune asks :func:`is_provably_disposable`, so the two cannot answer apart:
    a file a sparse checkout leaves out holds nothing, and a config that hides an edit hides nothing.
    """
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    make_state(made.path)
    wt = cw._wt()
    assert cw.is_provably_disposable(wt, repo, made.path, None)[1] == "its HEAD is detached"
    verdict = cw.is_provably_disposable(wt, repo, made.path, None, head_at=made.base)
    assert verdict[0] is removable
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed is removable
    assert report.kept_because == verdict[1]
    assert os.path.isdir(made.path) is not removable


def _commit_a_submodule(repo: str) -> None:
    """Record a gitlink at ``sub``, as a repository with a submodule has one."""
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{head},sub")
    _git(repo, "commit", "-q", "-m", "submodule")


def test_an_unpopulated_submodule_holds_nothing_and_the_worktree_goes(projects):
    root, repo = projects
    _commit_a_submodule(repo)
    made = cw.make_run_worktree(repo, RUN_A, [root])
    assert os.path.isdir(os.path.join(made.path, "sub"))  # worktree add leaves it empty
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed and not os.path.exists(made.path)


def test_a_populated_submodule_keeps_the_worktree_before_any_status_runs(projects, monkeypatch):
    """``status`` would enter the submodule and run its own configured commands."""
    from kiro_crew.dashboard.handlers import worktree as wt

    root, repo = projects
    _commit_a_submodule(repo)
    made = cw.make_run_worktree(repo, RUN_A, [root])
    with open(os.path.join(made.path, "sub", ".git"), "w", encoding="utf-8") as fh:
        fh.write("gitdir: elsewhere\n")
    ran: list[list[str]] = []
    real = wt._run_git

    def spy(args, cwd, **kwargs):
        ran.append(list(args))
        return real(args, cwd, **kwargs)

    monkeypatch.setattr(wt, "_run_git", spy)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed
    assert report.kept_because == "it holds files in a submodule folder (sub)"
    assert not [args for args in ran if "status" in args]
    assert os.path.isdir(made.path)


def test_a_file_written_into_an_empty_submodule_folder_keeps_the_worktree(projects):
    """``status`` never lists it and ``git worktree remove`` would delete it."""
    root, repo = projects
    _commit_a_submodule(repo)
    made = cw.make_run_worktree(repo, RUN_A, [root])
    result = os.path.join(made.path, "sub", "result.txt")
    with open(result, "w", encoding="utf-8") as fh:
        fh.write("unsaved work\n")
    assert _git(made.path, "status", "--porcelain", "--untracked-files=all") == ""
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed
    assert report.kept_because == "it holds files in a submodule folder (sub)"
    with open(result, encoding="utf-8") as fh:
        assert fh.read() == "unsaved work\n"


def test_a_staged_submodule_commit_keeps_the_worktree(projects):
    root, repo = projects
    _commit_a_submodule(repo)
    made = cw.make_run_worktree(repo, RUN_A, [root])
    _git(made.path, "update-index", "--cacheinfo", f"160000,{'1' * 40},sub")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed
    assert report.kept_because == "it holds a staged submodule change (sub)"
    assert os.path.isdir(made.path)


def _commit_detached(path: str) -> str:
    _commit_in(path, "work.txt", "work")
    return _git(path, "rev-parse", "HEAD")


def test_a_commit_left_on_a_detached_head_keeps_the_worktree(projects):
    """Only this worktree's own HEAD reflog reaches it, and that goes with the worktree."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    sha = _commit_detached(made.path)
    _git(made.path, "checkout", "-q", "--detach", made.base)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed
    assert report.kept_because.startswith("it holds a commit no shared branch, tag or reflog keeps")
    assert _git(made.path, "cat-file", "-t", sha) == "commit"


@pytest.mark.parametrize("ref", ["refs/worktree/keep", "refs/bisect/bad"])
def test_a_commit_only_a_per_worktree_ref_keeps_keeps_the_worktree(projects, ref):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    sha = _commit_detached(made.path)
    _git(made.path, "update-ref", ref, sha)
    _git(made.path, "checkout", "-q", "--detach", made.base)
    _git(made.path, "reflog", "expire", "--expire=now", "--all")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and sha[:12] in report.kept_because


def test_a_commit_only_a_per_worktree_refs_reflog_keeps_keeps_the_worktree(projects):
    """A checkpoint the ref pointed at before it moved back is reached by nothing else."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    sha = _git(made.path, "commit-tree", "-m", "checkpoint", "-p", made.base, "HEAD^{tree}")
    _git(made.path, "update-ref", "--create-reflog", "refs/worktree/checkpoint", sha)
    _git(made.path, "update-ref", "refs/worktree/checkpoint", made.base)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and sha[:12] in report.kept_because


def _empty_commits_on_a_side_branch(repo: str, count: int) -> list[str]:
    """Empty commits on a shared branch: an operation replaying them changes no file."""
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "branch", "side")
    shas = []
    for n in range(count):
        tree = _git(repo, "rev-parse", "HEAD^{tree}")
        parent = shas[-1] if shas else base
        shas.append(_git(repo, "commit-tree", "-m", f"empty {n}", "-p", parent, tree))
    _git(repo, "update-ref", "refs/heads/side", shas[-1])
    return shas


def _paused(path: str, *args: str) -> None:
    """Run a git operation that is expected to stop part way."""
    subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8", check=False
    )


def _an_am_stopped_at_an_empty_patch(repo: str, path: str, tmp: Path) -> None:
    (sha,) = _empty_commits_on_a_side_branch(repo, 1)
    patches = tmp / "patches"
    _git(repo, "format-patch", "--always", "-1", sha, "-o", str(patches))
    _paused(path, "am", "--empty=stop", *sorted(str(p) for p in patches.iterdir()))


def _a_merge_not_yet_committed(repo: str, path: str, _tmp: Path) -> None:
    _empty_commits_on_a_side_branch(repo, 1)
    _paused(path, "merge", "--no-commit", "--no-ff", "side")


def _a_cherry_pick_sequence_stopped(repo: str, path: str, _tmp: Path) -> None:
    first, second = _empty_commits_on_a_side_branch(repo, 2)
    _paused(path, "cherry-pick", first, second)


def _a_revert_stopped_and_its_conflict_put_back(repo: str, path: str, _tmp: Path) -> None:
    # Reverting an empty commit stops with no state, so revert one that conflicts,
    # then put the file back as HEAD has it: status is clean, the revert still paused.
    _git(repo, "switch", "-q", "-c", "side")
    _commit_in(repo, "README.md", "second")
    _commit_in(repo, "README.md", "third")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-")
    _paused(path, "revert", "--no-edit", sha)
    _git(path, "checkout", "HEAD", "--", "README.md")


def _a_rebase_stopped_at_a_break(_repo: str, path: str, _tmp: Path) -> None:
    _paused(path, "-c", "sequence.editor=sh -c 'echo break > \"$1\"' -", "rebase", "-i", "HEAD")


def _a_bisect_started(_repo: str, path: str, _tmp: Path) -> None:
    _git(path, "bisect", "start")


@pytest.mark.parametrize(
    "pause",
    [
        _an_am_stopped_at_an_empty_patch,
        _a_merge_not_yet_committed,
        _a_cherry_pick_sequence_stopped,
        _a_revert_stopped_and_its_conflict_put_back,
        _a_rebase_stopped_at_a_break,
        _a_bisect_started,
    ],
    ids=lambda pause: pause.__name__[1:],
)
def test_a_paused_git_operation_keeps_the_worktree(projects, tmp_path, pause):
    """Its state lives in the worktree's own git folder and shows nowhere in ``status``."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    pause(repo, made.path, tmp_path)
    assert _git(made.path, "rev-parse", "HEAD") == made.base
    assert _git(made.path, "status", "--porcelain", "--untracked-files=all") == ""
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and os.path.isdir(made.path)
    assert report.kept_because.startswith("its own git folder holds ")


def _own_git_folder(path: str) -> str:
    return _git(path, "rev-parse", "--absolute-git-dir")


def test_an_unknown_file_in_the_worktrees_own_git_folder_keeps_it(projects):
    """What git may add there later is not known to be safe, so the worktree stays."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    with open(os.path.join(_own_git_folder(made.path), "NEW_STATE"), "w", encoding="utf-8") as fh:
        fh.write("state a later git keeps here\n")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and os.path.isdir(made.path)
    assert report.kept_because == (
        "its own git folder holds NEW_STATE, not known to be safe to remove"
    )


def test_what_normal_use_leaves_in_the_worktrees_own_git_folder_lets_it_go(projects):
    """A commit, checkout, fetch and reset, a sparse checkout set and disabled: all safe."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    _git(made.path, "checkout", "-q", "-b", "side")
    _commit_in(made.path, "README.md", "a change")
    _git(made.path, "fetch", "-q", repo)
    _git(made.path, "sparse-checkout", "set", "--no-cone", "/README.md")
    _git(made.path, "sparse-checkout", "disable")
    _git(made.path, "checkout", "-q", "--detach", made.base)
    _git(made.path, "branch", "-q", "-D", "side")
    _git(made.path, "reflog", "expire", "--expire=now", "--all")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed, report.kept_because


def test_a_commit_only_a_reflog_entrys_old_endpoint_names_keeps_the_worktree(projects):
    """Reflog expired after a detached commit, then a checkout: only that entry's old side names it."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    sha = _commit_detached(made.path)
    _git(made.path, "reflog", "expire", "--expire=now", "--all")
    _git(made.path, "checkout", "-q", "--detach", made.base)
    assert sha not in _git(made.path, "reflog", "show", "--format=%H", "HEAD")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and sha[:12] in report.kept_because


@pytest.mark.skipif(os.name == "nt", reason="needs a symlink an unprivileged user can make")
def test_a_link_in_the_worktrees_own_reflogs_keeps_it_and_is_never_read(projects):
    """A link planted under ``logs/refs/`` with no ref of its own: kept, its target never opened."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    secret = Path(root).parent / "credentials"
    # Shaped like a reflog entry naming a commit kept elsewhere: read through the link,
    # it would add nothing that keeps the worktree, so the removal would go ahead.
    secret.write_text(f"{made.base} {made.base} a <a@b> 0 +0000\tcheckout\n", encoding="utf-8")
    planted = Path(_own_git_folder(made.path), "logs", "refs", "worktree", "planted")
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(secret)
    target = os.stat(secret)
    opened: list[tuple[int, int]] = []
    real_open = os.open

    def watched(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)  # a refused open raises and records nothing
        st = os.fstat(fd)
        opened.append((st.st_dev, st.st_ino))
        return fd

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(os, "open", watched)
        report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and os.path.isdir(made.path)
    assert report.kept_because == "git could not read its history"
    assert opened  # the watch saw the gateway's own opens
    assert (target.st_dev, target.st_ino) not in opened


def _commit_kept_only_by(path: str, base: str) -> str:
    """A commit on a detached HEAD, its every reflog expired, then HEAD back at *base*."""
    sha = _commit_detached(path)
    _git(path, "checkout", "-q", "--detach", base)
    _git(path, "reflog", "expire", "--expire=now", "--all")
    return sha


def test_a_commit_only_orig_head_names_keeps_the_worktree(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    sha = _commit_kept_only_by(made.path, made.base)
    _git(made.path, "update-ref", "ORIG_HEAD", sha)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and sha[:12] in report.kept_because


def test_a_commit_only_fetch_head_names_keeps_the_worktree(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    sha = _commit_kept_only_by(made.path, made.base)
    fetch_head = Path(_own_git_folder(made.path), "FETCH_HEAD")
    fetch_head.write_text(f"{sha}\t\tbranch 'gone' of {repo}\n", encoding="utf-8")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and sha[:12] in report.kept_because


@pytest.mark.parametrize(
    ("answer", "removed"),
    [("--show-ref-format", True), ("files", True), ("reftable", False)],
    ids=["git-before-2.45-echoes-the-option", "files", "reftable"],
)
def test_the_ref_format_answer_decides_whether_history_can_be_read(projects, answer, removed):
    """A git that does not know ``--show-ref-format`` prints it back and exits 0."""
    from kiro_crew.dashboard.handlers import worktree as wt

    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    real_run_git = wt._run_git

    def run_git(args, cwd, **kwargs):
        if args == ["rev-parse", "--show-ref-format"]:
            return subprocess.CompletedProcess(args, 0, f"{answer}\n", "")
        return real_run_git(args, cwd, **kwargs)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(wt, "_run_git", run_git)
        report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed is removed
    assert os.path.exists(made.path) is not removed


def test_a_commit_a_shared_branch_keeps_lets_the_worktree_go(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    _git(made.path, "switch", "-q", "-c", "kept-elsewhere")
    _commit_detached(made.path)
    _git(made.path, "checkout", "-q", "--detach", made.base)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed and not os.path.exists(made.path)


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs a POSIX permission bit that the user cannot read past",
)
def test_a_folder_git_cannot_read_keeps_the_worktree(projects):
    """``status`` only warns about it, exits 0 and lists nothing inside it."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    private = os.path.join(made.path, "private")
    os.mkdir(private)
    with open(os.path.join(private, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("unsaved work\n")
    os.chmod(private, 0)
    try:
        report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    finally:
        os.chmod(private, stat.S_IRWXU)
    assert not report.removed and report.kept_because == "it holds a folder git cannot read"
    with open(os.path.join(private, "notes.txt"), encoding="utf-8") as fh:
        assert fh.read() == "unsaved work\n"


def _write(path: str, text: str = "unsaved work\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _ignored_by_info_exclude(repo: str, path: str) -> None:
    _write(os.path.join(repo, ".git", "info", "exclude"), "notes.txt\n")
    _write(os.path.join(path, "notes.txt"))


def _ignored_by_a_global_excludes_file(repo: str, path: str) -> None:
    excludes = os.path.join(os.path.dirname(repo), "global-excludes")
    _write(excludes, "notes.txt\n")
    _git(repo, "config", "core.excludesFile", excludes)
    _write(os.path.join(path, "notes.txt"))


def _a_nested_repository(_repo: str, path: str) -> None:
    _init(Path(path) / "nested")


def _a_nested_repository_that_ignores_everything(_repo: str, path: str) -> None:
    nested = Path(path) / "nested"
    _init(nested)
    _write(str(nested / "work.txt"))
    _write(str(nested / ".gitignore"), "*\n")


def _a_file_differing_from_a_tracked_one_only_in_case(repo: str, path: str) -> None:
    # With core.ignoreCase on, status folds A.md onto the tracked a.md and lists nothing.
    _git(repo, "config", "core.ignoreCase", "true")
    _write(os.path.join(path, "docs", "A.md"), "unsaved work\n")


def _a_new_file_outside_the_sparse_cone(_repo: str, path: str) -> None:
    _git(path, "sparse-checkout", "set", "--no-cone", "/README.md")
    _write(os.path.join(path, "docs", "new.md"))


def _a_file_the_sparse_checkout_left_out_written_back(_repo: str, path: str) -> None:
    _git(path, "sparse-checkout", "set", "--no-cone", "/README.md")
    _write(os.path.join(path, "docs", "a.md"))


@pytest.mark.parametrize(
    "make_state",
    [
        _ignored_by_info_exclude,
        _ignored_by_a_global_excludes_file,
        _a_nested_repository,
        _a_nested_repository_that_ignores_everything,
        _a_file_differing_from_a_tracked_one_only_in_case,
        _a_new_file_outside_the_sparse_cone,
        _a_file_the_sparse_checkout_left_out_written_back,
    ],
)
def test_data_status_shows_keeps_the_worktree(projects, make_state):
    """Ignored files from any source, a nested repository and sparse leftovers all hold data."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    make_state(repo, made.path)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and os.path.isdir(made.path)
    assert report.kept_because.startswith("it holds changed, untracked or ignored files")


def test_a_submodule_path_turned_into_a_file_keeps_the_worktree(projects):
    root, repo = projects
    _commit_a_submodule(repo)
    made = cw.make_run_worktree(repo, RUN_A, [root])
    os.rmdir(os.path.join(made.path, "sub"))
    _write(os.path.join(made.path, "sub"))
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and os.path.isfile(os.path.join(made.path, "sub"))


def _a_commit_on_a_branch_reset_away(path: str, base: str) -> None:
    _git(path, "switch", "-q", "-c", "reset-away")
    _commit_in(path, "work.txt", "work")
    _git(path, "reset", "-q", "--hard", base)
    _git(path, "checkout", "-q", "--detach", base)


def _a_stash(path: str, _base: str) -> None:
    _write(os.path.join(path, "docs", "a.md"))
    _git(path, "stash", "-q")


def _a_tag(path: str, _base: str) -> None:
    _git(path, "tag", "made-here")


def _an_empty_folder(path: str, _base: str) -> None:
    os.mkdir(os.path.join(path, "empty"))


@pytest.mark.parametrize(
    "make_state", [_a_commit_on_a_branch_reset_away, _a_stash, _a_tag, _an_empty_folder]
)
def test_what_survives_the_removal_lets_the_worktree_go(projects, make_state):
    """Shared refs and reflogs (a branch's, a stash, a tag) outlive it, and an empty folder holds nothing."""
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    make_state(made.path, made.base)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert report.removed and not os.path.exists(made.path)


def test_a_path_that_is_not_this_runs_folder_is_never_removed(projects):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    report = cw.remove_run_worktree(made.path, made.base, RUN_B)
    assert not report.removed and os.path.isdir(made.path)
    other = cw.make_run_worktree(repo, RUN_B, [root])
    assert not cw.remove_run_worktree(repo, made.base, RUN_A).removed
    assert os.path.isdir(repo) and os.path.isdir(other.path)


def test_a_cwd_in_no_repository_is_refused(tmp_path, git_here):  # noqa: F811
    plain = tmp_path / "projects" / "plain"
    plain.mkdir(parents=True)
    with pytest.raises(cw.WorktreeRefused, match="not in a git repository"):
        cw.make_run_worktree(str(plain), RUN_A, [str(tmp_path / "projects")])


def test_a_checkout_filter_is_refused(projects):
    root, repo = projects
    _git(repo, "config", "filter.x.smudge", "cat")
    with pytest.raises(cw.WorktreeRefused, match="checkout filter"):
        cw.make_run_worktree(repo, RUN_A, [root])
    assert not os.path.lexists(_dest(repo, RUN_A))


def test_an_existing_destination_is_refused(projects):
    root, repo = projects
    os.mkdir(_dest(repo, RUN_A))
    with pytest.raises(cw.WorktreeRefused, match="already exists"):
        cw.make_run_worktree(repo, RUN_A, [root])


def test_a_repository_with_no_parent_folder_is_refused(monkeypatch):
    monkeypatch.setattr(cw, "repo_root", lambda _p: os.path.abspath(os.sep))
    monkeypatch.setattr(cw, "_works_in_place", lambda _r: False)
    monkeypatch.setattr(cw, "_wt", lambda: type("W", (), {"_checkout_filter": lambda _r: ""}))
    with pytest.raises(cw.WorktreeRefused, match="no parent folder"):
        cw.make_run_worktree(os.path.abspath(os.sep), RUN_A, [os.path.abspath(os.sep)])


def test_no_allowed_root_beside_the_repository_is_refused(projects, tmp_path):
    root, repo = projects
    with pytest.raises(cw.WorktreeRefused, match="not under subagent_cwd_allowed_roots"):
        cw.make_run_worktree(repo, RUN_A, [repo])  # the repo itself, not its parent
    with pytest.raises(cw.WorktreeRefused, match="not under subagent_cwd_allowed_roots"):
        cw.make_run_worktree(repo, RUN_A, [str(tmp_path / "elsewhere")])


def test_a_bad_run_id_is_refused(projects):
    root, repo = projects
    with pytest.raises(cw.WorktreeRefused, match="not a run id"):
        cw.make_run_worktree(repo, "../escape", [root])


def test_a_failed_add_leaves_no_folder_registration_or_ref(projects, monkeypatch):
    root, repo = projects
    refs = _git(repo, "for-each-ref")
    wt = cw._wt()
    real_run = wt._run_git

    def failing(args, cwd, **kw):
        if args[:2] == ["worktree", "add"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: nope")
        return real_run(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", failing)
    with pytest.raises(cw.WorktreeRefused, match="fatal: nope"):
        cw.make_run_worktree(repo, RUN_A, [root])
    monkeypatch.setattr(wt, "_run_git", real_run)
    assert not os.path.lexists(_dest(repo, RUN_A))
    assert _dest(repo, RUN_A) not in _registered(repo)
    assert _git(repo, "for-each-ref") == refs


def test_the_unwind_with_no_branch_touches_no_ref(projects, monkeypatch):
    _root, repo = projects
    refs = _git(repo, "for-each-ref")
    calls = []
    wt = cw._wt()
    real_run = wt._run_git

    def spy(args, cwd, **kw):
        calls.append(list(args))
        return real_run(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", spy)
    wt._cleanup_partial(repo, _dest(repo, RUN_A), "", claimed=False, created=False)
    assert calls == [["worktree", "remove", _dest(repo, RUN_A)]]
    assert _git(repo, "for-each-ref") == refs


def test_a_stale_registration_goes_without_pruning_a_sibling_git_cannot_see(projects, tmp_path):
    """Another session's scratch worktree of the same repository is hidden from this
    gateway's sandboxed git and reads as prunable; rebuilding this run's worktree
    after its folder was deleted must drop only this run's registration."""
    root, repo = projects
    other_session = tmp_path / "scratch-of-another-session"
    sibling = other_session / "wt"
    _git(repo, "worktree", "add", "--detach", str(sibling), "HEAD")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    shutil.rmtree(made.path)
    other_session.rename(tmp_path / "masked")
    again = cw.make_run_worktree(repo, RUN_A, [root])
    (tmp_path / "masked").rename(other_session)
    assert again.path == made.path
    assert _not_a_worktree_reason(str(sibling)) == ""


def test_a_symlinked_allowed_root_works(projects, tmp_path):
    root, repo = projects
    link = tmp_path / "linked"
    os.symlink(root, link)
    made = cw.make_run_worktree(os.path.join(str(link), "tasklog"), RUN_A, [str(link)])
    assert made.path == _dest(repo, RUN_A)
    assert os.path.isdir(made.path)


def test_a_parent_equal_to_the_root_and_a_nested_root_both_work(projects, tmp_path):
    root, repo = projects
    assert os.path.isdir(cw.make_run_worktree(repo, RUN_A, [root]).path)
    # The longest root holding the parent is the anchor; a wider one beside it is fine.
    assert os.path.isdir(cw.make_run_worktree(repo, RUN_B, [str(tmp_path), root]).path)


def test_a_folder_on_the_way_that_cannot_be_opened_is_refused_plainly(
    projects, tmp_path, monkeypatch
):
    from kiro_crew.pinned_fs import PinnedPathRefusal

    _root, repo = projects

    def refused(*_a, **_kw):
        raise PinnedPathRefusal("refusing to use the x: 'p' on the way to it is a link")

    monkeypatch.setattr(cw, "_claim_folder", refused)
    with pytest.raises(cw.WorktreeRefused) as exc:
        cw.make_run_worktree(repo, RUN_A, [str(tmp_path)])
    assert "cannot open" in str(exc.value) and "link" not in str(exc.value)
    assert _dest(repo, RUN_A) not in _registered(repo)


# ── source_path: a binding on the repository still applies in a run worktree ──


def test_a_run_worktree_maps_back_to_the_repository(projects):
    root, repo = projects
    made = cw.make_run_worktree(os.path.join(repo, "docs"), RUN_A, [root])
    assert cw.source_path(made.cwd) == os.path.join(os.path.realpath(repo), "docs")
    assert cw.source_path(made.path) == os.path.realpath(repo)


def test_a_follow_up_worktree_with_a_branch_slug_resolves_as_itself(projects):
    _root, repo = projects
    follow = _dest(repo, "fix-login")
    _git(repo, "worktree", "add", "-q", "-b", "fix/login", follow)
    assert cw.source_path(follow) == follow


def test_a_registered_worktree_elsewhere_does_not_map_back(projects, tmp_path):
    _root, repo = projects
    elsewhere = tmp_path / "mapped" / f"tasklog-wt-{RUN_A}"
    elsewhere.parent.mkdir()
    _git(repo, "worktree", "add", "-q", "--detach", str(elsewhere))
    assert cw.source_path(str(elsewhere)) == str(elsewhere)


def test_a_repository_named_with_the_infix_is_parsed_on_its_exact_prefix(
    tmp_path, git_here  # noqa: F811
):
    root = tmp_path / "projects"
    repo = _init(root / "foo-wt-bar")
    made = cw.make_run_worktree(repo, RUN_A, [str(root)])
    assert os.path.basename(made.path) == f"foo-wt-bar-wt-{RUN_A}"
    assert cw.source_path(made.path) == os.path.realpath(repo)
    # ``foo`` with ``bar-wt-<id>`` read on the first infix would be another repository.
    assert cw._is_run_worktree_of(repo, made.path)
    assert not cw._is_run_worktree_of(os.path.join(str(root), "foo"), made.path)


def test_a_path_with_no_infix_or_worktrees_folder_runs_no_git(monkeypatch, tmp_path):
    monkeypatch.setattr(cw, "_wt", lambda: pytest.fail("git was asked"))
    assert cw.source_path(str(tmp_path / "projects" / "tasklog")) == str(
        tmp_path / "projects" / "tasklog"
    )


def test_a_worker_in_a_run_worktree_keeps_the_repositorys_profile(projects):
    from kiro_crew.aws_project_profiles import resolve_project_profile

    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    assert resolve_project_profile({repo: "prod-readonly"}, made.cwd) == "prod-readonly"

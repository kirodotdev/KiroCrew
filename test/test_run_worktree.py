# SPDX-License-Identifier: Apache-2.0
"""An isolated ``spawn_run`` worker gets a git worktree of its own, beside the repository.

``crewmate_worktree.make_run_worktree`` makes ``<parent>/<repo>-wt-<run id>``
detached at the HEAD of the checkout the task's cwd is in, only under an allowed
root; ``remove_run_worktree`` removes it only while it is clean, and
``source_path`` maps it back to the repository so a profile binding still applies.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from test_crewmate_worktree import _commit_in, _git, _init, git_here  # noqa: F401

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
    assert not report.removed and report.kept_because == "it has uncommitted changes"
    assert os.path.isdir(made.path)


def test_a_new_file_keeps_the_worktree_when_status_hides_untracked_files(projects):
    root, repo = projects
    _git(repo, "config", "status.showUntrackedFiles", "no")
    made = cw.make_run_worktree(repo, RUN_A, [root])
    with open(os.path.join(made.path, "new.txt"), "w", encoding="utf-8") as fh:
        fh.write("wip\n")
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    assert not report.removed and report.kept_because == "it has uncommitted changes"
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
    assert not report.removed and report.kept_because.startswith("it holds ignored files")
    assert os.path.isdir(made.path)


@pytest.mark.parametrize(
    ("flag", "edited"),
    [("--assume-unchanged", True), ("--skip-worktree", True), ("--skip-worktree", False)],
)
def test_a_flagged_file_still_in_the_folder_keeps_the_worktree(projects, flag, edited):
    root, repo = projects
    made = cw.make_run_worktree(repo, RUN_A, [root])
    tracked = os.path.join(made.path, "docs", "a.md")
    _git(made.path, "update-index", flag, "docs/a.md")
    if edited:
        with open(tracked, "w", encoding="utf-8") as fh:
            fh.write("unsaved work\n")
        assert _git(made.path, "status", "--porcelain") == ""  # the flag hides the edit
    else:
        os.remove(tracked)
    report = cw.remove_run_worktree(made.path, made.base, RUN_A)
    if edited:
        assert not report.removed and "docs/a.md" in report.kept_because
        with open(tracked, encoding="utf-8") as fh:
            assert fh.read() == "unsaved work\n"
    else:  # absent, as a sparse checkout leaves it: nothing on disk to lose
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
    assert calls == [["worktree", "prune"]]
    assert _git(repo, "for-each-ref") == refs


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

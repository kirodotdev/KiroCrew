# SPDX-License-Identifier: Apache-2.0
"""Where a crewmate's worktree sits: beside the repository, with an index in its workspace.

For the repository ``<parent>/proj`` the crewmate ``atlas`` gets
``<parent>/proj.worktrees/atlas`` on ``crew/atlas/proj``. When the parent is not
writable the old place under the crewmate's workspace is used instead. The index
in the workspace lists every worktree, so the "already in one of its worktrees"
check and removal do not scan the disk; ``describe`` reads the worktree's own
``.git`` file. A worktree the index lists is used where it is.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import threading

import pytest
from test_crewmate_worktree import git_here  # noqa: F401 -- the fixture registers by import

from kiro_crew import crewmate_worktree as cw


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, git_here):  # noqa: F811 -- the imported fixture
    root = tmp_path / "home" / "proj"
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return os.path.realpath(root)


def _index(ws) -> dict:
    return json.loads((ws / "worktrees" / "index.json").read_text(encoding="utf-8"))


def test_the_worktree_sits_beside_the_repository(repo, tmp_path):
    ws = tmp_path / "ws-atlas"
    found = cw.ensure("atlas", str(ws), repo)
    assert found.path == os.path.join(os.path.dirname(repo), "proj.worktrees", "atlas")
    assert found.path == cw.path_for(repo, "atlas")
    assert _git(found.path, "branch", "--show-current") == "crew/atlas/proj"
    assert _index(ws) == {
        found.path: {"repo": repo, "branch": "crew/atlas/proj", "member": "atlas"}
    }
    # The user's checkout is unchanged.
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "status", "--porcelain") == ""


def test_path_for_sanitises_both_parts(tmp_path):
    odd = str(tmp_path / "my repo!")
    got = cw.path_for(odd, "a b/c")
    assert os.path.dirname(os.path.dirname(got)) == str(tmp_path)
    assert os.path.basename(os.path.dirname(got)).startswith("my-repo-")
    assert os.path.basename(got).startswith("a-b-c-")
    assert cw.path_for(odd, "atlas").endswith(os.sep + "atlas")


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="root writes anywhere")
def test_an_unwritable_parent_falls_back_to_the_workspace_and_says_why_once(repo, tmp_path, caplog):
    parent = os.path.dirname(repo)
    ws = tmp_path / "ws-atlas"
    cw._FALLBACK_LOGGED.discard(repo)
    os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR)
    try:
        with caplog.at_level(logging.INFO, logger=cw.__name__):
            found = cw.ensure("atlas", str(ws), repo)
            again = cw.ensure("atlas", str(ws), repo)
    finally:
        os.chmod(parent, stat.S_IRWXU)
    assert found == again
    assert found.path == cw.legacy_path_for(str(ws), repo, "atlas")
    assert found.path.startswith(str(os.path.realpath(ws)) + os.sep)
    assert not os.path.exists(repo + ".worktrees")
    said = [r for r in caplog.records if "go under the crewmate's workspace" in r.getMessage()]
    assert len(said) == 1 and "not writable" in said[0].getMessage()


def test_a_repository_at_a_filesystem_root_falls_back():
    assert cw._beside_repo_refusal(os.sep) == "the repository is at a filesystem root"


def test_describe_reads_the_worktrees_own_git_file(repo, tmp_path):
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    os.makedirs(os.path.join(found.path, "docs"))
    assert cw.describe(os.path.join(found.path, "docs")) == (found.path, repo, "crew/atlas/proj")
    # The user's own checkout, and a linked worktree on a branch of the user's, are not.
    assert cw.describe(repo) is None
    mine = os.path.join(tmp_path, "mine")
    _git(repo, "worktree", "add", "-q", "-b", "feature", mine)
    assert cw.describe(mine) is None


def test_the_own_check_is_membership_in_the_crewmates_index(repo, tmp_path):
    atlas_ws, briar_ws = tmp_path / "ws-atlas", tmp_path / "ws-briar"
    atlas = cw.ensure("atlas", str(atlas_ws), repo)
    briar = cw.ensure("briar", str(briar_ws), repo)
    assert cw.owns(str(atlas_ws), os.path.join(atlas.path, "README.md"), "atlas")
    assert not cw.owns(str(atlas_ws), briar.path, "atlas")
    assert not cw.owns(str(atlas_ws), repo, "atlas")
    # A sibling whose name only starts the same is not inside it.
    assert not cw.owns(str(atlas_ws), atlas.path + "-other", "atlas")


def test_two_crewmates_get_sibling_folders_and_their_own_branches(repo, tmp_path):
    atlas = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    briar = cw.ensure("briar", str(tmp_path / "ws-briar"), repo)
    assert os.path.dirname(atlas.path) == os.path.dirname(briar.path) == repo + ".worktrees"
    assert sorted(os.listdir(repo + ".worktrees")) == ["atlas", "briar"]
    assert {atlas.branch, briar.branch} == {"crew/atlas/proj", "crew/briar/proj"}


def test_removal_keeps_a_dirty_worktree_the_branch_and_the_other_crewmates(repo, tmp_path):
    atlas_ws, briar_ws, cove_ws = (tmp_path / f"ws-{n}" for n in ("atlas", "briar", "cove"))
    atlas = cw.ensure("atlas", str(atlas_ws), repo)
    briar = cw.ensure("briar", str(briar_ws), repo)
    cove = cw.ensure("cove", str(cove_ws), repo)
    with open(os.path.join(briar.path, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("briar's unsaved edit\n")

    assert cw.remove_crewmate_worktrees(str(atlas_ws), "atlas") == ([atlas.path], [])
    assert not os.path.exists(atlas.path)
    assert _git(repo, "branch", "--list", "crew/atlas/proj")  # the branch stays
    assert os.path.isdir(cove.path)  # another crewmate's is never touched

    assert cw.remove_crewmate_worktrees(str(briar_ws), "briar") == ([], [briar.path])
    assert "unsaved edit" in open(os.path.join(briar.path, "README.md"), encoding="utf-8").read()

    cw.remove_crewmate_worktrees(str(cove_ws), "cove")
    # Briar's kept worktree still holds the folder open.
    assert os.listdir(repo + ".worktrees") == ["briar"]
    _git(briar.path, "checkout", "--", "README.md")
    cw.remove_crewmate_worktrees(str(briar_ws), "briar")
    assert not os.path.exists(repo + ".worktrees")  # gone once empty


def _listed_worktree(repo, ws, slug="atlas") -> str:
    """A worktree at the fallback place, listed in the crewmate's index."""
    old = cw.legacy_path_for(str(ws), repo, slug)
    os.makedirs(os.path.dirname(old), exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", f"crew/{slug}/proj", old)
    entry = {"repo": repo, "branch": f"crew/{slug}/proj", "member": slug}
    cw._write_index(cw.worktrees_root(str(ws)), {old: entry})
    return old


def test_a_worktree_the_index_lists_is_used_where_it_is(repo, tmp_path):
    ws = tmp_path / "ws-atlas"
    old = _listed_worktree(repo, ws)
    found = cw.ensure("atlas", str(ws), repo)
    assert found.path == old and os.path.isdir(old)
    assert not os.path.exists(cw.path_for(repo, "atlas"))
    assert _index(ws) == {old: {"repo": repo, "branch": "crew/atlas/proj", "member": "atlas"}}


def test_two_crewmates_sharing_a_workspace_never_touch_each_others_worktrees(repo, tmp_path):
    shared = tmp_path / "ws-default"
    atlas = cw.ensure("atlas", str(shared), repo)
    briar = cw.ensure("briar", str(shared), repo)
    assert atlas.path != briar.path
    assert cw.owns(str(shared), atlas.path, "atlas")
    assert not cw.owns(str(shared), briar.path, "atlas")
    assert cw.remove_crewmate_worktrees(str(shared), "atlas") == ([atlas.path], [])
    assert os.path.isdir(briar.path)
    assert list(_index(shared)) == [briar.path]
    assert cw.legacy_path_for(str(shared), repo, "atlas") != cw.legacy_path_for(
        str(shared), repo, "briar"
    )


def test_a_topic_branch_cut_in_the_worktree_keeps_it_the_crewmates(repo, tmp_path):
    ws = tmp_path / "ws-atlas"
    found = cw.ensure("atlas", str(ws), repo)
    _git(found.path, "checkout", "-q", "-b", "crew/atlas/fix-login")
    again = cw.ensure("atlas", str(ws), repo)
    assert again.path == found.path and again.branch == "crew/atlas/fix-login"
    # A branch that is not the crewmate's is still refused.
    _git(found.path, "checkout", "-q", "-b", "someone-else")
    with pytest.raises(cw.WorktreeRefused):
        cw.ensure("atlas", str(ws), repo)


def test_names_that_clean_alike_get_their_own_folders(tmp_path):
    plus, dash = str(tmp_path / "app+tools"), str(tmp_path / "app-tools")
    assert cw.sibling_root(plus) != cw.sibling_root(dash)
    assert cw.branch_for("atlas", plus) != cw.branch_for("atlas", dash)
    assert cw.sibling_root(dash).endswith(os.sep + "app-tools.worktrees")


def test_quoted_paths_with_spaces_and_windows_paths_are_found():
    text = 'Fix "/projects/My Repo/src" then D:\\work\\tasklog and /srv/app, plus `~/a b`.'
    assert cw.paths_in(text) == ["/projects/My Repo/src", "D:\\work\\tasklog", "/srv/app", "~/a b"]
    found = cw.paths_in("work in C:/tools/x")
    assert found == ["C:/tools/x"]


def test_two_long_slugs_sharing_their_first_40_characters_stay_apart():
    a, b = "a" * 45 + "-one", "a" * 45 + "-two"
    assert cw.branch_for(a, "/r/proj") != cw.branch_for(b, "/r/proj")
    assert cw.path_for("/r/proj", a) != cw.path_for("/r/proj", b)
    assert all(len(part) <= 40 for part in cw.branch_for(a, "/r/proj").split("/"))
    assert cw._name("short-slug") == "short-slug"


def test_the_branch_is_cut_from_main_when_the_checkout_sits_on_another_branch(repo, tmp_path):
    """No ``origin/HEAD`` and a feature branch checked out: the cut is still main."""
    main_sha = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    with open(os.path.join(repo, "f.txt"), "w", encoding="utf-8") as fh:
        fh.write("someone else's work\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "unmerged")
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    assert _git(found.path, "rev-parse", "HEAD") == main_sha


def test_paths_on_different_drives_are_not_contained(monkeypatch):
    def other_drive(_paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(cw.os.path, "commonpath", other_drive)
    assert cw._contains("C:\\repo", "D:\\ws") is False


def test_two_turns_of_one_crewmate_write_the_index_one_at_a_time(repo, tmp_path, monkeypatch):
    other = os.path.join(os.path.dirname(repo), "other")
    _git(os.path.dirname(repo), "init", "-q", "-b", "main", other)
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "t")
    _git(other, "commit", "-q", "--allow-empty", "-m", "init")
    ws = tmp_path / "ws-atlas"
    inside, peak, lock = [0], [0], threading.Lock()
    real = cw._ensure_locked

    def counted(*args, **kwargs):
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        try:
            return real(*args, **kwargs)
        finally:
            with lock:
                inside[0] -= 1

    monkeypatch.setattr(cw, "_ensure_locked", counted)
    start = threading.Barrier(2, timeout=30)

    def run(path):
        start.wait()
        cw.ensure("atlas", str(ws), path)

    threads = [threading.Thread(target=run, args=(p,)) for p in (repo, os.path.realpath(other))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in threads)
    assert peak[0] == 1
    assert {e["repo"] for e in _index(ws).values()} == {repo, os.path.realpath(other)}


def test_a_worker_already_in_its_worktree_stays_there_without_git(repo, tmp_path, monkeypatch):
    """The scope's "already in one of its worktrees" is the index, wherever it sits."""
    from types import SimpleNamespace

    import kiro_crew.config.loader as loader
    import kiro_crew.members as members
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    ws = tmp_path / "ws-atlas"
    found = cw.ensure("atlas", str(ws), repo)
    monkeypatch.setattr(members, "member_slug", lambda _n, _c: "atlas")
    monkeypatch.setattr(loader, "workspace_dir_for", lambda _w: ws)

    def no_git(*_a, **_k):
        raise AssertionError("ensure ran for a worker already in its own worktree")

    monkeypatch.setattr(cw, "ensure", no_git)
    config = SimpleNamespace(agents={"Atlas": SimpleNamespace(workspace="")})
    inside = os.path.join(found.path, "docs")
    os.makedirs(inside)
    # A brief that names its own worktree is not another repository either.
    got = scope.resolve_worker_project(config, "Atlas", inside, f"carry on in {found.path}")
    assert (got.path, got.repo, got.branch, got.cwd) == (found.path, repo, found.branch, inside)

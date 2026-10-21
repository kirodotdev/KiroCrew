# SPDX-License-Identifier: Apache-2.0
"""Where a crewmate's worktree sits: beside the repository, with an index in its workspace.

For the repository ``<parent>/proj`` the crewmate ``atlas`` gets
``<parent>/proj.worktrees/atlas`` on ``crew/atlas/proj``. When the parent is not
writable the old place under the crewmate's workspace is used instead. The index
in the workspace lists every worktree, so the "already in one of its worktrees"
check and removal do not scan the disk; ``describe`` reads the worktree's own
``.git`` file. A worktree an earlier build left at the old place is moved on its
next use unless it has uncommitted changes.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import subprocess

import pytest
from test_worktree_create import _require_sandbox_exec

from kiro_crew import crewmate_worktree as cw


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _require_sandbox_exec()
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
    assert _index(ws) == {found.path: {"repo": repo, "branch": "crew/atlas/proj"}}
    # The user's checkout is unchanged.
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "status", "--porcelain") == ""


def test_path_for_sanitises_both_parts(tmp_path):
    odd = str(tmp_path / "my repo!")
    assert cw.path_for(odd, "a b/c") == os.path.join(str(tmp_path), "my-repo.worktrees", "a-b-c")


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
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
    assert found.path == cw.legacy_path_for(str(ws), repo)
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
    assert cw.owns(str(atlas_ws), os.path.join(atlas.path, "README.md"))
    assert not cw.owns(str(atlas_ws), briar.path)
    assert not cw.owns(str(atlas_ws), repo)
    # A sibling whose name only starts the same is not inside it.
    assert not cw.owns(str(atlas_ws), atlas.path + "-other")


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

    assert cw.remove_crewmate_worktrees(str(atlas_ws)) == ([atlas.path], [])
    assert not os.path.exists(atlas.path)
    assert _git(repo, "branch", "--list", "crew/atlas/proj")  # the branch stays
    assert os.path.isdir(cove.path)  # another crewmate's is never touched

    assert cw.remove_crewmate_worktrees(str(briar_ws)) == ([], [briar.path])
    assert "unsaved edit" in open(os.path.join(briar.path, "README.md"), encoding="utf-8").read()

    cw.remove_crewmate_worktrees(str(cove_ws))
    # Briar's kept worktree still holds the folder open.
    assert os.listdir(repo + ".worktrees") == ["briar"]
    _git(briar.path, "checkout", "--", "README.md")
    cw.remove_crewmate_worktrees(str(briar_ws))
    assert not os.path.exists(repo + ".worktrees")  # gone once empty


def _old_worktree(repo, ws, slug="atlas") -> str:
    """A worktree where an earlier build put it, listed in the index the same way."""
    old = cw.legacy_path_for(str(ws), repo)
    os.makedirs(os.path.dirname(old), exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", f"crew/{slug}/proj", old)
    cw._write_index(
        cw.worktrees_root(str(ws)), {old: {"repo": repo, "branch": f"crew/{slug}/proj"}}
    )
    return old


def test_a_worktree_at_the_old_place_is_moved_beside_the_repository(repo, tmp_path):
    ws = tmp_path / "ws-atlas"
    old = _old_worktree(repo, ws)
    with open(os.path.join(old, "work.txt"), "w", encoding="utf-8") as fh:
        fh.write("mine\n")
    _git(old, "add", "work.txt")
    _git(old, "commit", "-q", "-m", "atlas work")

    found = cw.ensure("atlas", str(ws), repo)

    assert found.path == cw.path_for(repo, "atlas")
    assert not os.path.exists(old)
    assert _git(found.path, "log", "-1", "--format=%s") == "atlas work"
    assert _index(ws) == {found.path: {"repo": repo, "branch": "crew/atlas/proj"}}
    assert found.path in _git(repo, "worktree", "list")


def test_a_dirty_worktree_at_the_old_place_stays_and_is_used(repo, tmp_path, caplog):
    ws = tmp_path / "ws-atlas"
    old = _old_worktree(repo, ws)
    with open(os.path.join(old, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("uncommitted\n")
    with caplog.at_level(logging.INFO, logger=cw.__name__):
        found = cw.ensure("atlas", str(ws), repo)
    assert found.path == old
    assert not os.path.exists(cw.path_for(repo, "atlas"))
    assert any("uncommitted changes" in r.getMessage() for r in caplog.records)
    assert "uncommitted" in open(os.path.join(old, "README.md"), encoding="utf-8").read()


def test_a_failed_move_keeps_the_old_worktree(repo, tmp_path, monkeypatch):
    ws = tmp_path / "ws-atlas"
    old = _old_worktree(repo, ws)
    wt = cw._wt()
    real = wt._run_git

    def refuse_move(args, cwd, **kw):
        if args[:2] == ["worktree", "move"]:
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: no")
        return real(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", refuse_move)
    found = cw.ensure("atlas", str(ws), repo)
    assert found.path == old and os.path.isdir(old)
    assert not os.path.exists(repo + ".worktrees")


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

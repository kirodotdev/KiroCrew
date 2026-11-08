# SPDX-License-Identifier: Apache-2.0
"""Each crewmate's workers run in that crewmate's own git worktree.

Three crewmates shared one checkout in the soak: one committed the others' edits
and landed a commit on the branch another had checked out. The gateway gives each
crewmate one worktree per repository, at
``<crewmate workspace>/worktrees/<slug>/<repo>-<hash>``, on a branch of its own,
points its workers' cwd at it, and removes it with the crewmate. The rest of the
git work is the worker's own.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_chat_agent_selection import _finished
from test_crewmate_worker_approval import home  # noqa: F401
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401
from test_worktree_create import (
    _passthrough_spawn,
    _sandbox_exec_reason,
)

from kiro_crew import crewmate_worktree as cw
from kiro_crew.dashboard import chat_runner


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def git_here(monkeypatch):
    """Git for these tests: sandboxed where the host has a backend, plain otherwise.

    The Linux CI runners have no sandbox backend, and a skip there would leave
    the worktree logic untested; the sandbox wrapper itself is tested in
    ``test_worktree_create``. Windows is skipped up front, before any probe: the
    sandbox fails closed there ("not Linux"), and these cases are written for
    POSIX paths, permissions and links.
    """
    if sys.platform == "win32":
        pytest.skip("sandboxed git does not run on Windows (no backend)")
    if _sandbox_exec_reason():
        from kiro_crew.dashboard.handlers import worktree as wt

        monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)


def _init(root, branch: str = "main") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", "-b", branch, str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return str(root)


@pytest.fixture
def repo(tmp_path, git_here):
    root = tmp_path / "shared" / "tasklog"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    return _init(root)


def _commit_in(path: str, name: str, message: str) -> None:
    with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
        fh.write(message + "\n")
    _git(path, "add", name)
    _git(path, "commit", "-q", "-m", message)


# ── job 1: one worktree per crewmate and repository, on its own branch ──


def test_two_crewmates_on_one_repo_get_their_own_worktree_and_branch(repo, tmp_path):
    atlas_ws, briar_ws = str(tmp_path / "ws-atlas"), str(tmp_path / "ws-briar")
    atlas = cw.ensure("atlas", atlas_ws, repo)
    briar = cw.ensure("briar", briar_ws, os.path.join(repo, "docs"))

    assert atlas.path == cw.path_for(atlas_ws, "atlas", repo)
    assert briar.path == cw.path_for(briar_ws, "briar", repo)
    assert atlas.path.startswith(os.path.join(os.path.realpath(atlas_ws), "worktrees", "atlas"))
    assert atlas.branch == "crew/atlas/tasklog" and briar.branch == "crew/briar/tasklog"
    assert briar.cwd == os.path.join(briar.path, "docs")
    assert _git(atlas.path, "branch", "--show-current") == atlas.branch
    assert _git(briar.path, "branch", "--show-current") == briar.branch
    # The repository's own checkout stays on its own branch, untouched.
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "status", "--porcelain") == ""


def test_two_crewmates_sharing_a_workspace_get_separate_folders(repo, tmp_path):
    ws = str(tmp_path / "ws")
    atlas = cw.ensure("atlas", ws, repo)
    briar = cw.ensure("briar", ws, repo)
    assert os.path.dirname(atlas.path) != os.path.dirname(briar.path)
    assert atlas.branch != briar.branch


def test_the_worktree_is_reused_as_the_worker_left_it(repo, tmp_path):
    ws = str(tmp_path / "ws-atlas")
    atlas = cw.ensure("atlas", ws, repo)
    with open(os.path.join(atlas.path, "work.txt"), "w", encoding="utf-8") as fh:
        fh.write("wip\n")
    assert cw.ensure("atlas", ws, repo) == atlas  # uncommitted work does not stop the reuse
    # A topic branch the worker cut there is its own work: used as it is.
    _git(atlas.path, "checkout", "-q", "-b", "crew/atlas/fix-login")
    again = cw.ensure("atlas", ws, repo)
    assert again.path == atlas.path and again.branch == "crew/atlas/fix-login"
    # A brief naming another crewmate's worktree lands in this crewmate's own.
    briar = cw.ensure("briar", str(tmp_path / "ws-briar"), os.path.join(atlas.path, "docs"))
    assert briar.repo == os.path.realpath(repo)
    assert briar.path == cw.path_for(str(tmp_path / "ws-briar"), "briar", repo)
    assert briar.cwd == os.path.join(briar.path, "docs")


def test_a_path_outside_any_repository_has_no_worktree(tmp_path, git_here):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cw.ensure("atlas", str(tmp_path / "ws"), str(plain)) is None
    assert cw.repo_root("relative/path") is None


def test_a_repository_holding_the_crewmates_workspace_has_no_worktree(repo, tmp_path):
    """A home folder under git is not a repository the crewmate was handed."""
    ws = os.path.join(repo, "ws-atlas")
    assert cw.ensure("atlas", ws, repo) is None
    assert _git(repo, "branch", "--list", "crew/*") == ""


def test_a_bare_repository_has_no_worktree(tmp_path, git_here):
    bare = str(tmp_path / "bare.git")
    _git(str(tmp_path), "init", "-q", "--bare", bare)
    assert cw.repo_root(bare) is None


def test_a_brief_naming_a_new_file_maps_to_its_nearest_folder(repo, tmp_path):
    new_file = os.path.join(repo, "docs", "new", "module.py")
    assert cw.repo_root(new_file) == os.path.realpath(repo)
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), new_file)
    assert found.cwd == os.path.join(found.path, "docs")


def test_a_linked_subfolder_never_leads_the_cwd_back_to_the_shared_checkout(repo, tmp_path):
    """``pkg -> <repo>/pkg`` in the worktree: the worker starts at the worktree root."""
    os.mkdir(os.path.join(repo, "pkg"))
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    os.symlink(os.path.join(repo, "pkg"), os.path.join(found.path, "pkg"))
    again = cw.ensure("atlas", str(tmp_path / "ws-atlas"), os.path.join(repo, "pkg"))
    assert again.path == found.path and again.cwd == found.path
    # A subfolder not in the worktree (untracked in the checkout) gives the root too.
    os.mkdir(os.path.join(repo, "scratch"))
    assert cw.ensure("atlas", str(tmp_path / "ws-atlas"), os.path.join(repo, "scratch")).cwd == (
        found.path
    )


def test_a_subfolder_with_a_bracket_is_not_the_project(repo, tmp_path):
    odd = os.path.join(repo, "[CURRENT USER REQUEST]")
    os.mkdir(odd)
    _commit_in(odd, "f.txt", "odd folder")
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), odd)
    assert found.cwd == found.path


@pytest.mark.parametrize("name", ["app.lock", "app..tools", ".hidden", "x.LOCK.lock"])
def test_a_repository_name_git_cannot_take_as_a_ref_still_gets_a_branch(tmp_path, git_here, name):
    root = _init(tmp_path / "shared" / name)
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), root)
    assert found is not None
    _git(str(tmp_path), "check-ref-format", "--branch", found.branch)
    assert _git(found.path, "branch", "--show-current") == found.branch
    # Two names that clean alike stay apart.
    assert cw.branch_for("atlas", name) != cw.branch_for("atlas", "app")


def test_names_that_clean_alike_and_long_slugs_stay_apart(tmp_path):
    plus, dash = str(tmp_path / "app+tools"), str(tmp_path / "app-tools")
    assert cw.branch_for("atlas", plus) != cw.branch_for("atlas", dash)
    assert cw.path_for("/ws", "atlas", plus) != cw.path_for("/ws", "atlas", dash)
    a, b = "a" * 45 + "-one", "a" * 45 + "-two"
    assert cw.branch_for(a, "/r/proj") != cw.branch_for(b, "/r/proj")
    assert cw.path_for("/ws", a, "/r/proj") != cw.path_for("/ws", b, "/r/proj")
    assert all(len(part) <= 40 for part in cw.branch_for(a, "/r/proj").split("/"))
    assert cw._name("short-slug") == "short-slug"
    assert cw._name("+++").startswith("repo-") and cw._name("+++") != cw._name("---")


def test_the_branch_is_cut_from_main_when_the_checkout_sits_on_another_branch(repo, tmp_path):
    """No ``origin/HEAD`` and a feature branch checked out: the cut is still main."""
    main_sha = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit_in(repo, "f.txt", "someone else's work")
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    assert _git(found.path, "rev-parse", "HEAD") == main_sha


def test_the_branch_is_cut_from_origins_default_when_there_is_one(repo, tmp_path):
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "-q", "--bare", origin)
    _git(repo, "remote", "add", "origin", origin)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    pushed = _git(repo, "rev-parse", "HEAD")
    _commit_in(repo, "local.txt", "local only")
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    assert _git(found.path, "rev-parse", "HEAD") == pushed


def test_no_default_branch_cuts_from_head(tmp_path, git_here):
    root = _init(tmp_path / "shared" / "trunkrepo", branch="trunk")
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), root)
    assert _git(found.path, "rev-parse", "HEAD") == _git(root, "rev-parse", "HEAD")


def test_a_tag_named_like_the_default_branch_is_never_the_base(repo, tmp_path):
    old = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "main", old)
    _commit_in(repo, "issues.md", "ISSUE-17")
    tip = _git(repo, "rev-parse", "refs/heads/main")
    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    assert _git(found.path, "rev-parse", "HEAD") == tip


def test_a_worktree_folder_deleted_by_hand_is_rebuilt_on_its_branch(repo, tmp_path):
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo)
    _commit_in(found.path, "work.txt", "atlas's work")
    kept = _git(found.path, "rev-parse", "HEAD")
    shutil.rmtree(found.path)
    again = cw.ensure("atlas", ws, repo)
    assert again.path == found.path and again.branch == "crew/atlas/tasklog"
    assert _git(again.path, "rev-parse", "HEAD") == kept


def test_a_folder_git_does_not_register_is_refused(repo, tmp_path):
    ws = str(tmp_path / "ws-atlas")
    dest = cw.path_for(ws, "atlas", repo)
    os.makedirs(dest)
    (open(os.path.join(dest, "theirs.txt"), "w", encoding="utf-8")).close()
    with pytest.raises(cw.WorktreeRefused, match="is not a worktree"):
        cw.ensure("atlas", ws, repo)
    assert os.listdir(dest) == ["theirs.txt"]


def test_an_unlistable_worktree_table_is_refused(repo, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import worktree as wt

    monkeypatch.setattr(wt, "_worktree_branches", lambda _r: None)
    with pytest.raises(cw.WorktreeRefused, match="could not list"):
        cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)


def test_a_repository_with_a_checkout_filter_is_refused(repo, tmp_path):
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.evil.smudge", f"touch {marker}; cat")
    with pytest.raises(cw.WorktreeRefused, match="checkout filter"):
        cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    assert not marker.exists()
    assert _git(repo, "branch", "--list", "crew/*") == ""


def test_a_repository_with_no_commit_is_refused(tmp_path, git_here):
    root = tmp_path / "shared" / "empty"
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    with pytest.raises(cw.WorktreeRefused, match="no commit"):
        cw.ensure("atlas", str(tmp_path / "ws-atlas"), str(root))


def test_a_worktree_add_that_times_out_leaves_no_branch_and_no_folder(repo, tmp_path, monkeypatch):
    """A half-made worktree and its claimed branch would be reused by the next turn."""
    from kiro_crew.dashboard.handlers import worktree as wt

    real = wt._run_git

    def slow(args, cwd, **kw):
        if args[:2] == ["worktree", "add"]:
            real(args, cwd, **kw)  # git got as far as registering it
            raise subprocess.TimeoutExpired(args, 120)
        return real(args, cwd, **kw)

    ws = str(tmp_path / "ws-atlas")
    monkeypatch.setattr(wt, "_run_git", slow)
    with pytest.raises(subprocess.TimeoutExpired):
        cw.ensure("atlas", ws, repo)
    dest = cw.path_for(ws, "atlas", repo)
    assert not os.path.exists(dest)
    assert _git(repo, "branch", "--list", "crew/atlas/tasklog") == ""
    assert dest not in _git(repo, "worktree", "list", "--porcelain")
    # The next turn builds it whole.
    monkeypatch.setattr(wt, "_run_git", real)
    again = cw.ensure("atlas", ws, repo)
    assert again.path == dest and os.path.isfile(os.path.join(dest, ".git"))


def test_a_failed_add_keeps_a_branch_it_did_not_cut(repo, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import worktree as wt

    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo)
    _commit_in(found.path, "work.txt", "atlas's work")
    kept = _git(found.path, "rev-parse", "HEAD")
    shutil.rmtree(found.path)
    real = wt._run_git

    def fail(args, cwd, **kw):
        if args[:2] == ["worktree", "add"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: add failed")
        return real(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", fail)
    with pytest.raises(cw.WorktreeRefused, match="add failed"):
        cw.ensure("atlas", ws, repo)
    assert not os.path.exists(found.path)
    assert _git(repo, "rev-parse", "crew/atlas/tasklog") == kept


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and no-follow descriptors")
def test_a_failed_add_never_removes_what_a_swapped_parent_points_at(repo, tmp_path, monkeypatch):
    """``worktrees/<slug>`` swapped for a link to the crew home before the cleanup: it stays."""
    from kiro_crew.dashboard.handlers import worktree as wt

    crew_home = tmp_path / "crew-home"
    ws = str(tmp_path / "ws-atlas")
    dest = cw.path_for(ws, "atlas", repo)
    (crew_home / os.path.basename(dest)).mkdir(parents=True)
    (crew_home / os.path.basename(dest) / "ceiling.json").write_text("{}", encoding="utf-8")
    parent = os.path.dirname(dest)
    real = wt._run_git

    def swap_then_fail(args, cwd, **kw):
        if args[:2] == ["worktree", "add"]:
            os.rename(parent, parent + ".moved")
            os.symlink(crew_home, parent)
            return subprocess.CompletedProcess(args, 128, "", "fatal: add failed")
        return real(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", swap_then_fail)
    with pytest.raises(cw.WorktreeRefused, match="add failed"):
        cw.ensure("atlas", ws, repo)
    assert (crew_home / os.path.basename(dest) / "ceiling.json").read_text(encoding="utf-8") == "{}"
    # The folder this lookup made moved with the swap and was left, not chased.
    assert os.path.isdir(os.path.join(parent + ".moved", os.path.basename(dest)))
    assert _git(repo, "branch", "--list", "crew/atlas/tasklog") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and no-follow descriptors")
def test_a_worktree_folder_is_made_through_no_follow_descriptors(repo, tmp_path):
    """``<workspace>/worktrees/<slug>`` linked elsewhere: nothing is made through it."""
    ws = tmp_path / "ws-atlas"
    elsewhere = tmp_path / "governance" / "profiles"
    elsewhere.mkdir(parents=True)
    (ws / "worktrees").mkdir(parents=True)
    (ws / "worktrees" / "atlas").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(cw.WorktreeRefused, match="not a plain folder"):
        cw.ensure("atlas", str(ws), repo)
    assert os.listdir(elsewhere) == []
    assert _git(repo, "branch", "--list", "crew/atlas/tasklog") == ""
    # Removal does not follow it either.
    assert cw.remove_crewmate_worktrees(str(ws), "atlas") == ([], [])


def test_quoted_paths_with_spaces_and_windows_paths_are_found():
    """A drive path counts only where it is absolute: on a Windows gateway."""
    text = 'Fix "/projects/My Repo/src" then D:\\work\\tasklog and /srv/app, plus `~/a b`.'
    drive = ["D:\\work\\tasklog"] if os.name == "nt" else []
    assert cw.paths_in(text) == ["/projects/My Repo/src", *drive, "/srv/app", "~/a b"]
    # Never the ``/tools/x`` inside a drive path either.
    assert cw.paths_in("work in C:/tools/x") == (["C:/tools/x"] if os.name == "nt" else [])
    assert cw.paths_in(" ".join(f"/p/{i}" for i in range(30)))[-1] == "/p/19"
    assert cw.paths_in(None) == [] and cw._absolute_here("\0") is False


def test_paths_on_different_drives_are_not_contained(monkeypatch):
    def other_drive(_paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(cw.os.path, "commonpath", other_drive)
    assert cw._contains("C:\\repo", "D:\\ws") is False


# ── job 3: removal with the crewmate ──


def test_removal_removes_clean_worktrees_and_keeps_everything_else(repo, tmp_path):
    ws = str(tmp_path / "ws-atlas")
    other = _init(tmp_path / "shared" / "other")
    clean = cw.ensure("atlas", ws, repo)
    dirty = cw.ensure("atlas", ws, other)
    with open(os.path.join(dirty.path, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("unsaved\n")
    stray = os.path.join(os.path.dirname(clean.path), "not-a-worktree")
    os.mkdir(stray)
    os.symlink(repo, os.path.join(os.path.dirname(clean.path), "link"))
    with open(os.path.join(repo, "user-notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("the user's own work\n")

    removed, kept = cw.remove_crewmate_worktrees(ws, "atlas")
    assert removed == [clean.path] and sorted(kept) == sorted([dirty.path, stray])
    assert not os.path.exists(clean.path)
    assert os.path.exists(os.path.join(dirty.path, "README.md"))
    # Branches stay, and the repository's own checkout keeps the user's file.
    assert _git(repo, "branch", "--list", "crew/atlas/tasklog")
    assert os.path.exists(os.path.join(repo, "user-notes.txt"))
    assert cw.remove_crewmate_worktrees(str(tmp_path / "nobody"), "atlas") == ([], [])


def test_removal_keeps_a_worktree_holding_only_ignored_files(repo, tmp_path):
    """``worktree remove`` would take ignored worker output with it."""
    with open(os.path.join(repo, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("results/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore results")
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo)
    os.mkdir(os.path.join(found.path, "results"))
    with open(os.path.join(found.path, "results", "run.csv"), "w", encoding="utf-8") as fh:
        fh.write("a,b\n")
    assert _git(found.path, "status", "--porcelain") == ""
    assert cw.remove_crewmate_worktrees(ws, "atlas") == ([], [found.path])
    assert os.path.exists(os.path.join(found.path, "results", "run.csv"))


def test_removal_keeps_a_worktree_whose_repository_names_a_filter(repo, tmp_path):
    """``worktree remove`` checks the files for changes, which can run a clean filter."""
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo)
    _git(repo, "config", "filter.evil.clean", f"touch {tmp_path / 'filter-ran'}; cat")
    assert cw.remove_crewmate_worktrees(ws, "atlas") == ([], [found.path])
    assert os.path.isdir(found.path)
    assert not (tmp_path / "filter-ran").exists()


def test_removal_keeps_a_worktree_git_will_not_remove(repo, tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import worktree as wt

    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo)
    _git(repo, "worktree", "lock", found.path)
    assert cw.remove_crewmate_worktrees(ws, "atlas") == ([], [found.path])

    def gone(*_a, **_k):
        raise OSError("git gone")

    monkeypatch.setattr(wt, "_run_git", gone)
    assert cw.remove_crewmate_worktrees(ws, "atlas") == ([], [found.path])
    monkeypatch.setattr(
        wt, "_run_git", lambda *_a, **_k: subprocess.CompletedProcess([], 1, "", "")
    )
    assert cw._ignored_files(wt, found.path) == "unknown"


def test_the_ignored_list_is_bounded(monkeypatch):
    many = "\0".join(f"f{i}" for i in range(8)) + "\0"
    wt = SimpleNamespace(_run_git=lambda *_a, **_k: subprocess.CompletedProcess([], 0, many, ""))
    assert cw._ignored_files(wt, "/x") == "f0, f1, f2, f3, f4 and 3 more"


def test_remove_for_member_never_raises(monkeypatch, caplog):
    import kiro_crew.config.loader as loader
    import kiro_crew.members as members

    config = SimpleNamespace(agents={"Atlas": SimpleNamespace(workspace="")})
    cw.remove_for_member("Nobody", config)  # no record: nothing to do
    monkeypatch.setattr(members, "member_slug", lambda _n, _c: "atlas")
    monkeypatch.setattr(loader, "workspace_dir_for", lambda _w: "/ws")
    monkeypatch.setattr(cw, "remove_crewmate_worktrees", lambda *_a: ([], ["/ws/kept"]))
    cw.remove_for_member("Atlas", config)
    assert "/ws/kept" in caplog.text

    def boom(*_a):
        raise RuntimeError("no sandbox")

    monkeypatch.setattr(cw, "remove_crewmate_worktrees", boom)
    cw.remove_for_member("Atlas", config)
    assert "were not removed" in caplog.text


# ── job 2: the worker turn's cwd ──


def _scope_env(monkeypatch, ws):
    import kiro_crew.config.loader as loader
    import kiro_crew.members as members
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    monkeypatch.setattr(members, "member_slug", lambda _n, _c: "atlas")
    monkeypatch.setattr(loader, "workspace_dir_for", lambda _w: ws)
    return scope, SimpleNamespace(agents={"Atlas": SimpleNamespace(workspace="")})


def test_only_a_crewmates_worker_is_scoped(monkeypatch):
    from kiro_crew.dashboard import crewmate_approval
    from kiro_crew.dashboard import crewmate_worktree_scope as scope
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    conductor = SimpleNamespace(selection_kind="agent", template_id="")
    assert scope._worker_crewmate(None, conductor) == ""
    worker = SimpleNamespace(selection_kind="template", template_id=MATE_WORKER_AGENT)
    monkeypatch.setattr(crewmate_approval, "crewmate_name_for", lambda _c, _e: "Atlas")
    assert scope._worker_crewmate(None, worker) == "Atlas"
    slot = SimpleNamespace(project="/p", key="s")
    assert asyncio.run(scope.scope_worker_turn(None, slot, None, conductor, "")) == ""
    assert slot.project == "/p"


def test_each_candidate_is_tried_in_turn(monkeypatch, tmp_path):
    scope, config = _scope_env(monkeypatch, str(tmp_path / "ws"))
    assert scope.resolve_worker_project(config, "Nobody", "/r/project", "") is None
    hit = cw.CrewmateWorktree(repo="/r", path="/ws/wt/atlas/r", branch="crew/atlas/r", cwd="/ws/a")
    tried: list[str] = []

    def ensure(_slug, _ws, candidate):
        tried.append(candidate)
        if candidate == "/r/named":
            raise cw.WorktreeRefused("checkout filter")
        if candidate == "/r/bad":
            raise PermissionError("unsearchable")
        return hit if candidate == "/r/project" else None

    monkeypatch.setattr(cw, "ensure", ensure)
    # The message's paths first, then the slot's project.
    got = scope.resolve_worker_project(config, "Atlas", "/r/project", "see /r/named, /r/bad, /r/x")
    assert got is hit and tried == ["/r/named", "/r/bad", "/r/x", "/r/project"]
    assert scope.resolve_worker_project(config, "Atlas", "", "see /r/named") is None

    from kiro_crew.dashboard.handlers import worktree as wt

    def no_sandbox(*_a):
        raise wt.SandboxUnavailable("no backend")

    tried.clear()
    monkeypatch.setattr(cw, "ensure", no_sandbox)
    assert scope.resolve_worker_project(config, "Atlas", "/r/project", "see /r/named") is None


def test_a_path_of_the_same_repository_leaves_a_worker_in_its_worktree(repo, tmp_path, monkeypatch):
    """``~/...`` in its own worktree, or the repository's checkout: no move, no reset."""
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, os.path.join(repo, "docs"))
    scope, config = _scope_env(monkeypatch, ws)
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, _e: "Atlas")
    monkeypatch.setenv("HOME", str(tmp_path))
    spelled = "~/" + os.path.relpath(os.path.join(found.path, "README.md"), str(tmp_path))
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda _k: object()))
    for named in (spelled, repo):
        slot = SimpleNamespace(project=found.cwd, key="w1", _dirty=False)
        out = asyncio.run(scope.scope_worker_turn(state, slot, config, None, f"edit {named}"))
        assert out == "" and slot.project == found.cwd, named
        assert not hasattr(slot, "_pending_reset_history_key") and slot._dirty is False


def test_a_brief_naming_another_repository_moves_the_turn_there(tmp_path, git_here, monkeypatch):
    alpha = _init(tmp_path / "shared" / "alpha")
    beta = _init(tmp_path / "shared" / "beta")
    ws = str(tmp_path / "ws-atlas")
    first = cw.ensure("atlas", ws, alpha)
    scope, config = _scope_env(monkeypatch, ws)
    new_file = os.path.join(beta, "new_package", "module.py")
    got = scope.resolve_worker_project(config, "Atlas", first.cwd, f"Create {new_file}")
    assert got is not None and got.repo == os.path.realpath(beta)
    assert got.path == cw.path_for(ws, "atlas", beta) and got.cwd == got.path


def test_a_new_worktree_moves_the_project_and_resets_a_running_session(monkeypatch):
    from kiro_crew.dashboard import chat_utils
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    hit = cw.CrewmateWorktree(repo="/r", path="/ws/wt/atlas/r", branch="crew/atlas/r", cwd="/ws/a")
    pushes: list[int] = []

    def push():
        pushes.append(1)
        raise RuntimeError("socket closed")

    monkeypatch.setattr(scope, "_worker_crewmate", lambda *_a: "Atlas")
    monkeypatch.setattr(scope, "resolve_worker_project", lambda *_a: hit)
    monkeypatch.setattr(chat_utils, "effective_session_key", lambda slot: "dashboard:" + slot.key)
    state = SimpleNamespace(
        sessions=SimpleNamespace(get_provider=lambda _key: object()), push_slots_update=push
    )
    slot = SimpleNamespace(project="/r/src", key="w1", _dirty=False)
    # A failed push is repaired by the next one.
    assert asyncio.run(scope.scope_worker_turn(state, slot, None, None, "")) == hit.cwd
    assert (slot.project, slot._dirty) == (hit.cwd, True)
    assert slot._pending_reset_history_key == "dashboard:w1"
    assert pushes == [1]
    # Already there: nothing moves.
    assert asyncio.run(scope.scope_worker_turn(state, slot, None, None, "")) == ""
    monkeypatch.setattr(scope, "resolve_worker_project", lambda *_a: None)
    assert asyncio.run(scope.scope_worker_turn(state, slot, None, None, "")) == ""
    # No running session: the move arms no reset.
    idle = SimpleNamespace(project="", key="w2", _dirty=False)
    monkeypatch.setattr(scope, "resolve_worker_project", lambda *_a: hit)
    assert asyncio.run(scope.scope_worker_turn(None, idle, None, None, "")) == hit.cwd
    assert not hasattr(idle, "_pending_reset_history_key")


@pytest.mark.skipif(os.name == "nt", reason="a drive path is absolute on Windows")
def test_a_drive_path_on_a_posix_gateway_never_reaches_the_gateways_own_checkout(
    tmp_path, git_here, monkeypatch
):
    """The gateway's cwd is a checkout: ``C:\\tools\\x`` must not resolve into it."""
    own = _init(tmp_path / "gateway-checkout")
    monkeypatch.chdir(own)
    assert cw.repo_root("C:/tools/x") is None
    ws = str(tmp_path / "ws-atlas")
    scope, config = _scope_env(monkeypatch, ws)
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, _e: "Atlas")
    slot = SimpleNamespace(project="", key="w1", _dirty=False)
    out = asyncio.run(scope.scope_worker_turn(None, slot, config, None, "fix C:\\tools\\x now"))
    assert out == "" and slot.project == ""
    assert _git(own, "branch", "--list", "crew/*") == ""


# ── job 2: the repository's aws.project_profiles binding holds in its worktree ──


def _profile(name: str) -> dict[str, str]:
    return {"AWS_PROFILE": name, "AWS_DEFAULT_PROFILE": name}


def test_a_worker_in_its_worktree_keeps_the_repositorys_aws_profile(repo, tmp_path):
    """``/repos/app -> sandbox``: the worktree sits in the workspace, not under ``/repos/app``."""
    from kiro_crew.aws_project_profiles import bound_profile_env

    ws = str(tmp_path / "ws-atlas")
    real = os.path.realpath(repo)
    # The workspace's own binding never stands in for the repository's.
    mapping = {os.path.dirname(real): "production", real: "sandbox", ws: "personal"}
    found = cw.ensure("atlas", ws, os.path.join(repo, "docs"))
    assert found.cwd == os.path.join(found.path, "docs")
    assert cw.source_path(found.path) == real
    assert cw.source_path(found.cwd) == os.path.join(real, "docs")
    for cwd in (found.path, found.cwd, os.path.join(found.cwd, "not-made-yet")):
        assert bound_profile_env(mapping, cwd, None) == _profile("sandbox"), cwd
    # An unmapped repository's worktree is bound as its checkout is: not at all.
    caller = {"FOO": "bar"}
    assert bound_profile_env({str(tmp_path / "elsewhere"): "dev"}, found.cwd, caller) is caller


def test_the_binding_is_read_from_git_so_it_holds_after_a_restart(repo, tmp_path):
    """A worktree an earlier gateway made (plain git here): nothing in this process knew it."""
    from kiro_crew.aws_project_profiles import resolve_project_profile

    ws = str(tmp_path / "ws-atlas")
    dest = cw.path_for(ws, "atlas", repo)
    os.makedirs(os.path.dirname(dest))
    _git(repo, "worktree", "add", "-q", "-b", "crew/atlas/tasklog", dest, "main")
    real = os.path.realpath(repo)
    assert resolve_project_profile({real: "sandbox"}, os.path.join(dest, "docs")) == "sandbox"
    # And on every later start in the reused worktree.
    assert cw.ensure("atlas", ws, repo).path == dest
    assert resolve_project_profile({real: "sandbox"}, dest) == "sandbox"


def test_only_a_registered_worktree_at_the_crewmate_layout_is_mapped(repo, tmp_path):
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo)
    # A repository cloned inside the worktree is its own checkout.
    nested = _init(Path(found.path) / "vendor" / "lib")
    assert cw.source_path(nested) == nested
    # A folder named for the repository that git does not register as its worktree.
    other_ws = str(tmp_path / "ws-briar")
    look_alike = cw.path_for(other_ws, "briar", repo)
    os.makedirs(os.path.join(look_alike, "docs"))
    assert cw.source_path(os.path.join(look_alike, "docs")) == os.path.join(look_alike, "docs")
    _init(Path(look_alike))
    assert cw.source_path(look_alike) == look_alike
    # A ``.git`` copied in from a real worktree: git does not register this folder.
    forged = cw.path_for(str(tmp_path / "ws-pixel"), "pixel", repo)
    os.makedirs(os.path.join(forged, "docs"))
    shutil.copy(os.path.join(found.path, ".git"), os.path.join(forged, ".git"))
    assert cw.source_path(os.path.join(forged, "docs")) == os.path.join(forged, "docs")
    # A worktree named for the repository, outside a ``worktrees/<slug>/`` folder.
    shallow = str(tmp_path / "worktrees" / os.path.basename(found.path))
    _git(repo, "worktree", "add", "-q", "-b", "user/shallow", shallow, "main")
    assert cw.source_path(os.path.join(shallow, "docs")) == os.path.join(shallow, "docs")
    # A worktree of the repository moved to a folder not named for it.
    moved = os.path.join(os.path.dirname(found.path), "other-12345678")
    _git(repo, "worktree", "move", found.path, moved)
    assert cw.source_path(moved) == moved
    # A worktree outside any ``worktrees/<slug>/`` folder.
    beside = str(tmp_path / "beside")
    _git(repo, "worktree", "add", "-q", "-b", "user/beside", beside, "main")
    assert cw.source_path(beside) == beside


def test_a_path_with_no_worktrees_folder_runs_no_git(monkeypatch, tmp_path):
    from kiro_crew.dashboard.handlers import worktree as wt

    ran: list[list[str]] = []
    monkeypatch.setattr(wt, "_run_git", lambda args, *_a, **_k: ran.append(list(args)))
    for path in (str(tmp_path / "repos" / "app" / "src"), "", "relative/worktrees/a/b"):
        assert cw.source_path(path) == path
    assert ran == [], "no git for an ordinary path"

    def no_sandbox(*_a, **_k):
        raise wt.SandboxUnavailable("no backend")

    monkeypatch.setattr(wt, "_run_git", no_sandbox)
    inside = str(tmp_path / "ws" / "worktrees" / "atlas" / "app-12345678" / "src")
    assert cw.source_path(inside) == inside


@pytest.mark.asyncio
async def test_a_session_started_in_the_worktree_gets_the_repositorys_profile(repo, tmp_path):
    from test_aws_project_profiles import _cfg, _recording_factory

    from kiro_crew.session import SessionManager

    found = await asyncio.to_thread(cw.ensure, "atlas", str(tmp_path / "ws-atlas"), repo)
    calls: list[dict] = []
    cfg = _cfg({os.path.realpath(repo): "sandbox"})
    mgr = SessionManager(cfg, provider_factory=_recording_factory(calls))
    try:
        await mgr.get_or_create("chat-23563", cwd=found.cwd, extra_env=None)
    finally:
        await mgr.close_all()
    assert calls[0]["extra_env"] == _profile("sandbox")


# ── end to end: a worker turn, and deleting the crewmate ──


@pytest.mark.asyncio
async def test_a_workers_cwd_is_its_crewmates_worktree(home, repo, monkeypatch):  # noqa: F811
    from test_chat_agent_selection import _turn_state

    from kiro_crew.config.loader import KiroCrewConfig, workspace_dir_for
    from kiro_crew.history import ConversationLog
    from kiro_crew.members import member_slug

    state, caller, _execution, tmp_path = home
    created = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    shared = created.project
    turn_state = _turn_state(tmp_path, monkeypatch)
    turn_state.conversation_log = ConversationLog()
    worker = turn_state.get_or_create_slot(created.key)
    for field in ("agent", "agent_kind", "workspace", "memory_store", "project"):
        setattr(worker, field, getattr(created, field))
    seen = {}
    claim = turn_state.sessions.get_or_create

    async def _claim(*args, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        return await claim(*args, **kwargs)

    turn_state.sessions.get_or_create = _claim
    await _finished(
        chat_runner._run_chat(turn_state, worker, f"Fix the failing tests in {repo}."),
        "worker turn",
    )

    cfg = KiroCrewConfig.load()
    ws = str(workspace_dir_for(cfg.agents["pixel"].workspace))
    expected = cw.path_for(ws, member_slug("pixel", cfg), repo)
    assert seen.get("cwd") == expected, f"worker ran in {seen.get('cwd')}, not its worktree"
    assert worker.project == expected and worker.project != shared
    assert _git(expected, "branch", "--show-current").startswith("crew/")
    assert _git(repo, "branch", "--show-current") == "main"


@pytest.mark.asyncio
async def test_removing_the_crewmate_removes_its_worktrees_and_keeps_dirty_ones(
    repo, tmp_path, monkeypatch
):
    from test_agent_delete_prunes_private_copy import _delete_request

    from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_delete

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )
    clean_ws, dirty_ws = tmp_path / "ws-clean", tmp_path / "ws-dirty"
    cfg = KiroCrewConfig()
    cfg.workspaces = {
        "m-clean": WorkspaceConfig(dir=str(clean_ws)),
        "m-dirty": WorkspaceConfig(dir=str(dirty_ws)),
    }
    cfg.agents = {
        "kirocrew": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "Atlas": KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="m-clean"),
        "Briar": KiroCrewAgentConfig(kiro_agent="kirocrew", workspace="m-dirty"),
    }
    cfg.default_agent = "kirocrew"
    await asyncio.to_thread(cfg.save)
    clean = await asyncio.to_thread(cw.ensure, "atlas", str(clean_ws), repo)
    dirty = await asyncio.to_thread(cw.ensure, "briar", str(dirty_ws), repo)
    with open(os.path.join(dirty.path, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("briar's unsaved edit\n")
    with open(os.path.join(repo, "user-notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("the user's own work\n")

    for name in ("Atlas", "Briar"):
        resp = await api_kirocrew_agent_delete(_delete_request(name))
        assert resp.status == 200

    assert not os.path.exists(clean.path)
    assert clean.path not in _git(repo, "worktree", "list")
    assert os.path.exists(os.path.join(dirty.path, "README.md")), "uncommitted work was deleted"
    # Branches stay, and the shared checkout keeps the user's uncommitted file.
    assert _git(repo, "branch", "--list", "crew/atlas/tasklog")
    assert open(os.path.join(repo, "user-notes.txt"), encoding="utf-8").read()

# SPDX-License-Identifier: Apache-2.0
"""Each crewmate's workers run in that crewmate's own git worktree.

Three crewmates shared one checkout in the soak: one committed the others' edits
and landed a commit on the branch another had checked out. The gateway now gives
each crewmate a worktree of its own per repository, on a branch of its own, and
points its workers' cwd at it. The worktree sits beside the repository,
``<repo>.worktrees/<slug>/``, and the crewmate's index in its workspace lists it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from test_chat_agent_selection import _finished
from test_crewmate_worker_approval import home  # noqa: F401
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401
from test_worktree_create import _require_sandbox_exec

from kiro_crew import crewmate_worktree as cw
from kiro_crew.dashboard import chat_runner


def _git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _require_sandbox_exec()
    root = tmp_path / "shared" / "tasklog"
    (root / "docs").mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    (root / "docs" / "a.md").write_text("a\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return str(root)


def test_two_crewmates_on_one_repo_get_their_own_worktree_and_branch(repo, tmp_path):
    atlas = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    briar = cw.ensure("briar", str(tmp_path / "ws-briar"), os.path.join(repo, "docs"))

    assert atlas.path != briar.path
    assert atlas.branch == "crew/atlas/tasklog" and briar.branch == "crew/briar/tasklog"
    # Sibling folders beside the repository, one per crewmate.
    beside = os.path.realpath(repo) + ".worktrees"
    assert (atlas.path, briar.path) == (
        os.path.join(beside, "atlas"),
        os.path.join(beside, "briar"),
    )
    # The index stays in the crewmate's workspace; no worktree is made there.
    assert os.listdir(tmp_path / "ws-atlas" / "worktrees") == ["index.json"]
    assert briar.cwd == os.path.join(briar.path, "docs")
    assert _git(atlas.path, "branch", "--show-current") == atlas.branch
    assert _git(briar.path, "branch", "--show-current") == briar.branch
    # The shared checkout stays on its own branch, untouched.
    assert _git(repo, "branch", "--show-current") == "main"
    assert _git(repo, "status", "--porcelain") == ""


def test_the_worktree_is_reused_and_another_crewmates_path_maps_to_your_own(repo, tmp_path):
    atlas = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    (open(os.path.join(atlas.path, "work.txt"), "w")).write("wip\n")
    again = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    assert again == atlas  # uncommitted work in it does not stop the reuse

    # A brief naming Atlas's worktree, handed to Briar, lands in Briar's own.
    briar = cw.ensure("briar", str(tmp_path / "ws-briar"), os.path.join(atlas.path, "docs"))
    assert briar.repo == os.path.realpath(repo)
    assert briar.path == cw.path_for(repo, "briar")
    assert briar.cwd == os.path.join(briar.path, "docs")


def test_a_path_outside_any_repository_has_no_worktree(tmp_path):
    _require_sandbox_exec()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert cw.ensure("atlas", str(tmp_path / "ws"), str(plain)) is None


def test_the_project_line_names_the_worktree_and_its_branch(repo, tmp_path):
    from kiro_crew.context_assembly.turn import rail_parts

    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    text = "".join(
        rail_parts(
            project=found.cwd,
            context_groups=None,
            board_tags=None,
            minimal_context=False,
            folder_path=None,
            session_key=None,
            agent=None,
            request_prefix_context=None,
        )
    )
    assert f"[PROJECT] Active project directory: {found.cwd}" in text
    assert f"worktree of {os.path.realpath(repo)}, on branch crew/atlas/tasklog" in text
    # A further branch, when one is needed, is named under the crewmate's prefix.
    assert "named under crew/atlas/ (such as crew/atlas/<topic>)" in text


@pytest.mark.asyncio
async def test_a_workers_cwd_is_its_crewmates_worktree(home, repo, monkeypatch):  # noqa: F811
    from test_chat_agent_selection import _turn_state

    from kiro_crew.history import ConversationLog

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

    expected = cw.path_for(repo, "pixel")
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


def _advance_main(repo: str, name: str) -> str:
    """A new commit on the shared checkout's main, the way the driver files an issue."""
    with open(os.path.join(repo, "issues.md"), "a", encoding="utf-8") as fh:
        fh.write(f"- {name}\n")
    _git(repo, "add", "issues.md")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def test_a_clean_worktree_is_rebased_onto_main_before_the_turn(repo, tmp_path):
    from kiro_crew import crewmate_prompts

    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    with open(os.path.join(found.path, "work.txt"), "w", encoding="utf-8") as fh:
        fh.write("mine\n")
    _git(found.path, "add", "work.txt")
    _git(found.path, "commit", "-q", "-m", "crewmate work")
    assert cw.sync_with_base(found) == ""  # nothing new on main yet
    head = _advance_main(repo, "ISSUE-2")
    _advance_main(repo, "ISSUE-3")
    note = cw.sync_with_base(found)
    assert note == crewmate_prompts.fill(
        "worktree.base_rebased", branch="crew/atlas/tasklog", base="main", count=2
    )
    # Main is now an ancestor of the crewmate's branch, and its own commit is kept.
    _git(found.path, "merge-base", "--is-ancestor", head, "HEAD")
    assert os.path.exists(os.path.join(found.path, "issues.md"))
    assert _git(found.path, "log", "-1", "--format=%s") == "crewmate work"
    # The [PROJECT] line of the next turn carries the note, once.
    from kiro_crew.context_assembly.turn import rail_parts

    def _rails() -> str:
        return "".join(
            rail_parts(
                project=found.cwd,
                context_groups=None,
                board_tags=None,
                minimal_context=False,
                folder_path=None,
                session_key=None,
                agent=None,
                request_prefix_context=None,
            )
        )

    assert note in _rails()
    assert note not in _rails()


def test_a_dirty_worktree_is_left_and_the_worker_told_how_far_main_moved(repo, tmp_path):
    from kiro_crew import crewmate_prompts

    found = cw.ensure("briar", str(tmp_path / "ws-briar"), repo)
    with open(os.path.join(found.path, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("uncommitted\n")
    before = _git(found.path, "rev-parse", "HEAD")
    _advance_main(repo, "ISSUE-2")
    note = cw.sync_with_base(found)
    assert note == crewmate_prompts.fill(
        "worktree.base_moved_dirty", branch="crew/briar/tasklog", base="main", count=1
    )
    assert _git(found.path, "rev-parse", "HEAD") == before
    assert "uncommitted" in open(os.path.join(found.path, "README.md"), encoding="utf-8").read()


def test_a_rebase_that_stops_is_undone(repo, tmp_path):
    found = cw.ensure("cove", str(tmp_path / "ws-cove"), repo)
    with open(os.path.join(found.path, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("cove's readme\n")
    _git(found.path, "commit", "-q", "-am", "cove edits the readme")
    before = _git(found.path, "rev-parse", "HEAD")
    with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("main's readme\n")
    _git(repo, "commit", "-q", "-am", "main edits the readme")
    note = cw.sync_with_base(found)
    assert "the rebase was undone" in note
    assert _git(found.path, "rev-parse", "HEAD") == before
    assert _git(found.path, "status", "--porcelain") == ""
    assert not os.path.exists(os.path.join(found.path, ".git", "rebase-merge"))


def test_every_worker_turn_in_a_worktree_syncs_it_with_main_first(repo, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    found = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    synced: list[str] = []
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, _e: "Atlas")
    monkeypatch.setattr(cw, "sync_with_base", lambda entry: synced.append(entry.path) or "")
    import kiro_crew.config.loader as loader
    import kiro_crew.members as members

    monkeypatch.setattr(members, "member_slug", lambda _n, _c: "atlas")
    monkeypatch.setattr(loader, "workspace_dir_for", lambda _w: tmp_path / "ws-atlas")
    config = SimpleNamespace(agents={"Atlas": SimpleNamespace(workspace="")})
    # A later turn: already in the worktree, naming no other repository.
    slot = SimpleNamespace(project=found.cwd, key="w1")
    assert asyncio.run(scope.scope_worker_turn(None, slot, config, None, "next step")) == ""
    assert synced == [found.path]


def test_the_base_is_main_even_when_the_shared_checkout_sits_on_another_branch(repo, tmp_path):
    """Pod round 5: a worker checked another crewmate's branch out in the shared
    checkout, and HEAD there named that branch; the base stays the default branch."""
    atlas = cw.ensure("atlas", str(tmp_path / "ws-atlas"), repo)
    cove = cw.ensure("cove", str(tmp_path / "ws-cove"), repo)
    _advance_main(repo, "ISSUE-2")
    _git(repo, "checkout", "-q", "-b", "crew/atlas/escaped")
    with open(os.path.join(repo, "atlas.txt"), "w", encoding="utf-8") as fh:
        fh.write("atlas only\n")
    _git(repo, "add", "atlas.txt")
    _git(repo, "commit", "-q", "-m", "atlas work in the shared checkout")
    note = cw.sync_with_base(cove)
    assert "onto main's 1 new commit" in note
    assert not os.path.exists(os.path.join(cove.path, "atlas.txt"))
    assert os.path.exists(os.path.join(cove.path, "issues.md"))
    assert atlas.branch != cove.branch

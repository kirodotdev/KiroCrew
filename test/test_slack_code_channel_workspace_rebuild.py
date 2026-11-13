"""Code-channel workspace: the worktree identity gate, and rebuilding a lost
record from the remembered checkout.

Split from ``test_slack_code_channel_workspace`` (the file-line cap); the
harness, fixtures and fakes are that module's, imported by name so its tests
are not collected twice.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import test_slack_code_channel_workspace as _ws

from kiro_crew.slack.code_channels.workspace import git_ops, service
from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

FakeBinding = _ws.FakeBinding
Harness = _ws.Harness
_conductor_harness = _ws._conductor_harness
_git = _ws._git
# Fixtures, by name: pytest finds a fixture through the module's namespace.
_no_sandbox = _ws._no_sandbox
_feature_on = _ws._feature_on
audits = _ws.audits
repo = _ws.repo


def test_is_channel_worktree_holds_after_the_agent_switches_branches(repo, audits, tmp_path):
    """Identity does not depend on the branch checked out: the agent running
    ``git switch`` to an ordinary branch or detaching HEAD inside the channel's
    own worktree keeps it verified, while a record pointing at the main
    checkout, another channel's tree or an unregistered path still fails."""
    root = str(repo)
    branch = git_ops.channel_branch("fix the widget", "T1", "C1")
    made = git_ops.create_or_adopt_worktree(root, branch)
    assert not isinstance(made, str), made
    wt = made.path
    assert git_ops.is_channel_worktree(root, wt, branch, "T1", "C1") is True

    # An ordinary branch the agent creates itself.
    _git("switch", "-q", "-c", "fix/widget", cwd=Path(wt))
    assert git_ops.is_channel_worktree(root, wt, branch, "T1", "C1") is True
    # A detached HEAD (mid-rebase, bisect, checkout <sha>).
    _git("checkout", "-q", "--detach", cwd=Path(wt))
    assert git_ops.is_channel_worktree(root, wt, branch, "T1", "C1") is True

    # The main checkout is never a channel's tree, whatever it holds -- even
    # when it is on the channel's own branch (free now the worktree left it).
    assert git_ops.is_channel_worktree(root, root, branch, "T1", "C1") is False
    _git("switch", "-q", branch, cwd=repo)
    assert git_ops.is_channel_worktree(root, root, branch, "T1", "C1") is False
    _git("switch", "-q", "main", cwd=repo)
    # Another channel cannot claim this tree, nor this channel a forged branch.
    assert git_ops.is_channel_worktree(root, wt, branch, "T1", "C2") is False
    assert git_ops.is_channel_worktree(root, wt, "fix/widget", "T1", "C1") is False
    # An unregistered directory named like the destination does not pass.
    stray = tmp_path / "stray"
    stray.mkdir()
    assert git_ops.is_channel_worktree(root, str(stray), branch, "T1", "C1") is False
    # A second channel's own worktree is not this channel's.
    other = git_ops.channel_branch("other work", "T1", "C9")
    made2 = git_ops.create_or_adopt_worktree(root, other)
    assert not isinstance(made2, str), made2
    assert git_ops.is_channel_worktree(root, made2.path, branch, "T1", "C1") is False


def _other_repo(tmp_path) -> Path:
    other = tmp_path / "other-project"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    (other / "notes.txt").write_text("private\n", newline="\n")
    return other


def test_a_forged_registration_in_the_shared_git_dir_is_refused(repo, audits, tmp_path):
    """An agent that can write the shared ``.git`` can add a
    ``worktrees/<name>`` entry pointing at another checkout on the channel's
    branch; git then LISTS it, but asked from that checkout it is no linked tree
    of this repository, so the gate refuses it."""
    root = str(repo)
    branch = git_ops.channel_branch("fix the widget", "T1", "C1")
    other = _other_repo(tmp_path)
    admin = repo / ".git" / "worktrees" / "evil"
    admin.mkdir(parents=True)
    (admin / "gitdir").write_text((other / ".git").as_posix() + "\n", newline="\n")
    (admin / "HEAD").write_text(f"ref: refs/heads/{branch}\n", newline="\n")
    (admin / "commondir").write_text("../..\n", newline="\n")
    assert any(
        os.path.realpath(p) == os.path.realpath(other)
        for p in (git_ops._wt._worktree_branches(root) or {})
    )
    assert git_ops.is_channel_worktree(root, str(other), branch, "T1", "C1") is False


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_directory_swapped_in_at_the_destination_is_refused(repo, audits, tmp_path):
    """After registration, the channel's tree replaced by a link to another
    checkout matches the deterministic path, but it is not this repository's
    linked tree, so the gate refuses it."""
    root = str(repo)
    branch = git_ops.channel_branch("fix the widget", "T1", "C1")
    made = git_ops.create_or_adopt_worktree(root, branch)
    assert not isinstance(made, str), made
    wt = Path(made.path)
    assert git_ops.is_channel_worktree(root, str(wt), branch, "T1", "C1") is True
    other = _other_repo(tmp_path)
    wt.rename(tmp_path / "moved-away")
    wt.symlink_to(other, target_is_directory=True)
    assert git_ops.is_channel_worktree(root, str(wt), branch, "T1", "C1") is False


# ── a lost workspace record rebuilt from the session's worktree ─────────────


@pytest.fixture
def channel_store(tmp_path):
    """The code-channel store, EMPTY: production plans the worktree first."""
    from kiro_crew.slack.code_channels.store import CodeChannelStore, set_store

    store = CodeChannelStore(tmp_path / "routing" / "slack-code-channels.json")
    set_store(store)
    yield store
    set_store(None)


def _production_bind(tmp_path, monkeypatch, channel_store, project: str):
    """The real order: plan_session cuts the worktree, then the core stores the
    binding (project_dir = the worktree, repo_root = planned_root), then prepare."""
    from kiro_crew.slack.code_channels.store import Binding
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    h, dash, _made, _statuses = _conductor_harness(tmp_path, monkeypatch)
    monkeypatch.setattr(ws_core, "_SERVICE", h.svc)
    _folder, session_dir = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="Fix the widget",
            channel_name="fix-widget",
            project_dir=project,
            parent_folder_id="F0",
        )
    )
    channel_store.put_binding(
        Binding(
            channel_id="C1",
            team_id="T1",
            session_key="chat-1",
            anchor_ts="1.0",
            project_dir=session_dir,
            repo_root=ws_core.planned_root("T1", "C1"),
        )
    )
    first = h.run(h.svc.ensure(FakeBinding("C1", project_dir=session_dir)))
    return h, first, session_dir


def _rebuild(h: Harness, session_dir: str):
    """Lose the workspace record, then bind again from the session's project."""
    h.store._file().unlink()
    return h.run(h.svc.ensure(FakeBinding("C1", project_dir=session_dir)))


def _submodule(repo: Path, tmp_path: Path, path: str, name: str) -> Path:
    src = tmp_path / f"sub-src-{path}"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
        _git("config", key, value, cwd=src)
    (src / "x.txt").write_text("x\n", newline="\n")
    _git("add", ".", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)
    _git(
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        "--name",
        name,
        str(src),
        path,
        cwd=repo,
    )
    return repo / path


def test_a_record_rebuilt_from_the_worktree_uses_the_remembered_checkout(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    """In the real bind order the binding remembers the planned checkout; a
    rebuild from the worktree uses it, so the gate keeps confirming the tree."""
    h, first, session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(repo))
    assert first.worktree
    assert os.path.realpath(channel_store.get_binding("C1").repo_root) == os.path.realpath(repo)
    rebuilt = _rebuild(h, session_dir)
    assert os.path.realpath(rebuilt.worktree) == os.path.realpath(first.worktree)
    assert os.path.realpath(rebuilt.repo_root) == os.path.realpath(repo)
    assert rebuilt.base_sha == first.base_sha
    assert service._trusted_worktree(rebuilt) is True
    # The worktree itself is never remembered as a root.
    assert os.path.realpath(channel_store.get_binding("C1").repo_root) == os.path.realpath(repo)


def test_a_feature_worktree_origin_keeps_its_root_and_base_on_rebuild(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    """Cut from a user's own feature worktree: the rebuild returns to THAT
    checkout, not the main one, so the diff base does not change."""
    feat = tmp_path / "feat"
    _git("worktree", "add", "-q", "-b", "feature/x", str(feat), cwd=repo)
    (feat / "f.txt").write_text("f\n", newline="\n")
    _git("add", ".", cwd=feat)
    _git("commit", "-q", "-m", "feature", cwd=feat)
    h, first, session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(feat))
    assert os.path.realpath(first.repo_root) == os.path.realpath(feat)
    rebuilt = _rebuild(h, session_dir)
    assert os.path.realpath(rebuilt.repo_root) == os.path.realpath(feat)
    assert rebuilt.base_sha == first.base_sha
    assert service._trusted_worktree(rebuilt) is True


def test_a_hostile_submodule_origin_never_puts_a_worktree_in_a_git_dir(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    """A submodule named ``x/.git`` (its common dir ends in .git): the worktree is
    cut beside the submodule checkout, and a rebuild returns there too."""
    sub = _submodule(repo, tmp_path, "H", "x/.git")
    h, first, session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(sub))
    rebuilt = _rebuild(h, session_dir)
    for rec in (first, rebuilt):
        if rec.worktree:
            assert ".git" not in Path(os.path.realpath(rec.worktree)).parts
        assert ".git" not in Path(os.path.realpath(rec.repo_root)).parts
    assert os.path.realpath(rebuilt.repo_root) == os.path.realpath(first.repo_root)


def test_a_remembered_root_the_gate_does_not_confirm_is_ignored(
    tmp_path, repo, audits, channel_store
):
    """A remembered root that the top level is no channel tree of (another repo,
    a missing path) is not used: the top level is, as without one."""
    from kiro_crew.slack.code_channels.store import Binding

    channel_store.put_binding(
        Binding(channel_id="C1", team_id="T1", session_key="chat-1", anchor_ts="1.0")
    )
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)
    for remembered in (str(other), str(tmp_path / "missing")):
        channel_store.update_binding("C1", repo_root=remembered)
        assert service._remembered_root("C1", str(repo), "slack/x-00000000", "T1") == str(repo)


def test_a_root_that_is_the_worktree_is_never_remembered(tmp_path, channel_store):
    from kiro_crew.slack.code_channels.store import Binding

    channel_store.put_binding(
        Binding(channel_id="C1", team_id="T1", session_key="chat-1", anchor_ts="1.0")
    )
    service._remember_root("C1", str(tmp_path / "wt"), str(tmp_path / "wt"), "b", "T1")
    assert channel_store.get_binding("C1").repo_root == ""
    service._remember_root("C1", str(tmp_path / "repo"), str(tmp_path / "wt"), "b", "T1")
    assert channel_store.get_binding("C1").repo_root == str(tmp_path / "repo")


def test_a_rebuild_after_the_agent_switched_branches_adopts_the_same_tree(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    """The agent ran ``git switch -c`` in its tree, then the record was lost:
    the confirmed tree is adopted as it stands, not refused as occupied."""
    h, first, session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(repo))
    _git("switch", "-q", "-c", "agent-other", cwd=Path(first.worktree))
    rebuilt = _rebuild(h, session_dir)
    assert os.path.realpath(rebuilt.worktree) == os.path.realpath(first.worktree)
    assert rebuilt.branch == first.branch and rebuilt.base_sha == first.base_sha
    assert service._trusted_worktree(rebuilt) is True


def test_a_remembered_root_that_still_confirms_the_tree_is_not_replaced(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    h, first, _session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(repo))
    kept = channel_store.get_binding("C1").repo_root
    branch = first.branch
    service._remember_root("C1", str(tmp_path / "elsewhere"), first.worktree, branch, "T1")
    assert channel_store.get_binding("C1").repo_root == kept


def test_an_unreadable_store_is_never_rewritten_empty(tmp_path, monkeypatch):
    """A transient read error must abandon the write, not publish an empty store
    over every record; a display read still answers empty."""
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    store = WorkspaceStore(tmp_path / "store.json")
    store.put(WorkspaceRecord(team_id="T1", channel_id="C1", worktree="/wt/one"))
    before = (tmp_path / "store.json").read_text()
    real = Path.read_text

    def eio(self, *a, **k):
        if self.name == "store.json":
            raise OSError(5, "Input/output error")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", eio)
    with pytest.raises(OSError):
        store.put(WorkspaceRecord(team_id="T1", channel_id="C2", worktree="/wt/two"))
    assert store.get("T1", "C1") is None  # a display read: empty, never raises
    monkeypatch.setattr(Path, "read_text", real)
    assert (tmp_path / "store.json").read_text() == before
    assert store.get("T1", "C1").worktree == "/wt/one"


def test_a_refresh_whose_store_write_fails_logs_and_returns(
    tmp_path, repo, audits, channel_store, monkeypatch, caplog
):
    """A store that cannot be read abandons the refresh's write; the error stays
    inside refresh (its fire-and-forget task would otherwise drop it unlogged)."""
    h, _first, session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(repo))

    def eio(_record):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(h.store, "put", eio)
    with caplog.at_level("WARNING"):
        h.run(h.svc.refresh(FakeBinding("C1", project_dir=session_dir)))
    assert "refresh not stored" in caplog.text
    # The channel lock is released and no extra pass is queued.
    assert not h.svc._lock("T1:C1").locked()
    assert "T1:C1" not in h.svc._again


# ── the channel's diff tab across a record replacement ──────────────────────


def _second_repo(tmp_path: Path) -> Path:
    """A second repository with its own ``origin``, clean at its base."""
    remote = tmp_path / "remote-b.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    root = tmp_path / "proj-b"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _git("config", key, value, cwd=root)
    (root / "B.md").write_text("b\n", newline="\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    _git("remote", "add", "origin", "https://github.com/acme/other.git", cwd=root)
    _git("push", "-q", str(remote), "main:main", cwd=root)
    _git("fetch", "-q", str(remote), "+refs/heads/*:refs/remotes/origin/*", cwd=root)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    return root


def test_a_rebind_to_another_repository_still_removes_the_old_diff_tab(tmp_path, repo, audits):
    """The channel's tab is the channel's, not the tree's: rebound to a clean
    repository B, the tab repository A's diff opened is removed, not left beside
    B's context bar."""
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    rec = h.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("changed\n", newline="\n")
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_diff_view") and h.store.get("T1", "C1").view_id == "V1"
    other = FakeBinding("C1", session_key="chat-2", project_dir=str(_second_repo(tmp_path)))
    moved = h.run(h.svc.ensure(other))
    assert moved is not None and moved.worktree and moved.worktree != rec.worktree
    h.run(h.svc.refresh(other))
    assert h.client.of("remove_view") == [{"view_id": "V1"}]
    assert h.store.get("T1", "C1").view_id == ""


def test_a_known_tab_is_removed_even_when_its_digest_was_reset(tmp_path):
    """A digest reset for a republish (a rebind raced a refresh) keeps the tab's
    id; a clean diff then still removes that tab."""
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    h = Harness(tmp_path)
    record = WorkspaceRecord(team_id="T1", channel_id="C1", worktree="/wt", view_id="V7")
    h.run(h.svc._clear_diff(record))
    assert h.client.of("remove_view") == [{"view_id": "V7"}]
    assert (record.view_id, record.diff_hash) == ("", "")


def test_a_rebind_to_a_folder_with_no_repository_removes_the_old_diff_tab(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    rec = h.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("changed\n", newline="\n")
    h.run(h.svc.refresh(binding))
    notes = tmp_path / "notes"
    notes.mkdir()
    other = FakeBinding("C1", session_key="chat-2", project_dir=str(notes))
    moved = h.run(h.svc.ensure(other))
    assert moved is not None and moved.worktree == ""
    h.run(h.svc.refresh(other))
    assert h.client.of("remove_view") == [{"view_id": "V1"}]
    assert h.store.get("T1", "C1").view_id == ""


def test_a_rebind_landing_while_a_refresh_removes_the_tab_keeps_no_stale_id(
    tmp_path, repo, audits, monkeypatch
):
    """The refresh pass removes the tab (its tree went clean) while a rebind to
    another tree stores the old id it carried: the merge keeps what Slack shows
    (no tab), so no later clean turn retries removing a tab that is gone."""
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    rec = h.run(h.svc.ensure(binding))
    readme = Path(rec.worktree) / "README.md"
    readme.write_text("changed\n", newline="\n")
    h.run(h.svc.refresh(binding))
    readme.write_text("hello\n", newline="\n")
    real_clear = h.svc._clear_diff

    async def clear_while_rebound(record):
        await real_clear(record)
        stored = h.store.get("T1", "C1")
        stored.worktree, stored.branch = "/elsewhere", "slack/other"
        h.store.put(stored)  # the rebind's record, still carrying V1

    monkeypatch.setattr(h.svc, "_clear_diff", clear_while_rebound)
    h.run(h.svc.refresh(binding))
    assert h.client.of("remove_view") == [{"view_id": "V1"}]
    assert h.store.get("T1", "C1").view_id == ""


# ── a worktree removed by hand while its branch stays ───────────────────────


def test_a_removed_worktree_whose_branch_remains_is_checked_out_again(repo, audits):
    """``git worktree remove`` keeps the channel's branch: the next bind checks that
    branch out again at the derived destination, with its commits, instead of
    failing on "Branch already exists"."""
    root = str(repo)
    branch = git_ops.channel_branch("fix the widget", "T1", "C1")
    made = git_ops.create_or_adopt_worktree(root, branch)
    assert not isinstance(made, str), made
    (Path(made.path) / "work.txt").write_text("w\n", newline="\n")
    _git("add", ".", cwd=Path(made.path))
    _git("commit", "-q", "-m", "channel work", cwd=Path(made.path))
    head = _git("rev-parse", "HEAD", cwd=Path(made.path))
    _git("worktree", "remove", made.path, cwd=repo)
    assert not os.path.exists(made.path)
    again = git_ops.create_or_adopt_worktree(root, branch)
    assert not isinstance(again, str), again
    assert os.path.realpath(again.path) == os.path.realpath(made.path)
    assert _git("rev-parse", "HEAD", cwd=Path(again.path)) == head
    assert again.base_sha == made.base_sha
    assert git_ops.is_channel_worktree(root, again.path, branch, "T1", "C1") is True


def test_a_channel_whose_worktree_was_removed_rebinds_to_it_on_the_next_turn(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    h, first, session_dir = _production_bind(tmp_path, monkeypatch, channel_store, str(repo))
    _git("worktree", "remove", "--force", first.worktree, cwd=repo)
    rebuilt = h.run(h.svc.ensure(FakeBinding("C1", project_dir=session_dir)))
    assert rebuilt is not None and rebuilt.worktree_skipped == ""
    assert os.path.realpath(rebuilt.worktree) == os.path.realpath(first.worktree)
    assert os.path.isdir(rebuilt.worktree) and rebuilt.branch == first.branch


# ── channels bound before the workspace existed ─────────────────────────────


def test_existing_bindings_are_prepared_once_at_gateway_start(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    """After an upgrade a channel's first turn may come from the dashboard, which
    admits the session before any hook: the gateway's start prepares every
    stored binding, so that turn already runs in the channel's worktree."""
    from kiro_crew.slack.code_channels.store import Binding
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    channel_store.put_binding(
        Binding(
            channel_id="C1",
            team_id="T1",
            session_key="chat-1",
            anchor_ts="1.0",
            project_dir=str(repo),
            title="Fix the widget",
        )
    )
    channel_store.put_binding(
        Binding(
            channel_id="C2",
            team_id="T1",
            session_key="chat-2",
            anchor_ts="2.0",
            project_dir=str(repo),
            title="Done already",
            archived_at=5.0,
        )
    )
    h = Harness(tmp_path)
    assert h.store.get("T1", "C1") is None
    h.run(ws_core.prepare_existing(h.svc))
    record = h.store.get("T1", "C1")
    assert record is not None and record.worktree and os.path.isdir(record.worktree)
    assert h.projects == [("chat-1", record.worktree)]
    assert h.store.get("T1", "C2") is None  # a finished channel gets no tree back


def test_the_start_sweep_runs_again_once_the_connect_probe_reads_the_scopes(monkeypatch):
    """The workspace is installed before Slack's scopes are known, when the gate
    reads shut and the sweep prepares nothing: recording the scopes sweeps
    again (one at a time), so an upgraded channel's first dashboard turn
    already runs in its worktree."""
    import asyncio

    from kiro_crew.slack import features
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    monkeypatch.setattr(features, "_SCOPE_LISTENERS", [])
    monkeypatch.setattr(ws_core, "_SERVICE", None)
    monkeypatch.setattr(ws_core, "_SWEEP", set())
    monkeypatch.setattr(ws_core, "_SWEEP_AGAIN", False)
    sweeps: list[int] = []
    release = asyncio.Event()

    async def prepare_existing(_service):
        sweeps.append(len(sweeps))
        if len(sweeps) == 2:
            await release.wait()  # the second sweep is still running...

    monkeypatch.setattr(ws_core, "prepare_existing", prepare_existing)

    async def scenario():
        ws_core.install(lambda: None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert sweeps == [0]  # the start sweep, scopes still unknown
        features.record_granted_scopes(None)  # Slack did not say: nothing to do
        await asyncio.sleep(0)
        assert sweeps == [0]
        features.record_granted_scopes(["code_channels:manage"])
        for _ in range(3):
            await asyncio.sleep(0)
        assert sweeps == [0, 1]
        # ...when two more recordings land: they make ONE more sweep, after it.
        features.record_granted_scopes(["code_channels:manage"])
        features.record_granted_scopes(["code_channels:manage"])
        for _ in range(3):
            await asyncio.sleep(0)
        assert sweeps == [0, 1]
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        assert sweeps == [0, 1, 2]
        # A sweep cancelled at shutdown with another pending starts nothing.
        release.clear()
        sweeps.clear()
        sweeps.append(0)  # the next sweep is the "second" one again: it waits
        features.record_granted_scopes(["code_channels:manage"])
        for _ in range(3):
            await asyncio.sleep(0)
        features.record_granted_scopes(["code_channels:manage"])
        await asyncio.sleep(0)
        assert ws_core._SWEEP_AGAIN and len(ws_core._SWEEP) == 1
        for task in list(ws_core._SWEEP):
            task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        assert not ws_core._SWEEP and not ws_core._SWEEP_AGAIN
        assert sweeps == [0, 1]

    asyncio.run(scenario())


def test_a_rebuilt_record_recovers_its_channel_folder_for_the_watcher(
    tmp_path, repo, audits, channel_store, monkeypatch
):
    """The record is lost while the channel's folder survives: the rebuilt record
    finds that folder again (the one whose project is the channel's tree), so the
    watcher keeps refreshing the channel after its workers' turns."""
    from kiro_crew.slack.code_channels.store import Binding
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    h, dash, _made, _statuses = _conductor_harness(tmp_path, monkeypatch)
    monkeypatch.setattr(ws_core, "_SERVICE", h.svc)
    folder_id, session_dir = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="Fix the widget",
            channel_name="fix-widget",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    channel_store.put_binding(
        Binding(
            channel_id="C1",
            team_id="T1",
            session_key="chat-1",
            anchor_ts="1.0",
            project_dir=session_dir,
            repo_root=ws_core.planned_root("T1", "C1"),
        )
    )
    rebuilt = _rebuild(h, session_dir)
    assert rebuilt.worktree and rebuilt.folder_id == ""
    monkeypatch.setattr(service, "_gate_open", lambda: True)
    h.run(h.svc.watch_tick(dash))
    assert h.store.get("T1", "C1").folder_id == folder_id


def test_a_kept_branch_is_never_checked_out_again_in_a_filter_repo(repo, audits, tmp_path):
    """The repository has since declared a checkout filter: the kept-branch path
    refuses as the first cut does, and git never runs the filter."""
    root = str(repo)
    branch = git_ops.channel_branch("fix the widget", "T1", "C1")
    made = git_ops.create_or_adopt_worktree(root, branch)
    assert not isinstance(made, str), made
    # The channel branch itself carries the filtered file, so a checkout of it
    # would run the smudge filter if the refusal did not come first.
    (Path(made.path) / ".gitattributes").write_text("*.md filter=evil\n", newline="\n")
    (Path(made.path) / "notes.md").write_text("hello\n", newline="\n")
    _git("add", ".gitattributes", "notes.md", cwd=made.path)
    _git("commit", "-qm", "attrs", cwd=made.path)
    _git("worktree", "remove", made.path, cwd=repo)
    marker = tmp_path / "smudge-ran"
    (repo / ".gitattributes").write_text("*.md filter=evil\n", newline="\n")
    _git("config", "filter.evil.smudge", f"touch {marker}; cat", cwd=repo)
    result = git_ops.create_or_adopt_worktree(root, branch)
    assert not marker.exists() and not os.path.exists(made.path)
    assert isinstance(result, str) and "filter" in result


def test_an_archived_channel_publishes_again_once_it_is_unarchived(tmp_path, repo, audits):
    """Archiving is undone by unarchiving: a refusal while archived must not
    turn the channel's bar and diff off for good, and nothing is sent while
    the binding is stamped archived."""
    import dataclasses

    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    rec = h.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("changed\n", newline="\n")
    # A turn ending in the instant before the archive event lands.
    h.client.fail["set_context_bar"] = "is_archived"
    h.client.fail["set_diff_view"] = "is_archived"
    h.client.calls.clear()
    h.run(h.svc.refresh(binding))
    assert [m for m, _c, _k in h.client.calls] == ["set_diff_view"]  # refused
    assert h.store.get("T1", "C1").slack_off == ""
    # Archived: nothing is sent at all.
    archived = dataclasses.replace(binding, archived_at=5.0)
    h.client.calls.clear()
    h.run(h.svc.refresh(archived))
    assert h.client.calls == []
    # Unarchived: the diff goes out on the next turn.
    h.client.fail.clear()
    h.run(h.svc.refresh(dataclasses.replace(binding, archived_at=0.0)))
    assert [m for m, _c, _k in h.client.calls] == ["set_diff_view"]
    assert h.store.get("T1", "C1").slack_off == ""
    # A channel first prepared while archived gets its tree but no bar yet.
    other = FakeBinding("C2", project_dir=str(repo), archived_at=5.0)
    h.client.calls.clear()
    assert h.run(h.svc.ensure(other)) is not None
    assert h.client.calls == []


def test_a_dashboard_turn_waits_for_its_workspace_and_is_refused_without_one(monkeypatch):
    """Code channels switched on after the gateway started: the start sweep had
    nothing to prepare, and the turn-start hook runs after admission has read
    the session's project. Admission awaits the workspace's ``prepare`` first --
    directly, with no hook timeout, so a slow ``git worktree add`` is waited
    out -- and a worktree that should exist but does not refuses the turn
    rather than run it in the shared checkout. So does a bind that raised. A
    project that needs no worktree, or whose bind attempts are spent, runs in
    its project as usual."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels import binding as binding_mod
    from kiro_crew.slack.code_channels import hooks
    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord

    outcome: dict = {"record": None, "raise": False}
    ensured: list[tuple[str, bool]] = []

    class _Service:
        async def ensure(self, b, *, strict=False):
            ensured.append((b.channel_id, strict))
            if outcome["raise"]:
                raise RuntimeError("store write failed")
            return outcome["record"]

    monkeypatch.setattr(ws_core, "_SERVICE", _Service())
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    monkeypatch.setattr(ws_core, "_RAISED", {})
    monkeypatch.setattr(hooks, "_PREPARER", [])
    hooks.set_turn_preparer(ws_core.prepare)  # what install() sets
    bound = SimpleNamespace(channel_id="C1", anchor_ts="1.0")
    monkeypatch.setattr(
        binding_mod,
        "active_binding",
        lambda ch, ts: bound if (ch, ts) == ("C1", "1.0") else None,
    )
    links = {"chat-1": ("1.0", "C1"), "chat-2": ("", "")}
    state = SimpleNamespace(sessions=SimpleNamespace(get_slack_link=lambda key: links[key]))
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_utils.effective_session_key", lambda slot: slot.key
    )

    def turn(key):
        return asyncio.run(binding_mod.notify_turn_prepare(state, SimpleNamespace(key=key)))

    rec = lambda **kw: WorkspaceRecord(team_id="T1", channel_id="C1", **kw)  # noqa: E731
    outcome["record"] = rec(worktree="/w/tree")
    assert turn("chat-1") is True and ensured == [("C1", True)]  # awaited, strictly
    outcome["record"] = rec(worktree_skipped="not_a_repo")
    assert turn("chat-1") is True  # needs no worktree: runs in its project
    outcome["record"] = rec(worktree_skipped="git_error", bind_attempts=1)
    assert turn("chat-1") is False  # should have one, a retry is left: refused
    spent = svc_mod._MAX_BIND_ATTEMPTS
    outcome["record"] = rec(worktree_skipped="git_error", bind_attempts=spent)
    assert turn("chat-1") is True  # no retry left: runs in its project, as before
    outcome["raise"] = True
    assert turn("chat-1") is False  # the bind raised: never the shared checkout
    assert turn("chat-2") is True and len(ensured) == 5  # not a code channel


def test_a_repository_that_can_never_get_a_worktree_is_refused_only_while_a_retry_is_left(
    monkeypatch, tmp_path
):
    """A bind error that describes the repository (a content filter, no commit
    to branch from, another directory at the destination) fails every attempt.
    Turns are refused while a retry is left, then run in the project: a refusal
    that tells the user to send again must be one that can pass."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    proj = tmp_path / "repo"
    proj.mkdir()
    monkeypatch.setattr(svc_mod, "_gate_open", lambda: True)
    monkeypatch.setattr(svc_mod, "_archived", lambda b: False)
    monkeypatch.setattr(svc_mod.git_ops, "toplevel", lambda p: str(proj))
    monkeypatch.setattr(svc_mod, "_remembered_root", lambda *a: str(proj))
    monkeypatch.setattr(svc_mod, "is_sensitive_path", lambda p: False)
    monkeypatch.setattr(
        svc_mod.git_ops,
        "create_or_adopt_worktree",
        lambda r, b: "This repository configures a content filter (lfs).",
    )
    service = svc_mod.WorkspaceService(lambda: None, store=WorkspaceStore(tmp_path / "ws.json"))
    monkeypatch.setattr(ws_core, "_SERVICE", service)
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    b = SimpleNamespace(
        team_id="T", channel_id="C1", session_key="s1", project_dir=str(proj), archived_at=0
    )
    spent = svc_mod._MAX_BIND_ATTEMPTS
    results = [asyncio.run(ws_core.prepare(b)) for _ in range(spent + 2)]
    assert results == [False] * (spent - 1) + [True] * 3
    assert service._store.get("T", "C1").bind_attempts == spent


def test_a_context_bar_that_fails_to_publish_never_refuses_a_ready_worktree(monkeypatch, tmp_path):
    """The bar is cosmetic. A git timeout reading its state (or a store write
    after it) is logged; the turn runs in the worktree, which is ready."""
    import asyncio
    import subprocess
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

    tree = tmp_path / "tree"
    tree.mkdir()
    store = WorkspaceStore(tmp_path / "ws.json")
    store.put(
        WorkspaceRecord(
            team_id="T",
            channel_id="C1",
            project_dir="/p",
            worktree=str(tree),
            pointed_session="s1",
            session_key="s1",
        )
    )
    monkeypatch.setattr(svc_mod, "_gate_open", lambda: True)
    monkeypatch.setattr(svc_mod, "_archived", lambda b: False)
    service = svc_mod.WorkspaceService(lambda: object(), store=store)

    async def timed_out(record):
        raise subprocess.TimeoutExpired(["git"], 30)

    monkeypatch.setattr(service, "_bar_state", timed_out)
    monkeypatch.setattr(ws_core, "_SERVICE", service)
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    b = SimpleNamespace(team_id="T", channel_id="C1", session_key="s1", project_dir="/p")
    assert [asyncio.run(ws_core.prepare(b)) for _ in range(2)] == [True, True]
    assert asyncio.run(service.ensure(b)).worktree == str(tree)  # not None either


def test_spent_bind_attempts_do_not_carry_over_to_a_new_project(monkeypatch, tmp_path):
    """Attempts count per project: a channel moved to another repository after
    the first one spent its attempts gets its own retries there."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    monkeypatch.setattr(svc_mod, "_gate_open", lambda: True)
    monkeypatch.setattr(svc_mod, "_archived", lambda b: False)
    monkeypatch.setattr(svc_mod.git_ops, "toplevel", lambda p: p)
    monkeypatch.setattr(svc_mod, "_remembered_root", lambda channel, top, *a: top)
    monkeypatch.setattr(svc_mod, "is_sensitive_path", lambda p: False)
    monkeypatch.setattr(svc_mod.git_ops, "create_or_adopt_worktree", lambda r, b: "git failed")
    service = svc_mod.WorkspaceService(lambda: None, store=WorkspaceStore(tmp_path / "ws.json"))
    monkeypatch.setattr(ws_core, "_SERVICE", service)
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)

    def at(project):
        return SimpleNamespace(
            team_id="T", channel_id="C1", session_key="s1", project_dir=str(project), archived_at=0
        )

    spent = svc_mod._MAX_BIND_ATTEMPTS
    for _ in range(spent):
        asyncio.run(ws_core.prepare(at(first)))
    assert service._store.get("T", "C1").bind_attempts == spent
    assert asyncio.run(ws_core.prepare(at(second))) is False  # a retry is left there
    assert service._store.get("T", "C1").bind_attempts == 1


def test_a_bind_that_always_raises_is_refused_only_while_a_retry_is_left(monkeypatch, tmp_path):
    """A failure that never clears is capped like a git error: the turns it
    refuses are bounded, and a later success clears the count."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    proj = tmp_path / "plain"
    proj.mkdir()
    monkeypatch.setattr(svc_mod, "_gate_open", lambda: True)
    monkeypatch.setattr(svc_mod, "_archived", lambda b: False)
    monkeypatch.setattr(svc_mod.git_ops, "toplevel", lambda p: "")  # not a repository
    service = svc_mod.WorkspaceService(lambda: None, store=WorkspaceStore(tmp_path / "ws.json"))
    real_bind = service._bind

    async def broken(binding):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(service, "_bind", broken)
    monkeypatch.setattr(ws_core, "_SERVICE", service)
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    monkeypatch.setattr(ws_core, "_RAISED", {})
    b = SimpleNamespace(
        team_id="T", channel_id="C1", session_key="s1", project_dir=str(proj), archived_at=0
    )
    spent = svc_mod._MAX_BIND_ATTEMPTS
    results = [asyncio.run(ws_core.prepare(b)) for _ in range(spent + 1)]
    assert results == [False] * (spent - 1) + [True] * 2
    monkeypatch.setattr(service, "_bind", real_bind)  # whatever failed has cleared
    assert asyncio.run(ws_core.prepare(b)) is True
    assert ws_core._RAISED == {}


def test_an_unwritable_store_never_refuses_a_turn_whose_outcome_is_known(monkeypatch, tmp_path):
    """Recording is bookkeeping once the answer is known: a project that needs
    no worktree runs in its project, and a session already pointed at a ready
    worktree runs there, though the workspace store cannot be written."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceRecord, WorkspaceStore

    plain, tree = tmp_path / "plain", tmp_path / "tree"
    plain.mkdir()
    tree.mkdir()
    store = WorkspaceStore(tmp_path / "ws.json")
    store.put(
        WorkspaceRecord(
            team_id="T",
            channel_id="C2",
            project_dir="/p",
            worktree=str(tree),
            pointed_session="old",
            session_key="old",
            bar_hash="h",
        )
    )

    def unwritable(record):
        raise PermissionError("ws.json is read-only")

    monkeypatch.setattr(store, "put", unwritable)
    monkeypatch.setattr(svc_mod, "_gate_open", lambda: True)
    monkeypatch.setattr(svc_mod, "_archived", lambda b: False)
    monkeypatch.setattr(svc_mod.git_ops, "toplevel", lambda p: "")  # not a repository
    service = svc_mod.WorkspaceService(lambda: None, store=store)
    pointed: list[str] = []

    async def point(binding, record):
        pointed.append(binding.session_key)
        return True

    monkeypatch.setattr(service, "_point_session", point)
    monkeypatch.setattr(ws_core, "_SERVICE", service)
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    monkeypatch.setattr(ws_core, "_RAISED", {})
    plain_b = SimpleNamespace(
        team_id="T", channel_id="C1", session_key="s1", project_dir=str(plain), archived_at=0
    )
    assert [asyncio.run(ws_core.prepare(plain_b)) for _ in range(2)] == [True, True]
    taken_over = SimpleNamespace(
        team_id="T", channel_id="C2", session_key="new", project_dir="/p", archived_at=0
    )
    assert asyncio.run(ws_core.prepare(taken_over)) is True
    assert pointed == ["new"]


@pytest.mark.parametrize(
    "status, has_base, refused",
    [
        (409, True, True),  # a content filter, someone else's directory, the branch
        (403, True, True),  # a refused destination
        (400, False, True),  # no commit to branch from
        (400, True, False),  # ``git worktree add`` failed (a lock): retried
        (503, True, False),  # git could not list the worktrees: retried
        (500, True, False),
    ],
)
def test_a_deterministic_refusal_is_told_apart_from_a_git_error(
    monkeypatch, status, has_base, refused
):
    from kiro_crew.dashboard.handlers import worktree as wt
    from kiro_crew.slack.code_channels.workspace import git_ops

    monkeypatch.setattr(wt, "_resolve_base_ref", lambda root: "main")
    monkeypatch.setattr(wt, "_create_worktree_sync", lambda root, branch: ({"error": "no"}, status))
    monkeypatch.setattr(git_ops, "_registered_path_for", lambda root, branch: "")
    monkeypatch.setattr(git_ops, "_checkout_kept_branch", lambda root, branch: "")
    monkeypatch.setattr(
        git_ops,
        "resolve_commit",
        lambda root, ref: "" if ref.startswith("refs/heads/") or not has_base else "abc123",
    )
    made = git_ops.create_or_adopt_worktree("/repo", "slack/x")
    assert made == "no"
    assert isinstance(made, git_ops.WorktreeRefused) is refused


def test_a_kept_branch_that_could_not_be_checked_out_again_is_retried(monkeypatch):
    """The channel's branch survives its worktree (removed by hand): the 409 is
    "Branch already exists", and checking the branch out again failed for a
    reason that can pass (a lock). That is retried, never settled, or the
    channel would run in the shared checkout without its own commits."""
    from kiro_crew.dashboard.handlers import worktree as wt
    from kiro_crew.slack.code_channels.workspace import git_ops

    monkeypatch.setattr(wt, "_resolve_base_ref", lambda root: "main")
    monkeypatch.setattr(
        wt,
        "_create_worktree_sync",
        lambda root, branch: ({"error": f"Branch already exists: {branch}"}, 409),
    )
    monkeypatch.setattr(git_ops, "_registered_path_for", lambda root, branch: "")
    monkeypatch.setattr(git_ops, "_checkout_kept_branch", lambda root, branch: "")  # a lock
    monkeypatch.setattr(git_ops, "resolve_commit", lambda root, ref: "abc123")
    made = git_ops.create_or_adopt_worktree("/repo", "slack/x")
    assert made == "Branch already exists: slack/x"
    assert not isinstance(made, git_ops.WorktreeRefused)
    # No branch at all: the same refusal is a held ref lock, retried too.
    monkeypatch.setattr(
        git_ops, "resolve_commit", lambda root, ref: "" if ref.startswith("refs/") else "abc123"
    )
    made = git_ops.create_or_adopt_worktree("/repo", "slack/x")
    assert made == "Branch already exists: slack/x"
    assert not isinstance(made, git_ops.WorktreeRefused)


def test_a_repository_that_refuses_a_worktree_runs_its_turns_in_the_project_at_once(
    monkeypatch, tmp_path
):
    """No retry can pass a repository's own refusal, so no turn is refused for
    it: the record is settled, audited as denied, and never rebound."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.slack.code_channels.workspace import core as ws_core
    from kiro_crew.slack.code_channels.workspace import git_ops
    from kiro_crew.slack.code_channels.workspace import service as svc_mod
    from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

    proj = tmp_path / "repo"
    proj.mkdir()
    calls: list[str] = []

    def refuse(root, branch):
        calls.append(branch)
        return git_ops.WorktreeRefused("This repository configures a content filter (lfs).")

    monkeypatch.setattr(svc_mod, "_gate_open", lambda: True)
    monkeypatch.setattr(svc_mod, "_archived", lambda b: False)
    monkeypatch.setattr(svc_mod.git_ops, "toplevel", lambda p: str(proj))
    monkeypatch.setattr(svc_mod, "_remembered_root", lambda *a: str(proj))
    monkeypatch.setattr(svc_mod, "is_sensitive_path", lambda p: False)
    monkeypatch.setattr(svc_mod.git_ops, "create_or_adopt_worktree", refuse)
    audits: list[tuple] = []
    monkeypatch.setattr(svc_mod, "_audit", lambda *a: audits.append(a))
    service = svc_mod.WorkspaceService(lambda: None, store=WorkspaceStore(tmp_path / "ws.json"))
    monkeypatch.setattr(ws_core, "_SERVICE", service)
    monkeypatch.setattr(ws_core, "start_watch", lambda: None)
    monkeypatch.setattr(ws_core, "_RAISED", {})
    b = SimpleNamespace(
        team_id="T", channel_id="C1", session_key="s1", project_dir=str(proj), archived_at=0
    )
    assert [asyncio.run(ws_core.prepare(b)) for _ in range(3)] == [True, True, True]
    record = service._store.get("T", "C1")
    assert record.worktree_skipped == svc_mod.WORKTREE_REFUSED and record.bind_attempts == 0
    assert len(calls) == 1  # settled: no further ``git worktree add``
    assert [a[1] for a in audits if a[0] == "code_channel_worktree"] == ["denied"]


def test_a_channel_branch_checked_out_in_the_shared_checkout_is_never_adopted(
    monkeypatch, tmp_path
):
    """The channel's tree was removed and its branch checked out in the main
    checkout: adopting that would run the channel in the shared checkout. Only
    a linked worktree is adopted; the branch held by the main one is not."""
    from kiro_crew.dashboard.handlers import worktree as wt
    from kiro_crew.slack.code_channels.workspace import git_ops

    main, linked = tmp_path / "repo", tmp_path / "repo-wt-slack-x"
    main.mkdir()
    linked.mkdir()
    trees = {str(main): "slack/x"}  # git lists the main checkout first
    monkeypatch.setattr(wt, "_worktree_branches", lambda root: dict(trees))
    assert git_ops._registered_path_for(str(main), "slack/x") is None
    trees[str(linked)] = "slack/y"
    assert git_ops._registered_path_for(str(main), "slack/y") == str(linked)

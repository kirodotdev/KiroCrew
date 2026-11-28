# SPDX-License-Identifier: Apache-2.0
"""An edition can give each Bot worker its own worktree, set it up, and tidy up after it.

``AgentRuntime.worker_worktree_subpath(repo)`` names where each worker's own
worktree goes, below ``worktrees/<slug>/workers/<worker>/``; ``prepare_worktree``
runs after each ``git worktree add`` the gateway makes and ``release_worktree``
after each ``git worktree remove``. A worker's own worktrees go when its session
is closed, and a worker reopened or a gateway restarted at any point of that
cleanup ends up back in its worktree. The shared per-Bot layout is covered in
``test_crewmate_worktree``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import stat
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state
from crewmate_worktree_helpers import (  # noqa: F401 - fixtures
    _commit_in,
    _git,
    _init,
    edition_runtime_fixture,
    git_here_fixture,
    repo_fixture,
)
from test_crewmate_worktree import _scope_env

from kiro_crew import crewmate_worktree as cw
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.platform.defaults import DefaultAgentRuntime

#: Ceiling that only turns a hang into a named failure; every wait here is on a signal.
_HANG_SECS = 60.0


class _PerWorker(DefaultAgentRuntime):
    """A build that needs ``<workspace root>/src/<package>``, the root holding a marker file."""

    def __init__(self, subpath=None, fail_prepare: bool = False) -> None:
        self.subpath = subpath
        self.fail_prepare = fail_prepare
        self.prepared: list[tuple[str, str]] = []
        self.released: list[tuple[str, str]] = []

    def worker_worktree_subpath(self, repo: str):
        return self.subpath if self.subpath is not None else ("src", os.path.basename(repo))

    def prepare_worktree(self, repo: str, path: str) -> None:
        if self.fail_prepare:
            raise RuntimeError("no build workspace")
        self.prepared.append((repo, path))
        if cw.WORKERS_DIR in path.split(os.sep):
            root = os.path.dirname(os.path.dirname(path))
            with open(os.path.join(root, "packageInfo"), "w", encoding="utf-8") as fh:
                fh.write("workspace\n")

    def release_worktree(self, repo: str, path: str) -> None:
        self.released.append((repo, path))
        root = os.path.dirname(os.path.dirname(path))
        if cw.WORKERS_DIR in path.split(os.sep) and not os.listdir(os.path.dirname(path)):
            os.remove(os.path.join(root, "packageInfo"))


def test_each_worker_gets_its_own_worktree_and_branch_at_the_editions_layout(
    repo, tmp_path, edition_runtime
):
    runtime = _PerWorker()
    edition_runtime(runtime)
    ws = str(tmp_path / "ws-atlas")
    real = os.path.realpath(repo)

    first = cw.ensure("atlas", ws, os.path.join(repo, "docs"), worker="w1")
    second = cw.ensure("atlas", ws, repo, worker="w2")

    root = os.path.join(os.path.realpath(ws), "worktrees", "atlas", "workers")
    assert first.path == os.path.join(root, "w1", "src", "tasklog")
    assert first.branch == "crew/atlas/w1/tasklog"
    assert first.cwd == os.path.join(first.path, "docs")
    assert second.path == os.path.join(root, "w2", "src", "tasklog")
    assert second.branch == "crew/atlas/w2/tasklog"
    assert _git(first.path, "branch", "--show-current") == "crew/atlas/w1/tasklog"
    assert runtime.prepared == [(real, first.path), (real, second.path)]
    # Reused as the worker left it: no second add, so no second prepare.
    assert cw.ensure("atlas", ws, repo, worker="w1").path == first.path
    assert len(runtime.prepared) == 2
    # A caller that is no worker still gets the Bot's shared worktree.
    shared = cw.ensure("atlas", ws, repo)
    assert shared.path == cw.path_for(ws, "atlas", repo) and shared.branch == "crew/atlas/tasklog"
    # Both layouts map back to the checkout for the repository's AWS profile binding.
    assert cw.source_path(first.cwd) == os.path.join(real, "docs")
    assert cw.source_path(shared.path) == real


def test_the_public_build_keeps_one_shared_worktree_and_hooks_that_do_nothing(repo):
    runtime = DefaultAgentRuntime()
    assert runtime.worker_worktree_subpath(repo) is None
    assert runtime.prepare_worktree(repo, "/x") is None
    assert runtime.release_worktree(repo, "/x") is None


def test_a_workers_turn_moves_into_its_own_worktree(repo, tmp_path, monkeypatch, edition_runtime):
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    scope, config = _scope_env(monkeypatch, ws)
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, _e: "Atlas")
    state = SimpleNamespace(sessions=SimpleNamespace(get_provider=lambda _k: None))
    slot = SimpleNamespace(project=repo, key="w1", _dirty=False)

    out = asyncio.run(scope.scope_worker_turn(state, slot, config, None, f"edit {repo}/docs"))

    expected = os.path.join(cw.worker_path_for(ws, "atlas", "w1", ("src", "tasklog")), "docs")
    assert out == expected and slot.project == expected
    assert _git(expected, "branch", "--show-current") == "crew/atlas/w1/tasklog"


@pytest.mark.parametrize(
    "answer", [(), ("..",), ("a/b",), ("a", "b", "c", "d"), "src", ("src", 3), ["x y"]]
)
def test_an_answer_that_is_not_a_layout_keeps_the_shared_worktree(
    repo, tmp_path, edition_runtime, answer, caplog
):
    edition_runtime(_PerWorker(subpath=answer))
    ws = str(tmp_path / "ws-atlas")
    with caplog.at_level(logging.WARNING, logger=cw.logger.name):
        found = cw.ensure("atlas", ws, repo, worker="w1")
    assert found.path == cw.path_for(ws, "atlas", repo)
    assert "is not a layout" in caplog.text


def test_an_edition_that_cannot_answer_keeps_the_shared_worktree(repo, tmp_path, edition_runtime):
    class _Raises(DefaultAgentRuntime):
        def worker_worktree_subpath(self, repo: str):
            raise RuntimeError("boom")

    class _Older:  # written before these methods existed
        def works_in_place(self, repo: str) -> bool:
            return False

    for slug, runtime in (("atlas", _Raises()), ("briar", _Older())):
        edition_runtime(runtime)
        ws = str(tmp_path / f"ws-{slug}")
        assert cw.ensure(slug, ws, repo, worker="w1").path == cw.path_for(ws, slug, repo)


def test_a_failed_prepare_undoes_the_add_and_a_later_turn_tries_again(
    repo, tmp_path, edition_runtime
):
    runtime = _PerWorker(fail_prepare=True)
    edition_runtime(runtime)
    ws = str(tmp_path / "ws-atlas")
    dest = cw.worker_path_for(ws, "atlas", "w1", ("src", "tasklog"))

    with pytest.raises(cw.WorktreeRefused, match="could not prepare"):
        cw.ensure("atlas", ws, repo, worker="w1")
    assert not os.path.exists(dest)
    assert _git(repo, "branch", "--list", "crew/*") == ""
    assert dest not in _git(repo, "worktree", "list")

    runtime.fail_prepare = False
    assert cw.ensure("atlas", ws, repo, worker="w1").path == dest
    assert runtime.prepared == [(os.path.realpath(repo), dest)]


def test_a_workers_close_removes_only_its_own_clean_worktrees(repo, tmp_path, edition_runtime):
    runtime = _PerWorker()
    edition_runtime(runtime)
    ws = str(tmp_path / "ws-atlas")
    other = _init(tmp_path / "shared" / "other")
    clean = cw.ensure("atlas", ws, repo, worker="w1")
    dirty = cw.ensure("atlas", ws, other, worker="w1")
    with open(os.path.join(dirty.path, "README.md"), "a", encoding="utf-8") as fh:
        fh.write("unsaved\n")
    sibling = cw.ensure("atlas", ws, repo, worker="w2")
    shared = cw.ensure("atlas", ws, repo)

    removed, kept = cw.remove_worker_worktrees(ws, "atlas", "w1")

    assert removed == [clean.path] and kept == [dirty.path]
    assert runtime.released == [(os.path.realpath(repo), clean.path)]
    assert not os.path.exists(clean.path)
    # The worker's folder stays only for the dirty worktree it still holds.
    assert os.path.exists(os.path.join(dirty.path, "README.md"))
    for path in (sibling.path, shared.path):
        assert os.path.isdir(path)
    assert _git(repo, "branch", "--list", "crew/atlas/w1/tasklog")  # branches stay

    os.remove(os.path.join(dirty.path, "README.md"))
    _git(dirty.path, "checkout", "--", "README.md")
    assert cw.remove_worker_worktrees(ws, "atlas", "w1") == ([dirty.path], [])
    assert not os.path.exists(os.path.dirname(os.path.dirname(dirty.path)))
    assert cw.remove_worker_worktrees(ws, "atlas", "nobody") == ([], [])


def _remove_workers_worktrees(ws: str, bot_deletion: bool) -> tuple[list[str], list[str]]:
    """Worker ``w1``'s worktrees removed by its close, or by deleting the Bot."""
    if bot_deletion:
        return cw.remove_crewmate_worktrees(ws, "atlas")
    return cw.remove_worker_worktrees(ws, "atlas", "w1")


@pytest.mark.parametrize("bot_deletion", [False, True])
def test_removal_keeps_a_detached_worktree_whose_commit_is_on_no_branch(
    repo, tmp_path, edition_runtime, bot_deletion
):
    """``worktree remove`` drops the worktree's HEAD reflog, the commit's last reference."""
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    _git(found.path, "checkout", "-q", "--detach")
    _commit_in(found.path, "orphan.txt", "a commit on no branch")
    orphan = _git(found.path, "rev-parse", "HEAD")
    assert _git(repo, "branch", "--contains", orphan) == ""
    assert _remove_workers_worktrees(ws, bot_deletion) == ([], [found.path])
    assert _git(found.path, "rev-parse", "HEAD") == orphan


@pytest.mark.parametrize("worker", ["", "w1"])
def test_bot_deletion_removes_a_clean_worktree_on_any_branch(
    repo, tmp_path, edition_runtime, worker
):
    """The Bot is gone, so no later worktree depends on the branch; the branch stays."""
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker=worker)
    _git(found.path, "switch", "-q", "-c", "someone-elses")
    assert cw.remove_crewmate_worktrees(ws, "atlas") == ([found.path], [])
    assert _git(repo, "branch", "--list", "someone-elses")


def test_removal_keeps_a_locked_worktree(repo, tmp_path, edition_runtime):
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    _git(repo, "worktree", "lock", found.path)
    assert cw.remove_worker_worktrees(ws, "atlas", "w1") == ([], [found.path])
    _git(repo, "worktree", "unlock", found.path)
    assert cw.remove_worker_worktrees(ws, "atlas", "w1") == ([found.path], [])


@pytest.mark.parametrize("failure", ["exit", "timeout"])
@pytest.mark.parametrize("command", ["worktree", "status", "ls-files"])
def test_removal_keeps_a_worktree_when_a_git_read_fails(
    repo, tmp_path, monkeypatch, edition_runtime, command, failure
):
    """A check git could not answer proves nothing, so the clean worktree stays."""
    from kiro_crew.dashboard.handlers import worktree as wt

    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    real_run_git = wt._run_git
    hits: list[str] = []

    def failing(args, cwd, **kw):
        if command in args and ("list" in args or command != "worktree"):
            hits.append(command)
            if failure == "timeout":
                raise subprocess.TimeoutExpired(["git", *args], 1)
            return subprocess.CompletedProcess(["git", *args], 128, "", "fatal: broken")
        return real_run_git(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", failing)
    assert cw.remove_worker_worktrees(ws, "atlas", "w1") == ([], [found.path])
    assert hits  # the failing read was the one asked
    assert os.path.exists(os.path.join(found.path, "README.md"))
    monkeypatch.setattr(wt, "_run_git", real_run_git)
    assert cw.remove_worker_worktrees(ws, "atlas", "w1") == ([found.path], [])


@pytest.mark.parametrize("bot_deletion", [False, True])
def test_removal_keeps_a_tracked_edit_core_ignore_stat_hides(
    repo, tmp_path, edition_runtime, bot_deletion
):
    """``core.ignoreStat=true`` hides the edit from ``worktree remove``'s own check too."""
    edition_runtime(_PerWorker())
    _git(repo, "config", "core.ignoreStat", "true")
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    tracked = os.path.join(found.path, "docs", "a.md")
    with open(tracked, "w", encoding="utf-8") as fh:
        fh.write("unsaved work\n")
    assert _git(found.path, "status", "--porcelain") == ""  # the config hides the edit
    assert _remove_workers_worktrees(ws, bot_deletion) == ([], [found.path])
    with open(tracked, encoding="utf-8") as fh:
        assert fh.read() == "unsaved work\n"


@pytest.mark.skipif(os.name == "nt", reason="Windows keeps no executable bit")
@pytest.mark.parametrize("bot_deletion", [False, True])
def test_removal_keeps_an_executable_bit_core_file_mode_hides(
    repo, tmp_path, edition_runtime, bot_deletion
):
    """``core.fileMode=false`` hides a chmod from ``worktree remove``'s own check too."""
    edition_runtime(_PerWorker())
    _git(repo, "config", "core.fileMode", "false")
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    tracked = os.path.join(found.path, "docs", "a.md")
    os.chmod(tracked, os.stat(tracked).st_mode | stat.S_IXUSR)
    assert _git(found.path, "status", "--porcelain") == ""  # the config hides the chmod
    assert _remove_workers_worktrees(ws, bot_deletion) == ([], [found.path])
    assert os.stat(tracked).st_mode & stat.S_IXUSR


@pytest.mark.parametrize("bot_deletion", [False, True])
def test_removal_keeps_an_untracked_file_a_status_config_hides(
    repo, tmp_path, edition_runtime, bot_deletion
):
    """``status.showUntrackedFiles=no`` hides the file from ``worktree remove``'s own check too."""
    edition_runtime(_PerWorker())
    _git(repo, "config", "status.showUntrackedFiles", "no")
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    draft = os.path.join(found.path, "docs", "draft.md")
    with open(draft, "w", encoding="utf-8") as fh:
        fh.write("unsaved work\n")
    assert _git(found.path, "status", "--porcelain") == ""  # the config hides the file
    assert _remove_workers_worktrees(ws, bot_deletion) == ([], [found.path])
    with open(draft, encoding="utf-8") as fh:
        assert fh.read() == "unsaved work\n"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("bot_deletion", [False, True])
def test_removal_keeps_a_worktree_holding_an_edit_hidden_by_an_index_flag(
    repo, tmp_path, edition_runtime, flag, bot_deletion
):
    """``git status`` and ``worktree remove`` both skip a flagged file's edit."""
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    found = cw.ensure("atlas", ws, repo, worker="w1")
    tracked = os.path.join(found.path, "docs", "a.md")
    _git(found.path, "update-index", flag, "docs/a.md")
    with open(tracked, "w", encoding="utf-8") as fh:
        fh.write("unsaved work\n")
    assert _git(found.path, "status", "--porcelain") == ""  # the flag hides the edit
    assert _remove_workers_worktrees(ws, bot_deletion) == ([], [found.path])
    with open(tracked, encoding="utf-8") as fh:
        assert fh.read() == "unsaved work\n"


def test_a_failed_prepare_leaves_no_checkout_where_a_tree_walk_cannot_pin(
    repo, tmp_path, monkeypatch, edition_runtime
):
    """Windows: the folder holds a checkout, so git removes it before the folder rollback."""
    from kiro_crew import pinned_fs

    monkeypatch.setattr(pinned_fs, "supports_pinned_tree_walk", lambda: False)
    runtime = _PerWorker(fail_prepare=True)
    edition_runtime(runtime)
    ws = str(tmp_path / "ws-atlas")
    dest = cw.worker_path_for(ws, "atlas", "w1", ("src", "tasklog"))

    with pytest.raises(cw.WorktreeRefused, match="could not prepare"):
        cw.ensure("atlas", ws, repo, worker="w1")
    assert not os.path.exists(dest)
    assert _git(repo, "branch", "--list", "crew/*") == ""
    assert dest not in _git(repo, "worktree", "list")

    runtime.fail_prepare = False
    assert cw.ensure("atlas", ws, repo, worker="w1").path == dest
    assert runtime.prepared == [(os.path.realpath(repo), dest)]


def test_a_workers_close_keeps_a_worktree_it_moved_to_another_branch(
    repo, tmp_path, edition_runtime
):
    """A worktree made again starts on the worker's own branch, so this one is kept."""
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    own = cw.ensure("atlas", ws, repo, worker="w1")
    _git(own.path, "switch", "-q", "-c", "crew/atlas/w1/feature")
    _commit_in(own.path, "feature.txt", "feature work")

    removed, kept = cw.remove_worker_worktrees(ws, "atlas", "w1")

    assert removed == [] and kept == [own.path]
    assert os.path.exists(os.path.join(own.path, "feature.txt"))
    # Back on its own branch, the same worktree is removed as usual.
    _git(own.path, "switch", "-q", own.branch)
    assert cw.remove_worker_worktrees(ws, "atlas", "w1")[0] == [own.path]


def test_removing_the_bot_removes_its_workers_worktrees_too(repo, tmp_path, edition_runtime):
    runtime = _PerWorker()
    edition_runtime(runtime)
    ws = str(tmp_path / "ws-atlas")
    own = cw.ensure("atlas", ws, repo, worker="w1")
    shared = cw.ensure("atlas", ws, repo)

    removed, kept = cw.remove_crewmate_worktrees(ws, "atlas")

    assert sorted(removed) == sorted([own.path, shared.path]) and kept == []
    assert not os.path.exists(os.path.join(os.path.realpath(ws), "worktrees", "atlas"))
    assert len(runtime.released) == 2


def test_worker_folders_never_follow_a_link(repo, tmp_path, edition_runtime):
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    own = cw.ensure("atlas", ws, repo, worker="w1")
    outside = _init(tmp_path / "outside" / "victim")
    os.symlink(os.path.dirname(outside), os.path.join(os.path.dirname(own.path), "link"))

    removed, _kept = cw.remove_worker_worktrees(ws, "atlas", "w1")

    assert removed == [own.path]
    assert os.path.isdir(os.path.join(outside, ".git"))


def test_a_path_at_another_layout_is_not_mapped_to_the_checkout(repo, tmp_path, edition_runtime):
    runtime = _PerWorker()
    edition_runtime(runtime)
    ws = str(tmp_path / "ws-atlas")
    own = cw.ensure("atlas", ws, repo, worker="w1")
    runtime.subpath = ("pkg", "tasklog")  # the layout the edition gives now
    assert cw.source_path(own.path) == own.path


def _worker_execution():
    return SimpleNamespace(
        selection_kind="template", template_id=MATE_WORKER_AGENT, memory_mode="persistent"
    )


class _Log:
    """The conversation log's guarded metadata write, recorded."""

    def __init__(self, project: str) -> None:
        self.meta = {"project": project}

    def update_metadata_if(self, _key, fields, guard) -> bool:
        if not guard(dict(self.meta)):
            return False
        self.meta.update(fields)
        return True

    def get_metadata(self, _key) -> dict:
        return dict(self.meta)


def _closed_worker(key: str, project: str, **extra):
    return SimpleNamespace(**{"key": key, "agent": MATE_WORKER_AGENT, "project": project, **extra})


@pytest.mark.asyncio
async def test_closing_a_worker_removes_its_own_worktrees_and_a_resume_rebuilds_it(
    repo, tmp_path, monkeypatch, edition_runtime
):
    """A persistent worker has no in-memory carrier; its durable record is what is read."""
    from kiro_crew import execution_context
    from kiro_crew.config import loader
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    own = cw.ensure("atlas", ws, os.path.join(repo, "docs"), worker="w1")
    _commit_in(own.path, "work.txt", "the worker's commit")
    _scope, config = _scope_env(monkeypatch, ws)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda _cls: config))
    assert execution_context.read_live_session_execution("dashboard:w1") is None
    read: list[str] = []

    def durable(key: str, **_kw):
        read.append(key)
        return _worker_execution()

    monkeypatch.setattr(execution_context, "read_session_execution", durable)
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, ex: "Atlas" if ex else "")
    log = _Log(own.cwd)
    state = SimpleNamespace(get_slot=lambda _k: None, _background_tasks=set(), conversation_log=log)

    for skipped in (
        _closed_worker("w1", own.cwd, agent="kirocrew"),
        _closed_worker("w1", own.cwd, _project_picked=True),
    ):
        scope.release_closed_worker(state, skipped, "dashboard:w1")
    assert state._background_tasks == set() and os.path.isdir(own.path)

    scope.release_closed_worker(state, _closed_worker("w1", own.cwd), "dashboard:w1")
    await asyncio.gather(*state._background_tasks)

    assert read == ["dashboard:w1"]
    assert not os.path.exists(own.path)
    # The saved project is the same folder in the checkout, so a resume finds the repository.
    real = os.path.realpath(repo)
    assert log.meta["project"] == os.path.join(real, "docs")
    again = cw.ensure("atlas", ws, log.meta["project"], worker="w1")
    assert again.path == own.path and again.branch == own.branch
    assert os.path.exists(os.path.join(again.path, "work.txt"))


class _GatewayStopped(Exception):
    """Raised at a held gate by :meth:`_Lifecycle.restart`: the cleanup dies there."""


class _Lifecycle:
    """One worker with its own worktree, closed and reopened under a controllable cleanup.

    ``hold`` names a point the cleanup stops at until ``release()``: ``"read"`` (before
    the execution read, so the cleanup is queued), ``"write"`` (the saved project is
    about to move) or ``"remove"`` (the removal is about to run). ``restart()`` ends
    a held cleanup at its gate and awaits it, then drops the in-memory state the way
    a gateway restart does, keeping only the disk.
    """

    def __init__(self, repo, ws, monkeypatch, config) -> None:
        import threading

        from kiro_crew import execution_context
        from kiro_crew.dashboard import crewmate_worktree_scope as scope

        self.scope, self.repo, self.ws, self.config = scope, repo, ws, config
        self.loop = asyncio.get_running_loop()
        self.reached = asyncio.Event()
        self.go = threading.Event()
        self.go.set()
        self.hold = ""
        self.stopped = False
        self.removals = 0
        self.own = cw.ensure("atlas", ws, os.path.join(repo, "docs"), worker="w1")
        _commit_in(self.own.path, "work.txt", "the worker's commit")
        self.log = _Log(self.own.cwd)
        self._fresh_state()

        def gate(point: str) -> None:
            if self.hold == point:
                self.loop.call_soon_threadsafe(self.reached.set)
                assert self.go.wait(_HANG_SECS)
                if self.stopped:
                    raise _GatewayStopped(point)

        def read(_key, **_kw):
            gate("read")
            return _worker_execution()

        real_write, real_remove = self.log.update_metadata_if, cw.remove_for_worker

        def write(*args):
            gate("write")
            return real_write(*args)

        def remove(*args):
            gate("remove")
            self.removals += 1
            return real_remove(*args)

        monkeypatch.setattr(execution_context, "read_session_execution", read)
        monkeypatch.setattr(self.log, "update_metadata_if", write)
        monkeypatch.setattr(cw, "remove_for_worker", remove)

    def _fresh_state(self) -> None:
        self.slots: dict = {}
        self.state = SimpleNamespace(
            get_slot=self.slots.get,
            _slots=self.slots,
            _background_tasks=set(),
            conversation_log=self.log,
            sessions=SimpleNamespace(get_provider=lambda _k: None),
            _slots_under_construction=set(),
        )

    async def close(self, hold: str = "") -> None:
        slot = self.slots.pop("w1", None)
        project = slot.project if slot is not None else self.log.meta["project"]
        self.hold, self.reached = hold, asyncio.Event()
        if hold:
            self.go.clear()
        self.scope.release_closed_worker(self.state, _closed_worker("w1", project), "dashboard:w1")
        if hold:
            await asyncio.wait_for(self.reached.wait(), _HANG_SECS)

    def reopen(self):
        slot = SimpleNamespace(
            key="w1", project=self.log.meta["project"], _dirty=False, agent=MATE_WORKER_AGENT
        )
        self.slots["w1"] = slot
        return slot

    def release(self) -> None:
        self.go.set()

    async def settle(self) -> None:
        self.release()
        await asyncio.wait_for(asyncio.gather(*self.state._background_tasks), _HANG_SECS)

    async def restart(self) -> None:
        self.stopped = True
        self.release()
        old = tuple(self.state._background_tasks)
        await asyncio.wait_for(asyncio.gather(*old, return_exceptions=True), _HANG_SECS)
        self.stopped = False
        self._fresh_state()

    async def turn(self, slot) -> None:
        execution = _worker_execution()
        await asyncio.wait_for(
            self.scope.scope_worker_turn(self.state, slot, self.config, execution, "carry on"),
            _HANG_SECS,
        )

    def assert_back_in_its_worktree(self, slot) -> None:
        assert slot.project == self.own.cwd and os.path.isdir(slot.project)
        assert _git(self.own.path, "branch", "--show-current") == self.own.branch
        assert os.path.exists(os.path.join(self.own.path, "work.txt"))


@pytest.fixture
def lifecycle_env(repo, tmp_path, monkeypatch, edition_runtime):
    """What a :class:`_Lifecycle` needs; ``async with _lifecycle(env)`` builds and settles it."""
    from kiro_crew.config import loader
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    _scope, config = _scope_env(monkeypatch, ws)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda _cls: config))
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, ex: "Atlas" if ex else "")
    return repo, ws, monkeypatch, config


@contextlib.asynccontextmanager
async def _lifecycle(env):
    """A :class:`_Lifecycle`, its background cleanups released and awaited on exit."""
    life = _Lifecycle(*env)
    try:
        yield life
    finally:
        life.release()
        await asyncio.wait_for(asyncio.gather(*life.state._background_tasks), _HANG_SECS)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["read", "write", "remove", "finished"])
async def test_a_worker_reopened_at_any_point_of_its_cleanup_is_back_in_its_worktree(
    lifecycle_env, point
):
    """Reopened while the cleanup is queued, about to move the project, removing, or done."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        await _reopen_at(lifecycle, point)


async def _reopen_at(lifecycle, point: str) -> None:
    await lifecycle.close(hold="" if point == "finished" else point)
    if point == "finished":
        await lifecycle.settle()
    slot = lifecycle.reopen()
    turn = asyncio.create_task(lifecycle.turn(slot))
    await lifecycle.settle()
    await turn
    lifecycle.assert_back_in_its_worktree(slot)
    # The turn waited for the whole cleanup, removal included, then made it again.
    assert lifecycle.removals == 1
    _assert_key_free(lifecycle.state)
    assert not getattr(lifecycle.state, "_worker_moves", {})


def _assert_key_free(state) -> None:
    assert not getattr(state, "_worker_key_holders", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["read", "write", "remove", "finished"])
async def test_a_project_the_cleanup_moves_while_the_turn_waits_is_reported_to_the_turn(
    lifecycle_env, monkeypatch, point
):
    """The turn refreshes its binding only for a project the scope returns, lookup or not.

    Reopened at each point of the cleanup with the lookup then finding nothing: any
    project the wait changed is the one returned, and an unchanged one returns ``""``.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        await lifecycle.close(hold="" if point == "finished" else point)
        if point == "finished":
            await lifecycle.settle()
        slot = lifecycle.reopen()
        before = slot.project
        monkeypatch.setattr(lifecycle.scope, "resolve_worker_project", lambda *_a, **_k: None)
        turn = asyncio.create_task(
            lifecycle.scope.scope_worker_turn(
                lifecycle.state, slot, lifecycle.config, _worker_execution(), "carry on"
            )
        )
        await lifecycle.settle()
        returned = await asyncio.wait_for(turn, _HANG_SECS)
        assert returned == (slot.project if slot.project != before else "")
        _assert_key_free(lifecycle.state)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["", "read", "write", "remove"])
async def test_a_resume_still_building_its_slot_loses_nothing(lifecycle_env, point):
    """A resume reserves the key and may hold its slot outside ``_slots`` while it awaits.

    Reserved before the close or at each point of the cleanup: the work is on the
    worker's branch, and the saved project leads back to it, so a restart before
    the next turn loses nothing and the turn is back in its worktree.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        leads_back = (lifecycle.own.cwd, cw.source_path(lifecycle.own.cwd))
        reserved = lifecycle.state._slots_under_construction
        if not point:
            reserved.add("w1")
        await lifecycle.close(hold=point)
        reserved.add("w1")
        await lifecycle.settle()
        assert lifecycle.log.meta["project"] in leads_back
        reserved.discard("w1")
        await lifecycle.restart()
        slot = lifecycle.reopen()
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_turn_waits_for_its_cleanup_however_long_it_runs(lifecycle_env):
    """No time limit on the wait: the turn never starts in a folder still being removed."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        await lifecycle.close(hold="remove")
        slot = lifecycle.reopen()
        ahead = _last_in_line(lifecycle.state)
        turn = asyncio.create_task(lifecycle.turn(slot))
        await _until_in_line_behind(lifecycle.state, ahead)
        assert not turn.done()
        await lifecycle.settle()
        await asyncio.wait_for(turn, _HANG_SECS)
        lifecycle.assert_back_in_its_worktree(slot)
        assert lifecycle.removals == 1 and not getattr(lifecycle.state, "_worker_moves", {})


def _last_in_line(state, worker: str = "w1"):
    return getattr(state, "_worker_key_holders", {}).get(worker)


async def _until_in_line_behind(state, ahead, worker: str = "w1") -> None:
    """Wait until a new holder or waiter took the place after *ahead* in *worker*'s line."""
    from kiro_crew.testing.wait import async_wait_until

    await async_wait_until(lambda: _last_in_line(state, worker) not in (None, ahead))


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_it_waits_for_the_cleanup_leaves_the_next_turn_whole(
    lifecycle_env,
):
    async with _lifecycle(lifecycle_env) as lifecycle:
        await lifecycle.close(hold="remove")
        slot = lifecycle.reopen()
        ahead = _last_in_line(lifecycle.state)
        turn = asyncio.create_task(lifecycle.turn(slot))
        await _until_in_line_behind(lifecycle.state, ahead)
        behind = _last_in_line(lifecycle.state)
        later = asyncio.create_task(lifecycle.scope.settle_worker_project(lifecycle.state, slot))
        await _until_in_line_behind(lifecycle.state, behind)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert not later.done()  # the cancelled place passes on only after the cleanup
        await lifecycle.settle()
        await asyncio.wait_for(later, _HANG_SECS)
        _assert_key_free(lifecycle.state)
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)


class _KeyedLog:
    """The conversation log's guarded metadata write, one record per transcript key."""

    def __init__(self) -> None:
        self.meta: dict[str, dict] = {}

    def update_metadata_if(self, key, fields, guard) -> bool:
        current = self.meta.setdefault(key, {})
        if not guard(dict(current)):
            return False
        current.update(fields)
        return True

    def get_metadata(self, key) -> dict:
        return dict(self.meta.get(key, {}))


@pytest.mark.asyncio
async def test_two_workers_closing_at_once_on_the_same_folder_each_resume_in_their_own(
    lifecycle_env,
):
    """Both moves name the same checkout folder; each worker's own record leads it back."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        from kiro_crew.dashboard.chat_utils import slot_history_key

        repo, ws = lifecycle.repo, lifecycle.ws
        second = cw.ensure("atlas", ws, os.path.join(repo, "docs"), worker="w2")
        _commit_in(second.path, "work.txt", "the second worker's commit")
        log = _KeyedLog()
        lifecycle.state.conversation_log = log
        own = {"w1": lifecycle.own, "w2": second}
        for worker, tree in own.items():
            closed = _closed_worker(worker, tree.cwd)
            log.meta[slot_history_key(closed)] = {"project": tree.cwd}
            lifecycle.scope.release_closed_worker(lifecycle.state, closed, f"dashboard:{worker}")
        await lifecycle.settle()
        assert lifecycle.removals == 2
        for worker, tree in own.items():
            slot = SimpleNamespace(
                key=worker, project=log.meta[f"dashboard:{worker}"]["project"], _dirty=False
            )
            lifecycle.slots[worker] = slot
            await lifecycle.turn(slot)
            assert slot.project == tree.cwd and os.path.isdir(slot.project)
            assert _git(tree.path, "branch", "--show-current") == tree.branch
            assert os.path.exists(os.path.join(tree.path, "work.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["read", "write", "remove", "finished"])
async def test_a_restart_at_any_point_of_the_cleanup_resumes_into_its_worktree(
    lifecycle_env, point
):
    """The cleanup dies with the gateway; the saved project is all the resume has."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        await _restart_at(lifecycle, point)


async def _restart_at(lifecycle, point: str) -> None:
    await lifecycle.close(hold="" if point == "finished" else point)
    if point == "finished":
        await lifecycle.settle()
    await lifecycle.restart()
    slot = lifecycle.reopen()
    await lifecycle.turn(slot)
    lifecycle.assert_back_in_its_worktree(slot)
    assert lifecycle.removals == (1 if point == "finished" else 0)


@pytest.mark.asyncio
async def test_two_quick_close_and_reopen_cycles_end_back_in_the_worktree(lifecycle_env):
    async with _lifecycle(lifecycle_env) as lifecycle:
        await _two_quick_cycles(lifecycle)


async def _two_quick_cycles(lifecycle) -> None:
    await lifecycle.close(hold="remove")
    first = lifecycle.reopen()
    await lifecycle.close()  # closed again before the first cleanup finished
    second = lifecycle.reopen()
    turn = asyncio.create_task(lifecycle.turn(second))
    await lifecycle.settle()
    await turn
    lifecycle.assert_back_in_its_worktree(second)
    assert first is not second


@pytest.mark.asyncio
async def test_a_second_close_cleans_up_after_the_first_and_the_turn_after_both(lifecycle_env):
    """Closed, reopened, closed again: the cleanups take the key in order, the turn last."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        await lifecycle.close(hold="remove")  # the first cleanup, held before its removal
        first = set(lifecycle.state._background_tasks)
        lifecycle.reopen()
        await lifecycle.close()  # the second, in line behind the first
        second = set(lifecycle.state._background_tasks) - first
        slot = lifecycle.reopen()
        ahead = _last_in_line(lifecycle.state)
        waiting = asyncio.create_task(lifecycle.scope.settle_worker_project(lifecycle.state, slot))
        await _until_in_line_behind(lifecycle.state, ahead)
        assert not any(task.done() for task in second) and not waiting.done()
        await lifecycle.settle()
        await asyncio.wait_for(waiting, _HANG_SECS)
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)
        assert lifecycle.removals == 2  # each cleanup removed, the turn came after both


@pytest.mark.asyncio
async def test_a_project_picked_during_the_cleanup_stands_and_its_turn_waits(lifecycle_env):
    """A person picks the old worktree while it is removed: the cleanup's move does not apply."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close(hold="remove")
        slot = lifecycle.reopen()
        slot.project, slot._project_picked = old, True
        ahead = _last_in_line(lifecycle.state)
        turn = asyncio.create_task(lifecycle.turn(slot))
        await _until_in_line_behind(lifecycle.state, ahead)
        assert not turn.done()  # the picked turn waits for the removal too
        await lifecycle.settle()
        await turn
        assert slot.project == old and slot._project_picked
        assert not getattr(lifecycle.state, "_worker_moves", {})


@pytest.mark.asyncio
async def test_another_workers_close_mid_cleanup_keeps_the_first_workers_move(lifecycle_env):
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close(hold="remove")
        other = _closed_worker("w2", "")
        lifecycle.scope.release_closed_worker(lifecycle.state, other, "dashboard:w2")
        await lifecycle.settle()
        # A resume that read the old project during the removal and published its
        # slot only after both cleanups finished: the kept move is still there.
        slot = SimpleNamespace(key="w1", project=old, _dirty=False)
        lifecycle.slots["w1"] = slot
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)
        assert not lifecycle.state._worker_moves


@pytest.mark.asyncio
async def test_a_saved_project_at_a_layout_the_edition_no_longer_gives_keeps_the_worktree(
    lifecycle_env, edition_runtime
):
    """No checkout to move the saved project to, so nothing it names is removed."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        edition_runtime(_PerWorker(subpath=("pkg", "tasklog")))
        await lifecycle.close()
        await lifecycle.settle()
        assert lifecycle.removals == 0 and os.path.isdir(lifecycle.own.path)
        assert lifecycle.log.meta["project"] == lifecycle.own.cwd


@pytest.mark.asyncio
async def test_a_saved_project_only_the_workers_branch_has_keeps_the_worktree(lifecycle_env):
    """A package committed on the worker's branch has no checkout folder to resume in."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        package = os.path.join(lifecycle.own.path, "newpkg")
        os.mkdir(package)
        _commit_in(package, "a.txt", "a new package")
        lifecycle.log.meta["project"] = package
        await lifecycle.close()
        await lifecycle.settle()
        assert lifecycle.removals == 0 and os.path.isdir(package)
        assert lifecycle.log.meta["project"] == package


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="needs a symlink an unprivileged user can make")
async def test_a_saved_project_a_symlink_leads_out_of_the_checkout_keeps_the_worktree(
    lifecycle_env, tmp_path
):
    """The checkout's folder is now a link elsewhere, so resuming there would leave the repository."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        package = os.path.join(lifecycle.own.path, "pkg")
        os.mkdir(package)
        _commit_in(package, "a.txt", "a package")
        elsewhere = tmp_path / "not-a-repository"
        elsewhere.mkdir()
        os.symlink(str(elsewhere), os.path.join(cw.repo_root(lifecycle.own.path), "pkg"))
        lifecycle.log.meta["project"] = package
        await lifecycle.close()
        await lifecycle.settle()
        assert lifecycle.removals == 0 and os.path.isdir(package)
        assert lifecycle.log.meta["project"] == package


def _checkout_folder_gone(repo, _tmp_path):
    shutil.rmtree(os.path.join(repo, "docs"))


def _checkout_folder_a_file(repo, _tmp_path):
    shutil.rmtree(os.path.join(repo, "docs"))
    with open(os.path.join(repo, "docs"), "w", encoding="utf-8") as fh:
        fh.write("a file now\n")


def _checkout_folder_a_link_out(repo, tmp_path):
    shutil.rmtree(os.path.join(repo, "docs"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), os.path.join(repo, "docs"))


def _checkout_folder_a_link_to_another_repository(repo, tmp_path):
    shutil.rmtree(os.path.join(repo, "docs"))
    os.symlink(_init(tmp_path / "another-repository"), os.path.join(repo, "docs"))


def _checkout_folder_another_repository(repo, _tmp_path):
    _git(repo, "init", "-q", os.path.join(repo, "docs"))


def _checkout_folder_unreadable(repo, _tmp_path):
    os.chmod(os.path.join(repo, "docs"), 0)


def _checkout_folder_renamed_in_case(repo, _tmp_path):
    os.rename(os.path.join(repo, "docs"), os.path.join(repo, "Docs"))


def _checkout_on_a_branch_without_it(repo, _tmp_path):
    _git(repo, "switch", "-q", "-c", "no-docs")
    _git(repo, "rm", "-q", "-r", "docs")
    _git(repo, "commit", "-q", "-m", "drop docs")


def _checkout_stopped_mid_rebase(repo, _tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-c", "side")
    _commit_in(repo, "README.md", "side")
    _git(repo, "switch", "-q", "--detach", base)
    _commit_in(repo, "README.md", "main")
    rebase = subprocess.run(["git", "rebase", "side"], cwd=repo, capture_output=True, check=False)
    assert rebase.returncode != 0  # stopped on the conflict, mid-rebase


_CHECKOUT_CHANGES = [
    _checkout_stopped_mid_rebase,
    _checkout_folder_gone,
    _checkout_folder_a_file,
    _checkout_folder_a_link_out,
    _checkout_folder_a_link_to_another_repository,
    _checkout_folder_another_repository,
    _checkout_folder_unreadable,
    _checkout_folder_renamed_in_case,
    _checkout_on_a_branch_without_it,
]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="needs POSIX links and permission bits")
@pytest.mark.parametrize("when", ["before the close", "between the close and the resume"])
@pytest.mark.parametrize("change", _CHECKOUT_CHANGES, ids=lambda change: change.__name__[1:])
async def test_a_resumed_worker_is_back_in_its_own_folder_whatever_the_checkout_holds(
    lifecycle_env, tmp_path, change, when
):
    """The worktree comes from the repository and the folder's path in it, never the checkout.

    Each change leaves the checkout's ``docs`` something other than a folder of this
    repository that a lookup could follow back: gone, a file, a link out or into another
    repository, another repository itself, unreadable, renamed in case, or absent
    on the branch the checkout switched to.
    """
    if os.geteuid() == 0 and change is _checkout_folder_unreadable:
        pytest.skip("root reads past the permission bits")
    repo = lifecycle_env[0]
    async with _lifecycle(lifecycle_env) as lifecycle:
        try:
            if when == "before the close":
                change(repo, tmp_path)
            await lifecycle.close()
            await lifecycle.settle()
            if when != "before the close":
                change(repo, tmp_path)
            slot = lifecycle.reopen()
            await lifecycle.turn(slot)
        finally:
            docs = os.path.join(repo, "docs")
            if os.path.isdir(docs) and not os.path.islink(docs):
                os.chmod(docs, stat.S_IRWXU)
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_saved_project_that_moved_on_keeps_the_worktree(lifecycle_env):
    """Saved with another project since this close: that close's own cleanup owns it."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        lifecycle.reopen()  # its slot still names the worktree
        lifecycle.log.meta["project"] = "/elsewhere"
        await lifecycle.close()
        await lifecycle.settle()
        assert lifecycle.removals == 0 and os.path.isdir(lifecycle.own.path)
        assert lifecycle.log.meta["project"] == "/elsewhere"


@pytest.mark.asyncio
async def test_closing_a_worker_in_the_bots_shared_worktree_leaves_its_project_alone(
    repo, tmp_path, monkeypatch, edition_runtime
):
    """The public build: nothing is removed on a close, so the saved project stays put."""
    from kiro_crew import execution_context
    from kiro_crew.config import loader
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    edition_runtime(DefaultAgentRuntime())
    ws = str(tmp_path / "ws-atlas")
    shared = cw.ensure("atlas", ws, os.path.join(repo, "docs"), worker="w1")
    assert shared.path == cw.path_for(ws, "atlas", repo)
    _scope, config = _scope_env(monkeypatch, ws)
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda _cls: config))
    monkeypatch.setattr(
        execution_context, "read_session_execution", lambda _k, **_kw: _worker_execution()
    )
    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, ex: "Atlas" if ex else "")
    mapped: list[str] = []
    monkeypatch.setattr(cw, "source_path", lambda p: mapped.append(p) or p)
    log = _Log(shared.cwd)
    state = SimpleNamespace(
        get_slot=lambda _k: None, _slots={}, _background_tasks=set(), conversation_log=log
    )

    scope.release_closed_worker(state, _closed_worker("w1", shared.cwd), "dashboard:w1")
    await asyncio.wait_for(asyncio.gather(*state._background_tasks), _HANG_SECS)

    assert log.meta["project"] == shared.cwd and os.path.isdir(shared.path)
    assert mapped == [] and not getattr(state, "_worker_moves", {})


def test_kept_moves_are_bounded(monkeypatch):
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    monkeypatch.setattr(scope, "_MAX_KEPT_MOVES", 3)
    state = SimpleNamespace(_slots={})
    for i in range(5):
        scope._remember_move(state, f"w{i}", (f"/old{i}", f"/new{i}"))
    assert list(state._worker_moves) == ["w2", "w3", "w4"]
    live = SimpleNamespace(project="/old9", _dirty=False)
    state._slots["w9"] = live
    scope._remember_move(state, "w9", ("/old9", "/new9"))
    assert live.project == "/new9" and live._dirty and "w9" not in state._worker_moves


#: Each start that reads a slot's project ahead of the slot's next turn, and the
#: first read of it: ``(module, function, read)``, where ``read`` is a call name or
#: ``slot.project``. A worker turn reads it in ``scope_worker_turn`` itself.
_PROJECT_READERS_AHEAD_OF_A_TURN = [
    ("kiro_crew.dashboard.chat_turn.eager_spawn", "_eager_spawn", "_slot_binding"),
    ("kiro_crew.dashboard.chat_threads", "_thread_turn_body", "slot.project"),
    ("kiro_crew.dashboard.handlers.side", "_run_side_turn", "slot.project"),
]


@pytest.mark.parametrize(
    ("module", "function", "read"), _PROJECT_READERS_AHEAD_OF_A_TURN, ids=lambda v: str(v)
)
def test_a_start_ahead_of_a_turn_settles_a_workers_project_before_reading_it(
    module, function, read
):
    import ast
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module))
    tree = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function
    )

    def lines(match) -> list[int]:
        return sorted(node.lineno for node in ast.walk(tree) if match(node))

    settles = lines(
        lambda n: isinstance(n, ast.Await)
        and isinstance(n.value, ast.Call)
        and getattr(n.value.func, "id", "") == "settle_worker_project"
    )
    if read == "slot.project":
        reads = lines(
            lambda n: isinstance(n, ast.Attribute)
            and n.attr == "project"
            and getattr(n.value, "id", "") == "slot"
        )
    else:
        reads = lines(lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", "") == read)
    assert settles and reads, (settles, reads)
    assert settles[0] < reads[0]


@pytest.mark.asyncio
async def test_a_worker_reopened_during_the_rest_of_its_close_waits_for_the_cleanup(
    lifecycle_env,
):
    """The cleanup's place is taken at the slot's removal, before the close's teardown awaits.

    A resume and its turn in that window (the teardown, the hand-back) wait for the
    cleanup, which then removes nothing the turn is running in.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        scope = lifecycle.scope
        closed = _closed_worker("w1", lifecycle.log.meta["project"])
        place = scope.reserve_worker_cleanup(lifecycle.state, closed)  # at the removal
        slot = lifecycle.reopen()
        turn = asyncio.create_task(lifecycle.turn(slot))
        await _until_in_line_behind(lifecycle.state, place[1])
        assert not turn.done()  # the rest of the close is still running
        scope.release_closed_worker(lifecycle.state, closed, "dashboard:w1", place)
        await lifecycle.settle()
        await asyncio.wait_for(turn, _HANG_SECS)
        lifecycle.assert_back_in_its_worktree(slot)
        assert lifecycle.removals == 1
        _assert_key_free(lifecycle.state)


@pytest.mark.asyncio
async def test_a_pin_saving_the_old_path_during_the_cleanup_still_resumes_in_the_worktree(
    lifecycle_env,
):
    """A slot save between the move and the removal writes the old worktree path back.

    The removal then deletes that folder and a restart resumes on it; the turn makes
    the worktree again at the same path from the recorded repository.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close(hold="remove")  # the move to the checkout is written
        lifecycle.log.meta["project"] = old  # a pin's save of the slot still on the old path
        await lifecycle.settle()
        assert not os.path.exists(old)
        await lifecycle.restart()
        slot = lifecycle.reopen()
        assert slot.project == old
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_project_picked_during_the_cleanup_is_made_again_for_its_turn(lifecycle_env):
    """A person picks the old worktree while it is removed: the pick stands and exists again."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close(hold="remove")
        slot = lifecycle.reopen()
        slot.project, slot._project_picked = old, True
        lifecycle.log.meta["project"] = old  # the pick is saved
        await lifecycle.settle()
        assert not os.path.exists(old)
        await lifecycle.turn(slot)
        assert slot.project == old and slot._project_picked
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_pick_saved_before_the_cleanup_moves_the_project_keeps_the_worktree(
    lifecycle_env,
):
    """Reopened while the cleanup is queued and its worktree picked: the pick is never moved.

    The saved project string is unchanged by the pick, so only the saved pick flag
    tells the cleanup; it keeps the worktree, and a restart resumes on the pick.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close(hold="write")  # queued before the guarded move
        lifecycle.log.meta["project_picked"] = True  # a save of the reopened, picked slot
        await lifecycle.settle()
        assert lifecycle.log.meta["project"] == old and lifecycle.removals == 0
        await lifecycle.restart()
        slot = lifecycle.reopen()
        slot._project_picked = True
        await lifecycle.turn(slot)
        assert slot.project == old
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_picked_removed_worktree_a_start_made_empty_is_made_again(lifecycle_env):
    """An agent start in the picked, removed folder makes it again, empty, without a .git.

    The next turn still rebuilds the worktree there: a place with no ``.git`` is a
    missing worktree, and only its empty folders are cleared.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close(hold="remove")
        slot = lifecycle.reopen()
        slot.project, slot._project_picked = old, True
        lifecycle.log.meta["project"] = old
        await lifecycle.settle()
        os.makedirs(old)  # what the agent start's ensure_directory leaves behind
        await lifecycle.turn(slot)
        assert slot.project == old and slot._project_picked
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_start_ahead_of_the_turn_rebuilds_a_removed_worktree_first(lifecycle_env):
    """No cleanup queued and no move left: the pre-turn settle still makes the worktree again."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close()
        await lifecycle.settle()
        lifecycle.log.meta["project"] = old  # a late save of the old path
        await lifecycle.restart()
        slot = lifecycle.reopen()
        slot._project_picked = True
        assert not os.path.exists(old)
        await asyncio.wait_for(
            lifecycle.scope.settle_worker_project(lifecycle.state, slot), _HANG_SECS
        )
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("picked", [False, True], ids=["moved", "picked"])
async def test_a_start_ahead_of_the_turn_goes_back_from_the_checkout_to_the_worktree(
    lifecycle_env, picked
):
    """A clean close moved the project into the checkout; a side chat or thread reply starts next.

    The start reads the worker's own worktree, where its commit is, not the
    checkout. A person's pick of the checkout path stays where it is.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        await lifecycle.close()
        await lifecycle.settle()
        checkout = lifecycle.log.meta["project"]
        assert checkout != lifecycle.own.cwd
        assert not os.path.exists(os.path.join(checkout, "work.txt"))
        slot = lifecycle.reopen()
        slot._project_picked = picked
        await asyncio.wait_for(
            lifecycle.scope.settle_worker_project(lifecycle.state, slot), _HANG_SECS
        )
        if picked:
            assert slot.project == checkout
        else:
            lifecycle.assert_back_in_its_worktree(slot)
        _assert_key_free(lifecycle.state)


@pytest.mark.asyncio
async def test_a_restart_on_a_removed_worktrees_path_resumes_in_the_worktree(lifecycle_env):
    """The cleanup finished; a late save of the old path is what the restart resumes on."""
    async with _lifecycle(lifecycle_env) as lifecycle:
        old = lifecycle.own.cwd
        await lifecycle.close()
        await lifecycle.settle()
        assert lifecycle.log.meta["project"] != old and not os.path.exists(old)
        lifecycle.log.meta["project"] = old
        await lifecycle.restart()
        slot = lifecycle.reopen()
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)


def test_a_rebuild_refuses_a_path_outside_the_workers_own_folder(repo, tmp_path, edition_runtime):
    """Outside by its path, or through a link on the way: refused, and nothing is made."""
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    own = cw._worker_dir(ws, "atlas", "w1")
    outside = tmp_path / "outside"
    outside.mkdir()
    for project in (str(outside / "docs"), os.path.join(repo, "gone")):
        with pytest.raises(cw.WorktreeRefused):
            cw.rebuild_worker_worktree("atlas", ws, repo, project, "w1")
    assert not os.path.lexists(own)
    if os.name == "nt":
        return  # an unprivileged Windows user cannot make the link
    os.makedirs(os.path.dirname(own))
    os.symlink(str(outside), own)
    expected = cw.worker_path_for(ws, "atlas", "w1", _PerWorker().worker_worktree_subpath(repo))
    with pytest.raises(cw.WorktreeRefused):
        cw.rebuild_worker_worktree("atlas", ws, repo, os.path.join(expected, "docs"), "w1")
    assert os.listdir(outside) == []


def test_a_rebuild_keeps_a_place_holding_a_file(repo, tmp_path, edition_runtime):
    """Only empty folders at the place are cleared; one holding a file refuses the rebuild."""
    edition_runtime(_PerWorker())
    ws = str(tmp_path / "ws-atlas")
    dest = cw.worker_path_for(ws, "atlas", "w1", _PerWorker().worker_worktree_subpath(repo))
    note = os.path.join(dest, "docs", "note.txt")
    _write_file(note, "someone's notes\n")
    with pytest.raises(cw.WorktreeRefused):
        cw.rebuild_worker_worktree("atlas", ws, repo, os.path.join(dest, "docs"), "w1")
    assert os.path.isfile(note)


def _write_file(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.mark.asyncio
async def test_a_close_that_does_not_commit_gives_its_cleanup_place_back(lifecycle_env):
    async with _lifecycle(lifecycle_env) as lifecycle:
        scope = lifecycle.scope
        slot = lifecycle.reopen()
        place = scope.reserve_worker_cleanup(lifecycle.state, _closed_worker("w1", slot.project))
        turn = asyncio.create_task(lifecycle.turn(slot))
        await _until_in_line_behind(lifecycle.state, place[1])
        scope.cancel_worker_cleanup(lifecycle.state, slot, place)
        await asyncio.wait_for(turn, _HANG_SECS)
        lifecycle.assert_back_in_its_worktree(slot)
        assert lifecycle.removals == 0
        _assert_key_free(lifecycle.state)


@pytest.mark.asyncio
async def test_close_slot_takes_the_cleanup_place_before_its_teardown_awaits(tmp_path, monkeypatch):
    """The reservation runs inside the pre-pop check, before any teardown await."""
    from kiro_crew.dashboard import chat_handlers as handlers
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock(return_value=True))
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("w1")
    events: list[tuple[str, bool]] = []
    place = object()

    def reserve(_state, closing):
        events.append(("reserve", state.get_slot("w1") is closing))
        return place

    def release(_state, closing, key, given=None):
        events.append(("release", given is place))

    monkeypatch.setattr(scope, "reserve_worker_cleanup", reserve)
    monkeypatch.setattr(scope, "release_closed_worker", release)

    await handlers.close_slot(state, slot, "w1")

    # Reserved while the slot was still in the map, so before the pop and every await after it.
    assert events == [("reserve", True), ("release", True)]


def test_a_worker_turn_looks_up_its_worktree_holding_the_workers_key(monkeypatch):
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    state = SimpleNamespace(conversation_log=None)
    held: list[bool] = []

    def lookup(*_args):
        held.append("w1" in state._worker_key_holders)

    monkeypatch.setattr(scope, "_worker_crewmate", lambda _c, _e: "Atlas")
    monkeypatch.setattr(scope, "resolve_worker_project", lookup)
    slot = SimpleNamespace(project="/r", key="w1", _project_picked=False)
    assert asyncio.run(scope.scope_worker_turn(state, slot, None, None, "")) == ""
    assert held == [True] and not state._worker_key_holders


@pytest.mark.asyncio
@pytest.mark.parametrize("starter", ["turn", "settle"])
@pytest.mark.parametrize(
    "order",
    [
        "resume before the close",
        "close held at read",
        "close held at write",
        "close held at remove",
        "close after its cleanup",
        "close during the turn's lookup",
    ],
)
async def test_a_close_and_a_resume_interleaved_at_every_await_point_never_overlap(
    lifecycle_env, monkeypatch, starter, order
):
    """The cleanup and a turn's lookup (or a start reading the project) never run at once.

    Each order puts the close at a different await point against a resume whose
    first work is a turn or a project read ahead of one (the eager spawn, a thread
    reply, a side chat). Whatever the order, nothing reads the project or looks up
    a worktree while the cleanup runs, and the next turn is back in the worktree.
    """
    async with _lifecycle(lifecycle_env) as lifecycle:
        scope = lifecycle.scope
        running = {"cleanup": 0, "lookup": 0}
        overlaps: list[str] = []
        lookup_entered, lookup_go = asyncio.Event(), threading.Event()
        lookup_go.set()
        real_release, real_lookup = scope._release_worker, scope.resolve_worker_project

        def release(*args):
            running["cleanup"] += 1
            if running["lookup"]:
                overlaps.append("cleanup during a lookup")
            try:
                return real_release(*args)
            finally:
                running["cleanup"] -= 1

        def lookup(*args, **kwargs):
            running["lookup"] += 1
            if running["cleanup"]:
                overlaps.append("lookup during a cleanup")
            lifecycle.loop.call_soon_threadsafe(lookup_entered.set)
            assert lookup_go.wait(_HANG_SECS)
            try:
                return real_lookup(*args, **kwargs)
            finally:
                running["lookup"] -= 1

        monkeypatch.setattr(scope, "_release_worker", release)
        monkeypatch.setattr(scope, "resolve_worker_project", lookup)

        async def resume(slot) -> None:
            if starter == "turn":
                await lifecycle.turn(slot)
                return
            await asyncio.wait_for(scope.settle_worker_project(lifecycle.state, slot), _HANG_SECS)
            if running["cleanup"]:
                overlaps.append("project read during a cleanup")

        if order == "close during the turn's lookup":
            slot = lifecycle.reopen()
            lookup_go.clear()
            first = asyncio.create_task(lifecycle.turn(slot))
            await asyncio.wait_for(lookup_entered.wait(), _HANG_SECS)
            await lifecycle.close()  # the tab closes while its turn holds the key
            lookup_go.set()
            await asyncio.wait_for(first, _HANG_SECS)
        elif order == "resume before the close":
            slot = lifecycle.reopen()
            await lifecycle.close()
            await resume(slot)
        else:
            point = order.rsplit(" ", 1)[-1]
            await lifecycle.close(hold="" if order == "close after its cleanup" else point)
            if order == "close after its cleanup":
                await lifecycle.settle()
            slot = lifecycle.reopen()
            task = asyncio.create_task(resume(slot))
            await lifecycle.settle()
            await asyncio.wait_for(task, _HANG_SECS)
        await lifecycle.settle()
        assert overlaps == []
        _assert_key_free(lifecycle.state)
        slot = lifecycle.reopen()
        await lifecycle.turn(slot)
        lifecycle.assert_back_in_its_worktree(slot)


@pytest.mark.asyncio
async def test_a_close_releases_the_worker_and_an_idle_archive_does_not(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_handlers as handlers
    from kiro_crew.dashboard import crewmate_worktree_scope as scope

    monkeypatch.setattr(handlers, "save_slot_off_loop", AsyncMock(return_value=True))
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        scope,
        "release_closed_worker",
        lambda _s, slot, key, _place=None: released.append((slot.key, key)),
    )
    state = _make_state(tmp_path)

    await handlers.close_slot(state, state.get_or_create_slot("w1"), "w1")
    await handlers.close_slot(
        state, state.get_or_create_slot("w2"), "w2", hand_back=False, release_worktrees=False
    )

    assert released == [("w1", "dashboard:w1")]

# SPDX-License-Identifier: Apache-2.0
"""``spawn_run(isolate="worktree")``: each worker runs in a git worktree of its own.

The admission gate refuses ``isolate`` without a ``cwd`` (or with another value)
without git; the worktree is made at run start, so the provider, the session
record and a continuation all take it as the run's cwd; the gateway's record in
the run folder decides what the run's prune removes, and a release removes
nothing; and the completion event names the worktree and its commits.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import subagent_continuable_helpers
from test_crewmate_worktree import _commit_in, _git, _init, git_here  # noqa: F401

import kiro_crew.subagent_persistence as sp
from kiro_crew import crewmate_worktree as cw
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers
continuation_runtime = subagent_continuable_helpers.continuation_runtime


@pytest.fixture
def world(continuation_runtime, git_here):  # noqa: F811
    """The continuation runtime with a repository under its one allowed root."""
    repo = _init(Path(continuation_runtime.project) / "tasklog")
    os.mkdir(os.path.join(repo, "docs"))
    _commit_in(os.path.join(repo, "docs"), "a.md", "docs")
    continuation_runtime.repo = repo
    return continuation_runtime


async def _close(sessions) -> None:
    await await_until(
        asyncio.create_task(sessions.close_all(drain_timeout=0)), what="the sessions closing"
    )


def _provider_cwds(world) -> list[str]:
    return [p._cwd for p in world.made]


@pytest.mark.asyncio
async def test_an_isolated_task_starts_in_a_fresh_worktree_of_its_repository(world):
    sessions, manager = world.new_manager()
    head = _git(world.repo, "rev-parse", "HEAD")
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "t", agent="worker", cwd=os.path.join(world.repo, "docs"), isolate="worktree"
            )
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the isolated run finishing")
        assert not child.error, child.error
        dest = os.path.join(world.project, f"tasklog-wt-{child.id}")
        assert child.worktree.path == dest and child.worktree.base == head
        assert _provider_cwds(world) == [os.path.join(dest, "docs")]
        assert _git(dest, "rev-parse", "HEAD") == head
        assert _git(world.repo, "status", "--porcelain") == ""
        # The gateway's record carries the path and base; state.json holds no copy.
        assert await asyncio.to_thread(sp.read_run_worktree_record, child.id) == (dest, head)
        state = await asyncio.to_thread(sp.read_state, child.id)
        assert "worktree" not in state
        assert state["cwd"] == os.path.join(dest, "docs")
        assert dest in child.worktree_note and "made no commits" in child.worktree_note
    finally:
        await _close(sessions)


@pytest.mark.asyncio
async def test_two_isolated_tasks_on_one_repository_get_two_worktrees(world):
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            a = manager.spawn("a", agent="worker", cwd=world.repo, isolate="worktree")
            b = manager.spawn("b", agent="worker", cwd=world.repo, isolate="worktree")
            runs = [manager._tasks[a.id], manager._tasks[b.id]]
            for run in runs:
                await await_until(run, what="an isolated run finishing")
        assert not a.error and not b.error
        assert sorted(_provider_cwds(world)) == sorted([a.worktree.path, b.worktree.path])
        assert a.worktree.path != b.worktree.path
    finally:
        await _close(sessions)


@pytest.mark.asyncio
async def test_a_task_without_isolate_runs_in_the_shared_checkout(world):
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn("t", agent="worker", cwd=world.repo)
            await await_until(manager._tasks[child.id], what="the run finishing")
        assert not child.error and child.worktree is None and child.worktree_note == ""
        assert _provider_cwds(world) == [os.path.realpath(world.repo)]
        assert await asyncio.to_thread(sp.read_run_worktree_record, child.id) is None
    finally:
        await _close(sessions)


@pytest.mark.asyncio
async def test_a_continuation_of_an_isolated_run_runs_in_its_worktree(world):
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "t", agent="worker", cwd=world.repo, isolate="worktree", keep=True
            )
            await await_until(manager._tasks[child.id], what="the isolated run finishing")
            assert not child.error
            recorded = await asyncio.to_thread(manager.recorded_cwd, child.id)
            assert recorded == child.worktree.path
            followup = manager.continue_conversation(child.id, "next", cwd=recorded)
            assert followup is not None and not followup.error
            await await_until(manager._tasks[followup.id], what="the continuation finishing")
        assert not followup.error, followup.error
        assert _provider_cwds(world)[-1] == child.worktree.path
        assert followup.worktree is None  # made once, by the run itself
    finally:
        await _close(sessions)


@pytest.mark.asyncio
async def test_a_queued_isolated_task_still_gets_its_worktree_when_drained(world):
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            manager._running_count = manager._max_concurrent
            child = manager.spawn("t", agent="worker", cwd=world.repo, isolate="worktree")
            assert child is not None and child.queued
            assert manager._queue[0]["isolate"] == "worktree"
            manager._running_count = 0
            manager._drain_queue()
            await async_wait_until(
                lambda: child.id in manager._tasks, describe=lambda: "the drained run never started"
            )
            await await_until(manager._tasks[child.id], what="the drained run finishing")
        run = manager._agents.get(child.id) or child
        dest = os.path.join(world.project, f"tasklog-wt-{child.id}")
        assert _provider_cwds(world) == [dest], getattr(run, "error", "")
    finally:
        await _close(sessions)


def _spawn_refused(isolate: str, cwd: str = ""):
    manager = subagent_continuable_helpers._manager()
    sel_mock = MagicMock()
    cfg = MagicMock()
    cfg.agent.spawn_min_memory_gb = 0
    cfg.agent.subagent_cwd_allowed_roots = [os.path.dirname(cwd) or "/"]
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel", return_value=sel_mock),
        patch("kiro_crew.subagent.KiroCrewConfig.load", return_value=cfg),
    ):
        info = manager.spawn("t", cwd=cwd, isolate=isolate)
    outcomes = [c.kwargs.get("outcome") for c in sel_mock.log_tool_invocation.call_args_list]
    return manager, info, outcomes


@pytest.mark.asyncio
async def test_isolate_without_a_cwd_is_rejected_and_counted():
    manager, info, outcomes = _spawn_refused("worktree")
    await manager.wait_taskq_ready()
    assert info is not None and info.done and "isolate needs a cwd" in info.error
    assert "rejected_invalid_isolate" in outcomes
    assert info.id not in manager._tasks


@pytest.mark.asyncio
async def test_isolate_with_another_value_is_rejected(tmp_path):
    manager, info, outcomes = _spawn_refused("branch", str(tmp_path))
    await manager.wait_taskq_ready()
    assert info is not None and info.done and "isolate must be 'worktree'" in info.error
    assert "rejected_invalid_isolate" in outcomes


@pytest.mark.asyncio
async def test_an_isolated_cwd_in_no_repository_fails_the_run_with_the_reason(world):
    plain = os.path.join(world.project, "plain")
    os.mkdir(plain)
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn("t", agent="worker", cwd=plain, isolate="worktree")
            assert child is not None and not child.error  # admitted
            await await_until(manager._tasks[child.id], what="the run failing")
        assert child.done and "not in a git repository" in child.error
        assert world.made == []  # never started a session
    finally:
        await _close(sessions)


# ── the record that drives removal, and the removal at retention end ──


def _isolated(world, run_id: str):
    made = cw.make_run_worktree(world.repo, run_id, [world.project])
    sp.create_agent_folder(run_id, task=run_id)
    sp.write_run_worktree_record(run_id, made.path, made.base)
    return made


def _seed_prunable(run_id: str) -> None:
    sp.update_state(run_id, keep=False)
    sp.write_tombstone(run_id, cause="delivered", recovery_action="none")
    path = sp._agent_dir(run_id) / "tombstone.json"
    tombstone = json.loads(path.read_text(encoding="utf-8"))
    tombstone["died"] = 1
    path.write_text(json.dumps(tombstone), encoding="utf-8")


def test_at_prune_a_clean_worktree_is_removed_and_a_committed_one_kept(world):
    clean = _isolated(world, "0000000000000a01")
    worked = _isolated(world, "0000000000000a02")
    _commit_in(worked.path, "out.txt", "result")
    for run_id in ("0000000000000a01", "0000000000000a02"):
        _seed_prunable(run_id)
    assert sp.prune_stale_tombstones() == 2
    assert not os.path.exists(clean.path)
    assert os.path.isdir(worked.path) and "result" in _git(worked.path, "log", "--oneline")
    assert not sp._agent_dir("0000000000000a01").exists()


@pytest.mark.parametrize("seam", ["cwd check", "record"])
def test_a_worktree_that_cannot_be_handed_out_is_removed_again(world, monkeypatch, seam):
    from kiro_crew.subagent_worktree import prepare_run_worktree

    run_id = "0000000000000b01"
    dest = os.path.join(world.project, f"tasklog-wt-{run_id}")
    sp.create_agent_folder(run_id, task=run_id)
    seen = []  # the made folder, as the failing seam saw it: proves the attempt got that far

    def refuse(cwd, _roots):
        seen.append((cwd, os.path.isdir(cwd)))
        return "", "refused for the test"

    def unwritable(_run_id, path, _base):
        seen.append((path, os.path.isdir(path)))
        raise OSError("the record folder is read-only")

    if seam == "cwd check":
        monkeypatch.setattr("kiro_crew.subagent.validate_cwd", refuse)
    else:
        monkeypatch.setattr(sp, "write_run_worktree_record", unwritable)
    with pytest.raises(cw.WorktreeRefused, match=r"^isolate:"):
        prepare_run_worktree(run_id, world.repo)
    assert seen == [(dest, True)]
    assert not os.path.exists(dest)
    assert f"tasklog-wt-{run_id}" not in _git(world.repo, "worktree", "list", "--porcelain")


def test_a_removal_failure_does_not_block_the_prune(world, monkeypatch):
    made = _isolated(world, "0000000000000a03")
    _seed_prunable("0000000000000a03")

    def broken(*_a, **_kw):
        raise OSError("git is gone")

    monkeypatch.setattr(cw, "remove_run_worktree", broken)
    assert sp.prune_stale_tombstones() == 1
    assert not sp._agent_dir("0000000000000a03").exists()
    assert os.path.isdir(made.path)


@pytest.mark.parametrize(
    ("sid", "label", "cleanup"),
    [
        ("kas-1", ACP_BACKEND_KAS, {"return_value": True}),
        ("sid-1", "acp", {"return_value": False}),
        ("sid-1", "acp", {"side_effect": OSError("store busy")}),
        ("", "acp", {"return_value": True}),
        ("sid-1", "acp", {"return_value": True}),
    ],
    ids=["kas", "cleanup-not-done", "cleanup-raises", "no-session-in-hand", "disposed"],
)
def test_a_release_leaves_the_worktree_and_the_prune_removes_it(world, sid, label, cleanup):
    from kiro_crew.subagent_manager.continuation import ContinuationCoordinator

    made = _isolated(world, "0000000000000a0a")
    with patch("kiro_crew.subagent._cleanup_session_files_sync", **cleanup):
        ContinuationCoordinator._release_disk_sync("0000000000000a0a", sid, label)
    assert os.path.isdir(made.path)
    _seed_prunable("0000000000000a0a")
    assert sp.prune_stale_tombstones() == 1
    assert not os.path.exists(made.path)


def test_the_record_lives_in_the_sealed_run_folder_not_under_trust(world):
    run_id = "0000000000000a0b"
    _isolated(world, run_id)
    assert (sp._agent_dir(run_id) / "run-worktree.json").is_file()
    trust = sp._subagents_dir().parent / "trust" / "subagent-cleanup-identities" / run_id
    assert not trust.exists()


def test_a_forged_worktree_record_is_redacted_when_shown():
    from kiro_crew.dashboard.handlers.messaging import _worktree_field
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=35037)
    shown = _worktree_field(f"/p/x-wt-1 {token}", token)["worktree"]
    assert token not in shown["path"] and token not in shown["base"]
    assert shown["path"].startswith("/p/x-wt-1 ")


# ── the completion line ──


def test_the_completion_note_names_the_worktree_and_its_commits(world):
    from kiro_crew.subagent_worktree import completion_note

    made = _isolated(world, "0000000000000a07")
    _commit_in(made.path, "one.txt", "one")
    sha = _git(made.path, "rev-parse", "--short=7", "HEAD")
    note = completion_note("0000000000000a07", made)
    assert made.path in note and "1 commit(s)" in note and sha in note


@pytest.mark.parametrize("fields", [{"result": "ok"}, {"error": "boom"}])
def test_the_completion_event_carries_the_note_whatever_the_outcome(fields):
    from kiro_crew.slack.gateway import _compose_subagent_completion

    info = subagent_continuable_helpers.SubagentInfo(id="0000000000000a08", task="t", **fields)
    info.done = True
    info.worktree_note = "Note: Agent `0000000000000a08` worked in its own git worktree /p/x-wt-1"
    composed = _compose_subagent_completion(info, _flush_only=False)
    assert composed.announce.rstrip().endswith(info.worktree_note)
    assert composed.no_tools_note.endswith(info.worktree_note)


@pytest.mark.asyncio
async def test_a_retry_of_an_isolated_run_is_isolated_from_the_source_checkout(world):
    from kiro_crew.dashboard.messaging_api import run_control

    # The source is a linked worktree on a branch ahead of main, not the main checkout.
    linked = os.path.join(world.project, "tasklog-feature")
    _git(world.repo, "worktree", "add", "-b", "feature", linked)
    _commit_in(linked, "feature.txt", "feature work")
    source = os.path.join(linked, "docs")
    made = await asyncio.to_thread(
        cw.make_run_worktree, source, "0000000000000a09", [world.project]
    )
    old = subagent_continuable_helpers.SubagentInfo(
        id="0000000000000a09", task="t", cwd=made.cwd, isolate="worktree", error="boom"
    )
    old.worktree = made
    old.done = True
    old.execution_context = MagicMock()
    asked: dict = {}

    def spawn(task, **kwargs):
        asked.update(kwargs)
        return None  # capacity: nothing starts

    state = SimpleNamespace(subagents=SimpleNamespace(spawn=spawn, max_concurrent=1))
    with patch.object(run_control, "link_spawn", create=True):
        response = await run_control._retry_failed_run(state, old.id, old)
    assert response.status == 429
    assert asked["isolate"] == "worktree"
    assert asked["cwd"] == source

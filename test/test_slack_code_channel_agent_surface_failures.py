"""Code-channel agent surface: failure, cancel and shutdown paths leave nothing stuck.

Split from ``test_slack_code_channel_agent_surface.py`` (2,000-line cap); same
recorded-payload harness, from ``slack_code_channel_surface_helpers``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    BRANCH,
    CH,
    ORIGIN,
    OTHER_CH,
    OTHER_SESSION,
    OTHER_SLOT,
    SESSION,
    SLOT,
    _aturn,
    _bind,
    _calls,
    _channel_tree,
    _click,
    _flaky_archive_stamp,
    _git,
    _noop,
    _passthrough_spawn,
    _register_workspace,
    _repo_with_channel_tree,
    _Request,
    _retarget_service,
    _run,
    _slash,
    _tool_rows,
    _turn,
    _wrap,
    env_fixture,
    worktree_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.config.loader import ACTIVATION_OFF
from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels import commands, status, surface, tools, wrapup
from kiro_crew.slack.code_channels.surface_store import SurfaceStore
from kiro_crew.slack.code_channels.surface_store import store as surface_store
from kiro_crew.slack.code_channels.workspace import git_ops
from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

# ── Failure and cancel paths leave nothing stuck ──────────────────────────


def test_a_cancel_after_the_retarget_moves_the_sessions_back_and_carries_on(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    from kiro_crew.slack.code_channels import cleanup

    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    real = cleanup._retarget

    async def retarget_then_cancel(*a: Any) -> str:
        await real(*a)
        raise asyncio.CancelledError

    monkeypatch.setattr(cleanup, "_retarget", retarget_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cleanup.release_channel_workspace(env.store.get_binding(CH), caller=OWNER))
    assert tree.is_dir() and "worktree_remove" not in [e[0] for e in events]
    assert events[-1] == ("set_project", f"{SLOT}->{tree}")
    kept = [
        r
        for r in env.sel.ops("slack.code_channel.worktree_release")
        if "result=kept" in r["resources"]
    ]
    assert kept and "cleanup cancelled" in str(kept[-1])


def test_a_wrap_up_whose_delivery_raises_is_disarmed(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import routing

    binding = _bind(env)

    async def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("slot lookup failed")

    monkeypatch.setattr(routing, "deliver_directed", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), _noop))
    assert not surface_store().get(CH).wrapup_pending_since
    assert CH not in wrapup._requests
    _turn(binding, "the next message")
    assert env.slack.blocks_posts == []


def test_a_posted_wrap_up_whose_ts_is_not_stored_loses_its_button(
    env: Any, monkeypatch: Any
) -> None:
    _bind(env)
    real = wrapup.update_async

    async def refuse_ts(channel: str, **changes: Any) -> Any:
        if "summary_ts" in changes:
            raise OSError("read-only")
        return await real(channel, **changes)

    monkeypatch.setattr(wrapup, "update_async", refuse_ts)
    _run(env, tools.WRAP_UP, {"summary": "Done"})
    assert len(env.slack.blocks_posts) == 1
    channel, ts, _text, blocks = env.slack.updates[-1]
    assert channel == CH and ts == "300.000001"
    assert not any(b.get("type") == "actions" for b in blocks)
    assert wrapup.UNRECORDED_NOTE in str(blocks[-1])


def test_a_context_bar_publish_that_raises_puts_the_stored_items_back(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.surface_client import SurfaceClient

    _bind(env)
    first = [{"key": "tokens", "label": "1k"}]
    assert _run(env, tools.SET_CONTEXT_BAR, {"items": first}).ok

    async def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("socket closed")

    monkeypatch.setattr(SurfaceClient, "set_context_bar", boom)
    out = _run(env, tools.SET_CONTEXT_BAR, {"items": [{"key": "docs", "label": "Docs"}]})
    assert not out.ok
    assert [i["key"] for i in surface_store().get(CH).context_bar] == ["tokens"]


def test_a_canvas_whose_id_failed_to_store_is_stored_by_the_next_call(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    _bind(env)
    env.api.responses["conversations.canvases.create"] = {"ok": True, "canvas_id": "F1"}
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "VP"}
    real = ss.atomic_write
    failures = [1]

    def flaky(*a: Any, **k: Any) -> None:
        if failures[0]:
            failures[0] -= 1
            raise OSError("disk full")
        real(*a, **k)

    monkeypatch.setattr(ss, "atomic_write", flaky)
    assert not _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan"}).ok
    out = _run(env, tools.PUBLISH_PLAN_CANVAS, {"markdown": "# Plan v2"})
    assert out.ok, out.text
    assert len(_calls(env, "conversations.canvases.create")) == 1
    assert surface_store().get(CH).plan_canvas_id == "F1"
    assert _calls(env, "agents.conversations.setCanvasContent")[0]["canvas_id"] == "F1"


def test_records_pointing_at_slack_outlast_the_record_cap(tmp_path: Path, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    monkeypatch.setattr(ss, "MAX_RECORDS", 2)
    st = SurfaceStore(tmp_path / "s.json")
    st.update("C1", plan_canvas_id="F1")
    st.update("C2", commands_digest="a")
    st.update("C3", commands_digest="b")
    assert st.get("C1") is not None and st.get("C1").plan_canvas_id == "F1"
    assert st.get("C2") is None


def test_tools_and_turn_end_wrap_up_obey_the_channels_own_activation(env: Any) -> None:
    binding = _bind(env)
    asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    env.activation[CH] = ACTIVATION_OFF
    out = _run(env, tools.RENAME, {"title": "x"})
    assert out.code == "channel_off" and _calls(env, "agents.sessions.rename") == []
    asyncio.run(wrapup.on_turn_start(binding, None))
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert env.slack.blocks_posts == []


def test_a_failed_archive_recovery_is_logged(env: Any, monkeypatch: Any, caplog: Any) -> None:
    binding = _bind(env)

    async def boom(*_a: Any) -> None:
        raise RuntimeError("cleanup exploded")

    monkeypatch.setattr(wrapup, "_recover_archive", boom)

    async def go() -> None:
        wrapup._start_recovery(binding, OWNER, 1.0, "", "")
        await wrapup.drain_recoveries()
        await asyncio.sleep(0)

    with caplog.at_level("WARNING"):
        asyncio.run(go())
    assert any("archive recovery failed" in r.getMessage() for r in caplog.records)


def test_archiving_clears_a_held_status(env: Any) -> None:
    _bind(env)
    status.hold_after_turn(CH, "suspended")
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert CH not in status._held


def test_a_second_cancel_during_the_removal_still_reports_what_git_did(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    import threading

    from kiro_crew.slack.code_channels import cleanup

    root, tree = worktree
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    started, release = threading.Event(), threading.Event()
    real = cleanup._remove_sync

    def slow_remove(*a: Any) -> tuple[str, str]:
        started.set()
        release.wait(10)
        return real(*a)

    monkeypatch.setattr(cleanup, "_remove_sync", slow_remove)

    async def go() -> None:
        task = asyncio.ensure_future(
            cleanup.release_channel_workspace(env.store.get_binding(CH), caller=OWNER)
        )
        from kiro_crew.testing.wait import async_wait_until

        try:
            # Bounded, and fails at once if the cleanup ended before the removal.
            await async_wait_until(lambda: started.is_set() or task.done())
            ended = (task.exception() or task.result()) if task.done() else "pending"
            assert started.is_set(), f"removal never started: {ended!r}"
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)  # let each cancel reach the cleanup's wait
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert not tree.exists()
    # Removed, so nothing was moved back onto the deleted tree.
    assert events[-1][0] == "worktree_remove"
    rows = env.sel.ops("slack.code_channel.worktree_release")
    assert rows and "result=removed" in rows[-1]["resources"]


def test_a_disarm_whose_write_fails_still_posts_no_stray_wrap_up(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import routing

    binding = _bind(env)
    real = wrapup.update_async

    async def refuse_clear(channel: str, **changes: Any) -> Any:
        if changes.get("wrapup_pending_since") == 0.0:
            raise OSError("read-only")
        return await real(channel, **changes)

    async def boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("slot lookup failed")

    monkeypatch.setattr(wrapup, "update_async", refuse_clear)
    monkeypatch.setattr(routing, "deliver_directed", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), _noop))
    assert surface_store().get(CH).wrapup_pending_since  # the stored flag stuck
    asyncio.run(wrapup.on_turn_start(binding, None))
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert env.slack.blocks_posts == []


def test_the_record_being_written_survives_a_cap_full_of_pointers(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    monkeypatch.setattr(ss, "MAX_RECORDS", 2)
    st = SurfaceStore(tmp_path / "s.json")
    st.update("C1", plan_canvas_id="F1")
    st.update("C2", summary_ts="1.2")
    st.update("C3", wrapup_pending_since=5.0)
    assert st.get("C3") is not None and st.get("C3").wrapup_pending_since == 5.0


def test_a_cancel_right_after_the_wrap_up_is_armed_disarms_it(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    real = wrapup.request_wrap_up

    tags: list[str] = []

    async def arm_then_cancel(channel: str, user: str, tag: str | None = None) -> str:
        tags.append(await real(channel, user, tag))
        raise asyncio.CancelledError

    monkeypatch.setattr(wrapup, "request_wrap_up", arm_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), _noop))
    assert not surface_store().get(CH).wrapup_pending_since
    assert CH not in wrapup._requests
    _turn(binding, f"a turn somehow carrying ({tags[0]})")
    assert env.slack.blocks_posts == []


def test_a_binding_that_names_its_worktree_is_cleaned_and_goes_home(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """The common path: a git-backed bind stores the WORKTREE as the binding's
    project, and the project it came from lives only in the workspace record."""
    root, tree = worktree
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    assert ("set_project", f"{SLOT}->{root}") in events
    rows = env.sel.ops("slack.code_channel.worktree_release")
    assert rows[-1]["outcome"] == "allowed" and "result=removed" in rows[-1]["resources"]


def test_a_worktree_binding_whose_record_names_another_repo_is_refused(
    env: Any, worktree: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _bind(env, project=str(tree))
    other = tmp_path / "other-repo"
    other.mkdir()
    _git("init", "-q", cwd=other)
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.project_dir = str(other)  # forged: a project outside the recorded repo
    WorkspaceStore().put(record)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir()
    rows = env.sel.ops("slack.code_channel.worktree_release")
    assert rows[-1]["outcome"] == "denied"


def test_a_worktree_binding_with_only_a_resolved_project_goes_home(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """A bind with no project of its own keeps the session's as resolved_project."""
    root, tree = worktree
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.project_dir, record.resolved_project = "", str(root)
    WorkspaceStore().put(record)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists() and ("set_project", f"{SLOT}->{root}") in events


def test_a_binding_in_a_subdirectory_of_the_tree_goes_home_to_that_repo(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    (root / "sub").mkdir()
    (tree / "sub").mkdir()
    _bind(env, project=str(tree / "sub"))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.project_dir, record.subdir = str(root / "sub"), "sub"
    WorkspaceStore().put(record)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists() and ("set_project", f"{SLOT}->{root / 'sub'}") in events


@pytest.mark.parametrize("other_state", ["channel_branch", "detached", "switched"])
def test_a_record_naming_another_linked_worktree_as_home_is_refused(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path, other_state: str
) -> None:
    root, tree = worktree
    other_branch = git_ops.channel_branch("Other work", "T1", OTHER_CH)
    other = _channel_tree(root, other_branch)
    _git("worktree", "add", "-q", "-b", other_branch, str(other), cwd=root)
    # The other channel's agent may detach or switch its own tree's branch.
    if other_state == "detached":
        _git("switch", "-q", "--detach", cwd=other)
    elif other_state == "switched":
        _git("switch", "-q", "-c", "feature", cwd=other)
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    # Forged: another channel's linked worktree as both the repo and the home.
    record.project_dir = record.repo_root = str(other)
    WorkspaceStore().put(record)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and not any(e[0] == "set_project" for e in events)
    assert env.sel.ops("slack.code_channel.worktree_release")[-1]["outcome"] == "denied"


def test_a_channel_cut_from_the_users_own_linked_worktree_is_cleaned(
    env: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    main, _ = _repo_with_channel_tree(tmp_path / "repo")
    user_wt = tmp_path / "repo-wt-feature"
    _git("worktree", "add", "-q", "-b", "feature", str(user_wt), cwd=main)
    tree = _channel_tree(user_wt)
    _git("worktree", "add", "-q", "-b", BRANCH, str(tree), cwd=user_wt)
    _bind(env, project=str(tree))
    _register_workspace(user_wt, tree)  # the bind's repo_root is the user's worktree
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists() and ("set_project", f"{SLOT}->{user_wt}") in events


def test_a_repository_whose_path_has_a_space_is_cleaned(
    env: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = _repo_with_channel_tree(tmp_path / "My Projects" / "app")
    _git("worktree", "add", "-q", "-b", BRANCH, str(tree), cwd=root)
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists() and ("set_project", f"{SLOT}->{root}") in events


def test_a_symlink_planted_at_the_expected_path_does_not_vouch_for_a_forged_root(
    tmp_path: Path,
) -> None:
    from kiro_crew.slack.code_channels import cleanup

    real = tmp_path / "proj-wt-real"
    real.mkdir()
    fake_root = tmp_path / "other"
    planted = tmp_path / f"other-wt-{wt._dir_slug(BRANCH)}"
    planted.symlink_to(real, target_is_directory=True)
    assert not cleanup._cut_from(str(fake_root), str(real), BRANCH)
    assert cleanup._cut_from(str(tmp_path / "proj"), str(_channel_tree(tmp_path / "proj")), BRANCH)


@pytest.mark.parametrize("modifier", ["!temporary", "!incognito"])
def test_a_wrap_up_refused_for_a_privacy_modifier_is_disarmed(
    env: Any, monkeypatch: Any, modifier: str
) -> None:
    from kiro_crew.slack.code_channels import routing

    binding = _bind(env)
    real = routing.deliver_directed

    async def deliver(*a: Any, **k: Any) -> bool:
        # The real router, minus only the stubbed slot hop the env replaces.
        return await real(*a, **k)

    monkeypatch.setattr(routing, "deliver_directed", deliver)
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up", text=modifier), _noop))
    assert env.routed == []  # refused: the session never got the prompt
    assert not surface_store().get(CH).wrapup_pending_since
    asyncio.run(wrapup.on_turn_start(binding, None))
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert env.slack.blocks_posts == []


@pytest.mark.parametrize("fails", ["safe_tip", "store_put"])
def test_a_failure_after_git_removed_the_tree_never_points_back_at_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path, fails: str
) -> None:
    from kiro_crew.slack.code_channels import cleanup

    root, tree = worktree
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.folder_id = "f1"
    WorkspaceStore().put(record)
    env.state._folders = [{"id": "f1", "name": "fix", "project_dir": str(tree)}]

    async def mutate_folders(apply: Any, **_k: Any) -> Any:
        _changed, result = apply(env.state._folders)
        return result

    env.state.mutate_folders = mutate_folders
    events: list[tuple[str, str]] = []
    svc = _retarget_service(monkeypatch, tmp_path, events, applies=True)

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    if fails == "safe_tip":
        monkeypatch.setattr(cleanup, "_safe_tip", boom)
    else:
        monkeypatch.setattr(svc.store, "put", boom)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    assert env.state._folders[0]["project_dir"] == str(root)
    assert events[-1] != ("set_project", f"{SLOT}->{tree}")
    rows = env.sel.ops("slack.code_channel.worktree_release")
    assert rows[-1]["outcome"] == "allowed" and "result=removed" in rows[-1]["resources"]


def test_an_unreadable_parent_counts_as_still_there(tmp_path: Path, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import cleanup

    def denied(_p: Any, *_a: Any, **_k: Any) -> Any:
        raise PermissionError("EACCES")

    with monkeypatch.context() as m:
        m.setattr(cleanup.os, "lstat", denied)
        there = cleanup._still_there(str(tmp_path / "x"))
    assert there


def test_a_shutdown_cancel_while_git_removes_the_tree_restores_nothing(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    """asyncio.run's shutdown cancels EVERY task while git is deleting the tree:
    the cleanup must still learn git's real result and move nothing back."""
    import threading

    from kiro_crew.slack.code_channels import cleanup

    root, tree = worktree
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    from kiro_crew.testing.wait import async_wait_until

    real_git = git_ops.run_git
    running, release = threading.Event(), threading.Event()

    def held_git(args: list[str], cwd: Any, *a: Any, **k: Any) -> Any:
        if args[:2] == ["worktree", "remove"]:
            running.set()
            release.wait(10)  # held until the cancels below have landed
        return real_git(args, cwd, *a, **k)

    monkeypatch.setattr(git_ops, "run_git", held_git)

    async def go() -> None:
        task = asyncio.ensure_future(
            cleanup.release_channel_workspace(env.store.get_binding(CH), caller=OWNER)
        )
        try:
            await async_wait_until(lambda: running.is_set() or task.done())
            assert running.is_set()
            me = asyncio.current_task()
            for other in asyncio.all_tasks():
                if other is not me:
                    other.cancel()  # what asyncio.run does to every task at shutdown
            await asyncio.sleep(0)  # let the cancel reach the cleanup's wait
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert not tree.exists()
    assert not [e for e in events if e == ("set_project", f"{SLOT}->{tree}")]
    rows = env.sel.ops("slack.code_channel.worktree_release")
    assert rows[-1]["outcome"] == "allowed" and "result=removed" in rows[-1]["resources"]


def test_a_tree_git_half_deleted_counts_as_gone(tmp_path: Path) -> None:
    from kiro_crew.slack.code_channels import cleanup

    tree = tmp_path / "half"
    tree.mkdir()
    (tree / "leftover.txt").write_text("x\n")
    assert not cleanup._still_there(str(tree))  # no .git link left
    (tree / ".git").write_text("gitdir: /elsewhere\n")
    assert cleanup._still_there(str(tree))


def test_agent_text_goes_through_the_installations_own_redactor(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.platform import context

    monkeypatch.setattr(
        context, "redact_via_context", lambda text: text.replace("CORPSECRET", "[REDACTED]")
    )
    assert "CORPSECRET" not in wrapup._redact("token CORPSECRET here")
    assert tools._redact("x CORPSECRET") == "x [REDACTED]"


def test_agent_text_is_sanitized_by_the_shared_slack_display_sink() -> None:
    """One spelling of the Slack outbound sink: wrapup.py keeps no redaction or
    mention regexes of its own, and the display-form passes the shared sink adds
    (a credential split by markup) reach the code channel's posts too."""
    import inspect

    key = "AKIA" + "IOSFODNN7EXAMPLE"
    split = f"key {key[:8]}**{key[8:]}**"
    for redact in (wrapup._redact, wrapup._redact_payload, tools._redact, tools._redact_payload):
        assert key not in redact(split).replace("*", "")
    assert "<!channel>" not in wrapup._redact("Done <!channel>")
    assert "re.compile" not in inspect.getsource(wrapup)


def test_html_views_and_bar_urls_are_not_mention_defanged(env: Any) -> None:
    """An HTML document and a context-bar url are not message text: Slack parses no
    mention there, so the defang would only break them, while credentials still go."""
    _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    secret = "ghp_" + "A" * 36
    page = f"<!DOCTYPE html><style>@media print {{}}</style><p>{secret}</p>"
    assert _run(env, tools.SET_VIEW, {"view_key": "r", "content": page}).ok
    sent = _calls(env, "agents.conversations.setView")[0]["content"]
    assert sent.startswith("<!DOCTYPE html>") and "@media print" in sent and secret not in sent
    url = "https://www.npmjs.com/package/@aws-sdk/client-s3"
    items = [{"key": "pkg", "label": "SDK", "url": url}]
    assert _run(env, tools.SET_CONTEXT_BAR, {"items": items}).ok
    bar = _calls(env, "agents.conversations.setProperties")[-1]["code_channel"]
    assert [i.get("url") for i in bar["context_bar_items"] if i["key"] == "pkg"] == [url]


def test_an_unreadable_surface_file_is_never_overwritten(tmp_path: Path, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    path = tmp_path / "surface.json"
    first = SurfaceStore(path)
    first.update("C1", plan_canvas_id="F1")
    st = SurfaceStore(path)
    real_read = Path.read_text
    fail = [True]

    def flaky_read(self: Path, *a: Any, **k: Any) -> str:
        if self == path and fail[0]:
            raise OSError("EIO")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky_read)
    with pytest.raises(ss.SurfaceStoreUnavailable):
        st.update("C2", commands_digest="d")
    with pytest.raises(ss.SurfaceStoreUnavailable):
        st.get("C1")
    fail[0] = False  # the read recovers: the load is retried, nothing was lost
    assert st.get("C1") is not None and st.get("C1").plan_canvas_id == "F1"
    st.update("C2", commands_digest="d")
    assert SurfaceStore(path).get("C1").plan_canvas_id == "F1"


def test_a_corrupt_surface_file_is_left_as_is(tmp_path: Path) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    path = tmp_path / "surface.json"
    path.write_text("{not json")
    with pytest.raises(ss.SurfaceStoreUnavailable):
        SurfaceStore(path).update("C1", commands_digest="d")
    assert path.read_text() == "{not json"


def test_a_failed_bar_publish_never_rolls_back_a_newer_calls_items(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels.surface_client import SurfaceClient

    _bind(env)
    gate = asyncio.Event
    calls: list[str] = []

    async def go() -> None:
        from kiro_crew.testing.wait import async_wait_until

        first_in, first_go = gate(), gate()
        real = SurfaceClient.set_context_bar

        async def publish(self: Any, channel: str, items: list) -> Any:
            key = items[0]["key"] if items else ""
            calls.append(key)
            if key == "old":
                first_in.set()
                # Bounded: a test that fails before releasing this must not hang.
                await async_wait_until(first_go.is_set, describe=lambda: "first_go released")
                raise RuntimeError("socket closed")
            return await real(self, channel, items)

        monkeypatch.setattr(SurfaceClient, "set_context_bar", publish)
        slow = asyncio.ensure_future(
            tools.run_tool(
                env.state, SESSION, tools.SET_CONTEXT_BAR, {"items": [{"key": "old", "label": "o"}]}
            )
        )
        newer: asyncio.Future[Any] | None = None
        try:
            await async_wait_until(lambda: first_in.is_set() or slow.done())
            # The old call returning before its publish (a store write failed) is a
            # failure to report, not a handshake to wait out.
            assert first_in.is_set(), f"the old call never reached its publish: {slow.result()}"
            newer = asyncio.ensure_future(
                tools.run_tool(
                    env.state,
                    SESSION,
                    tools.SET_CONTEXT_BAR,
                    {"items": [{"key": "new", "label": "n"}]},
                )
            )

            def newer_got_in() -> bool:
                # Either it stored its items (no serialization), or it waits its turn.
                assert newer is not None
                if newer.done():
                    return True
                record = surface_store().get(CH)
                if record is not None and [i["key"] for i in record.context_bar] == ["new"]:
                    return True
                lock = getattr(tools, "_bar_locks", {}).get(CH)
                return bool(lock is not None and getattr(lock, "_waiters", None))

            await async_wait_until(newer_got_in, off_loop=False)
        finally:
            first_go.set()
            pending = [t for t in (slow, newer) if t is not None]
            await async_wait_until(lambda: all(t.done() for t in pending))
        assert not slow.result().ok
        assert newer is not None and newer.result().ok

    asyncio.run(go())
    assert calls == ["old", "new"]  # serialized: the newer call ran after the rollback
    assert [i["key"] for i in surface_store().get(CH).context_bar] == ["new"]


def test_a_message_queued_ahead_of_wrap_up_does_not_take_its_turn(
    env: Any, monkeypatch: Any
) -> None:
    binding = _bind(env)
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    # The ordinary message queued ahead runs its turn first.
    _turn(binding, "the ordinary message queued ahead")
    assert env.slack.blocks_posts == []
    # Now the /wrap-up turn itself runs, without calling the tool.
    _turn(binding, f"<@{OWNER}> ran /wrap-up in this Slack code channel. ({tag})")
    assert len(env.slack.blocks_posts) == 1


def test_a_folder_changed_mid_archive_keeps_the_tree(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    (tree / "sub").mkdir()
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.folder_id = "f1"
    WorkspaceStore().put(record)
    env.state._folders = [{"id": "f1", "name": "fix", "project_dir": str(tree)}]

    async def mutate_folders(apply: Any, **_k: Any) -> Any:
        # Someone repoints the folder inside the tree just before our swap applies.
        env.state._folders[0]["project_dir"] = str(tree / "sub")
        _changed, result = apply(env.state._folders)
        return result

    env.state.mutate_folders = mutate_folders
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir()
    assert env.sel.ops("slack.code_channel.worktree_release")[-1]["outcome"] == "denied"


def test_a_turn_ending_while_wrap_up_is_still_being_delivered_posts_nothing(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import routing

    binding = _bind(env)
    seen: list[int] = []

    async def deliver(*_a: Any, **_k: Any) -> bool:
        # An earlier queued message's turn runs while delivery is still waiting.
        await _aturn(binding, "an earlier queued message")
        seen.append(len(env.slack.blocks_posts))
        return True

    monkeypatch.setattr(routing, "deliver_directed", deliver)
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), _noop))
    assert seen == [0]
    assert CH in wrapup._requests  # still armed for its own turn


# ── The /wrap-up fallback belongs to the turn whose prompt carries its tag ──


def test_a_turn_ending_while_the_request_is_being_stored_posts_nothing(
    env: Any, monkeypatch: Any
) -> None:
    """The arming window: the flag is on disk, the request's own turn hasn't run."""
    binding = _bind(env)
    real = wrapup.update_async
    seen: list[int] = []

    async def write_then_end_a_turn(channel: str, **changes: Any) -> Any:
        stored = await real(channel, **changes)
        if changes.get("wrapup_pending_since"):
            await _aturn(binding, "the running turn, unrelated")
            seen.append(len(env.slack.blocks_posts))
        return stored

    monkeypatch.setattr(wrapup, "update_async", write_then_end_a_turn)
    asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    assert seen == [0]


def test_a_second_wrap_up_survives_the_first_ones_failed_delivery(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import routing

    binding = _bind(env)
    calls: list[str] = []
    second: list[str] = []

    async def deliver(_orch: Any, _r: Any, _e: Any, text: str, _u: Any) -> bool:
        calls.append(text)
        if len(calls) == 1:
            # While the first is still being delivered, a second /wrap-up arrives.
            assert await commands.handle_slash(env.orch, _slash("/wrap-up", trigger="T-2"), _noop)
            second.append(wrapup._requests[CH])
            return False  # ...and the first never reaches the session
        return True

    monkeypatch.setattr(routing, "deliver_directed", deliver)
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up", trigger="T-1"), _noop))
    assert wrapup._requests.get(CH) == second[0]  # the first's failure didn't cancel it
    assert surface_store().get(CH).wrapup_pending_since
    _turn(binding, calls[1])
    assert len(env.slack.blocks_posts) == 1


def test_a_discarded_wrap_up_prompt_never_posts_later(env: Any) -> None:
    binding = _bind(env)
    asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    # Its queued prompt is deleted; ordinary turns go on running.
    for prompt in ("next message", "another one"):
        _turn(binding, prompt)
    assert env.slack.blocks_posts == []


def test_a_restart_never_turns_a_stored_flag_into_an_unasked_wrap_up(env: Any) -> None:
    binding = _bind(env)
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    wrapup._requests.clear()  # the gateway restarted: the stored flag outlives the tag
    _turn(binding, f"a later turn ({tag})")
    assert env.slack.blocks_posts == []


def test_a_failed_wrap_up_turn_disarms_only_its_own_request(env: Any) -> None:
    binding = _bind(env)
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    _turn(binding, f"/wrap-up ({tag})", outcome="error")
    assert env.slack.blocks_posts == [] and CH not in wrapup._requests
    assert not surface_store().get(CH).wrapup_pending_since


def test_the_turn_start_notification_carries_the_prompt(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels import hooks

    binding = _bind(env)
    seen: list[str] = []

    async def capture(_b: Any, event: Any) -> None:
        seen.append(event.prompt)

    monkeypatch.setattr(hooks, "_START_HOOKS", [capture])
    monkeypatch.setattr(cc_binding, "active_binding", lambda *_a: binding)
    assert asyncio.run(
        cc_binding.notify_turn_start(
            CH, binding.anchor_ts, SLOT, source="slack", prompt="hello (wrap-up request ab12)"
        )
    )
    assert seen == ["hello (wrap-up request ab12)"]


def test_a_surface_file_of_the_wrong_shape_is_left_as_is(tmp_path: Path) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    for doc in ("[]", '{"records": {}}', '{"records": [{"no_channel": 1}]}'):
        path = tmp_path / "surface.json"
        path.write_text(doc)
        with pytest.raises(ss.SurfaceStoreUnavailable):
            SurfaceStore(path).update("C1", commands_digest="d")
        assert path.read_text() == doc


def test_fields_from_a_newer_build_survive_a_write(tmp_path: Path) -> None:
    import json

    path = tmp_path / "surface.json"
    path.write_text(
        json.dumps(
            {"version": 1, "records": [{"channel_id": "C1", "plan_canvas_id": "F1", "future": 7}]}
        )
    )
    SurfaceStore(path).update("C1", commands_digest="d")
    row = json.loads(path.read_text())["records"][0]
    assert row["future"] == 7 and row["plan_canvas_id"] == "F1" and row["commands_digest"] == "d"


def test_a_cancel_during_the_first_bar_write_rolls_the_items_back(
    env: Any, monkeypatch: Any
) -> None:
    import threading

    from kiro_crew.slack.code_channels import surface_store as ss
    from kiro_crew.testing.wait import async_wait_until

    _bind(env)
    assert _run(env, tools.SET_CONTEXT_BAR, {"items": [{"key": "old", "label": "o"}]}).ok
    real = ss.SurfaceStore.update
    entered, release = threading.Event(), threading.Event()

    def held_update(self: Any, channel: str, **changes: Any) -> Any:
        if [i.get("key") for i in changes.get("context_bar", ())] == ["new"]:
            entered.set()
            release.wait(10)
        return real(self, channel, **changes)

    monkeypatch.setattr(ss.SurfaceStore, "update", held_update)

    async def go() -> None:
        task = asyncio.ensure_future(
            tools._set_context_bar(
                tools.Call(
                    binding=env.store.get_binding(CH), client=surface.client(), caller=SESSION
                ),
                {"items": [{"key": "new", "label": "n"}]},
            )
        )
        try:
            await async_wait_until(lambda: entered.is_set() or task.done(), off_loop=False)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)

    asyncio.run(go())
    assert [i["key"] for i in surface_store().get(CH).context_bar] == ["old"]


def test_the_turn_start_reads_the_persons_text_not_the_runners_prepends() -> None:
    """A cancelled-turn preamble can quote an older /wrap-up prompt; only the
    person's own text decides which turn is the /wrap-up turn."""
    import inspect

    from kiro_crew.dashboard.chat_turn import turn_dispatch_gates

    src = inspect.getsource(turn_dispatch_gates)
    call = src[src.index("notify_code_channel_turn_start(") :]
    call = call[: call.index(")\n")]
    assert "prompt=str(turn._jev_route_text" in call
    assert "turn.message" not in call


def test_a_wrap_up_posted_in_one_turn_leaves_a_newer_request_live(env: Any) -> None:
    binding = _bind(env)
    first = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    asyncio.run(wrapup.on_turn_start(binding, SimpleNamespace(prompt=f"/wrap-up ({first})")))
    second = asyncio.run(wrapup.request_wrap_up(CH, OWNER))  # queued while turn 1 runs
    assert _run(env, tools.WRAP_UP, {"summary": "Done"}).ok  # turn 1's agent wraps up
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert wrapup._requests.get(CH) == second
    _turn(binding, f"/wrap-up ({second})")  # its own turn, without the tool call
    assert len(env.slack.blocks_posts) == 2


@pytest.mark.parametrize(
    "doc",
    [
        '{"version": 2, "records": []}',
        '{"records": [{"channel_id": "C1", "context_bar": [{"key": {"nested": 1}}]}]}',
        '{"records": [{"channel_id": "C1", "context_bar": "bar"}]}',
    ],
)
def test_a_surface_file_from_a_newer_build_is_left_as_is(tmp_path: Path, doc: str) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    path = tmp_path / "surface.json"
    path.write_text(doc)
    with pytest.raises(ss.SurfaceStoreUnavailable):
        SurfaceStore(path).update("C1", commands_digest="d")
    assert path.read_text() == doc


# ── The per-channel policy ceiling (allowed_channel_ids) on every entry point ──


@pytest.fixture
def refused(monkeypatch: Any) -> Any:
    from kiro_crew.slack import channel_policy

    monkeypatch.setattr(
        channel_policy,
        "_verdict",
        lambda ch: f"slack channel {ch!r} refused by the security policy" if ch == CH else "",
    )


@pytest.mark.parametrize("command", ["/summarize", "/wrap-up", "/create-pr"])
def test_a_command_in_a_channel_the_policy_refuses_runs_nothing(
    env: Any, refused: Any, command: str
) -> None:
    _bind(env)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    assert asyncio.run(commands.handle_slash(env.orch, _slash(command), respond))
    assert env.routed == [] and replies and "not authorized" in replies[0]
    assert not surface_store().get(CH) or not surface_store().get(CH).wrapup_pending_since


def test_an_archive_click_in_a_channel_the_policy_refuses_archives_nothing(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack import channel_policy

    _bind(env)
    _, ts = _wrap(env)  # posted while the channel was still allowed
    monkeypatch.setattr(channel_policy, "_verdict", lambda ch: "refused" if ch == CH else "")
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert _calls(env, "agents.conversations.archive") == []
    assert not env.store.get_binding(CH).archived_at


def test_a_tool_in_a_channel_the_policy_refuses_posts_nothing(env: Any, refused: Any) -> None:
    _bind(env)
    out = _run(env, tools.RENAME, {"title": "x"})
    assert out.code == "channel_refused" and env.api.calls == []


def test_no_fallback_wrap_up_posts_into_a_channel_the_policy_refuses(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack import channel_policy

    binding = _bind(env)
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    monkeypatch.setattr(channel_policy, "_verdict", lambda ch: "refused" if ch == CH else "")
    _turn(binding, f"/wrap-up ({tag})")
    assert env.slack.blocks_posts == []


def test_every_surface_client_call_meets_the_channel_policy(env: Any, refused: Any) -> None:
    """The seam itself: no raw agents.* call reaches Slack for a refused channel."""
    from kiro_crew.slack.code_channels.models import CodeChannelApiError
    from kiro_crew.slack.code_channels.surface_client import CHANNEL_REFUSED

    client = surface.client()
    calls = [
        client.rename(CH, "t"),
        client.set_status(CH, "active"),
        client.archive(CH, summary_message_ts="1.2"),
        client.set_commands(CH, []),
        client.set_canvas_content(CH, "F1", "x"),
        client.set_context_bar(CH, []),
    ]
    for coro in calls:
        with pytest.raises(CodeChannelApiError) as exc:
            asyncio.run(coro)
        assert exc.value.code == CHANNEL_REFUSED
    assert env.api.calls == []
    # Another channel is untouched by the refusal.
    asyncio.run(client.rename(OTHER_CH, "t"))
    assert _calls(env, "agents.sessions.rename") == [{"channel_id": OTHER_CH, "title": "t"}]


def test_a_refusal_is_audited_with_the_caller(env: Any, refused: Any, monkeypatch: Any) -> None:
    import kiro_crew.sel as sel_module

    monkeypatch.setattr(sel_module, "sel", lambda: env.sel)  # the policy seam's own audit
    _bind(env)
    _run(env, tools.RENAME, {"title": "x"})
    rows = [r for r in env.sel.ops("slack.code_channel.tool") if r["outcome"] == "denied"]
    assert rows and rows[-1]["error"] == "channel_not_allowed_by_policy"
    assert rows[-1]["caller"] == SESSION


def test_the_workspace_service_writes_through_the_policy_checked_client(
    env: Any, monkeypatch: Any
) -> None:
    """The bar carries the agent's own items: the workspace's own publishes (a
    dashboard turn's, the context-bar tool's) meet the policy at write time."""
    from kiro_crew.slack.code_channels.surface_client import SurfaceClient
    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    monkeypatch.setattr(workspace_core, "_SERVICE", None)
    monkeypatch.setattr(workspace_core, "register", lambda *_a, **_k: None)
    service = workspace_core.install(lambda: env.slack)
    assert isinstance(service._client(), SurfaceClient)


def test_a_bar_publish_for_a_refused_channel_sends_nothing(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack import channel_policy

    _bind(env)
    assert _run(env, tools.SET_CONTEXT_BAR, {"items": [{"key": "a", "label": "a"}]}).ok
    sent = len(_calls(env, "agents.conversations.setProperties"))
    monkeypatch.setattr(channel_policy, "_verdict", lambda ch: "refused" if ch == CH else "")
    client = surface.client()
    with pytest.raises(Exception):
        asyncio.run(client.set_context_bar(CH, [{"key": "a", "label": "a"}]))
    assert len(_calls(env, "agents.conversations.setProperties")) == sent


def test_an_unreadable_recheck_after_the_stamp_removes_nothing(
    env: Any, worktree: Any, monkeypatch: Any
) -> None:
    root, tree = worktree
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    answers = iter([True, None, None, None, None, None, None])

    async def still_archived(_ch: str) -> Any:
        return next(answers, None)

    monkeypatch.setattr(wrapup, "_still_archived", still_archived)

    async def fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(wrapup.asyncio, "sleep", fast_sleep)
    binding = env.store.get_binding(CH)
    surface.fence_archived(CH)
    asyncio.run(wrapup._recover_archive(binding, OWNER, 1.0, f"channel={CH}", str(tree)))
    assert tree.is_dir()


def test_an_archived_channel_takes_no_more_work_and_gets_no_tree_back(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    from kiro_crew.slack.code_channels import routing

    root, tree = worktree
    _bind(env, project=str(tree))
    _register_workspace(root, tree)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert not tree.exists()
    prepared: list[Any] = []

    async def prepare(binding: Any) -> None:
        prepared.append(binding)

    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    monkeypatch.setattr(workspace_core, "prepare", prepare)
    routed_before = len(env.routed)
    delivered = asyncio.run(
        routing.deliver_directed(
            env.orch,
            routing.InboundResult(routing.Gate.DIRECTED, binding=env.store.get_binding(CH)),
            {"channel": CH, "team": "T1"},
            "a late message",
            OWNER,
        )
    )
    assert delivered is False and prepared == [] and len(env.routed) == routed_before
    assert not tree.exists()


def test_a_replaced_request_never_posts_its_fallback(env: Any, monkeypatch: Any) -> None:
    binding = _bind(env)
    first = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    real = wrapup.gather_facts

    async def facts_then_a_new_wrap_up(b: Any) -> Any:
        out = await real(b)
        await wrapup.request_wrap_up(CH, OWNER)  # lands while the facts are read
        return out

    monkeypatch.setattr(wrapup, "gather_facts", facts_then_a_new_wrap_up)
    _turn(binding, f"/wrap-up ({first})")
    assert env.slack.blocks_posts == []
    assert wrapup._requests.get(CH) not in (None, first)  # the newer one stays live


def test_an_agent_wrap_up_in_an_ordinary_turn_settles_no_queued_request(env: Any) -> None:
    binding = _bind(env)
    tag = asyncio.run(wrapup.request_wrap_up(CH, OWNER))
    asyncio.run(wrapup.on_turn_start(binding, SimpleNamespace(prompt="an ordinary message")))
    assert _run(env, tools.WRAP_UP, {"summary": "early"}).ok
    asyncio.run(wrapup.on_turn_end(binding, SimpleNamespace(outcome="ok")))
    assert wrapup._requests.get(CH) == tag
    assert surface_store().get(CH).wrapup_pending_since  # its stored flag kept too


def test_a_wrap_up_that_cannot_be_recorded_answers_and_runs_nothing(
    env: Any, monkeypatch: Any
) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    _bind(env)
    replies: list[str] = []

    async def respond(text: str) -> None:
        replies.append(text)

    async def refuse(*_a: Any, **_k: Any) -> Any:
        raise ss.SurfaceStoreUnavailable("surface.json")

    monkeypatch.setattr(wrapup, "update_async", refuse)
    assert asyncio.run(commands.handle_slash(env.orch, _slash("/wrap-up"), respond))
    assert replies == [commands.WRAP_UP_FAILED_TEXT] and env.routed == []
    assert CH not in wrapup._requests


@pytest.mark.parametrize(
    "row",
    ['{"channel_id": "C1", "summary_ts": {}}', '{"channel_id": "C1", "wrapup_pending_since": "x"}'],
)
def test_a_wrong_typed_field_is_an_unreadable_row(tmp_path: Path, row: str) -> None:
    from kiro_crew.slack.code_channels import surface_store as ss

    path = tmp_path / "surface.json"
    doc = '{"version": 1, "records": [' + row + "]}"
    path.write_text(doc)
    with pytest.raises(ss.SurfaceStoreUnavailable):
        SurfaceStore(path).update("C1", commands_digest="d")
    assert path.read_text() == doc


def _deliver(env: Any, text: str = "a message") -> bool:
    from kiro_crew.slack.code_channels import routing

    return asyncio.run(
        routing.deliver_directed(
            env.orch,
            routing.InboundResult(routing.Gate.DIRECTED, binding=env.store.get_binding(CH)),
            {"channel": CH, "team": "T1"},
            text,
            OWNER,
        )
    )


@pytest.mark.parametrize(
    "info",
    [
        {"ok": True, "channel": {"id": CH, "is_archived": False}},  # a read that lags the archive
        {"ok": True, "channel": {"id": CH, "is_archived": True}},
        {"ok": True, "channel": {"id": CH}},
    ],
)
def test_an_archive_stamp_is_only_cleared_by_slacks_unarchive_event(
    env: Any, monkeypatch: Any, info: dict
) -> None:
    """Only lifecycle's unarchive event clears the stamp. A message never does,
    whatever a read says: a read lagging an Archive in flight would otherwise
    clear its stamp and cut a worktree no cleanup will ever remove."""
    from kiro_crew.slack.code_channels.workspace import core as workspace_core

    async def prepare(_b: Any) -> None:
        raise AssertionError("an archived channel must not prepare a worktree")

    monkeypatch.setattr(workspace_core, "prepare", prepare)
    _bind(env)
    env.store.update_binding(CH, archived_at=123.0)
    env.api.responses["conversations.info"] = info
    assert _deliver(env) is False
    assert env.store.get_binding(CH).archived_at == 123.0 and env.routed == []


def test_an_answer_without_is_archived_is_never_read_as_unarchived(env: Any) -> None:
    env.api.responses["conversations.info"] = {"ok": True, "channel": {"id": CH}}
    assert asyncio.run(surface.client().archived_in_slack(CH)) is None
    assert asyncio.run(wrapup._still_archived(CH)) is None


@pytest.mark.parametrize("caller", ["prepare", "turn_start", "refresh"])
def test_no_path_cuts_a_worktree_for_an_archived_channel(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path, caller: str
) -> None:
    """The class, at the one seam every path goes through (service.ensure): a
    bind, a message, a resumed request, a turn's start or end. The caller's
    binding copy is stale (not archived); the stored one is."""
    from kiro_crew.slack.code_channels.workspace import core as workspace_core
    from kiro_crew.slack.code_channels.workspace import service as ws_service

    root, _tree = worktree
    stale = _bind(env, project=str(root))
    env.store.update_binding(CH, archived_at=123.0)
    cut: list[Any] = []
    monkeypatch.setattr(
        git_ops, "create_or_adopt_worktree", lambda *a, **k: cut.append(a) or "refused"
    )
    events: list[tuple[str, str]] = []

    class Client:
        async def set_context_bar(self, *_a: Any, **_k: Any) -> dict:
            events.append(("set_context_bar", ""))
            return {"ok": True}

    async def set_project(key: str, path: str) -> bool:
        events.append(("set_project", f"{key}->{path}"))
        return True

    svc = ws_service.WorkspaceService(
        lambda: Client(), store=WorkspaceStore(tmp_path / "ws.json"), set_project=set_project
    )
    if caller == "prepare":
        monkeypatch.setattr(workspace_core, "_SERVICE", svc)
        asyncio.run(workspace_core.prepare(stale))
    elif caller == "turn_start":
        asyncio.run(svc.on_turn_start(stale))
    else:
        asyncio.run(svc.refresh(stale))
        asyncio.run(svc.drain())
    assert cut == [] and events == []

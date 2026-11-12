"""Archive never removes a worktree a session it did not count is working in.

The population is the bound session, every session filed under the channel's
folder, and every live session whose project lies inside the worktree, whatever
folder it is filed in. A session outside the channel keeps the tree, and so
does one that starts using it while the sessions are being moved off.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    CH,
    SLOT,
    _bind,
    _click,
    _register_workspace,
    _retarget_service,
    _wrap,
    env_fixture,
    worktree_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import wrapup
from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore


@pytest.fixture(autouse=True)
def _folder_subtree(_floor_monkeypatch: Any) -> None:
    """The channel folder ``f1`` is its own subtree (no folder tree in this env)."""
    from kiro_crew.slack.code_channels.workspace import folders

    _floor_monkeypatch.setattr(
        folders, "subtree_ids", lambda _s, fid: frozenset({fid}) if fid else frozenset()
    )


def _channel_with_folder(env: Any, root: Path, tree: Path) -> None:
    _bind(env, project=str(root))
    _register_workspace(root, tree)
    record = WorkspaceStore().get("T1", CH)
    record.folder_id = "f1"
    WorkspaceStore().put(record)


def _kept(env: Any) -> list[dict]:
    return [
        r
        for r in env.sel.ops("slack.code_channel.worktree_release")
        if "result=kept" in r["resources"]
    ]


def test_a_refiled_chat_running_on_the_worktree_keeps_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _channel_with_folder(env, root, tree)
    slots = {
        SLOT: SimpleNamespace(turn_running=False, _turn_generation=1, folder_id="f1"),
        # Moved to another folder by hand, still working in the channel's tree.
        "chat-9-1": SimpleNamespace(
            turn_running=False, _turn_generation=0, folder_id="elsewhere", project=str(tree)
        ),
    }
    monkeypatch.setattr(env.state, "_slots", slots, raising=False)
    events: list[tuple[str, str]] = []
    _retarget_service(monkeypatch, tmp_path, events, applies=True)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and events == []  # nothing moved, nothing removed
    kept = _kept(env)
    assert kept and "outside the channel" in str(kept[-1]) and "chat-9-1" in str(kept[-1])
    assert env.notified and "outside the channel" in env.notified[-1][0][2]


def test_a_chat_that_joins_the_worktree_during_retarget_keeps_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _channel_with_folder(env, root, tree)
    slots = {SLOT: SimpleNamespace(turn_running=False, _turn_generation=1, folder_id="f1")}
    monkeypatch.setattr(env.state, "_slots", slots, raising=False)
    events: list[tuple[str, str]] = []
    svc = _retarget_service(monkeypatch, tmp_path, events, applies=True)
    real_set = svc._set_project
    projects: dict[str, str] = {}

    async def set_project(key: str, path: str) -> bool:
        ok = await real_set(key, path)
        projects[key] = path
        if path == str(root):  # while the bound session moves off, a chat opens in it
            slots["chat-9-1"] = SimpleNamespace(
                turn_running=False, _turn_generation=0, folder_id="", project=str(tree)
            )
        return ok

    async def reader(key: str) -> str:
        return projects.get(key, str(tree))

    svc._set_project, svc._project_of = set_project, reader
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir()
    assert not any(e[0] == "worktree_remove" for e in events)
    assert ("set_project", f"{SLOT}->{tree}") in events  # moved back
    kept = _kept(env)
    assert kept and "chat-9-1" in str(kept[-1])


def _hooked_retarget(
    env: Any, monkeypatch: Any, tmp_path: Path, root: Path, tree: Path, on_move: Any
) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    svc = _retarget_service(monkeypatch, tmp_path, events, applies=True)
    real_set = svc._set_project
    projects: dict[str, str] = {}

    async def set_project(key: str, path: str) -> bool:
        ok = await real_set(key, path)
        projects[key] = path
        if path == str(root):
            on_move(key)
        return ok

    async def reader(key: str) -> str:
        # The slot's own project when it has one (a worker elsewhere), else the tree.
        slot = env.state._slots.get(key)
        return projects.get(key) or getattr(slot, "project", "") or str(tree)

    svc._set_project, svc._project_of = set_project, reader
    return events


def test_a_counted_session_pointed_back_into_the_tree_keeps_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _channel_with_folder(env, root, tree)
    bound = SimpleNamespace(
        turn_running=False, _turn_generation=1, folder_id="f1", project=str(tree)
    )
    monkeypatch.setattr(env.state, "_slots", {SLOT: bound}, raising=False)

    def pointed_back(_key: str) -> None:
        bound.project = str(tree)  # someone points it at the tree again

    events = _hooked_retarget(env, monkeypatch, tmp_path, root, tree, pointed_back)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and not any(e[0] == "worktree_remove" for e in events)
    assert _kept(env) and "works in it" in str(_kept(env)[-1])


def test_a_folder_worker_elsewhere_that_starts_a_turn_keeps_it(
    env: Any, worktree: Any, monkeypatch: Any, tmp_path: Path
) -> None:
    root, tree = worktree
    _channel_with_folder(env, root, tree)
    worker = SimpleNamespace(
        turn_running=False, _turn_generation=4, folder_id="f1", project=str(tmp_path / "other")
    )
    slots = {
        SLOT: SimpleNamespace(turn_running=False, _turn_generation=1, folder_id="f1"),
        "chat-7-1": worker,
    }
    monkeypatch.setattr(env.state, "_slots", slots, raising=False)

    def worker_starts(_key: str) -> None:
        worker.turn_running, worker._turn_generation = True, 5  # e.g. pointed into it

    events = _hooked_retarget(env, monkeypatch, tmp_path, root, tree, worker_starts)
    _, ts = _wrap(env)
    assert asyncio.run(wrapup.handle_archive_click(env.orch, _click(OWNER, ts)))
    assert tree.is_dir() and not any(e[0] == "worktree_remove" for e in events)
    assert _kept(env)

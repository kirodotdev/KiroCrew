"""Regression coverage for agent ``pod down`` after a worktree directory is gone."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops

NAME = "kc-wt-11919"
NOT_FOUND = f"worktree not found: {NAME}"


class _RecordingMutex:
    def __init__(self, events: list[str]):
        self.events = events

    def __call__(self, _cfg, _name):
        return self

    def __enter__(self):
        self.events.append("lock-enter")
        return self

    def __exit__(self, *_exc):
        self.events.append("lock-exit")
        return False


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(env_file=lambda name: tmp_path / f"{name}.env")


def _missing_worktree():
    return patch.object(
        repository_mod, "_find_worktree", new_callable=AsyncMock, return_value=(None, NOT_FOUND)
    )


@pytest.mark.asyncio
async def test_down_reclaims_a_retained_prunable_worktree_under_the_name_lock(tmp_path):
    """A retained record attributes a missing checkout to this repository."""
    recorded = tmp_path / NAME
    cfg = _cfg(tmp_path)
    events: list[str] = []
    cp = SimpleNamespace(returncode=0, stdout="", stderr="")

    def _stop(_cfg, _name):
        events.append("stop")
        return cp

    stop = MagicMock(side_effect=_stop)
    run_cmd = AsyncMock()

    with (
        _missing_worktree(),
        patch.object(
            repository_mod,
            "_find_retained_worktree_path",
            new_callable=AsyncMock,
            return_value=(str(recorded), None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=cfg),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex(events)),
        patch.object(worktree_ops, "_read_pin_strict", return_value=(True, str(recorded))),
        patch.object(runtime_mod.rt, "stop_pod", stop),
        patch.object(runtime_mod, "_run_cmd", run_cmd),
    ):
        result = await worktree_ops._pod_down(NAME)

    assert result == {"ok": True, "error": None}
    assert events == ["lock-enter", "stop", "lock-exit"]
    stop.assert_called_once_with(cfg, NAME)
    run_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_down_refuses_when_git_has_no_retained_worktree_record(tmp_path):
    """No git record for the name means nothing ties it to this repository."""
    cfg = _cfg(tmp_path)
    events: list[str] = []
    stop = MagicMock()
    run_cmd = AsyncMock()

    with (
        _missing_worktree(),
        patch.object(
            repository_mod,
            "_find_retained_worktree_path",
            new_callable=AsyncMock,
            return_value=(None, NOT_FOUND),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=cfg),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex(events)),
        patch.object(runtime_mod.rt, "stop_pod", stop),
        patch.object(runtime_mod, "_run_cmd", run_cmd),
    ):
        result = await worktree_ops._pod_down(NAME)

    assert result == {"ok": False, "error": NOT_FOUND}
    assert events == []
    stop.assert_not_called()
    run_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_down_refuses_a_pin_for_a_different_checkout(tmp_path):
    recorded = tmp_path / NAME
    cfg = _cfg(tmp_path)
    stop = MagicMock()

    with (
        _missing_worktree(),
        patch.object(
            repository_mod,
            "_find_retained_worktree_path",
            new_callable=AsyncMock,
            return_value=(str(recorded), None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=cfg),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops, "_read_pin_strict", return_value=(True, "/foreign/checkout")),
        patch.object(runtime_mod.rt, "stop_pod", stop),
    ):
        result = await worktree_ops._pod_down(NAME)

    assert result["ok"] is False
    assert "basename collision" in result["error"]
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_down_refuses_when_the_retained_checkout_is_back_on_disk(tmp_path):
    recorded = tmp_path / NAME
    recorded.mkdir()
    cfg = _cfg(tmp_path)
    stop = MagicMock()

    with (
        _missing_worktree(),
        patch.object(
            repository_mod,
            "_find_retained_worktree_path",
            new_callable=AsyncMock,
            return_value=(str(recorded), None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=cfg),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops, "_read_pin_strict", return_value=(True, str(recorded))),
        patch.object(runtime_mod.rt, "stop_pod", stop),
    ):
        result = await worktree_ops._pod_down(NAME)

    assert result["ok"] is False
    assert "back on disk" in result["error"]
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_down_refuses_an_unpinned_orphan(tmp_path):
    recorded = tmp_path / NAME
    cfg = _cfg(tmp_path)
    stop = MagicMock()

    with (
        _missing_worktree(),
        patch.object(
            repository_mod,
            "_find_retained_worktree_path",
            new_callable=AsyncMock,
            return_value=(str(recorded), None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=cfg),
        patch.object(runtime_mod, "_POD_AVAILABLE", True),
        patch.object(runtime_mod.rt, "pod_name_mutex", _RecordingMutex([])),
        patch.object(worktree_ops, "_read_pin_strict", return_value=(False, None)),
        patch.object(runtime_mod.rt, "stop_pod", stop),
    ):
        result = await worktree_ops._pod_down(NAME)

    assert result["ok"] is False
    assert "no checkout pin" in result["error"]
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_up_still_refuses_a_missing_worktree():
    run_cmd = AsyncMock()

    with _missing_worktree(), patch.object(runtime_mod, "_run_cmd", run_cmd):
        result = await worktree_ops._pod_up(NAME)

    assert result == {"ok": False, "error": NOT_FOUND}
    run_cmd.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_retained_worktree_path_keeps_prunable_entries():
    raw = """\
worktree /repo/main
HEAD aaa
branch refs/heads/main

worktree /repo/kc-wt-live
HEAD bbb
branch refs/heads/live

worktree /repo/kc-wt-11919
HEAD ccc
branch refs/heads/orphan
prunable missing checkout

"""
    with (
        patch.object(repository_mod, "_repo", return_value="/repo"),
        patch.object(runtime_mod, "_run_cmd", new=AsyncMock(return_value=(0, raw, ""))),
    ):
        retained, error = await repository_mod._find_retained_worktree_path(NAME)
        live, live_error = await repository_mod._find_retained_worktree_path("kc-wt-live")
        missing, missing_error = await repository_mod._find_retained_worktree_path("absent")

    assert (retained, error) == ("/repo/kc-wt-11919", None)
    assert (live, live_error) == ("/repo/kc-wt-live", None)
    assert missing is None
    assert missing_error == "worktree not found: absent"

"""``spawn_continue`` of a run spawned without a cwd is not refused.

Such a run records its own per-run scratch directory,
``workspace_root()/subagent_<id>``, as its cwd, and that directory is reclaimed
when the run ends. Forwarding the gone path made every follow-up fail with
"cwd does not exist". The scratch directory was never a project, so
``recorded_cwd`` omits it and the follow-up goes where a spawn without a cwd
goes. Any other missing path is still forwarded, so ``spawn`` refuses it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.subagent import SubagentManager


def _mgr() -> SubagentManager:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions._pool_cwd = ""
    return SubagentManager(sessions=sessions, ctx_builder=MagicMock())


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("KIROCREW_WORKSPACE", str(root))
    return root.resolve()


def _record(monkeypatch: pytest.MonkeyPatch, state: dict) -> None:
    monkeypatch.setattr("kiro_crew.subagent.read_state", lambda _id: state)


def test_the_runs_own_reclaimed_scratch_dir_is_omitted(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    _record(monkeypatch, {"cwd": str(workspace / "subagent_abc123")})
    assert _mgr().recorded_cwd("abc123") == ""


def test_a_kept_runs_scratch_dir_under_its_conversation_key_is_omitted(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    _record(
        monkeypatch,
        {"cwd": str(workspace / "subagent_keep1"), "conversation_key": "subagent:keep1"},
    )
    assert _mgr().recorded_cwd("keep1") == ""


def test_a_scratch_dir_that_still_exists_is_kept(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    live = workspace / "subagent_abc123"
    live.mkdir()
    _record(monkeypatch, {"cwd": str(live)})
    assert _mgr().recorded_cwd("abc123") == str(live)


@pytest.mark.parametrize(
    "name",
    [
        "subagent_other9",  # another run's scratch dir: not this run's own
        "my-project",  # a project that was deleted
    ],
)
def test_any_other_missing_path_is_still_forwarded(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, name: str
) -> None:
    gone = workspace / name
    _record(monkeypatch, {"cwd": str(gone)})
    assert _mgr().recorded_cwd("abc123") == str(gone)

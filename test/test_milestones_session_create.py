"""The "parallel chats" milestone counts a session_create that made a child, and nothing else."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew import milestones as m
from kiro_crew.dashboard import session_control as sc


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    seen: list[str] = []
    _floor_monkeypatch.setattr(m, "record", seen.append)
    return seen


def test_a_created_child_counts_once(tmp_path: Path, _home: list[str]) -> None:
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    assert state.get_slot(created["target"]) is not None
    assert _home.count("parallel-chats") == 1


def test_a_refused_create_counts_nothing(tmp_path: Path, _home: list[str]) -> None:
    state = _make_state(tmp_path)
    with pytest.raises(sc.SessionControlError):
        asyncio.run(sc.create_session(state, caller_session_key="dashboard:chat-ghost"))
    assert "parallel-chats" not in _home

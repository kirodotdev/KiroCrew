"""``session_create`` places the child where its creator runs.

A worker that a chat on a session environment dispatches (an Ultra Mode chat's
workers, a conductor's items) must run in that environment, as a ``spawn_run``
subagent does. Left unbound it started on the gateway machine, with the
gateway's files and shell.

Invariants:

* a remote creator's child inherits its root owner and profile;
* a local creator's child stays local and writes no choice row;
* a choice that cannot be recorded refuses the create (``environment_unavailable``)
  and retracts the child, which is never armed: it would have run locally.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from chat_test_helpers import _make_state
from eager_spawn_helpers import (  # noqa: F401 -- autouse fixtures register by import
    _cfg,
    _isolate_armed_registry,
    _pin_prewarm_allowance,
    _prime,
)
from session_control_helpers import (  # noqa: F401 -- autouse fixtures register by import
    _agent_resolves,
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.coder.session_launch import SessionEnvironmentChoices
from kiro_crew.config import paths
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.session_environment import DEVELOPER_FLAG


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(DEVELOPER_FLAG, "1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "data_home", lambda: home)
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _agent_resolves(monkeypatch, caller.workspace)
    arms: list = []
    monkeypatch.setattr(
        chat_runner, "schedule_eager_spawn", lambda st, slot, **kw: arms.append(slot)
    )
    choices = SessionEnvironmentChoices(home / "coder" / "session-choices.json")
    return state, caller, choices, arms


def _create(state, caller):
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))


def test_a_remote_creators_child_runs_in_its_environment(world):
    state, caller, choices, arms = world
    parent = effective_session_key(caller)
    choices.set(parent, "dev")

    created = _create(state, caller)

    child = state.get_slot(created["target"])
    assert choices.resolve(effective_session_key(child)) == ("dev", parent)
    assert arms == [child]


def test_a_grandchild_lands_in_the_root_owners_environment(world):
    state, caller, choices, _arms = world
    root = effective_session_key(caller)
    choices.set(root, "dev")
    child = state.get_slot(_create(state, caller)["target"])

    assert choices.resolve(effective_session_key(child))[1] == root


def test_a_local_creators_child_stays_local_and_writes_nothing(world):
    state, caller, choices, arms = world

    created = _create(state, caller)

    child = state.get_slot(created["target"])
    assert choices.resolve(effective_session_key(child)) is None
    assert not choices.path.exists()
    assert arms == [child]


def test_the_flag_off_writes_nothing_even_for_a_bound_creator(world, monkeypatch):
    state, caller, choices, _arms = world
    choices.set(effective_session_key(caller), "dev")
    before = choices.path.read_text()
    monkeypatch.delenv(DEVELOPER_FLAG)

    _create(state, caller)

    assert choices.path.read_text() == before


def test_an_unreadable_choice_refuses_and_retracts_the_child(world):
    state, caller, choices, arms = world
    choices.set(effective_session_key(caller), "dev")
    slots_before = set(state._slots)
    choices.path.write_text(json.dumps({"version": 99}))

    with pytest.raises(sc.SessionControlError) as refused:
        _create(state, caller)

    assert refused.value.code == "environment_unavailable"
    assert refused.value.status == 503
    assert set(state._slots) == slots_before
    assert arms == []

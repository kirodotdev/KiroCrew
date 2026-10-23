"""A crew member in a workspace of its own can open a worker TEMPLATE.

Every crew member created since member workspaces landed is bound to its own
``member-<slug>`` workspace. A template has no workspace binding of its own --
``resolve_agent_bindings`` files every template session under the default
workspace -- so ``session_create``'s agent-workspace check compared ``default``
against the member's workspace and refused ``agent_workspace_mismatch`` for
``agent="kirocrew-worker"``. The template arm already binds such a child to the
caller's own store, so the template now inherits the caller's workspace too.

Driven through the REAL config, resolver and workspace lookup: the regression
lived in the agreement between those three, which a stand-in resolver hides.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from chat_test_helpers import _make_state
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew.config import loader
from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.members import DM_SLOT_KEY_PREFIX

_WORKER = "kirocrew-worker"


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


@pytest.fixture
def member_home(tmp_path, monkeypatch):
    """Two crew members, each bound to its own store AND its own workspace."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution
    from kiro_crew.history import ConversationLog

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    config = write_member_home(tmp_path, "pixel", "peer")
    config["workspaces"] = {
        "default": {"dir": "workspace"},
        "member-pixel": {"dir": "workspace-member-pixel"},
        "member-peer": {"dir": "workspace-member-peer"},
    }
    config["agents"]["pixel"]["workspace"] = "member-pixel"
    config["agents"]["peer"]["workspace"] = "member-peer"
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    for directory in ("workspace", "workspace-member-pixel", "workspace-member-peer"):
        (tmp_path / directory).mkdir(exist_ok=True)
    forget_declared_stores(monkeypatch)
    # The installed-template catalog, the one external boundary, seeded the way
    # the loader's own tests seed it: the worker is a declared template.
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset({_WORKER}))
    monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", True)
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(sc, "member_dispatch_enabled", lambda: True)

    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    caller = state.get_or_create_slot(DM_SLOT_KEY_PREFIX + "pixel", mode="member")
    caller.workspace = "member-pixel"
    caller.agent = "pixel"
    execution = resolve_member_execution(KiroCrewConfig.load(), "pixel")
    caller.memory_store = execution.store.legacy_name
    bind_session_execution(slot_history_key(caller), execution, vouch=True)
    assert sc.resolve_agent_bindings is loader.resolve_agent_bindings
    return state, caller, execution, tmp_path


def _create(state, caller, **kwargs):
    return asyncio.run(
        sc.create_session(state, caller_session_key=slot_history_key(caller), **kwargs)
    )


def test_a_member_in_its_own_workspace_opens_a_worker_template(member_home):
    from kiro_crew.execution_context import read_session_execution

    state, caller, execution, home = member_home

    result = _create(state, caller, agent=_WORKER)

    child = state.get_slot(result["target"])
    assert child is not None
    assert child.agent == _WORKER
    # The caller's workspace and its directory, never the default one.
    assert child.workspace == "member-pixel"
    assert child.project == os.path.realpath(home / "workspace-member-pixel")
    # The caller's own store and member identity, under the template persona.
    assert child.memory_store == execution.store.legacy_name
    actual = read_session_execution(slot_history_key(child), required=True)
    assert actual.store == execution.store
    assert actual.member_id == execution.member_id
    assert actual.selection_kind == "template"
    assert actual.template_id == _WORKER
    assert state.creator_slot_count(caller.key) == 1


def test_naming_a_peer_member_in_another_workspace_is_still_refused(member_home):
    state, caller, _execution, _home = member_home

    with pytest.raises(sc.SessionControlError) as error:
        _create(state, caller, agent="peer")

    assert error.value.code == "agent_workspace_mismatch"
    assert state.creator_slot_count(caller.key) == 0


def test_an_unresolvable_template_name_is_still_refused(member_home):
    state, caller, _execution, _home = member_home

    with pytest.raises(sc.SessionControlError) as error:
        _create(state, caller, agent="nobody", agent_kind="template")

    assert error.value.code == "agent_unresolved"
    assert state.creator_slot_count(caller.key) == 0


def test_an_omitted_agent_still_opens_the_members_own_session(member_home):
    from kiro_crew.execution_context import read_session_execution

    state, caller, execution, _home = member_home

    result = _create(state, caller)

    child = state.get_slot(result["target"])
    assert child is not None
    assert child.workspace == "member-pixel"
    actual = read_session_execution(slot_history_key(child), required=True)
    assert actual.store == execution.store
    assert actual.selection_kind == "member"


def test_a_non_member_caller_in_a_member_workspace_is_not_widened(member_home):
    # The inheritance is the MEMBER's: a caller with no member record in that
    # workspace has no store of its own there, so its template child would run
    # on the default store inside a member's workspace. Unchanged: refused.
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution

    state, _member, _execution, _home = member_home
    caller = state.get_or_create_slot("chat-owner")
    caller.workspace = "member-pixel"
    bind_session_execution(
        slot_history_key(caller),
        ExecutionContext(None, MemoryStoreRef("default"), "template", _WORKER, "persistent"),
    )

    with pytest.raises(sc.SessionControlError) as error:
        _create(state, caller, agent=_WORKER)

    assert error.value.code == "agent_workspace_mismatch"


def test_the_members_worker_opens_its_own_child_in_the_members_workspace(member_home):
    # The worker runs as a template, but its record keeps the member's id and
    # store, so its own child runs where it runs, on that same store.
    from kiro_crew.execution_context import read_session_execution

    state, caller, execution, _home = member_home
    worker = state.get_slot(_create(state, caller, agent=_WORKER)["target"])

    result = _create(state, worker, agent=_WORKER)

    child = state.get_slot(result["target"])
    assert child is not None
    assert child.workspace == "member-pixel"
    actual = read_session_execution(slot_history_key(child), required=True)
    assert actual.store == execution.store
    assert actual.member_id == execution.member_id


def test_a_template_caller_on_another_members_store_is_not_widened(member_home):
    # The member id alone is not enough: the store must be that member's own.
    from dataclasses import replace

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

    state, _member, execution, _home = member_home
    peer_store = resolve_member_execution(KiroCrewConfig.load(), "peer").store
    caller = state.get_or_create_slot("chat-borrowed")
    caller.workspace = "member-pixel"
    bind_session_execution(
        slot_history_key(caller),
        replace(
            execution.with_template(_WORKER, _WORKER),
            store=replace(peer_store, member_id=execution.member_id),
        ),
        vouch=True,
    )

    with pytest.raises(sc.SessionControlError) as error:
        _create(state, caller, agent=_WORKER)

    assert error.value.code == "agent_workspace_mismatch"

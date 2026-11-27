# SPDX-License-Identifier: Apache-2.0
"""A Bot may keep at most ``max_workers`` sessions that run as it open.

The owner sets the limit on the Bot's settings page (``PUT /api/agents/<name>``);
0, the default, sets none. ``session_create`` refuses the next session past it with
``bot_worker_cap_reached`` and creates nothing. A closed session frees its seat; a
session a restart brought back, or one still being created, takes one.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401

from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.session_control import SessionControlError
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.members import crewmate_worker_member_id, forget_crewmate_worker_session


def _set_cap(tmp_path, cap) -> None:
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["max_workers"] = cap
    path.write_text(json.dumps(config), encoding="utf-8")
    _invalidate_config_cache()


def test_a_bot_with_no_limit_set_has_none(crewmate):  # noqa: F811
    state, caller, _execution = crewmate
    assert KiroCrewConfig.load().agents["pixel"].max_workers == 0
    for _ in range(3):
        _create(state, caller)


@pytest.mark.parametrize("stored, read", [(-1, 0), (99, 32), ("x", 0)])
def test_a_hand_edited_limit_is_held_in_range(crewmate, tmp_path, stored, read):  # noqa: F811
    _set_cap(tmp_path, stored)
    assert KiroCrewConfig.load().agents["pixel"].max_workers == read


def test_the_worker_past_the_limit_is_refused_and_nothing_is_created(
    crewmate, tmp_path  # noqa: F811
):
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    _create(state, caller)
    _create(state, caller)
    before = set(state._slots)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"
    assert err.value.status == 409
    assert "allows 2" in str(err.value)
    assert set(state._slots) == before


def test_closing_a_worker_frees_its_seat(crewmate, tmp_path):  # noqa: F811
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    first = _create(state, caller)["target"]
    with pytest.raises(SessionControlError):
        _create(state, caller)
    state._slots.pop(first)
    assert _create(state, caller)["target"] != first


def test_a_worker_a_restart_brought_back_still_takes_its_seat(crewmate, tmp_path):  # noqa: F811
    """After a restart the in-memory worker count is empty; the saved record counts."""
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    first = _create(state, caller)["target"]
    key = slot_history_key(state.get_slot(first))
    forget_crewmate_worker_session(key)
    assert crewmate_worker_member_id(key) == ""
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"


def test_a_worker_is_counted_from_the_moment_its_slot_is_made(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    """The count is written with the slot, not by the later birth write.

    With the birth write's own registration switched off, the second create
    still sees the first worker: two parallel creates cannot both pass the cap.
    """
    from kiro_crew import mate_worker

    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    monkeypatch.setattr(mate_worker, "note_worker_session", lambda *_a, **_k: True)
    _create(state, caller)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"


def test_a_worker_cannot_open_a_second_worker_past_the_limit(crewmate, tmp_path):  # noqa: F811
    """A create from one of the Bot's workers runs under the Bot's own record."""
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    worker = state.get_slot(_create(state, caller)["target"])
    with pytest.raises(SessionControlError) as err:
        _create(state, worker, agent=MATE_WORKER_AGENT, agent_kind="template")
    assert err.value.code == "bot_worker_cap_reached"


def test_a_fork_of_a_worker_takes_a_seat(crewmate, tmp_path):  # noqa: F811
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    first = _create(state, caller)["target"]
    state.get_slot(first).messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    with pytest.raises(SessionControlError) as err:
        asyncio.run(
            sc.fork_session(state, caller_session_key=slot_history_key(caller), source=first)
        )
    assert err.value.code == "bot_worker_cap_reached"


def test_reviving_an_archived_worker_takes_a_seat(crewmate, tmp_path, monkeypatch):  # noqa: F811
    state, caller, _execution = crewmate
    # The crew log's creation lineage, which a fenced reviver needs, is off here.
    monkeypatch.setattr(sc, "_creation_parent", lambda key: caller.key)
    _set_cap(tmp_path, 1)
    archived = state.get_slot(_create(state, caller)["target"])
    archived.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    archived._dirty = True
    key = archived.key
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(caller), target=key))
    assert key not in state._slots
    meta = state.conversation_log.get_metadata(f"dashboard:{key}")
    # The close is strictly before the revive (the revive helpers explain why).
    state.conversation_log.update_metadata(
        f"dashboard:{key}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    _create(state, caller)
    with pytest.raises(SessionControlError) as err:
        asyncio.run(
            sc.revive_session(state, caller_session_key=slot_history_key(caller), target=key)
        )
    assert err.value.code == "bot_worker_cap_reached"
    assert key not in state._slots


def test_one_unreadable_restored_record_does_not_stop_the_others_counting(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    from kiro_crew import execution_context

    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    first = _create(state, caller)["target"]
    key = slot_history_key(state.get_slot(first))
    forget_crewmate_worker_session(key)
    # Another restored worker slot whose saved record cannot be read.
    broken = state.get_or_create_slot("chat-broken", agent=MATE_WORKER_AGENT)
    broken_key = slot_history_key(broken)
    real_read = execution_context.read_session_execution

    def read(session_key, *args, **kwargs):
        if session_key == broken_key:
            raise ValueError("unreadable execution record")
        return real_read(session_key, *args, **kwargs)

    monkeypatch.setattr(execution_context, "read_session_execution", read)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"


def _unreadable(monkeypatch, bad_key):
    """Make *bad_key*'s saved execution record raise as an undecodable one does."""
    from kiro_crew import execution_context

    real_read = execution_context.read_session_execution

    def read(session_key, *args, **kwargs):
        if session_key == bad_key:
            raise ValueError("unreadable execution record")
        return real_read(session_key, *args, **kwargs)

    monkeypatch.setattr(execution_context, "read_session_execution", read)


def test_a_revive_whose_record_cannot_be_read_is_not_an_exception(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    """The cap's read never turns an unreadable record into a raw error."""
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    monkeypatch.setattr(sc, "_creation_parent", lambda key: caller.key)
    archived = state.get_slot(_create(state, caller)["target"])
    archived.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    archived._dirty = True
    key = archived.key
    history_key = slot_history_key(archived)
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(caller), target=key))
    meta = state.conversation_log.get_metadata(f"dashboard:{key}")
    state.conversation_log.update_metadata(
        f"dashboard:{key}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    _unreadable(monkeypatch, history_key)
    try:
        asyncio.run(
            sc.revive_session(state, caller_session_key=slot_history_key(caller), target=key)
        )
    except SessionControlError:
        pass  # a structured refusal is an answer; a raw error is not


def test_a_fork_whose_source_record_cannot_be_read_is_not_an_exception(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    source = state.get_slot(_create(state, caller)["target"])
    source.messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    _unreadable(monkeypatch, slot_history_key(source))
    try:
        asyncio.run(
            sc.fork_session(state, caller_session_key=slot_history_key(caller), source=source.key)
        )
    except SessionControlError:
        pass  # a structured refusal is an answer; a raw error is not


def test_a_restart_read_never_uncounts_a_worker_counted_while_it_ran(
    crewmate, monkeypatch  # noqa: F811
):
    """A fork counts its child while a create's restart read is still in flight.

    The read saw the child before its record was bound, so it answers "nothing";
    applying that late must not take the child out of the count.
    """
    from kiro_crew.dashboard import state as state_facade
    from kiro_crew.dashboard.state import count_restored_workers
    from kiro_crew.members import note_crewmate_worker_session

    state, _caller, _execution = crewmate
    child = state.get_or_create_slot("chat-fork-child", agent=MATE_WORKER_AGENT)
    child_key = slot_history_key(child)

    def read_while_the_fork_counts_its_child(session_key):
        note_crewmate_worker_session(child.key, "pixel")
        return None

    monkeypatch.setattr(state_facade, "read_worker_execution", read_while_the_fork_counts_its_child)
    asyncio.run(count_restored_workers(state))
    assert crewmate_worker_member_id(child_key) == "pixel"


def test_a_restart_read_never_charges_a_chat_made_again_under_its_key(
    crewmate, monkeypatch  # noqa: F811
):
    """The owner remakes a restored Bot child's key as an ordinary chat mid-read.

    The record read for the closed child must not charge the new chat to the Bot.
    """
    from kiro_crew.dashboard import state as state_facade
    from kiro_crew.dashboard.state import count_restored_workers
    from kiro_crew.dashboard.state_parts import bot_worker_cap
    from kiro_crew.members import _slot_part

    state, caller, execution = crewmate
    child = state.get_or_create_slot("chat-restored-child", agent="kirocrew")
    child._created_by = caller.key
    child.memory_store = caller.memory_store
    child_key = slot_history_key(child)
    remade = []

    def read_while_the_owner_remakes_the_key(session_key):
        if session_key == child_key:
            state._slots.pop(child.key)
            remade.append(state.get_or_create_slot(child.key, agent="kirocrew"))
        return execution

    monkeypatch.setattr(state_facade, "read_worker_execution", read_while_the_owner_remakes_the_key)
    asyncio.run(count_restored_workers(state))
    assert remade and state._slots[child.key] is remade[0]
    assert slot_history_key(remade[0]) == child_key
    assert _slot_part(child_key) not in bot_worker_cap._BOT_SESSIONS
    assert _slot_part(child_key) not in bot_worker_cap._RECORDS_READ
    assert crewmate_worker_member_id(child_key) == ""


# Every session a verb opens as the Bot takes a seat, on any template and at any
# depth; the Bot's thread, a person's chat and a session with no member id do not.


def _restart(state) -> None:
    """What a restart leaves of the count: only the slots, none of the registries."""
    from kiro_crew.dashboard.state_parts import bot_worker_cap

    for key in list(state._slots):
        forget_crewmate_worker_session(key)
    bot_worker_cap._BOT_SESSIONS.clear()
    bot_worker_cap._RECORDS_READ.clear()


@pytest.fixture
def other_template(monkeypatch):
    """The stock leaf worker template, ``kirocrew-worker``, is configured."""
    from kiro_crew.config import loader

    monkeypatch.setattr(
        loader,
        "_MATERIALIZED_AGENTS",
        frozenset({*loader._MATERIALIZED_AGENTS, "kirocrew-worker", "kirocrew"}),
    )


def _other_template(state, worker):
    return _create(state, worker, agent="kirocrew-worker", agent_kind="template")["target"]


def test_a_workers_session_on_another_template_is_refused_past_the_limit(
    crewmate, other_template, tmp_path  # noqa: F811
):
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    worker = state.get_slot(_create(state, caller)["target"])
    before = set(state._slots)
    with pytest.raises(SessionControlError) as err:
        _other_template(state, worker)
    assert err.value.code == "bot_worker_cap_reached"
    assert err.value.status == 409
    assert "1 sessions open" in str(err.value)
    assert set(state._slots) == before


def test_a_workers_session_as_its_own_bot_is_refused_past_the_limit(
    crewmate, tmp_path  # noqa: F811
):
    """``agent=<its Bot>`` from a worker is a member selection carrying the Bot's id."""
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    worker = state.get_slot(_create(state, caller)["target"])
    child = _create(state, worker, agent="pixel")["target"]
    assert state.get_slot(child).agent == "pixel"
    before = set(state._slots)
    with pytest.raises(SessionControlError) as err:
        _create(state, worker, agent="pixel")
    assert err.value.code == "bot_worker_cap_reached"
    assert set(state._slots) == before


def test_a_session_such_a_child_opens_is_counted_too(
    crewmate, other_template, tmp_path  # noqa: F811
):
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 3)
    worker = state.get_slot(_create(state, caller)["target"])
    child = state.get_slot(_other_template(state, worker))
    _other_template(state, child)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"
    assert "3 sessions open" in str(err.value)


def test_a_restored_child_on_another_template_is_counted_from_its_saved_record(
    crewmate, other_template, tmp_path  # noqa: F811
):
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    worker = state.get_slot(_create(state, caller)["target"])
    _other_template(state, worker)
    _restart(state)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"
    assert "2 sessions open" in str(err.value)


def test_one_unreadable_restored_child_does_not_stop_the_others_counting(
    crewmate, other_template, tmp_path, monkeypatch  # noqa: F811
):
    state, caller, _execution = crewmate
    _set_cap(tmp_path, 3)
    worker = state.get_slot(_create(state, caller)["target"])
    child = state.get_slot(_other_template(state, worker))
    broken = state.get_slot(_other_template(state, worker))
    state._slots.pop(worker.key)
    _set_cap(tmp_path, 2)
    _restart(state)
    _unreadable(monkeypatch, slot_history_key(broken))
    # The thread's new worker and the readable child hold the two seats.
    _create(state, caller)
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"
    assert child.key in state._slots and broken.key in state._slots


def test_a_fork_one_below_the_limit_is_not_refused_by_its_own_child(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    """A restore pass running beside the fork counts the child during its bind."""
    from kiro_crew.dashboard import chat_fork
    from kiro_crew.members import note_crewmate_worker_session

    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    first = _create(state, caller)["target"]
    state.get_slot(first).messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )
    real_bind = chat_fork._bind_fork_execution
    counted_during_bind = []

    def bind_while_a_restore_pass_counts_the_child(identity, session_key, execution, **kwargs):
        result = real_bind(identity, session_key, execution, **kwargs)
        note_crewmate_worker_session(session_key, "pixel")
        counted_during_bind.append(session_key)
        return result

    monkeypatch.setattr(
        chat_fork, "_bind_fork_execution", bind_while_a_restore_pass_counts_the_child
    )
    forked = asyncio.run(
        sc.fork_session(state, caller_session_key=slot_history_key(caller), source=first)
    )
    assert counted_during_bind
    assert forked["target"] in state._slots
    with pytest.raises(SessionControlError) as err:
        _create(state, caller)
    assert err.value.code == "bot_worker_cap_reached"


def test_the_bots_thread_and_a_session_with_no_member_take_no_seat(
    crewmate, tmp_path  # noqa: F811
):
    from kiro_crew.dashboard.state import count_restored_workers

    state, caller, _execution = crewmate
    _set_cap(tmp_path, 1)
    plain = state.get_or_create_slot("chat-plain", agent="kirocrew")
    plain._created_by = caller.key
    plain.memory_store = caller.memory_store
    asyncio.run(count_restored_workers(state))
    # The one seat is still free: the thread and the plain session (no saved record
    # carrying a member id) hold none.
    _create(state, caller)
    with pytest.raises(SessionControlError):
        _create(state, caller)


@pytest.mark.parametrize("creator", ["", "cron:nightly"], ids=["person", "script-cron"])
def test_a_chat_no_verb_opened_takes_no_seat_on_the_bots_record(
    crewmate, tmp_path, creator  # noqa: F811
):
    """A person's chat, or a script cron's (its ``cron:`` stamp), running as the Bot."""
    from kiro_crew.dashboard.state import count_restored_workers
    from kiro_crew.execution_context import bind_session_execution

    state, caller, execution = crewmate
    _set_cap(tmp_path, 1)
    chat = state.get_or_create_slot("chat-not-a-verbs", agent="pixel")
    chat._created_by = creator
    chat.memory_store = caller.memory_store
    bind_session_execution(slot_history_key(chat), execution)
    asyncio.run(count_restored_workers(state))
    _create(state, caller)
    with pytest.raises(SessionControlError):
        _create(state, caller)


def test_a_counted_key_made_again_on_an_ordinary_template_frees_the_seat(
    crewmate, other_template, tmp_path  # noqa: F811
):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app_with_agent_routes
    from dashboard_owner_helpers import as_owner

    state, caller, _execution = crewmate
    _set_cap(tmp_path, 2)
    worker = state.get_slot(_create(state, caller)["target"])
    child = _other_template(state, worker)
    with pytest.raises(SessionControlError):
        _create(state, caller)

    # A person's explicit selection records the new agent on the same key
    # (``slot_guards._record_explicit_agent_selection``), as it does for a worker.
    state._slots.pop(child)

    async def remake():
        async with TestClient(TestServer(as_owner(_make_app_with_agent_routes(state)))) as client:
            response = await client.post(
                "/api/chat/slots",
                json={"name": child, "agent": "kirocrew", "agent_kind": "template"},
            )
            assert response.status == 200, await response.text()

    asyncio.run(remake())
    assert state._slots[child].agent == "kirocrew"
    _create(state, caller)

"""Which Bots have a dashboard turn running: the table ``GET /v1/crew/bots`` reads.

Two layers. The table alone: what is noted, what is not, and that a turn's entry
is its own. Then a real dashboard turn (``_run_chat`` on the shared turn state of
``chat_agent_selection_helpers.py``): the Bot is in the table while the turn holds
its session and out of it once the turn has ended, however it ended, and a fault in
the table never reaches the turn.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import test_app_session_bot_identity as kit
from chat_agent_selection_helpers import _finished, _turn_state
from chat_test_helpers import drain_background_tasks

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_runner, running_bot_turns
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

LOGGER = "kiro_crew.dashboard.running_bot_turns"


def _as_member(member_id: Any, kind: str = "member") -> SimpleNamespace:
    """What ``begin`` reads off an execution: how it was selected and its member id."""
    return SimpleNamespace(selection_kind=kind, member_id=member_id)


def _turn() -> SimpleNamespace:
    return SimpleNamespace()


# ── the table ────────────────────────────────────────────────────────────────


def test_a_noted_turn_is_running_until_it_ends() -> None:
    turn = _turn()
    assert running_bot_turns.running_member_ids() == frozenset()
    running_bot_turns.begin(turn, _as_member("writer-1"))
    assert running_bot_turns.running_member_ids() == {"writer-1"}
    running_bot_turns.end(turn)
    assert running_bot_turns.running_member_ids() == frozenset()


def test_a_real_member_execution_is_noted_by_its_member_id() -> None:
    execution = ExecutionContext(
        "writer-1", MemoryStoreRef("member-writer-1", "writer-1"), "member", "kirocrew"
    )
    turn = _turn()
    running_bot_turns.begin(turn, execution)
    assert running_bot_turns.running_member_ids() == {"writer-1"}
    running_bot_turns.end(turn)


@pytest.mark.parametrize(
    "execution",
    [
        None,
        _as_member(None),
        _as_member(""),
        _as_member(7),
        _as_member("writer-1", kind="template"),
    ],
    ids=[
        "no execution",
        "a template agent",
        "an empty member id",
        "a member id that is not text",
        "a helper that carries the Bot's member id under a template",
    ],
)
def test_a_turn_that_does_not_run_as_a_bot_itself_is_not_noted(execution) -> None:
    turn = _turn()
    running_bot_turns.begin(turn, execution)
    assert running_bot_turns.running_member_ids() == frozenset()
    assert not running_bot_turns._RUNNING
    running_bot_turns.end(turn)


@pytest.mark.parametrize("member_id", [None, "", 7, ["writer-1"]])
def test_a_scheduled_run_that_names_no_bot_is_not_noted(member_id) -> None:
    turn = _turn()
    running_bot_turns.begin_scheduled(turn, member_id)
    assert not running_bot_turns._RUNNING
    running_bot_turns.end(turn)


def test_a_scheduled_run_is_noted_by_the_member_id_it_is_given() -> None:
    turn = _turn()
    running_bot_turns.begin_scheduled(turn, "writer-1")
    assert running_bot_turns.running_member_ids() == {"writer-1"}
    running_bot_turns.end(turn)
    assert not running_bot_turns._RUNNING


def test_a_bot_with_two_turns_is_running_until_both_end() -> None:
    first, second = _turn(), _turn()
    running_bot_turns.begin(first, _as_member("writer-1"))
    running_bot_turns.begin(second, _as_member("writer-1"))
    running_bot_turns.end(first)
    assert running_bot_turns.running_member_ids() == {"writer-1"}
    running_bot_turns.end(second)
    assert running_bot_turns.running_member_ids() == frozenset()


def test_ending_a_turn_twice_or_one_never_noted_changes_nothing() -> None:
    noted, other = _turn(), _turn()
    running_bot_turns.end(other)
    running_bot_turns.begin(noted, _as_member("writer-1"))
    running_bot_turns.end(other)
    assert running_bot_turns.running_member_ids() == {"writer-1"}
    running_bot_turns.end(noted)
    running_bot_turns.end(noted)
    assert not running_bot_turns._RUNNING


def test_an_entry_holds_its_turn_so_another_turn_cannot_take_it_out() -> None:
    """The entry keeps the turn itself: its id cannot pass to a later turn while the
    entry is here, and ``end`` removes an entry only for the turn it holds."""
    earlier, later = _turn(), _turn()
    running_bot_turns.begin(earlier, _as_member("writer-1"))
    assert running_bot_turns._RUNNING[id(earlier)][0] is earlier
    # As if a later turn had been given the id the entry is filed under.
    running_bot_turns._RUNNING[id(later)] = running_bot_turns._RUNNING.pop(id(earlier))
    running_bot_turns.end(later)
    assert running_bot_turns.running_member_ids() == {"writer-1"}


def test_a_turn_noted_after_another_ended_is_its_own_entry() -> None:
    first = _turn()
    running_bot_turns.begin(first, _as_member("writer-1"))
    running_bot_turns.end(first)
    second = _turn()
    running_bot_turns.begin(second, _as_member("reviewer-1"))
    # The ended turn's late second ``end`` leaves the new turn's entry.
    running_bot_turns.end(first)
    assert running_bot_turns.running_member_ids() == {"reviewer-1"}
    running_bot_turns.end(second)
    assert not running_bot_turns._RUNNING


def test_a_turn_of_another_data_home_is_not_read(monkeypatch) -> None:
    turn = _turn()
    running_bot_turns.begin(turn, _as_member("writer-1"))
    monkeypatch.setattr(running_bot_turns, "_home", lambda: "/another/home")
    assert running_bot_turns.running_member_ids() == frozenset()


def _raises(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("the table is broken")


class _BrokenTable(dict):
    get = _raises
    values = _raises
    __setitem__ = _raises


@pytest.mark.parametrize("broken", ["_home", "_RUNNING"])
def test_a_fault_in_the_table_is_logged_and_never_raised(monkeypatch, caplog, broken) -> None:
    turn = _turn()
    monkeypatch.setattr(running_bot_turns, broken, _raises if broken == "_home" else _BrokenTable())
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        running_bot_turns.begin(turn, _as_member("writer-1"))
        running_bot_turns.begin_scheduled(turn, "writer-1")
        running_bot_turns.end(turn)
        assert running_bot_turns.running_member_ids() == frozenset()
    said = [record.getMessage() for record in caplog.records if record.name == LOGGER]
    assert "could not note a running Bot turn" in said
    assert "could not read the running Bot turns" in said
    # ``end`` reads the table only: it logs when the table itself is what broke.
    assert ("could not clear a running Bot turn" in said) == (broken == "_RUNNING")


# ── a real dashboard turn ────────────────────────────────────────────────────


async def _bot_chat(tmp_path, monkeypatch, name: str = "mine"):
    """The owner's chat opened as the private Bot, and that Bot's member id."""
    await asyncio.to_thread(kit._add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    async with kit._client(state) as client:
        response = await _finished(
            client.post("/api/chat/slots", json={"name": name, "agent": kit.PRIVATE_BOT}),
            "the request's response",
        )
        assert response.status == 200, await response.text()
    member_id = (await asyncio.to_thread(KiroCrewConfig.load)).agents[kit.PRIVATE_BOT].member_id
    assert member_id
    return state, state._slots[name], member_id


def _seen_at_the_claim(state, *, then: Any = None) -> list[frozenset[str]]:
    """Record the table each time the turn claims its session, which is after admission."""
    seen: list[frozenset[str]] = []
    provider = state.sessions.get_or_create.return_value

    async def claim(*_args: Any, **_kwargs: Any) -> Any:
        seen.append(running_bot_turns.running_member_ids())
        if then is not None:
            await then()
        return provider

    state.sessions.get_or_create.side_effect = claim
    return seen


@pytest.mark.asyncio
async def test_a_turn_admitted_as_a_bot_is_running_until_the_turn_ends(tmp_path, monkeypatch):
    state, slot, member_id = await _bot_chat(tmp_path, monkeypatch)
    seen = _seen_at_the_claim(state)

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    assert seen == [frozenset({member_id})]
    assert not [m for m in slot.messages if m["role"] == "error"]
    assert not running_bot_turns._RUNNING

    # The next turn in the same chat is noted again, and cleared again.
    await _finished(chat_runner._run_chat(state, slot, "again"), "the second chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")
    assert seen == [frozenset({member_id})] * 2
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_turn_on_a_template_agent_notes_no_bot(tmp_path, monkeypatch):
    await asyncio.to_thread(kit._add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("plain-chat")
    seen = _seen_at_the_claim(state)

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    assert seen == [frozenset()]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_turn_that_fails_after_admission_leaves_no_entry(tmp_path, monkeypatch):
    state, slot, member_id = await _bot_chat(tmp_path, monkeypatch)

    async def fail() -> None:
        raise RuntimeError("the session could not be claimed")

    seen = _seen_at_the_claim(state, then=fail)

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    assert seen == [frozenset({member_id})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_turn_stopped_after_admission_leaves_no_entry(tmp_path, monkeypatch):
    state, slot, member_id = await _bot_chat(tmp_path, monkeypatch)
    claimed = asyncio.Event()

    async def hold() -> None:
        claimed.set()
        await asyncio.Event().wait()

    seen = _seen_at_the_claim(state, then=hold)

    task = asyncio.ensure_future(chat_runner._run_chat(state, slot, "hi"))
    await _finished(claimed.wait(), "the turn reaching its session claim")
    assert running_bot_turns.running_member_ids() == {member_id}
    task.cancel()
    await _finished(asyncio.wait([task]), "the stopped turn ending")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    assert seen == [frozenset({member_id})]
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
async def test_a_turn_refused_before_its_identity_is_settled_notes_nothing(tmp_path, monkeypatch):
    """An app's session that names a Bot ends at admission, ahead of the note."""
    await asyncio.to_thread(kit._add_bots, monkeypatch)
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("shop-1", agent=kit.PRIVATE_BOT, app=kit.APP)
    noted: list[Any] = []
    monkeypatch.setattr(running_bot_turns, "begin", lambda *args: noted.append(args))

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_not_awaited()
    assert noted == []
    assert not running_bot_turns._RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["_home", "_RUNNING"])
async def test_a_fault_in_the_table_does_not_reach_the_turn(tmp_path, monkeypatch, broken):
    state, slot, _member_id = await _bot_chat(tmp_path, monkeypatch)
    monkeypatch.setattr(running_bot_turns, broken, _raises if broken == "_home" else _BrokenTable())

    await _finished(chat_runner._run_chat(state, slot, "hi"), "the chat turn")
    await _finished(drain_background_tasks(state), "the background tasks draining")

    state.sessions.get_or_create.assert_awaited_once()
    assert not [m for m in slot.messages if m["role"] == "error"]
    assert [m["content"] for m in slot.messages if m["role"] == "assistant"] == [
        "The task is complete."
    ]

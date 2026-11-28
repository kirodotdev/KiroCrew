"""The tracked-changes list across a ``/clear`` and a fork, through the real code paths.

A ``/clear`` that ran drops the entries, at once or where its queued discard lands; one
that has not started the session over leaves them. A fork takes the parent's list less
the pull requests the person unlinked there, and its own unlinked set and chips stay
as they are without this change.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_handlers, chat_runner  # noqa: F401
from kiro_crew.dashboard import tracked_changes as tc
from kiro_crew.dashboard.chat_fork import _shape_fork_child
from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.chat_utils_injected_rows import SUBAGENT_COMPLETION_KIND
from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
from kiro_crew.testing.wait import await_until

PR = "https://github.com/acme/widgets/pull/7"
OTHER = "https://github.com/acme/widgets/pull/8"


def _key(number: int, kind: str = "change") -> str:
    return source_ref_identity_key(
        ("github", "github.com", "acme", "widgets", number, "", kind, "")
    )


@pytest.fixture
def tab(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_runner, "schedule_eager_spawn", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        chat_runner, "_PENDING_RESET_RETRY_DELAY_SECS", 3600  # flake-ok: a tick that never fires
    )
    state.sessions.discard_conversation = AsyncMock(return_value=True)
    assert tc.record_tracked_change(slot, PR, "created")
    assert tc.record_tracked_change(slot, OTHER, "person")
    yield state, slot
    retry = state._pending_reset_retries.pop(slot.key, None)
    if retry is not None:
        retry[1].cancel()


async def _clear(state, slot) -> str:
    return await chat_runner._answer_slash_without_channel(
        state, slot, effective_session_key(slot), "/clear"
    )


@pytest.mark.asyncio
async def test_a_clear_that_ran_drops_the_entries(tab):
    state, slot = tab
    slot._dismissed_source_links = {_key(9)}
    slot._dirty = False
    assert await _clear(state, slot) == "discarded"
    assert list(slot._tracked_changes) == []
    assert slot._dismissed_source_links == {_key(9)}
    assert slot._dirty


async def _end_turn(state, slot) -> None:
    """The end-of-turn consume, then the hand-off, as ``_end_turn_tail`` orders them."""
    await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    if await chat_runner._start_next_queued_turn(state, slot):
        await await_until(slot.task, what="the drained turn")


@pytest.mark.asyncio
async def test_a_queued_clear_drops_the_list_only_where_its_discard_lands(tab, monkeypatch):
    state, slot = tab
    ran: list[str] = []

    async def completion_turn(_st, _sl, msg, **_kwargs):
        ran.append(msg)

    monkeypatch.setattr(chat_runner, "_run_chat", completion_turn)
    slot.queue_append("[Subagent completion event] run r1 finished", kind=SUBAGENT_COMPLETION_KIND)
    assert await _clear(state, slot) == "queued"
    # Queued: the conversation is not dropped yet, so neither is the list.
    assert list(slot._tracked_changes) == [_key(7), _key(8)]
    await _end_turn(state, slot)  # the completion runs in the old conversation
    assert len(ran) == 1 and list(slot._tracked_changes) == [_key(7), _key(8)]
    await _end_turn(state, slot)  # the discard lands
    assert slot._pending_discard_conversation_key is None
    assert list(slot._tracked_changes) == []


@pytest.mark.asyncio
async def test_a_clear_held_while_children_run_drops_the_list_when_it_lands(tab, monkeypatch):
    state, slot = tab
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=True))
    assert await _clear(state, slot) == "queued"
    assert list(slot._tracked_changes) == [_key(7), _key(8)]
    # The children finish; the held discard lands at the next boundary.
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=False))
    assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    assert slot._pending_discard_conversation_key is None
    assert list(slot._tracked_changes) == []
    assert slot._pending_discard_drops_tracked is False


@pytest.mark.asyncio
async def test_a_discard_the_agent_asked_for_keeps_the_list(tab):
    state, slot = tab
    # reset_conversation's discard is not a /clear: nothing marks the list.
    slot._pending_discard_conversation_key = effective_session_key(slot)
    assert await chat_runner._consume_pending_reset(state, slot, allow_discard=True, own_turn=True)
    assert list(slot._tracked_changes) == [_key(7), _key(8)]


def test_a_fork_of_a_session_with_an_unlinked_pr_shows_the_chips_it_shows_today(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent._titled = True
    parent.append("assistant", f"opened {PR} and filed https://github.com/acme/widgets/issues/5")
    parent.drain()
    assert tc.record_tracked_change(parent, PR, "created")
    assert tc.record_tracked_change(parent, OTHER, "created")
    # The person unlinked the PR and the issue on the parent.
    assert parent.dismiss_source_link(_key(7))
    assert parent.dismiss_source_link(_key(5, "issue"))
    _save_slot_to_history(state, parent, closed=False)

    child = state.get_or_create_slot("child")
    child.append("assistant", f"opened {PR} and filed https://github.com/acme/widgets/issues/5")
    child.drain()
    _shape_fork_child(
        child, parent, direction="head", jev_route_allowed=False, folders=[], folder_principal=""
    )
    # The unlinked PR stays out of the fork's list...
    assert list(child._tracked_changes) == [_key(8)]
    # ...and the fork's chips are what a fork shows without this change: it does not
    # inherit the parent's unlinks.
    chips = [link["url"] for link in child.to_dict()["source_links"]]
    assert PR in chips
    assert "https://github.com/acme/widgets/issues/5" in chips

    child._titled = True
    _save_slot_to_history(state, child, closed=False)
    del state._slots["child"]
    restored = _rehydrate_slot_from_history(state, "child")
    assert restored is not None
    assert restored._dismissed_source_links == set()
    assert list(restored._tracked_changes) == [_key(8)]

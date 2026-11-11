"""``swap_conversation``: an edit starts a fresh conversation on the live process.

Edit & Resend (the dashboard rewind) replaces the conversation under the SAME key.
Swapping it on the process the session already runs keeps every sub-agent of the
chat running, the ones that share that process included, and their reports still
resolve the key to the live, now fresh, conversation. When the session cannot swap,
nothing changes and the caller falls back to ``discard_conversation``, which still
ends every run.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.session_parts.record import FirstTurnState

KEY = "dashboard:chat-edit"


class _Runs:
    """The parent-end teardown handler, reduced to what a swap or a discard reads."""

    def __init__(self, children: list[SimpleNamespace]) -> None:
        self._children = children
        self.shared: set[str] = set()
        self.cancelled: list[tuple[tuple[str, ...], str]] = []

    @property
    def running(self) -> list[SimpleNamespace]:
        return [c for c in self._children if not c.done]

    def has_live_shared_session(self, session_key: str) -> bool:
        return session_key in self.shared

    def snapshot_teardown_children(self, parent: str) -> tuple[str, ...]:
        return tuple(c.id for c in self.running if c.parent_session_key == parent)

    async def cancel(self, agent_id: str) -> bool:
        next(c for c in self._children if c.id == agent_id).done = True
        return True

    async def cancel_for_teardown(self, ids, *, parent_session_key, verb="", reason=""):
        self.cancelled.append((tuple(ids), f"{verb}: {reason}" if reason else verb))
        for child in self._children:
            if child.id in ids:
                child.done = True
        return len(ids)


def _child(agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id,
        parent_session_key=KEY,
        conversation_key="",
        done=False,
        _reap_started=False,
        _reap_reason="",
        _stop_origin="",
    )


async def _setup(cfg, *, can_swap: bool = True, swap_raises: bool = False):
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    provider, _, _ = await mgr.get_or_create(KEY)
    mgr.release(mgr._fold_key(KEY))
    provider.cancel = AsyncMock(return_value="acked")
    provider.swaps_conversation_in_place = can_swap
    provider.session_id = "old-sid"

    async def _new_conversation() -> None:
        if swap_raises:
            raise RuntimeError("session/new refused")
        provider.session_id = "fresh-sid"

    provider.new_conversation = AsyncMock(side_effect=_new_conversation)
    mgr._session_map.set(KEY, "old-sid")
    children = [_child("a-1"), _child("a-2"), _child("a-3")]
    runs = _Runs(children)
    # All three share the parent's process: the case a discard cannot spare.
    runs.shared = {"subagent:a-1", "subagent:a-2", "subagent:a-3"}
    mgr.set_child_teardown_handler(runs)
    return mgr, provider, runs, children


@pytest.mark.asyncio
async def test_three_shared_sub_agents_survive_a_stop_then_an_edit(cfg):
    """stevruiz's report: Stop, then Edit & Resend, with three sub-agents running."""
    mgr, provider, runs, children = await _setup(cfg)

    assert await mgr.stop_turn(KEY) == "soft"
    assert await mgr.swap_conversation(KEY) == "swapped"

    assert [c.done for c in children] == [False, False, False]
    assert runs.cancelled == []
    provider.shutdown.assert_not_awaited()
    provider.new_conversation.assert_awaited_once_with()
    # A finished sub-agent's report resolves the parent through ``get_or_create``:
    # it gets the same live provider, now on the fresh conversation, whose first
    # turn replays the chat's history as a cold start would.
    again, is_new, resumed = await mgr.get_or_create(KEY)
    assert again is provider
    assert (is_new, resumed) == (True, False)
    mgr.release(mgr._fold_key(KEY))
    assert mgr._session_map._data[mgr._fold_key(KEY)]["sid"] == "fresh-sid"
    assert mgr._session_map.get_discarded_sid(KEY) == "old-sid"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_busy_session_is_not_swapped(cfg):
    mgr, provider, _runs, _children = await _setup(cfg)
    await mgr.get_or_create(KEY)  # holds the semaphore, like a channel turn

    assert await mgr.swap_conversation(KEY) == "busy"

    provider.new_conversation.assert_not_awaited()
    mgr.release(mgr._fold_key(KEY))
    await mgr.close_all()


@pytest.mark.parametrize("can_swap, swap_raises", [(False, False), (True, True)])
@pytest.mark.asyncio
async def test_a_session_that_cannot_swap_is_left_as_it_was(cfg, can_swap, swap_raises):
    """Another backend, a dead process, a remote workspace, or a refused session/new."""
    mgr, provider, runs, children = await _setup(cfg, can_swap=can_swap, swap_raises=swap_raises)
    session = mgr._sessions[mgr._fold_key(KEY)]
    first_turn = session.first_turn

    assert await mgr.swap_conversation(KEY) == "unavailable"

    assert mgr._sessions[mgr._fold_key(KEY)] is session
    assert session.first_turn is first_turn
    assert not session.semaphore.locked()
    assert mgr._session_map._data[mgr._fold_key(KEY)]["sid"] == "old-sid"
    assert runs.cancelled == []
    assert [c.done for c in children] == [False, False, False]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_session_flagged_for_an_account_change_is_not_swapped(cfg):
    mgr, provider, _runs, _children = await _setup(cfg)
    session = mgr._sessions[mgr._fold_key(KEY)]
    session.retire_on_identity_change = True

    assert await mgr.swap_conversation(KEY) == "unavailable"

    provider.new_conversation.assert_not_awaited()
    assert not session.semaphore.locked()
    assert mgr._session_map._data[mgr._fold_key(KEY)]["sid"] == "old-sid"
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_fallback_discard_still_ends_every_run_and_says_why(cfg):
    mgr, _provider, runs, _children = await _setup(cfg, can_swap=False)

    assert await mgr.swap_conversation(KEY) == "unavailable"
    assert await mgr.discard_conversation(KEY, teardown_reason="no reuse") is True

    assert runs.cancelled == [(("a-1", "a-2", "a-3"), "discard_conversation: no reuse")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_a_plain_discard_still_ends_every_run(cfg):
    mgr, _provider, runs, _children = await _setup(cfg)

    assert await mgr.discard_conversation(KEY) is True

    assert runs.cancelled == [(("a-1", "a-2", "a-3"), "discard_conversation")]
    await mgr.close_all()


@pytest.mark.asyncio
async def test_the_swap_arms_a_fresh_first_turn(cfg):
    mgr, _provider, _runs, _children = await _setup(cfg)
    mgr._sessions[mgr._fold_key(KEY)].first_turn = FirstTurnState.NOTHING_ARMED

    assert await mgr.swap_conversation(KEY) == "swapped"

    assert mgr._sessions[mgr._fold_key(KEY)].first_turn is FirstTurnState.FRESH
    await mgr.close_all()

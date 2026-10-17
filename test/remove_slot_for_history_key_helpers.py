"""Shared fakes for the ``test_remove_slot_for_history_key*.py`` files.

A ``DashboardState`` stand-in, a slot stand-in, the work-ledger guard, and a
driver that runs the teardown with the exact owner claim production captures.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Collection
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers import (
    _remove_slot_for_history_key as _remove_slot_for_history_key_impl,
)
from kiro_crew.dashboard.handlers import sessions as sessions_module
from kiro_crew.dashboard.handlers.sessions import _capture_history_delete_claim, _HistoryDeleteClaim


def _make_state(slots: dict) -> MagicMock:
    state = MagicMock()
    state._slots = dict(slots)
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.destroy = AsyncMock()
    session_generation = 1
    state.sessions.session_generation = MagicMock(return_value=session_generation)
    state.sessions.session_keys = MagicMock(return_value=set())
    # What a real destroy leaves behind: it removed this key's mapping, and no other
    # key points at that session id. A bare MagicMock would hand back a truthy Mock
    # here, which reads as "another holder still maps this id" and vetoes every
    # ledger removal -- so the default has to state the ordinary case explicitly.
    # The duplicate-mapping case sets its own return value.
    state.sessions.find_key_by_sid = MagicMock(return_value=None)

    async def destroy_if(
        key: str,
        expected_generation: object,
        should_destroy,
        *,
        preserve_autocompact_override: bool = False,
    ) -> bool:
        assert preserve_autocompact_override is True
        if expected_generation != state.sessions.session_generation.return_value:
            return False
        if not should_destroy():
            return False
        await state.sessions.destroy(key)
        return True

    state.sessions.destroy_if = AsyncMock(side_effect=destroy_if)
    state.sessions.drop_autocompact_overrides_matching = MagicMock(return_value=0)
    # No ACP session id unless a test says otherwise. A bare MagicMock would hand
    # back a truthy Mock, which is not an id, and the teardown would try to
    # address a ledger directory with it.
    state.sessions.resumable_sid = MagicMock(return_value=None)
    state.remove_chat_pins_for_slots = AsyncMock()
    state.conversation_log = MagicMock()
    state.crons = None
    return state


def _make_slot(key: str, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = running
    # A real slot is unbound unless its conversation lives on another session.
    # Left unset, a bare MagicMock hands back a truthy Mock as the session key,
    # so the teardown would target something that is not a key at all.
    slot.linked_session_key = ""
    slot.channel_origin = False
    if running:

        async def _hang():
            await asyncio.sleep(999)

        slot.task = asyncio.ensure_future(_hang())
    else:
        slot.task = None
    return slot


async def _remove_slot_for_history_key(
    state: MagicMock,
    key: str,
    *,
    delete_claim: _HistoryDeleteClaim | None = None,
    exact_owner_keys: Collection[str] = (),
) -> None:
    """Drive teardown with the same exact owner claim production captures."""
    claim = delete_claim or _capture_history_delete_claim(state, key)
    owner_keys = set(claim.cron_owner_keys)
    owner_keys.update(owner for owner in exact_owner_keys if owner)
    crons = getattr(state, "crons", None)
    if crons is not None and not exact_owner_keys:
        swept = await sessions_module._owner_keys_bound_to_transcript(crons, (key,))
        owner_keys.update(swept.get(key, ()))
    if owner_keys != set(claim.cron_owner_keys):
        claim = replace(claim, cron_owner_keys=frozenset(owner_keys))
    await _remove_slot_for_history_key_impl(state, key, delete_claim=claim)


def _guard_work_ledger_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("history deletion must preserve work ledgers")

    monkeypatch.setattr(session_ledger, "purge_matching", unexpected)

"""Archiving a session hands its remote workspace to the retention policy.

Driven through the real close paths (the tab close and the idle cleanup sweep),
because the question is whether a CLOSE schedules retention; a test calling
``retention.schedule_archived`` directly would pass whatever the close did.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state
from test_coder_retention import FakeCoder, _envs

from kiro_crew.coder import retention
from kiro_crew.dashboard.chat_handlers import api_chat_slots_cleanup, close_slot
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import await_until


def _state(tmp_path):
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    slot = state.get_or_create_slot("archived")
    return state, slot, slot_history_key(slot)


async def _archive(state, slot, path):
    if path == "tab_close":
        await close_slot(state, slot, slot.key)
    else:
        from test_slot_close_recreation_race import _Req

        slot.created_at = "2020-01-01T00:00:00+00:00"
        assert (await api_chat_slots_cleanup(_Req(state, slot.key))).status == 200
    # The close really ran, so an assertion below is about what it scheduled.
    assert state.get_slot(slot.key) is None


@pytest.fixture
def scheduled(monkeypatch):
    """Record every hand-off instead of reaching Coder."""
    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        retention, "schedule_archived", lambda keys, notify=None: calls.append(tuple(keys))
    )
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tab_close", "idle_cleanup"])
async def test_archiving_hands_the_session_key_to_retention(tmp_path, scheduled, path):
    state, slot, key = _state(tmp_path)
    await _archive(state, slot, path)
    assert len(scheduled) == 1
    assert key in scheduled[0]


@pytest.mark.asyncio
async def test_a_close_whose_key_a_replacement_took_schedules_nothing(
    tmp_path, scheduled, monkeypatch
):
    # A recreate that lands mid-close owns the key now: its workspace must not be
    # stopped or stamped for deletion because the ORIGINAL tab was dismissed.
    state, slot, _key = _state(tmp_path)
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers._slot_still_ours", lambda *_a, **_k: False
    )
    await close_slot(state, slot, slot.key)
    assert scheduled == []


@pytest.mark.asyncio
async def test_a_tab_close_stops_and_stamps_the_sessions_workspace(tmp_path, monkeypatch):
    """End to end: close -> background task -> registry stamp + coder stop."""
    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state, slot, key = _state(tmp_path)
    fake = FakeCoder()
    envs = _envs(tmp_path / "coder-home", fake, days=3)
    await envs.prepare(key, "dev")
    row = envs.registry.get_by_session(key)
    fake.calls.clear()

    async def environments():
        return envs  # every Coder call goes to the fake

    monkeypatch.setattr(retention, "_environments", environments)
    monkeypatch.setattr(retention, "_TASKS", set())
    await close_slot(state, slot, slot.key)
    # The close does not wait for Coder; wait for the task it started, bounded.
    assert retention._TASKS, "the close scheduled no retention task"
    await await_until(asyncio.gather(*retention._TASKS), what="archive retention tasks")

    assert f"stop {row.workspace_name}" in fake.calls
    stamped = envs.registry.get_by_session(key)
    due = datetime.fromisoformat(stamped.deletion_due_at.replace("Z", "+00:00"))
    assert timedelta(days=2, hours=23) < due - datetime.now(timezone.utc) <= timedelta(days=3)

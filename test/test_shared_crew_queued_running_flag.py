"""A chat turn accepted but still waiting in fair admission reads as queued, not running.

Drives the real ``GET /api/chat/slots/{slot}`` handler and the slot list
projection on a slot whose dispatched turn task is alive while admission holds
it (``admission_entry.admission_pending``). Every wait is an explicit event.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.shared_crew import admission_entry


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    return st


@pytest.mark.asyncio
async def test_a_turn_waiting_in_admission_reads_as_running_and_queued(state):
    slot = state.get_or_create_slot("waits")
    hold = asyncio.Event()
    slot.task = asyncio.ensure_future(hold.wait())
    admission_entry.mark_admission_pending(slot.key, True)
    try:
        async with TestClient(TestServer(_make_app(state))) as client:
            body = await (await client.get(f"/api/chat/slots/{slot.key}")).json()
            # Still in flight (the composer keeps Stop), but not started.
            assert body["running"] is True
            assert body["admission_queued"] is True
            listed = slot.to_dict()
            assert listed["running"] is True and listed["admission_queued"] is True

            # Admitted: the same live task is now the running turn.
            admission_entry.mark_admission_pending(slot.key, False)
            body = await (await client.get(f"/api/chat/slots/{slot.key}")).json()
            assert body["running"] is True
            assert body["admission_queued"] is False
            listed = slot.to_dict()
            assert listed["running"] is True and listed["admission_queued"] is False
    finally:
        admission_entry.mark_admission_pending(slot.key, False)
        hold.set()
        await slot.task

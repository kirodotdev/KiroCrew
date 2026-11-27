"""A teammate's steer names them, on the row, the live push and a requeue.

A send that lands while the session's turn runs is steered into that turn. The
plain send and the queued send carry the server-named Shared Crew author; the
steer must too, or a teammate's mid-turn message draws as the viewer's own,
with no name, for every other reader.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from steer_requeue_helpers import _running_slot

from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
from kiro_crew.shared_crew.contracts import MESSAGE_AUTHOR_META_KEY

AUTHOR = {"id": "kiro_relay:u-7", "kind": "human", "display": {"name": "teammate@example.com"}}


def _steer_client():
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    return client


@pytest.mark.asyncio
async def test_a_teammates_steer_row_and_push_name_the_teammate(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _running_slot(state)
    slot._acp_client = _steer_client()

    outcome = await steer_into_running_turn(
        state, slot, "B here, mid-turn", user_origin=True, admission={}, author=AUTHOR
    )

    assert outcome == STEER_STEERED
    row = next(m for m in slot.messages if m.get("content") == "B here, mid-turn")
    assert row["meta"][MESSAGE_AUTHOR_META_KEY] == AUTHOR
    pushes = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"]
    assert pushes and pushes[-1]["meta"][MESSAGE_AUTHOR_META_KEY] == AUTHOR
    # Held for the requeue, so a steer the turn never takes drains with its author.
    assert slot._steer_attachment_meta["B here, mid-turn"][MESSAGE_AUTHOR_META_KEY] == AUTHOR


@pytest.mark.asyncio
async def test_a_steer_with_no_author_keeps_its_old_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = _running_slot(state)
    slot._acp_client = _steer_client()

    await steer_into_running_turn(state, slot, "owner text", user_origin=True, admission={})

    row = next(m for m in slot.messages if m.get("content") == "owner text")
    assert MESSAGE_AUTHOR_META_KEY not in row["meta"]
    assert "owner text" not in slot._steer_attachment_meta

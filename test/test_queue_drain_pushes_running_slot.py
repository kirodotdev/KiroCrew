"""A turn the queue drain starts on an idle slot is pushed as running at once.

``_start_next_queued_turn`` sets ``slot.task`` and returns. Without its own
slots push the next frame came from ``_finish_queue_cycle`` at that turn's END,
so the Sessions list showed the row idle for the whole turn. This pins
a real ``slots`` frame that carries the row as running before the turn ends.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state


@pytest.mark.asyncio
async def test_drain_on_idle_slot_pushes_a_running_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.subagents = None
    slot = state.get_or_create_slot("idle-chat")

    from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

    queue_for_next_turn(state, slot, "queued while idle", directive_user_origin=True)
    assert not slot.running

    # No coalescing window: every push broadcasts at once, so the test reads
    # the frames on the push itself instead of waiting for a trailing timer.
    state._slots_broadcast_interval = 0
    frames: list[dict] = []

    def _capture(payload: dict) -> None:
        if payload.get("_type") == "slots":
            frames.append(next(row for row in payload["_slots_list"] if row.get("key") == slot.key))

    monkeypatch.setattr(state, "_broadcast", _capture)

    from kiro_crew.dashboard import chat_runner

    live_turn = MagicMock(done=MagicMock(return_value=False))
    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=live_turn),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True

    assert slot.task is live_turn
    assert frames, "the drain must push the slot list once the turn holds the slot"
    assert frames[-1].get("running") is True, frames[-1]

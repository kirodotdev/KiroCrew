"""The slot-detail queue says per entry whether its user row is already in the
transcript (``meta.written``).

The queue's cancel and edit refuse such an entry (``row_already_written``), so
the card reads this mark to offer only Send now (`row_already_written` is
pinned in test_responsive_chat). It is judged on the live slot,
in the same read, not guessed by the client.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard.responsive_chat import QUEUED_ROW_META_KEY
from kiro_crew.dashboard.slot_queue_repository import RESTORED_QUEUE_KEY


@pytest.mark.asyncio
async def test_the_slot_detail_queue_marks_an_entry_whose_row_is_written(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app
    from chat_test_helpers import _make_state as _make_full_state

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_full_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("written-chat")
    # The send wrote its row before the restart; the restored entry still waits.
    slot.messages.append(
        {"role": "user", "content": "sent words", "meta": {QUEUED_ROW_META_KEY: "w-1"}}
    )
    slot._queue.extend(
        [
            {"id": "w-1", "content": "sent words", "meta": {}, RESTORED_QUEUE_KEY: True},
            {"id": "h-1", "content": "unwritten words", "meta": {}, RESTORED_QUEUE_KEY: True},
        ]
    )
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots/written-chat")
        assert resp.status == 200
        queue = {q["id"]: q.get("meta") or {} for q in (await resp.json())["queue"]}

    assert queue["w-1"].get("written") is True
    assert queue["w-1"].get("held") is True
    assert "written" not in queue["h-1"]
    assert queue["h-1"].get("held") is True


def test_a_queue_push_frame_carries_written_for_an_entry_whose_row_is_written():
    from types import SimpleNamespace

    from kiro_crew.dashboard.responsive_chat import mark_written_on_push

    slot = SimpleNamespace(
        messages=[{"role": "user", "content": "sent", "meta": {QUEUED_ROW_META_KEY: "w-1"}}],
        _queue=[
            {"id": "w-1", "content": "sent", "meta": {}, RESTORED_QUEUE_KEY: True},
            {"id": "q-1", "content": "not yet", "meta": {}},
        ],
    )
    written: dict = {"queue_id": "w-1"}
    mark_written_on_push(slot, "w-1", written)
    assert written["meta"]["written"] is True
    plain: dict = {"queue_id": "q-1"}
    mark_written_on_push(slot, "q-1", plain)
    assert "meta" not in plain

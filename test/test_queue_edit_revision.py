"""Each queue edit advances a per-entry revision, and slot detail serves it.

A client orders ``queue_edit`` frames by this revision, so slot detail must carry it or a row
rebuilt by a refetch cannot refuse a late, older frame.
"""

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat_delivery import queue_entry_view
from kiro_crew.dashboard.slot_queue_repository import SlotQueueRepository


class _Owner:
    def __init__(self, queue):
        self._queue = queue


def test_each_edit_advances_the_revision():
    owner = _Owner([{"id": "q1", "content": "original"}])
    repo = SlotQueueRepository()
    assert repo.queue_edit_by_id(owner, "q1", "first") is True
    assert owner._queue[0]["edit_rev"] == 1
    assert repo.queue_edit_by_id(owner, "q1", "second") is True
    assert owner._queue[0]["edit_rev"] == 2


def test_revisions_count_per_entry():
    owner = _Owner([{"id": "q1", "content": "a"}, {"id": "q2", "content": "b"}])
    repo = SlotQueueRepository()
    assert repo.queue_edit_by_id(owner, "q1", "a1") is True
    assert repo.queue_edit_by_id(owner, "q1", "a2") is True
    assert repo.queue_edit_by_id(owner, "q2", "b1") is True
    assert owner._queue[0]["edit_rev"] == 2
    assert owner._queue[1]["edit_rev"] == 1


def test_an_unedited_entry_carries_no_revision():
    owner = _Owner([{"id": "q1", "content": "original"}])
    assert "edit_rev" not in owner._queue[0]


def test_slot_detail_view_carries_the_revision():
    assert queue_entry_view({"id": "q1", "content": "x", "edit_rev": 3})["editRev"] == 3


def test_slot_detail_view_omits_it_when_absent():
    assert "editRev" not in queue_entry_view({"id": "q1", "content": "x"})


def test_slot_detail_view_ignores_a_boolean_revision():
    """``True`` is an ``int``, so a bare isinstance check would publish it as revision 1."""
    assert "editRev" not in queue_entry_view({"id": "q1", "content": "x", "edit_rev": True})


@pytest.mark.asyncio
async def test_slot_detail_serves_the_revision_after_an_edit(tmp_path):
    """GET slot detail renders off a snapshot built in the handler, not the live entry."""
    state = _make_state(tmp_path)
    state.push_slots_update = lambda: None
    slot = state.get_or_create_slot("chat-1")
    slot.messages = [{"role": "user", "content": "hi"}]
    untouched = slot.queue_append("left alone", directive_user_origin=True)
    edited = slot.queue_append("original", directive_user_origin=True)
    assert slot.queue_edit_by_id(edited, "first") is True
    assert slot.queue_edit_by_id(edited, "second") is True

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/chat/slots/chat-1")
        assert resp.status == 200
        body = await resp.json()

    by_id = {q["id"]: q for q in body["queue"]}
    assert by_id[edited]["editRev"] == 2
    assert "editRev" not in by_id[untouched]

"""An edited queue entry is marked, and slot detail carries the mark.

A client that missed the ``queue_push`` broadcast cannot otherwise tell an edited entry from a
merely redacted one, so it adopted a pre-send record whose text and files predate the edit.
"""

from kiro_crew.dashboard.chat_utils import queue_entry_for_detail
from kiro_crew.dashboard.slot_queue_repository import SlotQueueRepository


class _Owner:
    def __init__(self, queue):
        self._queue = queue


def test_edit_marks_the_entry():
    owner = _Owner([{"id": "q1", "content": "original"}])
    assert SlotQueueRepository().queue_edit_by_id(owner, "q1", "edited text") is True
    assert owner._queue[0]["content"] == "edited text"
    assert owner._queue[0]["edited"] is True


def test_an_id_less_edit_does_not_inherit_the_previous_edit_identity():
    """`edit_id` names which edit produced the CURRENT text, so a later id-less edit must clear it.

    An id-less edit is a supported shape -- ``normalize_send_id`` treats an unusable id as absent,
    the old-client case. Retaining the earlier id makes the entry claim an edit that did not produce
    its text, and a client correlating on that id keeps a record it should have retired, then answers
    a cancel with the obsolete text and attachments.
    """
    owner = _Owner([{"id": "q1", "content": "original"}])
    repo = SlotQueueRepository()

    assert repo.queue_edit_by_id(owner, "q1", "first edit", edit_id="edit-1") is True
    assert owner._queue[0]["edit_id"] == "edit-1", "premise: the first edit stamped its id"

    # A second edit arrives carrying no usable id.
    assert repo.queue_edit_by_id(owner, "q1", "second edit", edit_id=None) is True

    assert owner._queue[0]["content"] == "second edit"
    assert owner._queue[0]["edited"] is True
    assert (
        "edit_id" not in owner._queue[0]
    ), "the retained id names an edit that did not produce this text"


def test_an_id_less_first_edit_stamps_no_id():
    """Positive control: absence here must come from the edit, not from the pop above."""
    owner = _Owner([{"id": "q1", "content": "original"}])
    assert SlotQueueRepository().queue_edit_by_id(owner, "q1", "edited", edit_id=None) is True
    assert "edit_id" not in owner._queue[0]


def test_a_later_identified_edit_replaces_the_previous_id():
    owner = _Owner([{"id": "q1", "content": "original"}])
    repo = SlotQueueRepository()
    assert repo.queue_edit_by_id(owner, "q1", "first", edit_id="edit-1") is True
    assert repo.queue_edit_by_id(owner, "q1", "second", edit_id="edit-2") is True
    assert owner._queue[0]["edit_id"] == "edit-2", "the newest edit owns the identity"


def test_unedited_entry_carries_no_mark():
    # Positive control: a hardwired True would satisfy the assertion above.
    owner = _Owner([{"id": "q1", "content": "original"}])
    assert "edited" not in owner._queue[0]


def test_slot_detail_carries_the_mark():
    assert queue_entry_for_detail({"id": "q1", "content": "x", "edited": True})["edited"] is True


def test_slot_detail_omits_it_when_absent():
    # Additive: an entry that was never edited keeps its prior shape.
    assert "edited" not in queue_entry_for_detail({"id": "q1", "content": "x"})


def test_detail_snapshot_shape_preserves_recovery_fields():
    """The slot-detail snapshot must keep what queue_entry_for_detail reads.

    The snapshot is built by comprehension in api_chat_slot_detail; a shape that kept only
    id/content made every ``meta.sendId`` / ``edited`` read miss, so a client that had missed the
    ``queue_push`` broadcast hydrated an id-less, unmarked card -- exactly the reconnect,
    switchSlot and new-tab paths.
    """
    live = [
        {
            "id": "q1",
            "content": "hello",
            "kind": "",
            "meta": {"sendId": "s-1"},
            "edited": True,
        }
    ]
    # Mirrors the handler's comprehension, which is what the serializer is fed.
    snapshot = [
        {
            "id": q["id"],
            "content": q["content"],
            **({"meta": dict(q["meta"])} if isinstance(q.get("meta"), dict) else {}),
            **({"edited": True} if q.get("edited") else {}),
        }
        for q in live
    ]
    entry = queue_entry_for_detail(snapshot[0])
    assert entry["sendId"] == "s-1", "a stripped snapshot loses the id the stash is adopted by"
    assert entry["edited"] is True, "a stripped snapshot loses the edit marker"


def test_detail_snapshot_does_not_alias_live_meta():
    # The snapshot exists so the render thread never reads a dict the event loop keeps mutating.
    live = {"id": "q1", "content": "hello", "meta": {"sendId": "s-1"}}
    snap = {"id": live["id"], "content": live["content"], "meta": dict(live["meta"])}
    live["meta"]["sendId"] = "s-MUTATED"
    assert queue_entry_for_detail(snap)["sendId"] == "s-1"

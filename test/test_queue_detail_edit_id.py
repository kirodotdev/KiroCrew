"""A slot-detail queue entry must name WHICH edit produced its text.

Hydration correlates a client's pre-send stash against the entry. Without the edit id the only
datum left is the redacted display, and redaction erases image markers -- so a concurrent edit
elsewhere that renders the same string leaves a stale record alive to answer a cancel with
obsolete text and attachments.

The helper is fed TWO shapes by its three callers: the raw queue item, which spells the field
``edit_id``, and the already-built queue snapshot, which spells it ``editId``. Both are pinned,
because a reader of only one passes while the other silently drops the correlation.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_utils import queue_entry_for_detail


def test_detail_entry_carries_edit_id_from_a_raw_queue_item() -> None:
    entry = queue_entry_for_detail(
        {"id": "q-1", "content": "look at an image", "edited": True, "edit_id": "edit-abc"}
    )
    assert (
        entry["editId"] == "edit-abc"
    ), "without the id, hydration falls back to comparing redacted display text"
    assert entry["edited"] is True


def test_detail_entry_carries_edit_id_from_a_queue_snapshot_entry() -> None:
    """The snapshot caller has already renamed the field, so reading only ``edit_id`` misses it."""
    entry = queue_entry_for_detail(
        {"id": "q-2", "content": "look at an image", "edited": True, "editId": "edit-xyz"}
    )
    assert entry["editId"] == "edit-xyz"


def test_detail_entry_omits_the_id_when_the_entry_has_none() -> None:
    entry = queue_entry_for_detail({"id": "q-3", "content": "plain", "edited": True})
    assert "editId" not in entry, "an unedited-by-id entry must keep its shape"
    assert entry["edited"] is True


def test_detail_entry_ignores_a_non_string_id() -> None:
    entry = queue_entry_for_detail({"id": "q-4", "content": "plain", "edited": True, "edit_id": 17})
    assert "editId" not in entry

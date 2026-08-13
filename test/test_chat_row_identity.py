"""Per-row delivery identity (``meta.mid``) stamped by ``_ChatSlot.append``.

A client sees the same row through two doors -- the slot-detail HTTP rebuild and
the live ``chat_message`` broadcast -- and must be able to tell "this row again"
from "another row that happens to look identical". ``ts`` cannot answer that (a
coarse OS clock stamps two same-tick appends identically) and neither can content
(two identical messages are legitimate), so the row carries an explicit id.
"""

from __future__ import annotations

from kiro_crew.dashboard.chat_persistence import _build_message_entry
from kiro_crew.dashboard.state import _ChatSlot


def _slot() -> _ChatSlot:
    return _ChatSlot("chat-1-123")


def test_append_stamps_a_row_id() -> None:
    slot = _slot()
    slot.append("assistant", "hello")
    mid = slot.messages[-1]["meta"]["mid"]
    assert isinstance(mid, str) and mid.startswith("m-") and len(mid) > 5


def test_two_identical_same_ts_rows_get_distinct_ids() -> None:
    """The whole point: byte-identical rows on one tick stay distinguishable.

    This is the shape a Slack channel-window replay produces, and the reason the
    client cannot key identity on (ts, role, content) -- under that key the second
    row is indistinguishable from a redelivery of the first and disappears.
    """
    slot = _slot()
    ts = "2026-08-06T01:02:03.000004+00:00"
    slot.append("user", "ok", ts=ts)
    slot.append("user", "ok", ts=ts)
    first, second = slot.messages[-2], slot.messages[-1]
    assert first["ts"] == second["ts"]
    assert first["content"] == second["content"]
    assert first["meta"]["mid"] != second["meta"]["mid"]


def test_append_preserves_a_supplied_row_id() -> None:
    """A row replayed from disk keeps its id, or a post-restart redelivery of
    that row would no longer be recognisable as the same row."""
    slot = _slot()
    slot.append("assistant", "restored", meta={"mid": "m-fromdisk", "other": 1})
    assert slot.messages[-1]["meta"]["mid"] == "m-fromdisk"
    assert slot.messages[-1]["meta"]["other"] == 1


def test_append_keeps_existing_meta_alongside_the_id() -> None:
    slot = _slot()
    slot.append("tool", "fs_read", meta={"tool_call_id": "call-A"})
    meta = slot.messages[-1]["meta"]
    assert meta["tool_call_id"] == "call-A"
    assert meta["mid"].startswith("m-")


def test_wire_only_roles_get_no_row_id() -> None:
    """``chunk`` is appended once per streamed token and is never broadcast as a
    ``chat_message`` nor persisted, so an id would cost a uuid4 and a dict on the
    runner's hottest path and buy nothing."""
    slot = _slot()
    for role in ("chunk", "done", "streaming"):
        slot.append(role, "x", role)
        assert "meta" not in slot.messages[-1] or "mid" not in slot.messages[-1].get("meta", {})


def test_row_id_survives_the_persistence_entry() -> None:
    """The id has to round-trip through disk: the rebuilt row is what a
    post-restart redelivery is compared against."""
    slot = _slot()
    slot.append("assistant", "persisted answer")
    entry = _build_message_entry(slot.messages[-1])
    assert entry is not None
    assert entry["meta"]["mid"] == slot.messages[-1]["meta"]["mid"]


def test_row_id_is_not_redacted_out_of_the_persistence_entry() -> None:
    """``_build_message_entry`` runs every meta string through the credential and
    exfil redactors. An id mangled there would differ between the two delivery
    paths and defeat the comparison, so its shape must survive verbatim."""
    slot = _slot()
    slot.append("assistant", "answer")
    mid = slot.messages[-1]["meta"]["mid"]
    entry = _build_message_entry(slot.messages[-1])
    assert entry is not None
    assert entry["meta"]["mid"] == mid

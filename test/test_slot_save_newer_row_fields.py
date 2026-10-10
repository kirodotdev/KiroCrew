"""A restored dashboard slot keeps a newer build's row fields through its flush.

The desktop updater allows downgrades. A transcript a newer build wrote can then be
restored by this build (``_rehydrate_slot_from_history``) and flushed
(``_save_slot_to_history``), and the flush re-serializes every row in the live window.
Each test saves a slot with the current code on a ``tmp_path`` home, edits the
transcript, restores the slot, flushes and reads the file back.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path):
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _saved_slot(tmp_path, monkeypatch, *pairs):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    for role, content in pairs:
        slot.append(role, content)
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    return state, state.conversation_log._path("dashboard:s1")


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[1:]]


def _edit_rows(path, edit):
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines[1:]]
    edit(rows)
    path.write_text("\n".join([lines[0], *(json.dumps(r) for r in rows)]) + "\n", encoding="utf-8")


def _restore(state):
    del state._slots["s1"]
    restored = _rehydrate_slot_from_history(state, "s1")
    assert restored is not None
    return restored


def test_a_restored_slots_flush_keeps_a_newer_builds_row_field(tmp_path, monkeypatch):
    state, path = _saved_slot(tmp_path, monkeypatch, ("user", "q1"), ("assistant", "a1"))

    def newer(rows):
        for row in rows:
            if row["role"] == "assistant":
                row["reactions"] = ["thumbs_up"]

    _edit_rows(path, newer)
    restored = _restore(state)
    restored.append("user", "q2")
    restored.drain()
    _save_slot_to_history(state, restored, closed=False)
    kept = [r.get("reactions") for r in _rows(path) if r["role"] == "assistant"]
    assert kept == [["thumbs_up"]], f"the flush dropped the newer row field: {kept}"


def test_a_row_from_this_version_round_trips_unchanged(tmp_path, monkeypatch):
    state, path = _saved_slot(tmp_path, monkeypatch, ("user", "q1"), ("assistant", "a1"))
    before = _rows(path)
    restored = _restore(state)
    _save_slot_to_history(state, restored, closed=False)
    assert _rows(path) == before


def test_a_field_this_build_changes_is_written_with_its_new_value(tmp_path, monkeypatch):
    """Restore redacts assistant content; the flush writes the redacted value, not the disk one."""
    state, path = _saved_slot(tmp_path, monkeypatch, ("user", "q1"), ("assistant", "a1"))
    leaked = "key AKIAIOSFODNN7EXAMPLE here"

    def with_secret(rows):
        for row in rows:
            if row["role"] == "assistant":
                row["content"] = leaked

    _edit_rows(path, with_secret)
    restored = _restore(state)
    restored.append("user", "q2")
    restored.drain()
    _save_slot_to_history(state, restored, closed=False)
    written = [r["content"] for r in _rows(path) if r["role"] == "assistant"]
    assert written and written != [leaked] and "AKIAIOSFODNN7EXAMPLE" not in written[0], written


def test_the_live_window_size_limit_is_unchanged(tmp_path, monkeypatch):
    pairs = [("user", f"q{i}") if i % 2 == 0 else ("assistant", f"a{i}") for i in range(510)]
    state, path = _saved_slot(tmp_path, monkeypatch, *pairs)
    restored = _restore(state)
    assert len(restored.messages) == 500
    assert restored._disk_older_count == 10


def _restored_with_newer_field(tmp_path, monkeypatch):
    state, path = _saved_slot(tmp_path, monkeypatch, ("user", "q1"), ("assistant", "a1"))

    def newer(rows):
        for row in rows:
            if row["role"] == "assistant":
                row["reactions"] = ["thumbs_up"]

    _edit_rows(path, newer)
    return state, _restore(state)


def test_the_slot_detail_payload_never_carries_the_private_key(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_utils import UNKNOWN_ROW_FIELDS_KEY, _prepare_messages

    _state, restored = _restored_with_newer_field(tmp_path, monkeypatch)
    assert any(
        UNKNOWN_ROW_FIELDS_KEY in m for m in restored.messages
    ), "precondition: kept in memory"
    out = _prepare_messages(list(restored.messages), False, live_child="")
    assert out and all(UNKNOWN_ROW_FIELDS_KEY not in m for m in out)
    assert "thumbs_up" not in json.dumps(out)


def test_the_websocket_chat_message_frame_never_carries_the_private_key(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_utils import UNKNOWN_ROW_FIELDS_KEY
    from kiro_crew.dashboard.state import chat_message_note

    _state, restored = _restored_with_newer_field(tmp_path, monkeypatch)
    row = next(m for m in restored.messages if UNKNOWN_ROW_FIELDS_KEY in m)
    frame = chat_message_note("s1", row, workspace=None)
    assert UNKNOWN_ROW_FIELDS_KEY not in json.dumps(frame, default=str)
    assert "thumbs_up" not in json.dumps(frame, default=str)


def test_the_row_builder_writes_no_key_outside_the_known_set():
    from kiro_crew.dashboard.slot_persistence.message_entries import (
        _KNOWN_ROW_KEYS,
        _build_message_entry_uncached,
    )

    m = {
        "role": "system",
        "content": "c",
        "ts": "1",
        "cls": "msg msg-s",
        "meta": {"mid": "x"},
        "variants": [{"content": "v"}],
        "variant_idx": 0,
        "source_thread": "t",
        "source_user": "u",
    }
    entry = _build_message_entry_uncached(m)
    assert set(entry) <= _KNOWN_ROW_KEYS, sorted(set(entry) - _KNOWN_ROW_KEYS)


def test_a_channel_window_rebuild_keeps_a_newer_builds_row_field(tmp_path, monkeypatch):
    """The channel restore loop (``channel_slots._rebuild_window``) feeds the same save."""
    from kiro_crew.dashboard.channel_slots import _rebuild_window

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("slack_1.1", linked_session_key="slack:1.1")
    rows = [
        {"role": "user", "content": "q1", "ts": "1"},
        {"role": "assistant", "content": "a1", "ts": "2", "reactions": ["thumbs_up"]},
    ]
    _rebuild_window(slot, rows)
    slot._dirty = True
    _save_slot_to_history(state, slot)
    kept = [
        r.get("reactions") for r in _rows(tmp_path / "slack_1.1.jsonl") if r["role"] == "assistant"
    ]
    assert kept == [
        ["thumbs_up"]
    ], f"the channel window rebuild dropped the newer row field: {kept}"


def _restored_with_fields(tmp_path, monkeypatch, user_fields, assistant_fields):
    state, path = _saved_slot(tmp_path, monkeypatch, ("user", "q1"), ("assistant", "a1"))

    def newer(rows):
        for row in rows:
            row.update(user_fields if row["role"] == "user" else assistant_fields)

    _edit_rows(path, newer)
    return state, path, _restore(state)


def test_a_16_mib_field_from_a_newer_build_is_not_kept_in_memory(tmp_path, monkeypatch, caplog):
    """The over-cap case: the row keeps none of its newer fields, says so, and saves without them."""
    from kiro_crew.dashboard.chat_utils import UNKNOWN_ROW_FIELDS_KEY

    huge = "x" * (16 * 1024 * 1024)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_persistence"):
        state, path, restored = _restored_with_fields(
            tmp_path, monkeypatch, {"reactions": ["thumbs_up"]}, {"blob": huge}
        )
    kept = {m["role"]: m.get(UNKNOWN_ROW_FIELDS_KEY) for m in restored.messages}
    assert kept["assistant"] is None, "the 16 MiB field stayed in live slot memory"
    assert kept["user"] == {"reactions": ["thumbs_up"]}, "a row under the cap lost its field"
    assert "over the" in caplog.text, "the refused row was not logged"
    restored.append("user", "q2")
    restored.drain()
    _save_slot_to_history(state, restored, closed=False)
    saved = {r["content"]: r for r in _rows(path)}
    assert "blob" not in saved["a1"]
    assert saved["q1"]["reactions"] == ["thumbs_up"]


def test_the_cap_keeps_a_row_at_the_cap_and_refuses_one_byte_over(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat_utils import UNKNOWN_ROW_FIELDS_KEY
    from kiro_crew.dashboard.slot_persistence.message_entries import MAX_UNKNOWN_ROW_FIELDS_BYTES

    fill = MAX_UNKNOWN_ROW_FIELDS_BYTES - len(json.dumps({"blob": ""}))
    _state, _path, restored = _restored_with_fields(
        tmp_path, monkeypatch, {"blob": "x" * fill}, {"blob": "x" * (fill + 1)}
    )
    kept = {m["role"]: m.get(UNKNOWN_ROW_FIELDS_KEY) for m in restored.messages}
    assert len(json.dumps(kept["user"])) == MAX_UNKNOWN_ROW_FIELDS_BYTES
    assert kept["assistant"] is None


def _kept_fields(slot):
    from kiro_crew.dashboard.chat_utils import UNKNOWN_ROW_FIELDS_KEY

    return [m.get(UNKNOWN_ROW_FIELDS_KEY) for m in slot.messages]


def _assert_within_the_slot_budget_oldest_dropped_first(kept):
    assert kept[-1] is not None, "the newest row lost its field"
    assert kept[0] is None, "the oldest row still keeps its field: the slot has no budget"
    from kiro_crew.dashboard.slot_persistence.message_entries import (
        MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES,
    )

    total = sum(len(json.dumps(dict(k))) for k in kept if k is not None)
    assert total <= MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES, f"{total} bytes kept in one slot"
    first = next(i for i, k in enumerate(kept) if k is not None)
    assert all(
        k is not None for k in kept[first:]
    ), "a newer row lost its field before an older one"


def test_a_restored_slot_keeps_its_budget_of_newer_fields_and_drops_the_oldest(
    tmp_path, monkeypatch, caplog
):
    """400 rows of 8 KB each: 3.2 MB of newer fields, past the slot's budget."""
    pairs = [p for i in range(200) for p in (("user", f"q{i}"), ("assistant", f"a{i}"))]
    state, path = _saved_slot(tmp_path, monkeypatch, *pairs)
    _edit_rows(path, lambda rows: [row.update(blob="x" * 8000) for row in rows])
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_persistence"):
        restored = _restore(state)
    _assert_within_the_slot_budget_oldest_dropped_first(_kept_fields(restored))
    assert "older row(s)" in caplog.text, "the dropped rows were not logged"
    restored.append("user", "q-new")
    restored.drain()
    _save_slot_to_history(state, restored, closed=False)
    saved = {r["content"]: r for r in _rows(path)}
    assert "blob" in saved["a199"] and "blob" not in saved["q0"]


def test_a_channel_window_rebuild_past_the_slot_budget_drops_the_oldest_rows_fields(
    tmp_path, monkeypatch
):
    from kiro_crew.dashboard.channel_slots import _rebuild_window

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("slack_2.2", linked_session_key="slack:2.2")
    rows = [
        {"role": ("user", "assistant")[i % 2], "content": f"m{i}", "ts": str(i), "blob": "x" * 8000}
        for i in range(300)
    ]
    _rebuild_window(slot, rows)
    _assert_within_the_slot_budget_oldest_dropped_first(_kept_fields(slot))


@pytest.mark.asyncio
async def test_a_drop_keeps_every_row_the_same_dict(tmp_path, monkeypatch):
    """Rewind and edit-resend find rows that arrived during an await by ``id(row)``.

    A reconcile that appends past the slot budget must drop the older rows' fields in
    place: a row replaced by a copy would read as arrived and be re-appended.
    """
    from kiro_crew.dashboard.chat_handlers import _reconcile_slot_window
    from kiro_crew.dashboard.chat_utils import UNKNOWN_ROW_FIELDS_KEY

    pairs = [p for i in range(50) for p in (("user", f"q{i}"), ("assistant", f"a{i}"))]
    state, path = _saved_slot(tmp_path, monkeypatch, *pairs)
    _edit_rows(path, lambda rows: [row.update(blob="x" * 8000) for row in rows])
    restored = _restore(state)
    before = list(restored.messages)
    assert all(m.get(UNKNOWN_ROW_FIELDS_KEY) is not None for m in before), "precondition"

    def another_writer(rows):
        last = rows[-1]
        for i in range(50):
            rows.append({**last, "content": f"late{i}", "blob": "x" * 8000})

    _edit_rows(path, another_writer)
    await _reconcile_slot_window(state, restored)
    assert len(restored.messages) == len(before) + 50, "precondition: the reconcile appended"
    kept = _kept_fields(restored)
    assert kept[0] is None, "precondition: the append passed the budget and dropped fields"
    now = {id(m) for m in restored.messages}
    lost = [i for i, m in enumerate(before) if id(m) not in now]
    assert not lost, f"{len(lost)} rows were replaced by new dicts, first at index {lost[0]}"


def test_a_window_rebuild_does_not_count_the_rows_it_replaced(tmp_path, monkeypatch):
    """A refresh that finds the transcript rotated rebuilds the window from it.

    The rebuild clears the window and appends the transcript again. The rows it
    cleared must not count against the tab's budget: 200 rows whose fields total
    802,400 bytes, under the 1 MiB budget, all keep them, and the next save writes
    every one of them back.
    """
    from kiro_crew.dashboard.channel_slots import _rebuild_window, refresh_channel_window
    from kiro_crew.dashboard.slot_persistence.message_entries import (
        MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES,
    )

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("slack_3.3", linked_session_key="slack:3.3")

    def transcript(prefix, n):
        return [
            {
                "role": ("user", "assistant")[i % 2],
                "content": f"{prefix}{i}",
                "ts": str(i),
                "blob": "x" * 4000,
            }
            for i in range(n)
        ]

    _rebuild_window(slot, transcript("before", 80))
    after = transcript("after", 200)
    refresh_channel_window(slot, after, 2.0)  # the window does not match the file: rebuild
    assert [m["content"] for m in slot.messages] == [
        r["content"] for r in after
    ], "precondition: the refresh rebuilt the window from the new transcript"
    total = sum(len(json.dumps({"blob": r["blob"]})) for r in after)
    assert total == 802_400 < MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES, "precondition: under the budget"
    lost = [i for i, kept in enumerate(_kept_fields(slot)) if kept is None]
    assert not lost, (
        f"{len(lost)} of 200 rows lost their fields after a rebuild, with 802,400 bytes "
        f"held, under the {MAX_UNKNOWN_ROW_FIELDS_SLOT_BYTES}-byte budget"
    )
    slot._dirty = True
    _save_slot_to_history(state, slot)
    saved = _rows(tmp_path / "slack_3.3.jsonl")
    assert sum("blob" in r for r in saved) == 200, "the save dropped fields from disk"

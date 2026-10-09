"""A restored dashboard slot keeps a newer build's row fields through its flush.

The desktop updater allows downgrades. A transcript a newer build wrote can then be
restored by this build (``_rehydrate_slot_from_history``) and flushed
(``_save_slot_to_history``), and the flush re-serializes every row in the live window.
Each test saves a slot with the current code on a ``tmp_path`` home, edits the
transcript, restores the slot, flushes and reads the file back.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

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

"""The shutdown save writes the tabs that owe a write and leaves clean ones alone.

A restored tab nobody touched has its state on disk already. Rewriting it at
shutdown puts the same bytes back and spends the shutdown deadline that the tabs
with unsaved changes need.
"""

from __future__ import annotations

import json
import os

import pytest
from chat_test_helpers import _make_state
from open_slots_persistence_helpers import _seed_session

from kiro_crew.dashboard.chat_persistence import restore_open_slots, save_all_slots_to_history
from kiro_crew.dashboard.chat_utils import _history_key_for


def _restored(tmp_path, monkeypatch, keys):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    seed = _make_state(tmp_path / "sessions")
    for key in keys:
        _seed_session(seed, key)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": keys, "ts": 0.0}))
    first = _make_state(tmp_path / "sessions")
    assert restore_open_slots(first) == len(keys)
    # A real gateway's lines were written by a full save; give the seeds one.
    for key in keys:
        first.get_slot(key)._dirty = True
    save_all_slots_to_history(first)
    state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state) == len(keys)
    return state


def test_shutdown_rewrites_only_the_slots_that_owe_a_write(tmp_path, monkeypatch) -> None:
    state = _restored(tmp_path, monkeypatch, ["chat-1-clean", "chat-2-edited"])
    log = state.conversation_log
    clean = log._path(_history_key_for("chat-1-clean"))
    edited = log._path(_history_key_for("chat-2-edited"))
    clean_before = (clean.stat().st_ino, clean.read_bytes())

    state.get_slot("chat-2-edited").append("assistant", "a reply after restore", "msg msg-a")
    save_all_slots_to_history(state)

    assert (clean.stat().st_ino, clean.read_bytes()) == clean_before
    assert b"a reply after restore" in edited.read_bytes()


def test_shutdown_still_saves_a_metadata_edit(tmp_path, monkeypatch) -> None:
    state = _restored(tmp_path, monkeypatch, ["chat-3-renamed"])
    slot = state.get_slot("chat-3-renamed")
    path = state.conversation_log._path(_history_key_for("chat-3-renamed"))
    slot.title = "Renamed after restore"
    slot._dirty = True

    save_all_slots_to_history(state)

    head = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert head.get("title") == "Renamed after restore"
    assert os.path.exists(path)


def test_a_title_that_failed_to_save_is_owed_by_the_next_flush(tmp_path, monkeypatch) -> None:
    import asyncio

    from kiro_crew.dashboard.chat_title import _persist_title

    state = _restored(tmp_path, monkeypatch, ["chat-4-title"])
    slot = state.get_slot("chat-4-title")
    assert slot._dirty is False

    def refuse(*_a, **_kw):
        raise OSError("disk busy")

    monkeypatch.setattr(state.conversation_log, "update_metadata_if", refuse)
    slot.title = "Title the disk refused"

    assert asyncio.run(_persist_title(state, slot)) is False
    # Without the mark only a forced save would ever write it.
    assert slot._dirty is True


def _reboot(tmp_path, key):
    state = _make_state(tmp_path / "sessions")
    assert restore_open_slots(state) >= 1
    return state.get_slot(key)


def _pick_model(slot):
    slot.model = "claude-shutdown-test"


def _pick_effort(slot):
    slot.reasoning_effort = "high"


def _pin(slot):
    slot.pinned = True


@pytest.mark.parametrize(
    ("change", "field", "value"),
    [
        (_pick_model, "model", "claude-shutdown-test"),
        (_pick_effort, "reasoning_effort", "high"),
        (_pin, "pinned", True),
    ],
    ids=["model", "reasoning_effort", "pinned"],
)
def test_a_change_that_leaves_the_slot_clean_survives_a_restart(
    tmp_path, monkeypatch, change, field, value
) -> None:
    # These edits change only the slot: nothing marks it dirty, and the
    # periodic flush skips it. The shutdown save is what puts them on disk.
    state = _restored(tmp_path, monkeypatch, ["chat-5-clean-edit"])
    slot = state.get_slot("chat-5-clean-edit")
    assert slot._dirty is False
    change(slot)
    assert slot._dirty is False

    save_all_slots_to_history(state)

    assert getattr(_reboot(tmp_path, "chat-5-clean-edit"), field) == value


def test_a_dropped_note_is_retired_by_the_shutdown_save(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.chat_utils import slot_history_key
    from kiro_crew.dashboard.slot_buffers import serialize_deferred_notes

    state = _restored(tmp_path, monkeypatch, ["chat-6-note"])
    slot = state.get_slot("chat-6-note")
    key = slot_history_key(slot)
    note = {
        "id": "dropped-at-rebind",
        "content": "held, then dropped at the rebind seam",
        "cls": "reconcile-note",
        "context": None,
        "session": "app:another-session",
    }
    state.conversation_log.update_metadata(
        key, {"deferred_notes": serialize_deferred_notes([note])}
    )
    # The rebind seam records the drop; only a save retires the durable entry.
    slot._dropped_note_ids.add("dropped-at-rebind")
    assert slot._dirty is False

    save_all_slots_to_history(state)

    held = state.conversation_log._read_metadata(key).get("deferred_notes") or []
    assert all(entry.get("id") != "dropped-at-rebind" for entry in held)


def test_a_row_edit_while_the_save_waits_is_still_written(tmp_path, monkeypatch) -> None:
    from kiro_crew.dashboard.slot_persistence import write_guards

    state = _restored(tmp_path, monkeypatch, ["chat-7-race"])
    slot = state.get_slot("chat-7-race")
    path = state.conversation_log._path(_history_key_for("chat-7-race"))
    before = path.stat().st_ino
    assert slot._dirty is False
    real = write_guards.read_line_for_save

    def edit_then_read(*args, **kwargs):
        # An in-place edit (a guide card's status, say) lands after the
        # shutdown pass found the slot clean, while its save waits.
        slot._dirty = True
        return real(*args, **kwargs)

    monkeypatch.setattr(write_guards, "read_line_for_save", edit_then_read)
    save_all_slots_to_history(state)

    assert path.stat().st_ino != before

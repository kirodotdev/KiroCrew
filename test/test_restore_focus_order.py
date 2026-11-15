"""The open-tab restore builds the focused tab first, then the visible ones, then the rest."""

from __future__ import annotations

import asyncio
import json
import os

from chat_test_helpers import _make_state
from open_slots_persistence_helpers import _seed_session

from kiro_crew.dashboard import chat_persistence, slot_retention


def _seed(tmp_path, monkeypatch, names):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    for name in names:
        _seed_session(state, name)
    return state


def _restore_order(monkeypatch, state) -> list[str]:
    built: list[str] = []
    real = chat_persistence._apply_restored_open_slot

    def spy(st, key, **kwargs):
        built.append(key)
        return real(st, key, **kwargs)

    monkeypatch.setattr(chat_persistence, "_apply_restored_open_slot", spy)
    asyncio.run(chat_persistence.restore_open_slots_async(state))
    return built


def test_persist_records_focus_and_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    state = _make_state(tmp_path / "sessions")
    state.open_slots_restored = True
    state.get_or_create_slot("chat-1-a")
    state.get_or_create_slot("chat-2-b")
    state._persist_open_slots()
    first = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert first["focus"] == "" and first["visible"] == []

    slot_retention.note_foreground(state, 1, "chat-1-a")
    slot_retention.note_foreground(state, 2, "chat-2-b")
    slot_retention.note_foreground(state, 1, None)
    state._persist_open_slots()
    second = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert second["focus"] == "chat-2-b"
    assert second["visible"] == []


def test_restore_order_is_focus_then_visible_then_recency(tmp_path, monkeypatch):
    names = ["chat-1-a", "chat-2-b", "chat-3-c", "chat-4-d"]
    _seed(tmp_path, monkeypatch, names)
    (tmp_path / "open_slots.json").write_text(
        json.dumps({"keys": names, "focus": "chat-1-a", "visible": ["chat-2-b", 7]})
    )
    built = _restore_order(monkeypatch, _make_state(tmp_path / "sessions"))
    assert built[:2] == ["chat-1-a", "chat-2-b"]
    assert sorted(built) == sorted(names)


def test_a_file_without_focus_keeps_recency_order(tmp_path, monkeypatch):
    names = ["chat-1-a", "chat-2-b", "chat-3-c"]
    state = _seed(tmp_path, monkeypatch, names)
    for age, name in enumerate(names):
        path = state.conversation_log._path(chat_persistence.slot_transcript_key(name))

        os.utime(path, (1_000_000 + age, 1_000_000 + age))
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": names}))
    built = _restore_order(monkeypatch, _make_state(tmp_path / "sessions"))
    assert built == ["chat-3-c", "chat-2-b", "chat-1-a"]


def test_a_client_focusing_an_unbuilt_tab_pulls_it_forward(tmp_path, monkeypatch):
    names = ["chat-1-a", "chat-2-b", "chat-3-c", "chat-4-d"]
    _seed(tmp_path, monkeypatch, names)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": names, "focus": "chat-1-a"}))
    state = _make_state(tmp_path / "sessions")
    real = chat_persistence._apply_restored_open_slot
    built: list[str] = []

    def spy(st, key, **kwargs):
        built.append(key)
        if len(built) == 1:
            slot_retention.note_foreground(st, 9, "chat-4-d")
        return real(st, key, **kwargs)

    monkeypatch.setattr(chat_persistence, "_apply_restored_open_slot", spy)
    asyncio.run(chat_persistence.restore_open_slots_async(state))
    assert built[:2] == ["chat-1-a", "chat-4-d"]
    assert sorted(built) == sorted(names)


def test_a_flush_before_any_focus_report_keeps_the_saved_focus(tmp_path, monkeypatch):
    names = ["chat-1-a", "chat-2-b", "chat-3-c"]
    _seed(tmp_path, monkeypatch, names)
    saved = {"keys": names, "focus": "chat-1-a", "visible": ["chat-2-b"]}
    (tmp_path / "open_slots.json").write_text(json.dumps(saved))
    state = _make_state(tmp_path / "sessions")
    state.get_or_create_slot("chat-3-c")
    state._persist_open_slots()
    after = json.loads((tmp_path / "open_slots.json").read_text(encoding="utf-8"))
    assert after["focus"] == "chat-1-a" and after["visible"] == ["chat-2-b"]
    built = _restore_order(monkeypatch, _make_state(tmp_path / "sessions"))
    assert built[:2] == ["chat-1-a", "chat-2-b"]


def test_a_focus_reported_during_the_snapshot_read_is_built_first(tmp_path, monkeypatch):
    names = ["chat-1-a", "chat-2-b", "chat-3-c", "chat-4-d"]
    _seed(tmp_path, monkeypatch, names)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": names, "focus": "chat-1-a"}))
    state = _make_state(tmp_path / "sessions")
    real_read = chat_persistence._read_open_slots_snapshot

    def read_then_focus():
        out = real_read()
        # The report lands while the restore holds the flag but has published no owed keys.
        slot_retention.note_foreground(state, 9, "chat-4-d")
        return out

    monkeypatch.setattr(chat_persistence, "_read_open_slots_snapshot", read_then_focus)
    built = _restore_order(monkeypatch, state)
    assert built[0] == "chat-4-d"
    assert sorted(built) == sorted(names)


def test_the_latest_focus_during_a_restore_step_is_built_first(tmp_path, monkeypatch):
    names = ["chat-1-a", "chat-2-b", "chat-3-c", "chat-4-d"]
    _seed(tmp_path, monkeypatch, names)
    (tmp_path / "open_slots.json").write_text(json.dumps({"keys": names, "focus": "chat-1-a"}))
    state = _make_state(tmp_path / "sessions")
    real = chat_persistence._apply_restored_open_slot
    built: list[str] = []

    def spy(st, key, **kwargs):
        built.append(key)
        if len(built) == 1:
            slot_retention.note_foreground(st, 9, "chat-2-b")
            slot_retention.note_foreground(st, 9, "chat-4-d")
        return real(st, key, **kwargs)

    monkeypatch.setattr(chat_persistence, "_apply_restored_open_slot", spy)
    asyncio.run(chat_persistence.restore_open_slots_async(state))
    assert built[:3] == ["chat-1-a", "chat-4-d", "chat-2-b"]

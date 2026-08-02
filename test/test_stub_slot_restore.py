"""Tests for lazy stub slot restoration (issue #895).

Pins two properties:
1. Startup restore creates stub slots without reading messages (no full
   materialization for sidebar-only rows).
2. materialize_slot loads the full transcript on demand when a tab is activated.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_persistence import (
    materialize_slot,
    restore_open_slots,
    restore_recent_sessions,
)
from kiro_crew.dashboard.chat_utils import _history_key_for


def _seed_session(state, slot_name: str, n_messages: int = 3) -> None:
    """Seed a session with metadata and messages."""
    log = state.conversation_log
    assert log is not None
    history_key = _history_key_for(slot_name)
    log.update_metadata(history_key, {"title": f"Title {slot_name}", "agent": "kirocrew"})
    for i in range(n_messages):
        log.append(history_key, "user" if i % 2 == 0 else "assistant", f"msg-{i}")


class TestStubSlotRestore:
    """Restore creates stubs that do not read messages."""

    def test_restore_open_slots_creates_stubs(self, tmp_path, monkeypatch):
        """restore_open_slots must NOT call read_messages_chained."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        _seed_session(state, "chat-1-test", n_messages=10)

        snapshot_path = tmp_path / "open_slots.json"
        snapshot_path.write_text(json.dumps({"keys": ["chat-1-test"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        chained_calls: list[str] = []
        real_chained = state2.conversation_log.read_messages_chained

        def _spy_chained(key, *a, **kw):
            chained_calls.append(key)
            return real_chained(key, *a, **kw)

        with patch.object(state2.conversation_log, "read_messages_chained", _spy_chained):
            restored = restore_open_slots(state2)

        assert restored == 1
        slot = state2._slots["chat-1-test"]
        assert slot._stub is True
        assert len(slot.messages) == 0
        # The critical assertion: no transcript was read during restore.
        assert chained_calls == [], (
            f"restore_open_slots called read_messages_chained {len(chained_calls)} "
            "time(s) - stubs must not read transcripts"
        )

    def test_restore_recent_sessions_creates_stubs(self, tmp_path, monkeypatch):
        """restore_recent_sessions must NOT call read_messages_chained."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path / "sessions")
        _seed_session(state, "chat-1-recent", n_messages=5)
        # Touch the file to make it recent
        (tmp_path / "sessions" / "dashboard_chat-1-recent.jsonl").touch()

        state2 = _make_state(tmp_path / "sessions")
        chained_calls: list[str] = []
        real_chained = state2.conversation_log.read_messages_chained

        def _spy_chained(key, *a, **kw):
            chained_calls.append(key)
            return real_chained(key, *a, **kw)

        with patch.object(state2.conversation_log, "read_messages_chained", _spy_chained):
            restored = restore_recent_sessions(state2, window_minutes=60)

        assert restored == 1
        slot = list(state2._slots.values())[0]
        assert slot._stub is True
        assert len(slot.messages) == 0
        assert chained_calls == [], (
            "restore_recent_sessions called read_messages_chained - "
            "stubs must not read transcripts"
        )

    def test_stub_to_dict_returns_sidebar_fields(self, tmp_path, monkeypatch):
        """to_dict on a stub returns metadata without scanning messages."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        _seed_session(state, "chat-1-stub", n_messages=5)

        snapshot_path = tmp_path / "open_slots.json"
        snapshot_path.write_text(json.dumps({"keys": ["chat-1-stub"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        restore_open_slots(state2)
        slot = state2._slots["chat-1-stub"]

        d = slot.to_dict()
        assert d["title"] == "Title chat-1-stub"
        assert d["agent"] == "kirocrew"
        assert d["running"] is False
        assert d["pending_approval"] is False
        assert d["folder_id"] == ""

    def test_stub_restricted_keys_seeded(self, tmp_path, monkeypatch):
        """Stubs seed _restricted_keys for non-persistent memory_mode."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        log = state.conversation_log
        assert log is not None
        history_key = _history_key_for("chat-1-incog")
        log.update_metadata(history_key, {"title": "Incog", "memory_mode": "incognito"})
        log.append(history_key, "user", "secret stuff")

        snapshot_path = tmp_path / "open_slots.json"
        snapshot_path.write_text(json.dumps({"keys": ["chat-1-incog"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        restore_open_slots(state2)
        # Privacy-critical: restricted_keys must be seeded from metadata even
        # for stubs, so consolidation is blocked for non-persistent sessions.
        assert "dashboard:chat-1-incog" in state2._restricted_keys


class TestMaterializeSlot:
    """materialize_slot loads the full transcript on demand."""

    def test_materialize_loads_messages(self, tmp_path, monkeypatch):
        """After materialize, slot has full message history."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        _seed_session(state, "chat-1-mat", n_messages=6)

        snapshot_path = tmp_path / "open_slots.json"
        snapshot_path.write_text(json.dumps({"keys": ["chat-1-mat"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        restore_open_slots(state2)
        slot = state2._slots["chat-1-mat"]
        assert slot._stub is True
        assert len(slot.messages) == 0

        materialize_slot(state2, slot)
        assert slot._stub is False
        assert len(slot.messages) == 6
        assert slot._resumed_count == 6
        assert slot._disk_window_len == 6

    def test_materialize_noop_on_non_stub(self, tmp_path, monkeypatch):
        """materialize_slot is a no-op on an already-materialized slot."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        _seed_session(state, "chat-1-full", n_messages=3)

        snapshot_path = tmp_path / "open_slots.json"
        snapshot_path.write_text(json.dumps({"keys": ["chat-1-full"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        restore_open_slots(state2)
        slot = state2._slots["chat-1-full"]
        materialize_slot(state2, slot)
        count_after_first = len(slot.messages)

        # Second call is a no-op.
        materialize_slot(state2, slot)
        assert len(slot.messages) == count_after_first

    def test_materialize_redacts_content(self, tmp_path, monkeypatch):
        """Content redaction happens during materialize, not at restore."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        state = _make_state(tmp_path / "sessions")
        log = state.conversation_log
        assert log is not None
        history_key = _history_key_for("chat-1-redact")
        log.update_metadata(history_key, {"title": "Redact Test"})
        log.append(history_key, "assistant", "key: AKIAIOSFODNN7EXAMPLE")

        snapshot_path = tmp_path / "open_slots.json"
        snapshot_path.write_text(json.dumps({"keys": ["chat-1-redact"], "ts": 0.0}))

        state2 = _make_state(tmp_path / "sessions")
        restore_open_slots(state2)
        slot = state2._slots["chat-1-redact"]
        assert slot._stub is True

        materialize_slot(state2, slot)
        # Credential must be redacted in loaded content.
        assert "AKIAIOSFODNN7EXAMPLE" not in slot.messages[0]["content"]
        assert "[REDACTED" in slot.messages[0]["content"]

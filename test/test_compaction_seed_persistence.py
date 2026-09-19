"""The two primitives the ``shake`` seed writer persists through.

``DashboardState.save_slot_strict`` is the periodic flush's opposite: every way
the write does not happen is an exception, because the caller is about to let
the native conversation be dropped on the strength of the row being on disk.
``_ChatSlot.withdraw`` is what that caller does when the write did not happen:
the just-appended row leaves the window as if it had never been appended.
"""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state


def _slot_with_history(tmp_path, monkeypatch, name: str = "strict"):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "world", "msg msg-a")
    return state, slot


class TestSaveSlotStrict:
    def test_writes_the_window_and_clears_dirty(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        assert slot._dirty is True
        state.save_slot_strict(slot)
        assert slot._dirty is False
        on_disk = state.conversation_log.read_messages_chained(f"dashboard:{slot.key}")
        assert [m["content"] for m in on_disk] == ["hello", "world"]

    def test_a_guarded_metadata_write_in_flight_is_a_failure_not_a_skip(
        self, tmp_path, monkeypatch
    ):
        # The periodic flush steps aside silently here; a caller that needs the
        # row on disk must hear that it is not.
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        slot._metadata_persist_inflight = 1
        with pytest.raises(RuntimeError, match="metadata write is in flight"):
            state.save_slot_strict(slot)
        assert slot._dirty is True

    def test_a_refused_save_raises(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.chat._save_slot_to_history", lambda *a, **k: False)
        with pytest.raises(RuntimeError, match="refused the save"):
            state.save_slot_strict(slot)
        assert slot._dirty is True

    def test_a_raising_writer_propagates(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.dashboard.chat._save_slot_to_history", _boom)
        with pytest.raises(OSError, match="disk full"):
            state.save_slot_strict(slot)
        assert slot._dirty is True

    def test_a_dirty_mark_set_during_the_write_survives(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        def _save_then_redirty(owner, target, *a, **k):
            ok = _save_slot_to_history(owner, target, *a, **k)
            target.append("user", "arrived mid-write", "msg msg-u")
            return ok

        monkeypatch.setattr("kiro_crew.dashboard.chat._save_slot_to_history", _save_then_redirty)
        state.save_slot_strict(slot)
        assert slot._dirty is True, "the newer row is still owed to disk"


class TestWithdraw:
    def test_removes_the_row_by_identity_and_walks_back_the_counters(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        before = (len(slot.messages), slot.total_messages)
        row = slot.append("compaction", "digest", "", meta={"kind": "x"}, broadcast=False)
        twin = dict(row)  # equal, not identical: must not be the one removed
        slot.messages.append(twin)
        assert slot.withdraw(row) is True
        assert row not in [m for m in slot.messages if m is row]
        assert any(m is twin for m in slot.messages), "an equal row is left alone"
        assert (len(slot.messages) - 1, slot.total_messages) == before
        assert all(m is not row for m in slot._pending)
        assert slot._dirty is True

    def test_an_unknown_row_is_reported_not_removed(self, tmp_path, monkeypatch):
        state, slot = _slot_with_history(tmp_path, monkeypatch)
        n = len(slot.messages)
        assert slot.withdraw({"role": "user", "content": "hello"}) is False
        assert len(slot.messages) == n

"""Tests for chat persistence on empty message windows (Issue #4501).

Closing an empty session must persist the `closed` and `closed_at` metadata flags
to disk so the close survives a restart. Plain saves on empty slots must remain
cheap no-ops.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history, slot_history_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog


class TestEmptySlotPersistence:
    def test_closing_empty_slot_persists_closed_metadata(self):
        """Closing a session with no messages writes closed=True and closed_at."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            clog = ConversationLog(Path(tmp_dir))
            state = DashboardState({}, [], [], 0.0, conversation_log=clog)
            slot = _ChatSlot(key="empty-close-1")
            hkey = slot_history_key(slot)

            _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

            meta = clog.get_metadata(hkey)
            assert meta.get("closed") is True
            assert meta.get("closed_at") == 12345.67
            assert clog._path(hkey).exists()

    def test_plain_save_on_empty_slot_is_noop(self):
        """A regular save on an empty slot must not write a file to disk."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            clog = ConversationLog(Path(tmp_dir))
            state = DashboardState({}, [], [], 0.0, conversation_log=clog)
            slot = _ChatSlot(key="empty-plain-1")
            hkey = slot_history_key(slot)

            _save_slot_to_history(state, slot)

            assert not clog._path(hkey).exists()
            assert clog.get_metadata(hkey) == {}

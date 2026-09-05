"""Empty-message-window saves in ``_save_slot_to_history``.

A slot with no messages still owns durable state in its metadata line, so the
empty window is not automatically a no-op. What each caller gets:

- a plain periodic save writes nothing (the cheap path this guard exists for);
- ``force`` and ``rewrite`` write, because the caller attached state (a folder
  move, a tag) or owns the file's contents outright;
- ``closed`` writes onto an EXISTING record, so a close survives a restart —
  but never mints one, or every discarded scratch tab would leave a phantom
  session behind.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.dashboard.chat_persistence import (
    _save_slot_to_history,
    restore_recent_sessions,
    slot_history_key,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.messaging.link import is_channel_session_key


def _state(tmp_path: Path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _birth_write(clog: ConversationLog, history_key: str) -> Path:
    """A metadata-only session file — what a message-less slot has on disk.

    Every path that stamps metadata on a slot before its first message leaves
    exactly this: ``update_metadata`` upserts the line (an agent switch, a
    programmatic ``session_control`` create), so the file exists with no
    message rows. This is the shape the close has to be able to stamp.
    """
    clog.update_metadata(history_key, {"agent": "kirocrew"})
    path = clog._path(history_key)
    assert path.exists(), "setup did not produce a metadata-only session file"
    assert [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln][1:] == []
    return path


class TestClosingAnEmptySlot:
    def test_the_close_reaches_disk(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-close")
        hkey = slot_history_key(slot)
        _birth_write(clog, hkey)

        _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        meta = clog.get_metadata(hkey)
        assert meta.get("closed") is True
        assert meta.get("closed_at") == 12345.67

    def test_the_closed_tab_stays_closed_across_a_restart(self, tmp_path: Path) -> None:
        """The user-visible defect: the tab came back on the next start."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-close-restart")
        hkey = slot_history_key(slot)
        _birth_write(clog, hkey)

        _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        # Restart: nothing in memory, the sidebar is rebuilt from disk alone.
        state._slots.clear()
        restore_recent_sessions(state, window_minutes=9999)
        assert "empty-close-restart" not in state._slots

    def test_a_delete_landing_before_the_lock_is_not_undone(self, tmp_path: Path) -> None:
        """The record can vanish between the decision and the write.

        A permanent delete unlinks the file while holding the same per-session
        lock this save takes, and leaves no tombstone. Deciding on an existence
        read taken BEFORE the lock would let the close recreate a session the
        user permanently deleted, with the deletion already reported as done.
        """
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-close-raced")
        hkey = slot_history_key(slot)
        path = _birth_write(clog, hkey)
        real_locked = clog._locked

        @contextlib.contextmanager
        def _delete_wins_the_lock(key: str):
            with real_locked(key):
                path.unlink()  # the delete held this lock just before us
                yield

        with patch.object(clog, "_locked", _delete_wins_the_lock):
            _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        assert not path.exists(), "the close recreated a permanently deleted session"

    def test_a_never_persisted_slot_is_not_given_a_record_to_close(self, tmp_path: Path) -> None:
        """A discarded scratch tab must not leave a phantom session behind."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-scratch")
        hkey = slot_history_key(slot)

        _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        assert not clog._path(hkey).exists()
        assert clog.get_metadata(hkey) == {}

    def test_a_channel_slot_close_still_stamps_its_own_record(self, tmp_path: Path) -> None:
        """The reconciler's ``closed_at`` still lands — on the file it reads."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="channel-with-file")
        slot.linked_session_key = "slack:1712793600.123456"
        hkey = slot_history_key(slot)
        assert is_channel_session_key(hkey), "setup did not produce a channel key"
        _birth_write(clog, hkey)

        _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        meta = clog.get_metadata(hkey)
        assert meta.get("closed") is True
        assert meta.get("closed_at") == 12345.67

    def test_not_even_a_channel_slot_is_exempt(self, tmp_path: Path) -> None:
        """Nothing reads a ``closed_at`` for a session with no file.

        ``eligible_channel_sessions`` evaluates ``_close_stands`` only over a
        ``list_sessions()`` result, and a session with no file is not in that
        listing — so minting one buys no reconciler decision and reopens the
        resurrection window instead (delete a live channel conversation, then
        close its tab).
        """
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="channel-no-file")
        slot.linked_session_key = "slack:1712793600.999999"
        hkey = slot_history_key(slot)
        assert is_channel_session_key(hkey), "setup did not produce a channel key"

        _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        assert not clog._path(hkey).exists()
        assert clog.get_metadata(hkey) == {}

    def test_a_cron_linked_slot_is_not_exempt(self, tmp_path: Path) -> None:
        """A link of any kind is not a licence to mint.

        ``cron_inject`` gives the tab it opens a ``cron:<job>`` link and appends
        the result only ``if result_text``, so a run that produced nothing leaves
        a linked slot with an empty window and no transcript.
        """
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="cron-emptyjob")
        slot.linked_session_key = "cron:emptyjob"
        hkey = slot_history_key(slot)
        assert not is_channel_session_key(hkey), "setup produced a channel key"

        _save_slot_to_history(state, slot, closed=True, closed_at=12345.67)

        assert not clog._path(hkey).exists()
        assert clog.get_metadata(hkey) == {}


class TestOtherCallersOnAnEmptyWindow:
    def test_a_plain_save_still_writes_nothing(self, tmp_path: Path) -> None:
        """The cheap path the guard exists for, preserved."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-plain")
        hkey = slot_history_key(slot)

        _save_slot_to_history(state, slot)

        assert not clog._path(hkey).exists()
        assert clog.get_metadata(hkey) == {}

    def test_a_forced_save_persists_the_state_the_caller_attached(self, tmp_path: Path) -> None:
        """``force=True`` on an existing record updates the caller-attached state."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-force")
        slot.folder_id = "fld-1"
        hkey = slot_history_key(slot)
        _birth_write(clog, hkey)

        _save_slot_to_history(state, slot, force=True)

        assert clog.get_metadata(hkey).get("folder_id") == "fld-1"

    def test_a_forced_save_on_a_plain_scratch_tab_mints_no_record(self, tmp_path: Path) -> None:
        """A forced save on a never-persisted tab leaves no phantom session."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-force-scratch")
        slot.folder_id = "fld-1"
        hkey = slot_history_key(slot)

        _save_slot_to_history(state, slot, force=True)

        assert not clog._path(hkey).exists()
        assert clog.get_metadata(hkey) == {}

    def test_a_rewrite_persists_its_snapshot(self, tmp_path: Path) -> None:
        """An explicit empty snapshot owns the file: it must not be skipped."""
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-rewrite")
        hkey = slot_history_key(slot)

        _save_slot_to_history(state, slot, messages=[])

        path = clog._path(hkey)
        assert path.exists()
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        assert json.loads(lines[0])["_type"] == "metadata"
        assert lines[1:] == [], "an empty snapshot must not fabricate message rows"

    def test_no_caller_fabricates_message_rows(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        clog = state.conversation_log
        slot = _ChatSlot(key="empty-rows")
        hkey = slot_history_key(slot)
        _birth_write(clog, hkey)

        _save_slot_to_history(state, slot, closed=True, closed_at=1.0)

        lines = [ln for ln in clog._path(hkey).read_text(encoding="utf-8").splitlines() if ln]
        assert lines[1:] == []

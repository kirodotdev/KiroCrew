"""A cleared project must not be resurrected by the metadata merge.

A forced save of a MESSAGE-LESS slot has no transcript window to write, so it reaches disk
through ``update_metadata_if`` -- an UPSERT, which can add or overwrite a key but cannot
delete one. A ``project`` written while the slot was scoped therefore outlives the clear
that replaced it unless the clear writes an explicit empty value over it, and the next
restore reads the stale directory back as though the user had never cleared it.

That is the newborn path: ``session_create`` persists ``folder_id`` at birth, so an empty
tab already has a metadata line, and a project set and then cleared before the first
message is persisted only by this merge. ``project`` accordingly joins the clearable family
the merge already enumerates (folder / tags / pin / title / mode / memory_store) instead of
being written only when non-empty.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

KEY = "dashboard:s1"


def _make_state(tmp_path) -> DashboardState:
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


def _newborn_with_stored_project(state, tmp_path):
    """A message-less slot whose metadata line already names a project directory."""
    scoped = str(tmp_path / "scoped")
    slot = state.get_or_create_slot("s1")
    state.conversation_log.update_metadata(KEY, {"project": scoped, "folder_id": "f1"})
    assert not slot.messages, "precondition: a window would route the save past the merge"
    stored = state.conversation_log._read_metadata(KEY).get("project")
    assert stored == scoped, (
        "precondition: no stored directory, so a later clear would have nothing to overwrite; "
        f"project={stored!r}"
    )
    return slot


class TestClearedProjectIsPersistedAsCleared:
    def test_clearing_a_project_overwrites_the_stored_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _newborn_with_stored_project(state, tmp_path)

        slot.project = ""
        _save_slot_to_history(state, slot, force=True)

        after = state.conversation_log._read_metadata(KEY).get("project")
        assert after == "", (
            "the clear left the previous directory on disk, so the next restore rebinds a "
            f"project the user removed; project={after!r}"
        )

    def test_a_live_project_survives_the_same_merge(self, tmp_path, monkeypatch):
        """The unconditional write must not blank a project that is still set."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = _newborn_with_stored_project(state, tmp_path)
        scoped = str(tmp_path / "scoped")

        slot.project = scoped
        _save_slot_to_history(state, slot, force=True)

        after = state.conversation_log._read_metadata(KEY).get("project")
        assert after == scoped, f"the merge dropped a live project; project={after!r}"

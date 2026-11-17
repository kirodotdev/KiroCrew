"""A persisted ``linked_session_key`` is adopted on restore only when it names its own file.

The metadata line is agent-writable and the link decides where the slot's turns
are routed, so a line naming another transcript must leave the slot unbound.
Both startup restore paths read the line, and each case runs through both.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.chat_persistence import (
    _rehydrate_slot_from_history,
    _save_slot_to_history,
    restore_recent_sessions,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

FOREIGN_KEY = "slack:C999:1700000000.999999"


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.channel_key_for_stem = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _persist_with_link(state, link: str, name: str = "s1") -> None:
    slot = state.get_or_create_slot(name)
    slot.append("user", "a turn that belongs to this transcript")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    state.conversation_log.update_metadata(f"dashboard:{name}", {"linked_session_key": link})
    del state._slots[name]


def _restore_via_open_tab(state, name: str = "s1"):
    return _rehydrate_slot_from_history(state, name)


def _restore_via_recent(state, name: str = "s1"):
    assert restore_recent_sessions(state, window_minutes=0) >= 1
    return state._slots[name]


RESTORE_PATHS = [
    pytest.param(_restore_via_open_tab, id="open-tab"),
    pytest.param(_restore_via_recent, id="recent"),
]


@pytest.mark.parametrize("restore", RESTORE_PATHS)
def test_a_link_naming_another_transcript_leaves_the_slot_unbound(restore, tmp_path) -> None:
    state = _make_state(tmp_path)
    _persist_with_link(state, FOREIGN_KEY)

    restored = restore(state)

    assert restored is not None
    assert restored.linked_session_key == ""


@pytest.mark.parametrize("restore", RESTORE_PATHS)
def test_a_link_naming_its_own_transcript_is_still_adopted(restore, tmp_path) -> None:
    state = _make_state(tmp_path)
    _persist_with_link(state, "dashboard:s1")

    restored = restore(state)

    assert restored is not None
    assert restored.linked_session_key == "dashboard:s1"


def test_a_refused_link_is_recorded_as_a_denied_governance_decision(tmp_path, monkeypatch) -> None:
    recorded: list[dict] = []
    sel = MagicMock(log_governance_decision=MagicMock(side_effect=lambda **k: recorded.append(k)))
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.sel", lambda: sel)
    state = _make_state(tmp_path)
    _persist_with_link(state, FOREIGN_KEY)

    _restore_via_open_tab(state)

    denials = [r for r in recorded if r.get("tool_name") == "chat:adopt_persisted_binding"]
    assert [(r["outcome"], r["item"]) for r in denials] == [("denied", FOREIGN_KEY)]


def test_an_unwritable_audit_still_refuses_the_link(tmp_path, monkeypatch) -> None:
    sel = MagicMock(log_governance_decision=MagicMock(side_effect=OSError("read-only")))
    monkeypatch.setattr("kiro_crew.dashboard.chat_persistence.sel", lambda: sel)
    state = _make_state(tmp_path)
    _persist_with_link(state, FOREIGN_KEY)

    restored = _restore_via_open_tab(state)

    assert restored is not None
    assert restored.linked_session_key == ""


@pytest.mark.parametrize(
    ("link", "history_key", "adopted"),
    [
        ("slack:1700000000.123456", "slack:1700000000.123456", True),
        ("slack:1700000000.123456", "slack_1700000000.123456", True),
        ("slack:1700000000.123456", "1700000000.123456", True),
        ("cron:abcd1234", "cron:abcd1234", True),
        (FOREIGN_KEY, "dashboard:s1", False),
        ("slack:1700000000.123456", "slack:1700000000.999999", False),
        ("dashboard:s2", "dashboard:s1", False),
        ("", "dashboard:s1", False),
    ],
)
def test_own_linked_session_key_accepts_only_the_files_own_spellings(
    link, history_key, adopted
) -> None:
    from kiro_crew.dashboard.slot_persistence.metadata_codec import own_linked_session_key

    expected = link if adopted else ""
    assert own_linked_session_key({"linked_session_key": link}, history_key) == expected


@pytest.mark.asyncio
async def test_the_async_restore_reads_app_claims_for_the_slots_own_session(
    tmp_path, monkeypatch
) -> None:
    from kiro_crew.dashboard import chat_persistence

    read_for: list[str] = []
    monkeypatch.setattr(
        chat_persistence, "_read_mcp_app_claims", lambda key: read_for.append(key) or []
    )
    state = _make_state(tmp_path)
    _persist_with_link(state, FOREIGN_KEY)

    restored = await chat_persistence.rehydrate_slot_from_history_async(state, "s1")

    assert restored is not None
    assert read_for == ["dashboard:s1"]

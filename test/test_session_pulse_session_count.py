"""The survey "new user" counter increments only on genuine user chats.

``DashboardState.get_or_create_slot`` is the sole place a brand-new slot is
minted. This asserts the durable session-pulse counter goes up by one only when
the new slot's origin is ``SlotOrigin.USER`` (a person starting a dashboard
chat), and stays put for cron / app / system / untagged origins and for
get_or_create calls that return an EXISTING slot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import session_pulse_counter as spc
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.history import ConversationLog


@pytest.fixture(autouse=True)
def _isolated_counter(tmp_path, monkeypatch: pytest.MonkeyPatch):
    counter_dir = tmp_path / "home"
    counter_dir.mkdir()
    # Both the counter module and state resolve config_dir(); point both at a
    # throwaway dir so the counter file and any open-slots snapshot stay local.
    monkeypatch.setattr(spc, "config_dir", lambda: counter_dir)
    import kiro_crew.dashboard.state as state_mod

    monkeypatch.setattr(state_mod, "config_dir", lambda: counter_dir, raising=False)
    return counter_dir


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
        conversation_log=ConversationLog(base_dir=tmp_path / "log"),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def test_user_origin_new_chat_increments(tmp_path) -> None:
    state = _make_state(tmp_path)
    assert spc.get_user_session_count() == 0
    state.get_or_create_slot(origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 1
    state.get_or_create_slot(origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin": SlotOrigin.CRON},
        {"origin": SlotOrigin.SYSTEM},
        {"app": "some-app"},  # resolves to APP origin
        {},  # untagged (origin="")
    ],
)
def test_non_user_origins_do_not_increment(tmp_path, kwargs) -> None:
    state = _make_state(tmp_path)
    state.get_or_create_slot(**kwargs)
    assert spc.get_user_session_count() == 0


def test_restore_shape_named_user_slot_does_not_increment(tmp_path) -> None:
    # Restore/rehydrate calls get_or_create_slot with the persisted key as
    # `name` and origin=USER. That must NOT count -- otherwise every gateway
    # restart re-counts each restored user session. Regression for the GPT
    # blocking finding "restoring sessions corrupts the durable session count".
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name="chat-9-1786589233", origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 0
    # Returning the now-existing slot also does not count.
    again = state.get_or_create_slot(name="chat-9-1786589233", origin=SlotOrigin.USER)
    assert again is slot
    assert spc.get_user_session_count() == 0

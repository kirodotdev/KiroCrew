"""What ``slots_diff`` buys, pinned: bytes per routine flush and work per pass.

400 slots, one owner tab that applies deltas and one that does not, 25 routine
changes (one appended message each). Every delta carries one row, all of them
together are under 1% of the full lists the legacy tab was sent, and each flush
re-projects the owner view once, so the diff tab costs no extra serialization.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.websocket_hub import SLOT_DIFF_WS_FLAG, SLOT_PATCH_WS_FLAG

_SLOTS = 400
_ROUNDS = 25


class _WS(dict):
    """A socket double holding what it was sent, as raw text."""

    def __init__(self, *, diff: bool) -> None:
        super().__init__(
            {"_is_dashboard_user": True, SLOT_PATCH_WS_FLAG: diff, SLOT_DIFF_WS_FLAG: diff}
        )
        self.closed = False
        self.sent: list[str] = []

    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other


@pytest.fixture(name="state")
def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> DashboardState:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    sessions = MagicMock(count=0)
    sessions.get_provider.return_value = None
    state = DashboardState(
        sessions=sessions, crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    monkeypatch.setattr(state, "is_yolo_active", lambda: False)
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, msg: ws.sent.append(msg))
    return state


def test_routine_flushes_send_small_deltas_and_reproject_once(
    state: DashboardState, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the flushes this test makes: a mutation's own push would broadcast too.
    monkeypatch.setattr(state, "push_slots_update", lambda **_kw: None)
    for i in range(_SLOTS):
        state.get_or_create_slot(f"chat-{i:03d}").append("user", f"question {i}", "msg")
    diff_ws, legacy_ws = _WS(diff=True), _WS(diff=False)
    for ws in (diff_ws, legacy_ws):
        state._ws_clients.append(ws)  # type: ignore[arg-type]
        state._owner_ws_clients.add(ws)  # type: ignore[arg-type]
    state._do_slots_broadcast()  # the diff tab's first, numbered, full list
    diff_ws.sent.clear()
    legacy_ws.sent.clear()

    reprojections: list[int] = []
    real = state._reproject_slots

    def counted(*args: Any, **kwargs: Any) -> list[dict]:
        reprojections.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(state, "_reproject_slots", counted)
    for round_no in range(_ROUNDS):
        slot = state._slots[f"chat-{round_no * 7 % _SLOTS:03d}"]
        slot.append("assistant", f"answer {round_no}", "msg")
        before = len(reprojections)
        state._do_slots_broadcast()
        assert len(reprojections) - before == 1, round_no

    diffs = [msg for msg in diff_ws.sent if msg.startswith('{"type": "slots_diff"')]
    assert len(diffs) == _ROUNDS
    assert not [msg for msg in diff_ws.sent if msg.startswith('{"type": "slots", ')]
    # One whole changed row (about 2.2 KB here) plus the frame around it.
    assert all(len(msg) < 4_000 for msg in diffs), max(map(len, diffs))
    legacy = [msg for msg in legacy_ws.sent if msg.startswith('{"type": "slots", ')]
    legacy_bytes = sum(len(msg) for msg in legacy)
    assert len(legacy) == _ROUNDS
    assert sum(len(msg) for msg in diffs) < legacy_bytes / 100

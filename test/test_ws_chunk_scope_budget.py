"""Frame and encode budget of ``chunk_scope.1`` stream scoping at dashboard scale.

Sixty capable tabs and one tab without the capability; twenty slots each stream a
turn. The counts are exact, so a change that sends a stream frame to a tab that
does not watch its slot, or encodes a coarse frame once per socket instead of once
per fan-out, fails here rather than on a load bed.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import ws_chunk_scope as scope
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

SOCKETS = 60
SLOTS = 20
THINKING = 3
CHUNKS = 10
#: What one turn below is to a tab that does not watch its slot: the thinking
#: phase, the held reasoning, the call, its return, the streaming phase and the
#: turn end.
UNWATCHED_PER_TURN = ["chat_phase", "chat_thinking", "tool_status", "tool_status", "chat_phase"]


class _WS(dict):
    def __init__(self) -> None:
        super().__init__(_is_dashboard_user=True)
        self.closed = False
        self.sent: list[str] = []

    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    def types(self) -> list[str]:
        return [json.loads(m)["type"] for m in self.sent]


def _turn(state: DashboardState, slot: str) -> None:
    for _ in range(THINKING):
        state.broadcast_ws("chat_thinking", {"slot": slot, "content": "r"})
    state.broadcast_ws("tool_call", {"slot": slot, "tool": "Shell", "tool_call_id": f"{slot}-t"})
    state.broadcast_ws("tool_result", {"slot": slot, "tool_call_id": f"{slot}-t", "output": "x"})
    for seq in range(CHUNKS):
        state.broadcast_ws("chat_chunk", {"slot": slot, "content": "ab", "seq": seq, "gen": "g"})
    state.broadcast_ws("chat_done", {"slot": slot})


@pytest.mark.asyncio
async def test_sixty_tabs_twenty_slots_frame_and_encode_budget(tmp_path, monkeypatch):
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "convo"),
    )
    encodes = 0
    real_dumps = scope.json.dumps

    def counting_dumps(*args, **kwargs):
        nonlocal encodes
        encodes += 1
        return real_dumps(*args, **kwargs)

    # Only this module's encodes: the hub's own encode of each frame for the tab
    # without the capability is the same as before scoping.
    monkeypatch.setattr(scope, "json", SimpleNamespace(dumps=counting_dumps, loads=json.loads))
    slots = [f"s{i}" for i in range(SLOTS)]
    tabs = []
    for i in range(SOCKETS):
        ws = _WS()
        state.register_ws(ws, owner=True)
        state.set_chunk_scope_capable(ws, True)
        # Each tab shows one slot; the first twenty cover every slot once.
        state.set_chunk_watch(ws, [slots[i % SLOTS]])
        tabs.append(ws)
    legacy = _WS()
    state.register_ws(legacy, owner=True)
    encodes = 0
    for slot in slots:
        _turn(state, slot)
    pending = [t for t in list(state._background_tasks) if not t.done()]
    await asyncio.gather(*pending)

    watched_turn = ["chat_thinking"] * THINKING + ["tool_call", "tool_result"]
    watched_turn += ["chat_chunk"] * CHUNKS + ["chat_done"]
    for i, ws in enumerate(tabs):
        mine = slots[i % SLOTS]
        expected: list[str] = []
        for slot in slots:
            expected += watched_turn if slot == mine else UNWATCHED_PER_TURN + ["chat_done"]
        assert ws.types() == expected
    assert legacy.types() == watched_turn * SLOTS
    # Per slot: the thinking phase, the held reasoning, the call, the return and
    # the streaming phase, each once however many tabs receive it.
    assert encodes == 5 * SLOTS

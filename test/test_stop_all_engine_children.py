"""Stop all on a chat whose running KAS turn holds engine-run sub-agents.

The engine runs its sub-agents as tool calls of the parent's turn
(``invoke_sub_agent``), so Kiro Crew holds no run of its own for them: they are
``native:<id>`` cards on the slot, and the KAS engine offers no request that stops
one of them, or all of a turn's, while the turn goes on. ``POST /api/spawn/stop-all``
reaches Kiro Crew's own runs (``cancel_for_parent``) and, only while such cards are
open, stops the parent's turn, which is what stops them.

Driven through the real route, a real ``DashboardState`` and ``_ChatSlot`` and the
real ``stop_slot_turn``; only the session store's ``stop_turn`` is a double.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from handlers_messaging_coverage_helpers import _payload, _Req, _run

import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.dashboard.state import _ChatSlot

_TEST_DIR = str(Path(__file__).resolve().parent)

SLOT = "chat-1"
#: The session key the route resolves the slot to (``effective_session_key``).
SESSION = f"dashboard:{SLOT}"
CHILD = "sub-1"


def _state(tmp_path: Path, crew_running: int) -> Any:
    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="stopped")
    state.subagents = MagicMock()
    state.subagents.cancel_for_parent = AsyncMock(return_value=(crew_running, 0))
    return state


def _slot(state: Any, *, backend: str, record: dict[str, Any] | None) -> _ChatSlot:
    """A slot whose turn is running, holding one native card *record* or none."""
    slot = _ChatSlot(SLOT)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._acp_client = SimpleNamespace(
        backend=backend,
        cancel_session=AsyncMock(),
        cancel_native_child=AsyncMock(return_value=False),
    )
    slot._native_subagent_tracker = {} if record is None else {CHILD: record}
    state._slots[SLOT] = slot
    return slot


def _open() -> dict[str, Any]:
    return {"id": f"native:{CHILD}", "done": False, "task": "t", "started": 0.0}


def _stop_all(state: Any) -> Any:
    return _run(mod.api_spawn_stop_all, _Req(state, {"slot": SLOT}))


def test_stop_all_reaches_the_running_turns_engine_children(tmp_path: Path) -> None:
    state = _state(tmp_path, crew_running=0)
    slot = _slot(state, backend=ACP_BACKEND_KAS, record=_open())

    resp = _stop_all(state)

    assert resp.status == 200
    state.subagents.cancel_for_parent.assert_awaited_once_with(SESSION)
    # The engine child stops only with the parent's turn: the turn's own stop ran.
    assert (
        state.sessions.stop_turn.await_count == 1
    ), "Stop all left the turn's engine sub-agents running"
    assert state.sessions.stop_turn.await_args.args[0] == SESSION
    assert _payload(resp) == {"ok": True, "stopped": 0, "running": 0, "queued": 0, "engine": 1}
    assert slot.task.cancel.call_count == 0


def test_a_second_stop_all_never_escalates_to_a_kill(tmp_path: Path) -> None:
    state = _state(tmp_path, crew_running=0)
    _slot(state, backend=ACP_BACKEND_KAS, record=_open())

    _stop_all(state)
    _stop_all(state)

    for call in state.sessions.stop_turn.await_args_list:
        assert not call.kwargs.get("force")


@pytest.mark.parametrize(
    ("backend", "record"),
    [
        (ACP_BACKEND_KAS, None),
        (ACP_BACKEND_KAS, {**_open(), "done": True}),
        (ACP_BACKEND_KAS, {**_open(), "background": True}),
        (ACP_BACKEND_KIRO, _open()),
    ],
    ids=["kas-no-engine-child", "kas-child-finished", "kas-background-card", "kiro-cli-native"],
)
def test_stop_all_without_open_engine_children_keeps_the_turn(
    tmp_path: Path, backend: str, record: dict[str, Any] | None
) -> None:
    """Kiro Crew's own sub-agents: Stop all is cancel_for_parent and nothing else."""
    state = _state(tmp_path, crew_running=3)
    slot = _slot(state, backend=backend, record=record)

    resp = _stop_all(state)

    assert _payload(resp) == {"ok": True, "stopped": 3, "running": 3, "queued": 0}
    state.subagents.cancel_for_parent.assert_awaited_once_with(SESSION)
    state.sessions.stop_turn.assert_not_awaited()
    assert slot.running
    assert slot._stop_state == "idle"


# ── A mixed turn: one Kiro Crew run and one engine sub-agent ───────────────────
#
# ``SubagentManager.cancel`` returns once the run it stopped has reported to its
# parent (``cancellation.cancel_impl`` awaits the reap, and the reap awaits the
# report), and a report for a parent whose turn is running lands when that turn
# ends. So the manager's stop can outlast the engine sub-agents it is asked beside.


@pytest.mark.asyncio
async def test_a_mixed_turn_counts_its_engine_children_before_the_manager_answers(
    tmp_path: Path,
) -> None:
    """By the time the manager's stop answers, the engine sub-agent has ended and its
    card has closed: it is counted, and the turn stopped, before that wait."""
    state = _state(tmp_path, crew_running=1)
    slot = _slot(state, backend=ACP_BACKEND_KAS, record=_open())

    async def _answers_late(_parent: str) -> tuple[int, int]:
        slot._native_subagent_tracker[CHILD]["done"] = True
        return (1, 0)

    state.subagents.cancel_for_parent = AsyncMock(side_effect=_answers_late)

    resp = await mod.api_spawn_stop_all(_Req(state, {"slot": SLOT}))

    assert _payload(resp) == {"ok": True, "stopped": 1, "running": 1, "queued": 0, "engine": 1}
    assert state.sessions.stop_turn.await_count == 1, "the engine sub-agent ran on"
    state.subagents.cancel_for_parent.assert_awaited_once_with(SESSION)


@pytest.mark.asyncio
async def test_a_mixed_turn_is_answered_without_waiting_for_the_turn_to_end_on_its_own(
    tmp_path: Path,
) -> None:
    """The manager's stop answers only when the parent's turn ends. Stop all ends that
    turn itself, so the request is answered instead of hanging until the turn is over."""
    from kiro_crew.testing.wait import finished

    state = _state(tmp_path, crew_running=1)
    _slot(state, backend=ACP_BACKEND_KAS, record=_open())
    turn_ended = asyncio.Event()

    async def _turn_stops(*_args: Any, **_kwargs: Any) -> str:
        turn_ended.set()
        return "stopped"

    async def _answers_at_turn_end(_parent: str) -> tuple[int, int]:
        await turn_ended.wait()
        return (1, 0)

    state.sessions.stop_turn = AsyncMock(side_effect=_turn_stops)
    state.subagents.cancel_for_parent = AsyncMock(side_effect=_answers_at_turn_end)

    # The answer is the signal: with the manager awaited first it never comes.
    resp = await finished(
        mod.api_spawn_stop_all(_Req(state, {"slot": SLOT})), "Stop all answering a mixed turn"
    )

    assert _payload(resp) == {"ok": True, "stopped": 1, "running": 1, "queued": 0, "engine": 1}
    assert state.sessions.stop_turn.await_count == 1

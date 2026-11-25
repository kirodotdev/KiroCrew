"""Stop all on a chat whose running KAS turn holds engine-run sub-agents.

The engine runs its sub-agents as tool calls of the parent's turn
(``invoke_sub_agent``), so Kiro Crew holds no run of its own for them: they are
``native:<id>`` cards on the slot, and the KAS engine offers no request that stops
one of them, or all of a turn's, while the turn goes on. ``POST /api/spawn/stop-all``
reaches Kiro Crew's own runs (``cancel_for_parent``) and, only while such cards are
open, stops the parent's turn, which is what stops them.

"Such cards" are the ones the engine path marked ``engine``
(``dashboard.engine_subagent_cards``): the mark the page's chip counts for its
"Stop all and the answer" label. A native card without it (a custom agent's own
sub-agent, a chat registered with the preview off) is not on the chip, so Stop all
there is labelled for Kiro Crew's runs alone and leaves the turn running.

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
from engine_turn_helpers import live_reading_says_kas
from handlers_messaging_coverage_helpers import _OWNER_SUBJECT, _payload, _Req, _run
from kas_engine_child_helpers import _agents_dir, _batch, _default_spec
from kas_engine_child_helpers import _handle as _session_handle
from kas_engine_child_helpers import _own, _setting, _write

import kiro_crew.dashboard.handlers.messaging as mod
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.dashboard.state import _ChatSlot

_TEST_DIR = str(Path(__file__).resolve().parent)

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

SLOT = "chat-1"
#: The session key the route resolves the slot to (``effective_session_key``).
SESSION = f"dashboard:{SLOT}"
CHILD = "sub-1"


def _state(tmp_path: Path, crew_running: int) -> Any:
    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    # The owner presses Stop all: ``_Req`` carries the owner's subject.
    state.owner_id = _OWNER_SUBJECT
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
    """An open card of an engine sub-agent, as the engine path's sync marks it."""
    return {**_unmarked(), "engine": True}


def _unmarked() -> dict[str, Any]:
    """An open native card the engine path did not mark: any other harness-run child."""
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
        (ACP_BACKEND_KAS, _unmarked()),
        # A mark off the wire is not the engine path's: only ``True`` counts.
        (ACP_BACKEND_KAS, {**_unmarked(), "engine": "yes"}),
    ],
    ids=[
        "kas-no-engine-child",
        "kas-child-finished",
        "kas-background-card",
        "kiro-cli-native",
        "kas-unmarked-native-card",
        "kas-mark-not-true",
    ],
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


# ── the mark is the session's: a real registration, a real roster, the real route ──
#
# What a session registered decides whether its roster rows become ``engine`` cards
# (``engine_subagent_cards.session_runs_engine_children``), and Stop all counts those
# cards alone. So the chip's label, the card's missing Cancel button and this route
# read one mark.


def _roster() -> list[dict[str, Any]]:
    """The roster a KAS session emits for one started dispatch."""
    from kiro_crew.acp import session_handle_native_children as children

    parser = SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )
    frame = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "invoke_subagent_toolu_1",
        "title": "Sub-agent: own-model",
        "status": "in_progress",
        "rawInput": {"name": "own-model", "prompt": "Run ls and report"},
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": CHILD}},
    }
    [event] = children._handle_kas_subagent(parser, frame) or []
    return event.subagents


async def _turn_with_an_open_child(state: Any, handle: Any, agent: str) -> _ChatSlot:
    """A running turn of *agent* on *handle*'s session whose roster reported one
    started child, drawn by the turn's own roster handler."""
    import kiro_crew.dashboard.chat_runner as cr
    from kiro_crew.acp.types import EVENT_SUBAGENT_LIST, AcpEvent

    slot = _slot(state, backend=ACP_BACKEND_KAS, record=None)
    slot._acp_client._handle = handle
    state.broadcast_ws = MagicMock()
    turn = SimpleNamespace(
        state=state,
        slot=slot,
        session_key=SESSION,
        kiro_agent=agent,
        _engine_children=None,
        _engine_dispatch_tasks={},
        _native_tracker=slot._native_subagent_tracker,
        _native_card_output={},
        _turn_native_activity=False,
    )
    await cr._on_subagent_list(turn, AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_roster()))
    [card] = slot._native_subagent_tracker.values()
    assert card["done"] is False
    return slot


@pytest.mark.asyncio
@pytest.mark.parametrize("preview", ["on", "off"])
async def test_stop_all_keeps_the_turn_of_a_custom_agent_whose_own_engine_children_are_open(
    agents_dir, setting, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preview: str
) -> None:
    """A person's agent that lists sub-agents in its own spec runs them in the engine
    on either value of the preview. Kiro Crew mounted nothing for it, so its cards
    carry no mark, the chip does not list them, and Stop all stops Kiro Crew's own
    runs and nothing else."""
    _write(
        agents_dir,
        _own(
            "boss",
            tools=["fs_read", "use_subagent"],
            toolsSettings={"subagent": {"availableAgents": ["own-model"]}},
        ),
    )
    _write(agents_dir, _own("own-model"))
    setting(preview)
    live_reading_says_kas(monkeypatch)
    handle = _session_handle(await _batch("boss"), tmp_path, agent="boss", sid="sess-1")
    state = _state(tmp_path, crew_running=2)
    slot = await _turn_with_an_open_child(state, handle, "boss")
    [card] = slot._native_subagent_tracker.values()
    assert "engine" not in card

    resp = await mod.api_spawn_stop_all(_Req(state, {"slot": SLOT}))

    assert _payload(resp) == {"ok": True, "stopped": 2, "running": 2, "queued": 0}
    state.subagents.cancel_for_parent.assert_awaited_once_with(SESSION)
    state.sessions.stop_turn.assert_not_awaited()
    assert slot.running


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opened_with", "engine"), [("on", 1), ("off", 0)], ids=["mounted", "not-mounted"]
)
async def test_stop_all_follows_what_the_default_agents_session_mounted_not_the_switch_now(
    agents_dir,
    setting,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    opened_with: str,
    engine: int,
) -> None:
    """The default agent's chat, registered while the preview read *opened_with* and
    stopped after the preview was moved the other way."""
    _write(agents_dir, _default_spec())
    setting(opened_with)
    handle = _session_handle(await _batch(), tmp_path, sid="sess-1")
    setting("off" if opened_with == "on" else "on")
    live_reading_says_kas(monkeypatch)
    state = _state(tmp_path, crew_running=0)
    slot = await _turn_with_an_open_child(state, handle, "kirocrew")
    [card] = slot._native_subagent_tracker.values()
    assert (card.get("engine") is True) is bool(engine)

    resp = await mod.api_spawn_stop_all(_Req(state, {"slot": SLOT}))

    expected: dict[str, Any] = {"ok": True, "stopped": 0, "running": 0, "queued": 0}
    if engine:
        expected["engine"] = engine
    assert _payload(resp) == expected
    assert state.sessions.stop_turn.await_count == engine

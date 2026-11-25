"""A stop retires, on every page, the approval cards it rejected in an engine chat.

A stop settles each approval the turn waits on as rejected. The composer's Stop
button also clears the cards of the tab that pressed it; a stop from anywhere else
settled the request and told no page, so the card stayed "Waiting for approval"
with live buttons that answered ``already_resolved``. In a chat whose sub-agents
run in the KAS engine, Stop all on the sub-agent chip is such a stop, so there the
stop sends ``approval_resolved`` itself. Every other chat is left as it was.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from engine_turn_helpers import client, live_reading_says_kas, registered
from handlers_messaging_coverage_helpers import _payload, _Req, _run

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.acp.kas_engine_subagents import KAS_SUBAGENTS_PREVIEW
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.dashboard import engine_subagent_cards as engine_cards
from kiro_crew.dashboard import stop_retires_approval as retire
from kiro_crew.dashboard.chat_handlers import _unblock_pending_waits
from kiro_crew.dashboard.state import _ChatSlot

_TEST_DIR = str(Path(__file__).resolve().parent)
SLOT = "chat-1"


def _slot() -> _ChatSlot:
    return _ChatSlot(SLOT)


def _waiting(slot: _ChatSlot, request_id: str, *, role: str = "permission", mid: str = "") -> Any:
    """One card *slot* waits on: its future and its row, as the runner leaves them."""
    fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
    slot._approval_futures[request_id] = fut
    meta = {"request_id": request_id}
    row: dict[str, Any] = {"role": role, "content": "mkdir -p probe", "cls": json.dumps(meta)}
    if mid:
        row["meta"] = {"mid": mid}
        # What the runner records when it registers the request for that row.
        slot._approval_instances[request_id] = (fut, mid)
    slot.messages.append(row)
    return fut


def _resolved_frames(state: Any) -> list[dict[str, Any]]:
    return [
        call.args[1]
        for call in state.broadcast_ws.call_args_list
        if call.args[0] == "approval_resolved"
    ]


def _state() -> Any:
    state = MagicMock()
    state.cancel_questions_for_slot.return_value = 0
    return state


@pytest.fixture
def engine_chat(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The chat's sub-agents run in the engine; records what the reading was asked."""
    asked: list[Any] = []

    def reading(slot: Any) -> bool:
        asked.append(slot)
        return True

    monkeypatch.setattr(engine_cards, "session_runs_engine_children", reading)
    return asked


@pytest.mark.asyncio
async def test_a_stop_tells_every_page_the_card_it_rejected_is_settled(engine_chat) -> None:
    state, slot = _state(), _slot()
    fut = _waiting(slot, "2", mid="m-97a4b14bdb6b475b")

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    assert fut.result() == "rejected"
    assert _resolved_frames(state) == [
        {
            "id": "2",
            "approved": False,
            "slot": SLOT,
            "origin": "native",
            "mid": "m-97a4b14bdb6b475b",
        }
    ]
    state.push_slots_update.assert_called_once_with()
    # The reading is asked about this chat's own slot, whose client holds its session.
    assert engine_chat == [slot]


@pytest.mark.asyncio
async def test_a_chat_whose_sub_agents_do_not_run_in_the_engine_is_left_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch off, another agent, another backend: the stop settles the request
    and sends no frame, exactly as before."""
    monkeypatch.setattr(engine_cards, "session_runs_engine_children", lambda *a: False)
    state, slot = _state(), _slot()
    fut = _waiting(slot, "2", mid="m-1")

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    assert fut.result() == "rejected"
    assert _resolved_frames(state) == []
    state.push_slots_update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("mounted", "frames"), [(True, 1), (False, 0)])
async def test_what_the_session_registered_decides_whatever_the_switch_reads_now(
    monkeypatch: pytest.MonkeyPatch, mounted: bool, frames: int
) -> None:
    """Through the real reading: a KAS chat on the default agent that mounted the
    engine's tool, or did not, while the switch now reads the other way."""
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = {KAS_SUBAGENTS_PREVIEW: not mounted}
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls, *a, **k: cfg))
    live_reading_says_kas(monkeypatch)
    state, slot = _state(), _slot()
    slot.agent = "kirocrew"
    slot._acp_client = client(registered(mounted_for="kirocrew" if mounted else ""))
    fut = _waiting(slot, "2")

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    assert fut.result() == "rejected"
    assert len(_resolved_frames(state)) == frames


@pytest.mark.asyncio
async def test_every_rejected_card_gets_its_own_frame_and_none_says_approved(engine_chat) -> None:
    state, slot = _state(), _slot()
    _waiting(slot, "1")
    _waiting(slot, "2")

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    frames = _resolved_frames(state)
    assert [frame["id"] for frame in frames] == ["1", "2"]
    assert all(frame["approved"] is False for frame in frames)
    assert all(fut.result() == "rejected" for fut in slot._approval_futures.values())


@pytest.mark.asyncio
async def test_an_answered_card_and_an_elicitation_card_get_no_frame(engine_chat) -> None:
    state, slot = _state(), _slot()
    answered = _waiting(slot, "1")
    answered.set_result("approved")
    # An MCP server's question: its own runner answers cancel and retires the card.
    _waiting(slot, "q1", role="elicitation")

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    assert _resolved_frames(state) == []
    assert answered.result() == "approved"
    state.push_slots_update.assert_not_called()


@pytest.mark.asyncio
async def test_a_stop_with_nothing_waiting_sends_nothing(engine_chat) -> None:
    state, slot = _state(), _slot()

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    assert _resolved_frames(state) == []
    state.push_slots_update.assert_not_called()
    # Nothing waits, so the session is not even read.
    assert engine_chat == []


@pytest.mark.asyncio
async def test_a_card_the_stop_did_not_settle_is_not_retired() -> None:
    """The frame follows the settlement: a card still waiting keeps its buttons."""
    state, slot = _state(), _slot()
    fut = _waiting(slot, "1")

    retire.retire_stopped_cards(state, slot, [("1", "")])

    assert not fut.done()
    assert _resolved_frames(state) == []


@pytest.mark.asyncio
async def test_a_page_frame_that_fails_does_not_undo_the_stop(engine_chat) -> None:
    state, slot = _state(), _slot()
    fut = _waiting(slot, "1")
    state.broadcast_ws.side_effect = RuntimeError("socket layer down")

    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        _unblock_pending_waits(state, slot)

    assert fut.result() == "rejected"
    state.cancel_questions_for_slot.assert_called_once_with(SLOT)


def test_a_slot_that_cannot_be_read_has_no_card_to_retire() -> None:
    assert retire.waiting_permission_cards(_state(), SimpleNamespace(messages=None)) == []


def test_stop_all_on_an_engine_turn_retires_the_childs_waiting_card(
    tmp_path: Path, engine_chat
) -> None:
    """The reported case: asking mode, an engine sub-agent's call waits on a card,
    and Stop all ends the turn. The card must not stay live on the page."""
    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    state.sessions.is_compacting = MagicMock(return_value=False)
    state.sessions.stop_turn = AsyncMock(return_value="stopped")
    state.subagents = MagicMock()
    state.subagents.cancel_for_parent = AsyncMock(return_value=(0, 0))
    state.broadcast_ws = MagicMock()
    slot = _slot()
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    slot._acp_client = SimpleNamespace(backend=ACP_BACKEND_KAS, cancel_session=AsyncMock())
    slot._native_subagent_tracker = {
        "sub-1": {"id": "native:sub-1", "done": False, "task": "t", "started": 0.0, "engine": True}
    }
    state._slots[SLOT] = slot
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        fut = _waiting(slot, "2", mid="m-1")
        resp = _run(messaging.api_spawn_stop_all, _Req(state, {"slot": SLOT}))
    finally:
        asyncio.set_event_loop(None)
        loop.close()

    assert _payload(resp)["engine"] == 1
    assert fut.result() == "rejected"
    assert _resolved_frames(state) == [
        {"id": "2", "approved": False, "slot": SLOT, "origin": "native", "mid": "m-1"}
    ]

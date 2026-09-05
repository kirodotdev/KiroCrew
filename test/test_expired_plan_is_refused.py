"""A plan whose stages are gone must say so, not return in silence.

The autopilot's plan SHAPE (``_stage_titles``, and so ``_plan_stage_count``) lives
only in a slot's memory. Nothing persists it, and that is deliberate: the
autopilot is a lightweight executor, not a task runner, so a plan nobody was
watching is not resumed across a gateway restart.

What IS persisted is ``mode``, and the transcript keeps the plan turn's
``[OPTION: Go | Go All | Cancel]`` row. So a restored orchestrator slot renders
those buttons over a plan that no longer exists. Pressing one used to do nothing
at all -- ``range(start_idx, 0)`` is empty, and the completion message is gated on
``start_idx < total`` -- so the user pressed Go and got no response whatsoever, and
no way to tell a dead plan from a hung one.

The fix is a refusal the user can read, not a resume. These tests pin the refusal
and pin that it costs nothing: no tracker, no config load, no model turn.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import _ChatSlot


def _state() -> MagicMock:
    state = MagicMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.subagents = MagicMock()
    state.subagents.running_agents_for = MagicMock(return_value=[])
    return state


def _restored_slot(key: str = "expired-plan-slot") -> _ChatSlot:
    """The shape a restart leaves behind: orchestrator mode, no plan.

    ``mode`` survives because it is persisted with the slot; the stage titles do
    not. No test sets ``_stage_titles`` here on purpose -- the absence IS the
    fixture.
    """
    slot = _ChatSlot(key, mode="orchestrator")
    slot._orch_tracker = None
    return slot


def _stub_turn(monkeypatch: Any) -> list[str]:
    """Record any model turn the loop attempts. It must attempt none."""
    turns: list[str] = []

    async def _fake_run_chat(_state: Any, _slot: Any, context: str, **_kwargs: Any) -> None:
        turns.append(context)

    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator._run_chat", _fake_run_chat)
    monkeypatch.setattr("kiro_crew.dashboard.chat_orchestrator.sel", MagicMock())
    return turns


@pytest.mark.asyncio
async def test_a_plan_with_no_stages_tells_the_user(monkeypatch: Any) -> None:
    """Go on a restored plan gets an answer, not silence."""
    from kiro_crew.dashboard.chat import _stage_loop

    turns = _stub_turn(monkeypatch)
    slot = _restored_slot()
    state = _state()

    await _stage_loop(state, slot, auto_run=False)

    assistant_rows = [m for m in slot.messages if m.get("role") == "assistant"]
    assert assistant_rows, (
        "the user pressed Go and the slot said nothing: a plan whose stages are "
        "gone must be refused out loud, not silently skipped"
    )
    body = " ".join(str(m.get("content", "")) for m in assistant_rows)
    assert "no longer active" in body, f"the refusal does not name the state: {body!r}"
    assert turns == [], "a plan with no stages must not run a model turn"


@pytest.mark.asyncio
async def test_the_refusal_closes_the_turn_out(monkeypatch: Any) -> None:
    """The frontend spinner has to stop, or the slot looks like it is working.

    ``chat_done`` is what clears it, and ``slot.task`` is what the next press
    checks, so an early return that skips either leaves the slot wedged in a way
    the user cannot see the cause of.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    _stub_turn(monkeypatch)
    slot = _restored_slot("expired-plan-closeout")
    state = _state()

    await _stage_loop(state, slot, auto_run=False)

    events = [call.args[0] for call in state.broadcast_ws.call_args_list]
    assert "chat_done" in events, f"the turn was never closed out: {events}"
    assert slot.task is None, "the slot still holds a task nothing will finish"
    assert state.push_slots_update.called, "the slot list was left stale"


@pytest.mark.asyncio
async def test_the_refusal_costs_nothing(monkeypatch: Any) -> None:
    """Refused before the tracker is built, so before the config is read.

    Placement matters: the config load is the loop's first await and it exists to
    give a RUNNING plan its budgets. Refusing after it would stat, read, merge and
    schema-validate ``config.json`` on a worker thread to serve a plan that is
    about to be turned away.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    _stub_turn(monkeypatch)
    loads: list[int] = []

    def _loader() -> Any:  # pragma: no cover - must never run
        loads.append(1)
        raise AssertionError("the config was loaded for a plan that cannot run")

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_orchestrator.KiroCrewConfig",
        MagicMock(load=_loader),
    )

    slot = _restored_slot("expired-plan-cost")
    await _stage_loop(_state(), slot, auto_run=False)

    assert loads == []
    assert (
        slot._orch_tracker is None
    ), "a tracker was built for a plan with no stages; the refusal must come first"


@pytest.mark.asyncio
async def test_go_all_is_refused_the_same_way(monkeypatch: Any) -> None:
    """Auto-run is not a different door.

    Go All reaches the same loop with ``auto_run=True``; if the gate sat behind an
    ``auto_run`` check, the unattended path -- the one nobody is watching -- would
    be the one that failed silently.
    """
    from kiro_crew.dashboard.chat import _stage_loop

    turns = _stub_turn(monkeypatch)
    slot = _restored_slot("expired-plan-go-all")
    slot._auto_run = True

    await _stage_loop(_state(), slot, auto_run=True)

    body = " ".join(
        str(m.get("content", "")) for m in slot.messages if m.get("role") == "assistant"
    )
    assert "no longer active" in body, f"Go All was refused in silence: {body!r}"
    assert turns == []

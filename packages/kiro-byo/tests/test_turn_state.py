from __future__ import annotations

import pytest

from kiro_byo.turn_state import (
    RoundDisposition,
    RoundOutcome,
    TurnPhase,
    TurnPhaseError,
    TurnState,
    allowed_next_phases,
)


def test_turn_phase_wire_names_are_stable_and_provider_neutral() -> None:
    assert [phase.value for phase in TurnPhase] == [
        "prepare", "route", "invoke", "stream", "tool", "continue", "post_stage", "complete",
    ]
    # Product policy names belong to Policy Packs, never the Turn Engine contract.
    assert not any(name in {phase.value for phase in TurnPhase} for name in {"sky", "multi", "power", "glitter"})


def test_normal_tool_turn_can_follow_explicit_phase_contract() -> None:
    state = TurnState()
    state.transition(TurnPhase.ROUTE, reason="route model")
    state.transition(TurnPhase.INVOKE)
    state.note_provider_round()
    state.transition(TurnPhase.STREAM)
    state.transition(TurnPhase.TOOL)
    state.note_tool_batch()
    state.transition(TurnPhase.CONTINUE)
    state.transition(TurnPhase.INVOKE)
    state.note_provider_round()
    state.transition(TurnPhase.POST_STAGE)
    state.transition(TurnPhase.COMPLETE, reason="success")

    assert state.complete
    assert state.provider_rounds == 2
    assert state.round_index == 2
    assert state.tool_batches == 1
    assert state.terminal_reason == "success"
    assert [(row.source, row.target) for row in state.transitions][-2:] == [
        (TurnPhase.INVOKE, TurnPhase.POST_STAGE),
        (TurnPhase.POST_STAGE, TurnPhase.COMPLETE),
    ]


def test_pristine_provider_failure_may_reenter_route_for_bounded_fallback() -> None:
    state = TurnState()
    state.transition(TurnPhase.ROUTE)
    state.transition(TurnPhase.INVOKE)
    state.note_provider_round()
    state.transition(TurnPhase.ROUTE, reason="first-round fallback")
    state.transition(TurnPhase.INVOKE)
    state.note_provider_round()
    state.transition(TurnPhase.COMPLETE, reason="success")

    assert state.provider_rounds == 2
    assert state.complete


def test_complete_is_terminal_and_invalid_transition_fails_closed() -> None:
    state = TurnState()
    state.transition(TurnPhase.COMPLETE, reason="route failed")
    assert allowed_next_phases(TurnPhase.COMPLETE) == frozenset()
    with pytest.raises(TurnPhaseError, match="complete -> invoke"):
        state.transition(TurnPhase.INVOKE)


def test_round_outcome_encodes_only_turn_engine_decision_facts() -> None:
    tools = RoundOutcome(
        RoundDisposition.TOOLS,
        provider_id="p",
        model_id="model-a",
        served_model_id="model-a-2026",
        tool_call_count=2,
    )
    final = RoundOutcome(RoundDisposition.FINAL, text="done")
    interrupted = RoundOutcome(
        RoundDisposition.INTERRUPTED,
        text="partial",
        interruption="steered",
    )

    assert tools.requires_tool_batch and not tools.is_terminal_candidate
    assert final.is_terminal_candidate and not final.requires_tool_batch
    assert not interrupted.is_terminal_candidate
    assert not hasattr(tools, "provider_context")


def test_round_outcome_rejects_inconsistent_tool_counts() -> None:
    with pytest.raises(ValueError, match="requires at least one tool call"):
        RoundOutcome(RoundDisposition.TOOLS)
    with pytest.raises(ValueError, match="only tools outcomes"):
        RoundOutcome(RoundDisposition.FINAL, tool_call_count=1)

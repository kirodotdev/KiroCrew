from __future__ import annotations

from kiro_byo.execution_guard import (
    ADAPTIVE_TOOL_CALL_THRESHOLD,
    ExecutionGuard,
)


def test_long_productive_run_is_not_stopped_at_adaptive_threshold() -> None:
    guard = ExecutionGuard()
    threshold_seen = False
    for index in range(ADAPTIVE_TOOL_CALL_THRESHOLD + 25):
        decision = guard.observe("read", {"path": f"file-{index}.txt"}, f"result-{index}")
        threshold_seen = threshold_seen or decision.adaptive_threshold_reached
        assert decision.action == "continue"
    assert threshold_seen
    assert guard.tool_calls_seen == ADAPTIVE_TOOL_CALL_THRESHOLD + 25


def test_identical_tool_result_warns_then_halts_only_after_replan_chance() -> None:
    guard = ExecutionGuard()
    actions = [guard.observe("read", {"path": "same.txt"}, "same result") for _ in range(6)]
    assert [item.action for item in actions[:3]] == ["continue", "continue", "continue"]
    assert actions[3].action == "warn"
    assert actions[4].action == "continue"
    assert actions[5].action == "halt"
    assert actions[5].reason == "stagnant_identical_action"


def test_short_repeating_cycle_is_detected() -> None:
    guard = ExecutionGuard()
    decisions = []
    for _ in range(4):
        decisions.append(guard.observe("search", {"q": "a"}, "A"))
        decisions.append(guard.observe("read", {"path": "b"}, "B"))
    assert any(item.action == "warn" for item in decisions)
    assert decisions[-1].action == "halt"
    assert decisions[-1].reason == "stagnant_action_cycle"


def test_new_productive_action_clears_stagnation_escalation() -> None:
    guard = ExecutionGuard()
    for _ in range(4):
        decision = guard.observe("read", {"path": "same.txt"}, "same")
    assert decision.action == "warn"
    assert guard.observe("read", {"path": "new.txt"}, "new").action == "continue"
    # A future repetition gets a fresh warning rather than inheriting the old halt state.
    actions = [guard.observe("search", {"q": "same"}, "same search") for _ in range(4)]
    assert actions[-1].action == "warn"

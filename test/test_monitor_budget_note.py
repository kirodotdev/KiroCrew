"""Tests for the budget line a structured watch's wake can carry.

``monitoring.budget_notice_pct`` names the share of a watch's agent turns, tokens or
runtime at which the wake states what is used, so the agent can ask for a larger budget
before the watch stops. ``monitor_budget_note`` builds the one line from whole numbers,
``format_monitor_wake`` places it above the next action, and a setting of 0 leaves the
envelope exactly as it was. The shared ``_armed`` builder lives in
``monitor_controller_helpers.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from monitor_controller_helpers import _armed, _result

from kiro_crew.config.section_builders import _build_monitoring_config
from kiro_crew.monitoring.controller import format_monitor_wake
from kiro_crew.monitoring.decision import monitor_budget_note, monitor_budget_reason
from kiro_crew.monitoring.limits import budget_notice_pct, coerce_budget_notice_pct
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDispatchResult,
    MonitorObservationStatus,
    MonitorState,
)

_BUDGETS = MonitorBudgets(max_runtime_secs=14_400, max_agent_turns=10, max_tokens=250_000)


def _state(**changes: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
        "budgets": _BUDGETS,
        "agent_turns": 8,
        "input_tokens": 200_000,
        "output_tokens": 12_000,
    }
    values.update(changes)
    return MonitorState(**values)


# 12,000 of 14,400 seconds used leaves 2,400.
_NOW = 13_000.0
_LINE = (
    "Budget: 8 of 10 agent turns used, 212000 of 250000 tokens used, "
    "2400s of 14400s runtime left; 80% or more used."
)


def test_note_is_empty_when_the_setting_is_off():
    assert monitor_budget_note(_state(), now=_NOW, pct=0) == ""


def test_note_names_turns_tokens_and_runtime_at_the_threshold():
    assert monitor_budget_note(_state(), now=_NOW, pct=80) == _LINE


def test_unknown_token_usage_reads_at_least_the_reported_count():
    note = monitor_budget_note(_state(token_usage_known=False), now=_NOW, pct=80)

    assert note == (
        "Budget: 8 of 10 agent turns used, at least 212000 of 250000 tokens used, "
        "2400s of 14400s runtime left; 80% or more used."
    )


@pytest.mark.parametrize(
    ("changes", "now", "reason"),
    [
        ({}, 15_400.0, "runtime_budget"),
        ({"agent_turns": 10}, _NOW, "agent_turn_budget"),
        ({"input_tokens": 250_000}, _NOW, "token_budget"),
        ({"input_tokens": 250_000, "token_usage_known": False}, _NOW, "token_budget"),
    ],
)
def test_the_stop_and_the_line_agree_at_each_bound(changes, now, reason):
    state = _state(**{"agent_turns": 0, "input_tokens": 0, "output_tokens": 0, **changes})

    assert monitor_budget_reason(state, now=now) == reason
    assert monitor_budget_note(state, now=now, pct=99) != ""


def test_unlimited_turns_omit_the_turn_part():
    budgets = MonitorBudgets(max_runtime_secs=14_400, max_agent_turns=0, max_tokens=250_000)

    note = monitor_budget_note(_state(budgets=budgets), now=_NOW, pct=80)

    assert note == (
        "Budget: 212000 of 250000 tokens used, 2400s of 14400s runtime left; 80% or more used."
    )


def test_below_the_threshold_adds_nothing():
    state = _state(agent_turns=7, input_tokens=100_000, output_tokens=0)

    assert monitor_budget_note(state, now=2_000.0, pct=80) == ""


def test_one_bound_at_the_share_is_enough():
    state = _state(agent_turns=1, input_tokens=0, output_tokens=0)

    note = monitor_budget_note(state, now=_NOW, pct=80)

    assert note.startswith("Budget: 1 of 10 agent turns used, 0 of 250000 tokens used, ")


def test_the_share_is_compared_in_whole_numbers():
    state = _state(agent_turns=0, input_tokens=0, output_tokens=0)

    assert monitor_budget_note(state, now=1_000.0 + 11_519, pct=80) == ""
    assert monitor_budget_note(state, now=1_000.0 + 11_520, pct=80) != ""


@pytest.mark.parametrize("pct", [-1, 100, "80", True, 80.0, None])
def test_out_of_range_setting_reads_as_off(pct):
    assert coerce_budget_notice_pct(pct) == 0
    assert monitor_budget_note(_state(), now=_NOW, pct=pct) == ""


@pytest.mark.parametrize("pct", [1, 80, 99])
def test_an_in_range_setting_is_kept(pct):
    assert coerce_budget_notice_pct(pct) == pct


def test_the_config_section_reads_the_setting_with_its_coercion():
    assert _build_monitoring_config({"budget_notice_pct": 80}, False).budget_notice_pct == 80
    assert _build_monitoring_config({"budget_notice_pct": "80"}, False).budget_notice_pct == 0
    assert _build_monitoring_config({}, False).budget_notice_pct == 0


def test_the_live_read_follows_the_applied_snapshot(monkeypatch):
    config = SimpleNamespace(monitoring=SimpleNamespace(budget_notice_pct=80))
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: config)
    assert budget_notice_pct() == 80

    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: None)
    assert budget_notice_pct() == 0


def _wake(budget_note: str | None = None) -> str:
    extra = {} if budget_note is None else {"budget_note": budget_note}
    return format_monitor_wake(
        monitor_id="mon-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        fingerprint="fp-1",
        reason_code="new_head_revision",
        canonical=_result(MonitorObservationStatus.ACTIONABLE).canonical,
        wake_instructions="Check the failed gate.",
        **extra,
    )


def test_the_line_sits_above_the_next_action_and_an_empty_note_changes_nothing():
    plain = _wake()

    assert _wake("") == plain
    with_line = _wake(_LINE)
    assert with_line.replace(f"{_LINE}\n", "") == plain
    assert with_line.index(_LINE) < with_line.index("Next action:")


async def _delivered_wake(tmp_path, monkeypatch, pct: int) -> str:
    envelopes: list[str] = []

    async def dispatch(_loop, envelope):
        envelopes.append(envelope)
        return MonitorDispatchResult.DISPATCHED

    service, loop, controller = await _armed(
        tmp_path,
        result=_result(MonitorObservationStatus.ACTIONABLE),
        dispatch=AsyncMock(side_effect=dispatch),
    )
    monkeypatch.setattr("kiro_crew.monitoring.controller.budget_notice_pct", lambda: pct)
    await controller.tick(loop, now=120.0)
    service.stop()
    assert len(envelopes) == 1
    return envelopes[0]


@pytest.mark.asyncio
async def test_a_delivered_wake_holds_the_line_only_when_the_setting_is_on(tmp_path, monkeypatch):
    off = await _delivered_wake(tmp_path / "off", monkeypatch, 0)
    on = await _delivered_wake(tmp_path / "on", monkeypatch, 1)

    assert "Budget:" not in off
    line = "Budget: 0 of 250000 tokens used, 580s of 600s runtime left; 1% or more used.\n"
    assert f"{line}Next action:" in on
    # Line 1 names the monitor, and each armed watch has its own id.
    assert on.replace(line, "").splitlines()[2:] == off.splitlines()[2:]

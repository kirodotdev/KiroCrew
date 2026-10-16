"""The autonomous-goal marker has one producer: the dashboard critical rules.

The full dashboard chat is the only surface that renders ``[GOAL: ...]`` as a
review card, so the rule that teaches the marker goes only to slots full chat
can open. One slot can also be shown in a split pane or the companion chat,
which the server cannot see, so the rule says those hosts offer the objective as
an ordinary reply. Every other surface keeps a rules block that never mentions
the marker.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.context import (
    _CRITICAL_RULES,
    _CRITICAL_RULES_CHANNEL,
    _CRITICAL_RULES_GOAL,
    _GOAL_RULE_DASHBOARD,
    ContextBuilder,
    _critical_rules_for,
)
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader


def test_only_the_goal_variant_teaches_the_marker() -> None:
    assert "[GOAL: objective]" in _CRITICAL_RULES_GOAL
    assert "[GOAL" not in _CRITICAL_RULES
    assert "[GOAL" not in _CRITICAL_RULES_CHANNEL


def test_goal_rule_precedes_the_final_options_rule_and_requires_review() -> None:
    assert "before any final [OPTIONS:] line" in _GOAL_RULE_DASHBOARD
    assert "nothing runs until the user" in _GOAL_RULE_DASHBOARD
    # The rule promises the card only in full chat and names the pane fallback.
    assert "The full chat view shows it as an Autonomous goal card" in _GOAL_RULE_DASHBOARD
    assert (
        "Split panes and the companion chat offer the same objective as an ordinary "
        "one-turn reply" in _GOAL_RULE_DASHBOARD
    )
    # The OPTIONS-must-be-last rule still follows, so the two never conflict.
    assert _CRITICAL_RULES_GOAL.index("[GOAL: objective]") < _CRITICAL_RULES_GOAL.index(
        "The [OPTIONS:] line MUST be the final line"
    )


def test_full_dashboard_chat_receives_the_goal_rule() -> None:
    assert _critical_rules_for("dashboard:chat-1", "dashboard") is _CRITICAL_RULES_GOAL


@pytest.mark.parametrize("mode", ["crew", "member"])
def test_crew_and_member_slots_never_learn_the_marker(mode: str) -> None:
    assert _critical_rules_for("dashboard:chat-1", "dashboard", mode=mode) is _CRITICAL_RULES


def test_app_kit_slot_never_learns_the_marker() -> None:
    app_turn = SimpleNamespace(app="notes")
    assert (
        _critical_rules_for("dashboard:chat-1", "dashboard", execution_context=app_turn)
        is _CRITICAL_RULES
    )


@pytest.mark.parametrize(
    ("session_key", "runtime_source"),
    [
        ("dashboard:chat-1", "discord"),
        ("slack:C1:1.0", None),
        ("cron:job-1", None),
        ("subagent:abc", None),
        ("cli_chat", None),
    ],
)
def test_non_dashboard_runtimes_never_learn_the_marker(
    session_key: str, runtime_source: str | None
) -> None:
    assert _critical_rules_for(session_key, runtime_source) is _CRITICAL_RULES_CHANNEL


def _builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )


def test_session_context_carries_the_rule_only_for_the_full_chat(tmp_path: Path) -> None:
    builder = _builder(tmp_path)
    chat = builder.build_session_context(session_key="dashboard:chat-1", runtime_source="dashboard")
    crew = builder.build_session_context(
        session_key="dashboard:chat-2", runtime_source="dashboard", mode="crew"
    )
    slack = builder.build_session_context(session_key="slack:C1:1.0", runtime_source="slack")

    assert chat.startswith(_CRITICAL_RULES_GOAL)
    assert chat.count("[GOAL: objective]") == 1
    assert "[GOAL" not in crew
    assert "[GOAL" not in slack

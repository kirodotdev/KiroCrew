"""A Bot must treat an owner's offhand remark as a suggestion, not a standing rule.

An owner who writes "maybe even a separate security reviewer" once means that
case, not a security review for every change. These tests pin that
the goal-conductor skill tells the Bot to read a remark as a suggestion unless
the owner says it is a rule, to ask once and name the cost before turning it
into a rule for every change, and to apply the same check before `learn_add`.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"


def _patrol_section() -> str:
    body = " ".join(GOAL_SKILL.read_text(encoding="utf-8").split())
    start = body.index("### Patrol")
    return body[start : body.index("### Close the round", start)]


def test_owner_remark_is_a_suggestion_unless_the_owner_says_it_is_a_rule():
    section = _patrol_section()
    assert "**An owner's remark is a suggestion, not a rule.**" in section
    assert "unless the owner says it is a rule" in section


def test_bot_asks_once_and_names_the_cost_before_a_rule_for_every_change():
    section = _patrol_section()
    assert "ask the owner once and name what it costs" in section


def test_learn_add_gets_the_same_check():
    section = _patrol_section()
    assert "Apply the same check before you save a remark with `learn_add`" in section

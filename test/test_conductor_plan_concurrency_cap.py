"""The concurrency a conductor's plan names is written as the goal's cap.

A plan's worker count is prose; only ``concurrency_cap`` bounds how many items
run at once, and with nothing written that bound is the ledger's own 32. These
tests pin that the goal-conductor skill and the conductor rules every Bot reads
tell the conductor to write the plan's number as ``concurrency_cap`` before the
first create, and to raise it only when the user says so.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import mcp_work
from kiro_crew.agent_parts.spec_prompts import (
    _CONDUCTOR_PROMPT_SECTIONS,
    _LEDGER_CONDUCTOR_RULES,
)
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_plan_names_its_concurrency_as_one_number():
    body = _flat(GOAL_SKILL.read_text(encoding="utf-8"))
    assert "name the concurrency as one number (how many items run at once)" in body


def test_the_skill_writes_the_plans_concurrency_as_the_cap():
    body = _flat(GOAL_SKILL.read_text(encoding="utf-8"))
    start = body.index("- **Concurrency cap.**")
    bullet = body[start : body.index("- **Item total.**", start)]
    assert "before the first `create`" in bullet
    assert "else the concurrency your approved plan named" in bullet
    assert 'A plan that says "two or three" writes 3.' in bullet
    assert "Raise it only when the user says so, never on your own." in bullet


def test_the_wave_rule_does_not_forbid_the_plans_number():
    body = _flat(GOAL_SKILL.read_text(encoding="utf-8"))
    assert "not your plan" not in body
    assert "carry no number of your own" not in body
    assert "The goal's `concurrency_cap` (see Concurrency cap below)" in body


def test_the_ledger_rules_a_bot_reads_say_the_same():
    rule = _flat(_LEDGER_CONDUCTOR_RULES["resource_wave"])
    assert "The concurrency your plan names is the goal's `concurrency_cap`" in rule
    assert "before the first create" in rule
    assert "raise it only when the user says so" in rule


def test_the_conductor_prompt_counts_the_plans_concurrency_as_the_cap():
    section = _flat(dict(_CONDUCTOR_PROMPT_SECTIONS)["spend_bounded"])
    assert "the user's bound, else your plan's concurrency, is `concurrency_cap`" in section


def test_the_cap_parameter_names_the_plans_concurrency():
    tool = next(t for t in mcp_work._list_tools() if t["name"] == "work_ledger_record")
    text = _flat(tool["inputSchema"]["properties"]["concurrency_cap"]["description"])
    assert "the user's bound, else the concurrency the plan named" in text

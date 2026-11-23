"""The goal conductor parks a finding that no "Done when" condition needs.

A conductor that turned every finding (from its plan reviewer, a worker or its
own patrol) into a new work item never finished: each item's pull request got
reviewed, the review found more, and each finding became another item. These
tests pin that the goal-conductor skill sends such a finding to the goal text's
`Parked:` line instead, lists parked findings in the round report, and leaves
moving one back in to the owner.
"""

from __future__ import annotations

from pathlib import Path

from skill_script_helpers import skill_text

from kiro_crew.agent_parts import spec_prompts
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"
TEAM_LEAD_SKILL = Path(_BUILTIN_SKILLS_DIR) / "team-lead" / "SKILL.md"


def _goal() -> str:
    return " ".join(skill_text(GOAL_SKILL).split())


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.index(heading)
    end = body.index(next_heading, start + len(heading))
    return body[start:end]


def test_goal_text_has_a_parked_part_before_changed():
    """The five parts are the shared ``goal_plan`` rule every conductor prompt renders;
    the skill's planning section points at it and keeps the order."""
    from kiro_crew import agent

    round0 = _section(_goal(), "### Round 0", "### Dispatch a round")
    assert "The goal text is the plan, in the five parts your prompt's goal-plan" in round0
    assert "(Outcome, Done when, Plan, Parked, Changed; Changed last)" in round0
    rule = " ".join(agent._LEDGER_CONDUCTOR_RULES["goal_plan"].split())
    assert "Parked: findings that no Done when condition needs" in rule
    # The Board reads the Changed part to the end of the goal text, so a Parked
    # part written after it would show up as the latest change.
    assert "to five parts, Changed last" in rule


def test_patrol_parks_a_finding_no_done_when_condition_needs():
    patrol = _section(_goal(), "### Patrol", "### Close the round")
    assert "**Park what no Done when condition needs.**" in patrol
    assert "A finding from the plan reviewer, a worker's report or your own patrol" in patrol
    assert "goes on the goal text's `Parked:` line" in patrol
    assert "not into a new item, a seed or a `decide`" in patrol
    assert "Only the owner moves a parked finding in" in patrol
    # The reviewer's fixes follow the same rule instead of becoming items.
    assert "a fix it finds becomes a new item if you take it" not in patrol
    assert "a fix it finds becomes a new item only when a Done when condition needs it" in patrol


def test_charter_and_team_lead_list_the_same_five_parts():
    # The skill points the conductor at its charter's goal text rule, so the
    # charter must name the Parked part too, or the two contradict each other.
    charter = " ".join(spec_prompts._LEDGER_CONDUCTOR_RULES["goal_plan"].split())
    assert "to five parts, Changed last:" in charter
    assert "- Parked: findings that no Done when condition needs" in charter
    assert charter.index("- Parked:") < charter.index("- Changed:")
    team_lead = " ".join(skill_text(TEAM_LEAD_SKILL).split())
    assert "(Outcome, Done when, Plan, Parked, Changed)" in team_lead


def test_round_report_lists_parked_findings():
    close = _section(_goal(), "### Close the round", "### Goal changes mid-flight")
    assert "list the findings you parked this round" in close

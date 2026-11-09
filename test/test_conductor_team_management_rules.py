"""The conductor skills carry five general team-management rules.

A conductor that queues a message to a busy worker, waits on its manager for a
routine incident, dispatches a remote worker it cannot see, acts on a wake before
reading the item it names, or repeats a slip it never wrote down, gets slow or
wrong results. These tests pin that the shared procedure (goal-conductor) says
each rule once, and that the team-lead skill and prompt carry only their delta.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import agent
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"
TEAM_LEAD_SKILL = Path(_BUILTIN_SKILLS_DIR) / "team-lead" / "SKILL.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.index(heading)
    end = body.index(next_heading, start + len(heading))
    return body[start:end]


def _goal() -> str:
    return _flat(GOAL_SKILL.read_text(encoding="utf-8"))


def test_goal_conductor_steers_a_working_fleet_like_one_worker():
    section = _section(_goal(), "### Goal changes mid-flight", "## Remote dispatch (preview)")
    # The fleet case points at the existing rule rather than restating it.
    assert "**Steer corrections, queue everything else.**" in section
    assert (
        "send a correction to several workers with `session_broadcast` with `mode: steer`"
        in section
    )


def test_goal_conductor_handles_routine_incidents_itself():
    patrol = _section(_goal(), "### Patrol", "### Close the round")
    assert "**Run from the goal: handle routine incidents yourself.**" in patrol
    for incident in (
        "out of disk or memory",
        "a stuck or late worker",
        "an empty queue",
        "a CI stall",
        "a feature branch whose gates are green",
    ):
        assert incident in patrol, incident
    assert (
        "Escalate only what needs the owner, a conflict with another lead, an "
        "irreversible action, or a change to an owner ruling" in patrol
    )
    # A merge or a cleanup is recorded before it happens, with its target and reason.
    assert (
        "Before you merge a feature branch or clear build output, record the decision "
        "first: `work_ledger_record` `action=decide` on the item, or a "
        "`session_ledger_record` event, naming the PR or path, the head sha, and the reason"
        in patrol
    )


def test_goal_conductor_dispatches_remote_workers_as_visible_sessions():
    which = _section(_goal(), "#### Which agent", "### Patrol")
    assert "**Remote workers are visible sessions.**" in which
    assert "never as hidden subagents" in which


def test_goal_conductor_reads_the_woken_item_alone():
    patrol = _section(_goal(), "### Patrol", "### Close the round")
    assert "Read that item in its own call, before and apart from any send or decide" in patrol


def test_goal_conductor_records_each_slip_and_saves_lessons():
    patrol = _section(_goal(), "### Patrol", "### Close the round")
    assert "**Record each slip.**" in patrol
    assert "Record a slip of your own with `session_ledger_record`" in patrol
    assert "Read it back with `session_ledger_read` at the start of every patrol" in patrol
    assert "the time, what happened, the root cause, and the fix or guard" in patrol
    assert "check the lessons already saved, then save it as a lesson" in patrol


def test_team_lead_carries_only_the_routine_incident_delta():
    skill = _flat(TEAM_LEAD_SKILL.read_text(encoding="utf-8"))
    section = _section(skill, "## 5. Running the team itself", "## 6. When another conductor")
    assert "**Fix a routine incident yourself when it fits one turn.**" in section
    prompt = _flat(agent._TEAM_LEAD_SYSTEM_PROMPT)
    assert "Routine incidents are yours to fix" in prompt
    # The rules themselves live once, in goal-conductor, not restated here.
    assert "Run from the goal: handle routine incidents yourself" not in skill
    assert "Record each slip" not in skill

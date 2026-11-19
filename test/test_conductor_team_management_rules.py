"""The conductor skills carry five general team-management rules.

A conductor that queues a direct message behind a busy worker's turn, waits on its
manager for a routine incident, dispatches a remote worker it cannot see, acts on a
wake before reading the item it names, or repeats a slip it never wrote down, gets
slow or wrong results. These tests pin that the shared procedure (goal-conductor)
says each rule once, and that the team-lead skill and prompt carry only their delta.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import agent
from kiro_crew.skills import _BUILTIN_SKILLS_DIR

GOAL_SKILL = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "SKILL.md"
TEAM_LEAD_SKILL = Path(_BUILTIN_SKILLS_DIR) / "team-lead" / "SKILL.md"
REMOTE_REF = Path(_BUILTIN_SKILLS_DIR) / "goal-conductor" / "references" / "remote-dispatch.md"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(body: str, heading: str, next_heading: str) -> str:
    start = body.index(heading)
    end = body.index(next_heading, start + len(heading))
    return body[start:end]


def _goal() -> str:
    return _flat(GOAL_SKILL.read_text(encoding="utf-8"))


def test_goal_conductor_steers_a_working_fleet_like_one_worker():
    section = _section(_goal(), "### Goal changes mid-flight", "## When a conductor dispatched you")
    # The fleet case points at the existing rule rather than restating it.
    assert "**Steer every direct follow-up; never queue it.**" in section
    assert "use `session_broadcast` with `mode: steer`" in section


def test_goal_conductor_handles_routine_incidents_itself():
    """The incident list and the record-first rule are the shared ``routine_incidents``
    rule every conductor prompt renders; the skill points at it and keeps the rest."""
    patrol = _section(_goal(), "### Patrol", "### Close the round")
    assert "**Run from the goal: handle routine incidents yourself.**" in patrol
    assert "Your prompt's routine-incidents rule lists them" in patrol
    rule = _flat(agent._LEDGER_CONDUCTOR_RULES["routine_incidents"])
    for incident in (
        "out of disk or memory",
        "a stuck or late worker",
        "an empty queue",
        "a CI stall",
        "a feature branch whose gates are green",
    ):
        assert incident in rule, incident
    assert (
        "Escalate to the person only an ask case (a)-(e); a change to an owner ruling "
        "is (e). Send a conflict with another lead to the lead that dispatched you as "
        "`question`; with none, decide it and record it." in patrol
    )
    # A merge or a cleanup is recorded before it happens, with its target and reason.
    assert "record the decision first" in rule
    assert "naming the PR or path, the head sha and the reason" in rule
    assert "`session_ledger_record` event serves when no item fits" in patrol


def test_goal_conductor_dispatches_remote_workers_as_visible_sessions():
    ref = _flat(REMOTE_REF.read_text(encoding="utf-8"))
    assert "**Remote workers are visible sessions.**" in ref
    assert "never as hidden subagents" in ref


def _crew_descriptions(monkeypatch, enabled: bool) -> list[str]:
    from kiro_crew import mcp_dashboard, remote_dispatch

    monkeypatch.setattr(remote_dispatch, "remote_dispatch_enabled", lambda: enabled)
    out = []
    for tool in mcp_dashboard._list_tools():
        props = tool.get("inputSchema", {}).get("properties", {})
        out += [props[k]["description"] for k in ("crew", "work_item") if k in props]
    return out


def test_remote_dispatch_text_is_reachable_only_while_the_preview_is_on(monkeypatch):
    # Off: SKILL.md says nothing about crew=, and no tool names the reference file.
    assert "crew=" not in _goal() and "remote-dispatch.md" not in _goal()
    assert "Remote dispatch" not in _goal()
    off = _crew_descriptions(monkeypatch, False)
    assert not any("remote-dispatch.md" in d for d in off)
    # On: every crew / work_item description points at the file, which exists and
    # keeps the owner-turn-only rule and its refusal codes.
    on = _crew_descriptions(monkeypatch, True)
    assert len(on) == 6 and all("references/remote-dispatch.md" in d for d in on)
    ref = _flat(REMOTE_REF.read_text(encoding="utf-8"))
    assert "**Conductor mode owner turns only.**" in ref
    for code in ("not_user_turn", "conductor_mode_only", "crew_owner_unverifiable"):
        assert code in ref, code


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

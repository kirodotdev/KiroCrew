"""Fixed Crew background admission; section limits cannot add capacity."""

from __future__ import annotations

from kiro_crew import context as ctx


def test_global_cap_is_one_shared_budget():
    assert ctx._MAX_CONTEXT_CHARS == ctx._CONTEXT_BUDGET_BASE
    assert ctx._resolve_caps(None).max_context == ctx._CONTEXT_BUDGET_BASE


def test_skills_steering_do_not_add_capacity():
    assert ctx._SKILLS_CAP > 0
    assert ctx._STEERING_CAP > 0
    assert ctx._SKILLS_CAP + ctx._STEERING_CAP < ctx._MAX_CONTEXT_CHARS


def test_section_caps_are_percentages_of_base():
    assert ctx._SKILLS_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.15)
    assert ctx._STEERING_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.10)
    assert ctx._LESSONS_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.226)

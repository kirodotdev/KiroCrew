"""Fixed Crew background admission; section limits cannot add capacity."""

from __future__ import annotations

from kiro_crew import context as ctx


def test_global_cap_is_one_shared_budget():
    assert ctx._MAX_CONTEXT_CHARS == ctx._CONTEXT_BUDGET_BASE
    assert ctx._resolve_caps(None).max_context == ctx._CONTEXT_BUDGET_BASE


def test_skills_steering_do_not_add_capacity():
    # Both are protected blocks with their own allowance, outside the pool: the
    # skill index as an authored block, the folder steering section outside the
    # session-context block. Neither widens the one shared admission budget.
    assert ctx._SKILLS_CAP > 0
    assert ctx._STEERING_CAP > 0
    assert ctx._resolve_caps(None).max_context == ctx._CONTEXT_BUDGET_BASE
    assert ctx._resolve_caps(1_000_000).max_context == ctx._CONTEXT_BUDGET_BASE


def test_section_caps_are_percentages_of_base():
    assert ctx._LESSONS_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.226)
    assert ctx._SEMANTIC_MEMORY_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.077)


def test_skill_index_cap_is_a_fixed_startup_allowance():
    # Not a share of the pinned base: the index is the only path a skill's name
    # and description have to the model at session start, and 15% of the pinned
    # base named about 25 skills. Derived like the steering allowance below.
    assert ctx._SKILLS_CAP == int(ctx._HISTORY_REFERENCE_BASE * 0.15)
    assert ctx._SKILLS_CAP > ctx._budget(0.15)
    assert ctx._resolve_caps(None).skills == ctx._SKILLS_CAP
    assert ctx._resolve_caps(200_000).skills == ctx._SKILLS_CAP


def test_folder_steering_cap_is_a_fixed_startup_allowance():
    # Not a share of the pinned base: the folder section carries every-session
    # rules and is the only steering path on harnesses that do not load
    # .kiro/steering. Derived like the lessons startup allowance.
    assert ctx._STEERING_CAP == int(ctx._HISTORY_REFERENCE_BASE * 0.10)
    assert ctx._STEERING_CAP > ctx._budget(0.10)
    assert ctx._resolve_caps(None).steering == ctx._STEERING_CAP
    assert ctx._resolve_caps(200_000).steering == ctx._STEERING_CAP

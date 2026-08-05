"""Context budget invariant: the global cap is the SUM of independent
per-section caps (Joe Guo's design), so skills/steering can't eat memory space.
"""

from __future__ import annotations

from kiro_crew import context as ctx


def test_global_cap_is_sum_of_section_caps():
    expected = (
        ctx._COMPRESSED_HISTORY_CAP
        + ctx._MEMORY_PREFS_CAP
        + ctx._MEMORY_PROJECTS_CAP
        + ctx._MEMORY_HISTORY_CAP
        + ctx._SEMANTIC_MEMORY_CAP
        + ctx._EPISODIC_MEMORY_CAP
        + ctx._LESSONS_CAP
        + ctx._SKILLS_CAP
        + ctx._STEERING_CAP
        + ctx._PREAMBLE_HEADROOM
    )
    assert ctx._MAX_CONTEXT_CHARS == expected


def test_skills_steering_are_additive_not_carved_from_memory():
    # The global ceiling grows to accommodate skills/steering; it EXCEEDS the
    # base (memory keeps its sizes) rather than shrinking memory to make room.
    assert ctx._MAX_CONTEXT_CHARS > ctx._CONTEXT_BUDGET_BASE
    assert ctx._SKILLS_CAP > 0
    assert ctx._STEERING_CAP > 0


def test_section_caps_are_percentages_of_base():
    # Caps stay expressed as percentages of the fixed base (not the derived
    # global), so a growing global never silently changes memory sizes.
    assert ctx._SKILLS_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.15)
    assert ctx._STEERING_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.10)
    assert ctx._LESSONS_CAP == int(ctx._CONTEXT_BUDGET_BASE * 0.226)

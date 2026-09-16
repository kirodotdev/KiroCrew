"""Arithmetic behind the configurable compaction method (``session_compaction_methods``).

Pure functions over numbers and row shapes, so every test here is a table: no
sessions, no files, no clock.
"""

from __future__ import annotations

import pytest

from kiro_crew import session_compaction_methods as scm
from kiro_crew.config.sections import COMPACTION_METHODS


class TestConstants:
    def test_advance_outcomes_are_distinct_strings(self):
        assert {scm.OUTCOME_UNAVAILABLE, scm.OUTCOME_INSUFFICIENT} == {
            "unavailable",
            "insufficient",
        }

    def test_method_names_come_from_the_config_section(self):
        # The name set is owned by the config layer; the module re-exports it
        # rather than restating the names, so the two cannot drift apart.
        assert scm.COMPACTION_METHODS is COMPACTION_METHODS
        assert set(scm.COMPACTION_METHODS) == {
            scm.COMPACTION_METHOD_NATIVE,
            scm.COMPACTION_METHOD_SOFT,
        }

    def test_every_rotation_method_has_a_history_fate_and_native_has_none(self):
        rotations = set(COMPACTION_METHODS) - {scm.COMPACTION_METHOD_NATIVE}
        assert set(scm.ROTATION_HISTORY_FATE) == rotations


class TestTokenArithmetic:
    @pytest.mark.parametrize(
        ("chars", "expected"),
        [(0, 0), (-5, 0), (1, 1), (4, 1), (5, 2), (80_000, 20_000)],
    )
    def test_estimate_rounds_up(self, chars, expected):
        assert scm.estimate_tokens(chars) == expected

    def test_no_second_tail_budget_exists(self):
        # The tail figure is the replay's own (``replay.replay_budget_chars``);
        # a module-local token->chars budget would be a second number, so its
        # absence is pinned.
        assert not hasattr(scm, "tail_budget_chars")


class TestProjection:
    def test_unknown_window_yields_no_projection(self):
        assert scm.project_pct_after(window_tokens=0, carried_tokens=1000) is None
        assert scm.project_pct_after(window_tokens=-1, carried_tokens=1000) is None

    def test_projection_is_carried_plus_overhead_over_window(self):
        pct = scm.project_pct_after(
            window_tokens=1_000_000, carried_tokens=20_000, overhead_tokens=40_000
        )
        assert pct == pytest.approx(6.0)

    def test_projection_clamps_at_one_hundred(self):
        assert scm.project_pct_after(window_tokens=100, carried_tokens=500) == 100.0

    def test_negative_inputs_count_as_zero(self):
        assert scm.project_pct_after(window_tokens=100, carried_tokens=-7) == 0.0

    @pytest.mark.parametrize(
        ("pct_after", "sufficient"),
        [
            (None, False),  # no figure, no verdict
            (90.0, False),  # at the threshold
            (86.0, False),  # inside the min-effect band
            (85.0, True),  # exactly the margin
            (10.0, True),
        ],
    )
    def test_sufficiency_requires_the_min_effect_margin(self, pct_after, sufficient):
        assert (
            scm.method_sufficient(pct_after, threshold_pct=90.0, min_effect_pct_points=5.0)
            is sufficient
        )


class TestIsRotationNotice:
    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            (
                {
                    "role": "assistant",
                    "meta": {"kind": "compaction", scm.ROTATION_NOTICE_META_KEY: "soft"},
                },
                True,
            ),
            ({"role": "assistant", "meta": {"kind": "compaction"}}, False),
            ({"role": "user", "meta": {"kind": "compaction", "rotation_method": "soft"}}, False),
            ({"role": "assistant", "meta": {"rotation_method": "soft"}}, False),
            ({"role": "assistant", "meta": "not a mapping"}, False),
            ({"role": "assistant"}, False),
        ],
    )
    def test_only_the_whole_dashboard_shape_counts(self, row, expected):
        assert scm.is_rotation_notice(row) is expected

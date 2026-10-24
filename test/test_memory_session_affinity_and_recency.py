"""Tests for session affinity (default on) and opt-in recency boost."""

from kiro_crew.memory_v2 import (
    RECENCY_HALF_WEIGHT_DAYS,
    SESSION_AFFINITY_BOOST,
    rank_score,
)


def _evidence(cosine: float = 0.8, coverage: float = 0.5) -> dict:
    return {"cosine": cosine, "query_coverage": coverage}


class TestBackwardCompatibility:
    """Omitting all new kwargs must produce the exact original score."""

    def test_no_new_args_matches_original(self):
        score = rank_score(_evidence(), importance=0.5)
        expected_relevance = 0.7 * 0.8 + 0.3 * 0.5
        expected = round(expected_relevance * (0.85 + 0.15 * 0.5), 4)
        assert score == expected

    def test_explicit_defaults_match_original(self):
        default = rank_score(_evidence(), importance=0.5)
        explicit = rank_score(
            _evidence(), importance=0.5,
            same_session=False, recency_boost=0.0, age_days=None,
        )
        assert default == explicit


class TestSessionAffinity:
    """Session affinity is always on — no config knob needed."""

    def test_same_session_beats_different(self):
        same = rank_score(_evidence(), importance=0.5, same_session=True)
        diff = rank_score(_evidence(), importance=0.5, same_session=False)
        assert same > diff

    def test_affinity_is_exact_boost(self):
        same = rank_score(_evidence(), importance=0.5, same_session=True)
        diff = rank_score(_evidence(), importance=0.5, same_session=False)
        assert abs((same - diff) - SESSION_AFFINITY_BOOST) < 0.001

    def test_affinity_cannot_promote_irrelevant(self):
        irrelevant_same = rank_score(
            _evidence(cosine=0.1, coverage=0.0), importance=0.2, same_session=True,
        )
        relevant_diff = rank_score(
            _evidence(cosine=0.85, coverage=0.8), importance=0.7, same_session=False,
        )
        assert relevant_diff > irrelevant_same


class TestOptInRecency:
    """Recency boost is opt-in: recency_boost=0 (default) means no change."""

    def test_zero_boost_means_no_recency(self):
        """With recency_boost=0 (the default), age has no effect."""
        today = rank_score(_evidence(), importance=0.5, recency_boost=0.0, age_days=0.0)
        old = rank_score(_evidence(), importance=0.5, recency_boost=0.0, age_days=30.0)
        no_age = rank_score(_evidence(), importance=0.5, recency_boost=0.0)
        assert today == old == no_age

    def test_enabled_today_beats_old(self):
        """When enabled, recent entries score higher."""
        today = rank_score(_evidence(), importance=0.5, recency_boost=0.15, age_days=0.0)
        old = rank_score(_evidence(), importance=0.5, recency_boost=0.15, age_days=30.0)
        assert today > old

    def test_enabled_boost_is_bounded(self):
        """The boost cannot exceed the configured recency_boost value."""
        no_age = rank_score(_evidence(), importance=0.5)
        today = rank_score(_evidence(), importance=0.5, recency_boost=0.15, age_days=0.0)
        ratio = today / no_age
        assert 1.0 < ratio <= 1.15 + 0.01

    def test_enabled_half_weight(self):
        """At RECENCY_HALF_WEIGHT_DAYS, boost is half the max."""
        no_age = rank_score(_evidence(), importance=0.5)
        today = rank_score(_evidence(), importance=0.5, recency_boost=0.15, age_days=0.0)
        week = rank_score(
            _evidence(), importance=0.5,
            recency_boost=0.15, age_days=RECENCY_HALF_WEIGHT_DAYS,
        )
        today_boost = today - no_age
        week_boost = week - no_age
        assert abs(week_boost - today_boost / 2) < 0.005

    def test_strong_old_still_wins_even_with_max_boost(self):
        """Even with recency enabled, a strong old match beats a weak recent one."""
        strong_old = rank_score(
            _evidence(cosine=0.95, coverage=0.9), importance=0.8,
            recency_boost=0.15, age_days=60.0,
        )
        weak_new = rank_score(
            _evidence(cosine=0.4, coverage=0.2), importance=0.3,
            recency_boost=0.15, age_days=0.0,
        )
        assert strong_old > weak_new

    def test_custom_boost_value(self):
        """Users can set a different boost value (e.g. 0.30 for heavy recency)."""
        low = rank_score(_evidence(), importance=0.5, recency_boost=0.05, age_days=0.0)
        high = rank_score(_evidence(), importance=0.5, recency_boost=0.30, age_days=0.0)
        no_boost = rank_score(_evidence(), importance=0.5)
        assert no_boost < low < high


class TestAffinityRecencyIndependence:
    """Verify affinity is truly independent of the recency knob (Opus 5.5 review fix)."""

    def test_affinity_independent_of_recency_boost(self):
        """Session affinity adds exactly SESSION_AFFINITY_BOOST regardless of recency_boost."""
        for boost in (0.0, 0.5, 1.0):
            same = rank_score(
                _evidence(), importance=0.5,
                same_session=True, recency_boost=boost, age_days=0.0,
            )
            diff = rank_score(
                _evidence(), importance=0.5,
                same_session=False, recency_boost=boost, age_days=0.0,
            )
            delta = round(same - diff, 4)
            assert abs(delta - SESSION_AFFINITY_BOOST) < 0.001, (
                f"At recency_boost={boost}, affinity delta was {delta}, "
                f"expected {SESSION_AFFINITY_BOOST}"
            )


class TestReviewerScenarioBoost1:
    """Opus 5.5 reviewer scenario: boost=1.0, weak-new-same vs strong-old-other."""

    def test_strong_old_other_beats_weak_new_same_at_max_boost(self):
        """At recency_boost=1.0, a 2.1x-stronger old entry from another session
        must still win, matching the documented win condition."""
        weak_new_same = rank_score(
            _evidence(cosine=0.45, coverage=0.3), importance=0.4,
            same_session=True, recency_boost=1.0, age_days=0.0,
        )
        strong_old_other = rank_score(
            _evidence(cosine=0.90, coverage=0.8), importance=0.8,
            same_session=False, recency_boost=1.0, age_days=60.0,
        )
        assert strong_old_other > weak_new_same

    def test_affinity_does_not_double_with_recency(self):
        """The exact scenario from the review: base 0.40 (today, same) vs
        base 0.84 (60d old, other), boost=1.0. The old entry must win."""
        # Construct evidence that yields base ~0.40 and ~0.84
        # base = (0.7*cos + 0.3*cov) * (0.85 + 0.15*imp)
        # For base ~0.40: cos=0.5, cov=0.2, imp=0.3 -> (0.35+0.06)*0.895 = 0.367
        # For base ~0.84: cos=0.95, cov=0.8, imp=0.8 -> (0.665+0.24)*0.97 = 0.878
        new_same = rank_score(
            _evidence(cosine=0.5, coverage=0.2), importance=0.3,
            same_session=True, recency_boost=1.0, age_days=0.0,
        )
        old_other = rank_score(
            _evidence(cosine=0.95, coverage=0.8), importance=0.8,
            same_session=False, recency_boost=1.0, age_days=60.0,
        )
        assert old_other > new_same, (
            f"Old strong entry ({old_other}) should beat new weak same-session ({new_same})"
        )
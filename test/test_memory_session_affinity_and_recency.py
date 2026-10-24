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

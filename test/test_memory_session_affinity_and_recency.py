"""Tests for opt-in session affinity and opt-in recency boost."""

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
            _evidence(),
            importance=0.5,
            same_session=False,
            session_affinity=0.0,
            recency_boost=0.0,
            age_days=None,
        )
        assert default == explicit


class TestSessionAffinity:
    """Session affinity is opt-in — requires session_affinity > 0."""

    def test_default_zero_means_no_affinity(self):
        """With session_affinity=0 (the default), same_session has no effect."""
        same = rank_score(_evidence(), importance=0.5, same_session=True, session_affinity=0.0)
        diff = rank_score(_evidence(), importance=0.5, same_session=False, session_affinity=0.0)
        assert same == diff

    def test_enabled_same_session_beats_different(self):
        same = rank_score(_evidence(), importance=0.5, same_session=True, session_affinity=0.10)
        diff = rank_score(_evidence(), importance=0.5, same_session=False, session_affinity=0.10)
        assert same > diff

    def test_affinity_is_exact_boost(self):
        same = rank_score(_evidence(), importance=0.5, same_session=True, session_affinity=0.10)
        diff = rank_score(_evidence(), importance=0.5, same_session=False, session_affinity=0.10)
        assert abs((same - diff) - 0.10) < 0.001

    def test_affinity_capped_at_session_affinity_boost(self):
        """Even if a larger value is passed, it is clamped to SESSION_AFFINITY_BOOST."""
        same = rank_score(_evidence(), importance=0.5, same_session=True, session_affinity=0.50)
        diff = rank_score(_evidence(), importance=0.5, same_session=False, session_affinity=0.50)
        assert abs((same - diff) - SESSION_AFFINITY_BOOST) < 0.001

    def test_affinity_cannot_promote_irrelevant(self):
        irrelevant_same = rank_score(
            _evidence(cosine=0.1, coverage=0.0),
            importance=0.2,
            same_session=True,
            session_affinity=0.10,
        )
        relevant_diff = rank_score(
            _evidence(cosine=0.85, coverage=0.8),
            importance=0.7,
            same_session=False,
            session_affinity=0.10,
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
            _evidence(),
            importance=0.5,
            recency_boost=0.15,
            age_days=RECENCY_HALF_WEIGHT_DAYS,
        )
        today_boost = today - no_age
        week_boost = week - no_age
        assert abs(week_boost - today_boost / 2) < 0.005

    def test_enabled_falls_off_hyperbolically(self):
        """At 14 days a third of the boost is left, not the quarter halving would give."""
        base = rank_score(_evidence(), importance=0.5)
        today = rank_score(_evidence(), importance=0.5, recency_boost=0.15, age_days=0.0)
        fortnight = rank_score(_evidence(), importance=0.5, recency_boost=0.15, age_days=14.0)
        boost, left = today - base, fortnight - base
        assert abs(left - boost / 3) < 1e-4
        assert abs(left - boost / 4) > 1e-3

    def test_strong_old_still_wins_even_with_max_boost(self):
        """Even with recency enabled, a strong old match beats a weak recent one."""
        strong_old = rank_score(
            _evidence(cosine=0.95, coverage=0.9),
            importance=0.8,
            recency_boost=0.15,
            age_days=60.0,
        )
        weak_new = rank_score(
            _evidence(cosine=0.4, coverage=0.2),
            importance=0.3,
            recency_boost=0.15,
            age_days=0.0,
        )
        assert strong_old > weak_new

    def test_at_the_cap_a_recent_entry_can_outrank_a_much_stronger_old_one(self):
        """At recency_boost=1.0 the bound the help text states is the real one."""
        recent = rank_score(
            _evidence(cosine=0.5, coverage=0.5), importance=0.5, recency_boost=1.0, age_days=0.0
        )
        old = rank_score(
            _evidence(cosine=0.9, coverage=0.9), importance=0.5, recency_boost=1.0, age_days=60.0
        )
        assert recent == 0.925
        assert old == 0.9195
        assert recent > old
        # The lift never exceeds 1 + boost, so an old entry scoring more than
        # (1 + boost) times higher still wins.
        unboosted = rank_score(_evidence(cosine=0.5, coverage=0.5), importance=0.5)
        assert recent <= unboosted * 2.0


class TestAffinityRecencyIndependence:
    """Verify affinity is truly independent of the recency knob."""

    def test_affinity_independent_of_recency_boost(self):
        """Session affinity adds the same delta regardless of recency_boost."""
        for boost in (0.0, 0.5, 1.0):
            same = rank_score(
                _evidence(),
                importance=0.5,
                same_session=True,
                session_affinity=0.10,
                recency_boost=boost,
                age_days=0.0,
            )
            diff = rank_score(
                _evidence(),
                importance=0.5,
                same_session=False,
                session_affinity=0.10,
                recency_boost=boost,
                age_days=0.0,
            )
            delta = round(same - diff, 4)
            assert abs(delta - 0.10) < 0.001, (
                f"At recency_boost={boost}, affinity delta was {delta}, " f"expected 0.10"
            )


class TestRecallQueryForwarding:
    """The ``recall_query`` path re-enters ``search_episodic``; it must keep both boosts."""

    def test_recall_query_path_forwards_the_ranking_settings(self):
        import threading

        from kiro_crew.vector_memory_runtime import episodic_search
        from kiro_crew.vector_memory_runtime.embedding import _RecallQuery

        seen: dict = {}

        class _Store:
            algorithm_version = "v2"
            _db_lock = threading.RLock()

            def _check_recall_query(self, query):
                return None

            def search_episodic(self, *args, **kwargs):
                return episodic_search.search_episodic(self, *args, **kwargs)

            def _search_episodic_v2(self, *args, **kwargs):
                seen.update(kwargs)
                return []

        episodic_search.search_episodic(
            _Store(),
            None,
            "which bug did we fix",
            recall_query=_RecallQuery(None, None, None),
            conversation_id="chat-7",
            session_affinity=0.1,
            recency_boost=0.15,
        )
        assert seen["conversation_id"] == "chat-7"
        assert seen["session_affinity"] == 0.1
        assert seen["recency_boost"] == 0.15


def test_recall_with_default_settings_ignores_conversation_id(tmp_path, monkeypatch):
    """With both settings at 0, recall() is the same with and without conversation_id."""
    from member_memory_helpers import declare_v2_store

    from kiro_crew import memory_stores
    from kiro_crew.vector_memory import open_member_database

    monkeypatch.setattr(memory_stores, "memory_stores_root", lambda: tmp_path / "memory_stores")
    directory = declare_v2_store(tmp_path, "member-alice")
    store = open_member_database(
        directory / "memory.db", member_id="alice", store_id="member-alice", embedding_dim=2
    )
    try:
        # The other session's episode carries more importance, so it ranks first by
        # default; a nonzero affinity would lift the same-session one above it.
        assert store.write_episodic(
            "The nightly report job exports the trading ledger.",
            conversation_id="chat-other",
            importance=0.9,
            defer_embedding=True,
        )
        assert store.write_episodic(
            "The nightly report job exports the project ledger.",
            conversation_id="chat-mine",
            importance=0.5,
            defer_embedding=True,
        )
        plain = store.recall("nightly report ledger", cap=3000)
        scoped = store.recall("nightly report ledger", cap=3000, conversation_id="chat-mine")
        assert plain["retrieval"]["episodes"], "both episodes should be admitted"
        assert scoped == plain
    finally:
        store.close()

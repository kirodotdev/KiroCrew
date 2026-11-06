"""Tests for the contradiction detection heuristic."""

from kiro_crew.vector_memory_runtime.contradiction import (
    _CONTRADICTION_COSINE_FLOOR,
    texts_contradict,
)


class TestContradictionDetection:

    def test_correction_language(self):
        c, r = texts_contradict(
            "The deployment is scheduled for Friday.",
            "Actually the deployment is scheduled for Monday, not Friday.",
        )
        assert c

    def test_value_conflict(self):
        c, r = texts_contradict("deadline: Friday", "deadline: Monday")
        assert c and "value_conflict" in r

    def test_negation_asymmetry(self):
        c, r = texts_contradict(
            "The service is running in production.",
            "The service is not running in production.",
        )
        assert c

    def test_different_topics_not_flagged(self):
        c, _ = texts_contradict(
            "We use Python for the backend.",
            "The frontend uses React with TypeScript.",
        )
        assert not c

    def test_complementary_not_flagged(self):
        c, _ = texts_contradict(
            "The API supports GET requests.",
            "The API also supports POST requests.",
        )
        assert not c

    def test_scratch_that(self):
        c, _ = texts_contradict(
            "Use the staging environment for testing.",
            "Scratch that, use the dev environment for testing instead.",
        )
        assert c

    def test_on_second_thought(self):
        c, _ = texts_contradict(
            "We should use DynamoDB for the cache.",
            "On second thought, we should use Redis for the cache, not DynamoDB.",
        )
        assert c

    def test_identical_not_flagged(self):
        c, _ = texts_contradict("The build uses gradle.", "The build uses gradle.")
        assert not c

    def test_is_vs_isnt(self):
        c, _ = texts_contradict(
            "The feature is enabled in production.",
            "The feature isn't enabled in production.",
        )
        assert c

    def test_cosine_floor_is_sensible(self):
        assert 0.75 <= _CONTRADICTION_COSINE_FLOOR < 1.0

    def test_dry_run_default(self):
        """sweep_contradictions defaults to dry_run=True — verify the function
        signature accepts it (we can't test the full sweep without a store)."""
        from kiro_crew.vector_memory_runtime.contradiction import sweep_contradictions
        import inspect
        sig = inspect.signature(sweep_contradictions)
        assert sig.parameters["dry_run"].default is True

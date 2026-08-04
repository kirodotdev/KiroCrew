"""Tests for the SQLite store timing helpers."""

from __future__ import annotations

import pytest

from kiro_crew.metrics import db_metrics


class _Rec:
    """Recorder double capturing histogram calls."""

    def __init__(self, explode: bool = False) -> None:
        self.calls: list[tuple[str, float, dict]] = []
        self.explode = explode

    def histogram(self, name, value, *, unit="ms", attrs=None, **_kw):
        if self.explode:
            raise RuntimeError("recorder down")
        self.calls.append((name, value, dict(attrs or {})))


@pytest.fixture()
def rec(monkeypatch):
    r = _Rec()
    monkeypatch.setattr(db_metrics, "get_recorder", lambda: r)
    return r


class TestLabels:
    def test_known_values_pass_through(self):
        assert db_metrics.store_label("memory") == "memory"
        assert db_metrics.op_label("search") == "search"

    def test_unknown_values_collapse_rather_than_pass_through(self):
        """Unbounded labels would blow up series cardinality."""
        assert db_metrics.store_label("some-new-store") == "other"
        assert db_metrics.op_label("SELECT * FROM t") == "other"

    def test_cardinality_stays_a_small_constant(self):
        # stores x ops x 2 outcomes. The point of the fixed sets is that this
        # number cannot grow with traffic or with what SQL is executed.
        assert len(db_metrics.STORES) * len(db_metrics.OPS) * 2 <= 100


class TestTimedQuery:
    def test_records_metric_name_labels_and_a_duration(self, rec):
        with db_metrics.timed_query("memory", "search"):
            pass
        assert len(rec.calls) == 1
        name, value, attrs = rec.calls[0]
        assert name == db_metrics.DB_METRIC
        assert value >= 0.0
        assert attrs == {"store": "memory", "op": "search", "outcome": "ok"}

    def test_a_raising_operation_is_still_timed_and_tagged_error(self, rec):
        with pytest.raises(ValueError):
            with db_metrics.timed_query("knowledge", "write"):
                raise ValueError("boom")
        assert rec.calls[0][2]["outcome"] == "error"

    def test_the_callers_exception_propagates_unchanged(self, rec):
        sentinel = ValueError("original")
        with pytest.raises(ValueError) as exc:
            with db_metrics.timed_query("memory", "search"):
                raise sentinel
        assert exc.value is sentinel

    def test_basexception_is_tagged_and_re_raised(self, rec):
        """A cancelled or interrupted operation is still an outcome."""
        with pytest.raises(KeyboardInterrupt):
            with db_metrics.timed_query("memory", "search"):
                raise KeyboardInterrupt
        assert rec.calls[0][2]["outcome"] == "error"

    def test_a_failing_recorder_never_breaks_the_store_call(self, monkeypatch):
        """Telemetry must not be able to fail a DB operation."""
        monkeypatch.setattr(db_metrics, "get_recorder", lambda: _Rec(explode=True))
        ran = []
        with db_metrics.timed_query("memory", "search"):
            ran.append(True)
        assert ran == [True]  # no exception escaped

    def test_a_recorder_that_cannot_be_obtained_is_also_contained(self, monkeypatch):
        def _boom():
            raise RuntimeError("no provider")

        monkeypatch.setattr(db_metrics, "get_recorder", _boom)
        with db_metrics.timed_query("memory", "search"):
            pass  # must not raise


class TestTimedDecorator:
    def test_wraps_a_method_and_preserves_its_result(self, rec):
        @db_metrics.timed("vector", "search")
        def search(n):
            return n * 2

        assert search(21) == 42
        assert rec.calls[0][2] == {"store": "vector", "op": "search", "outcome": "ok"}

    def test_preserves_identity_for_introspection(self, rec):
        @db_metrics.timed("vector", "write")
        def set_semantic(self, key):
            """docstring."""

        assert set_semantic.__name__ == "set_semantic"
        assert set_semantic.__doc__ == "docstring."

    def test_tags_error_and_re_raises(self, rec):
        @db_metrics.timed("vector", "write")
        def broken():
            raise KeyError("k")

        with pytest.raises(KeyError):
            broken()
        assert rec.calls[0][2]["outcome"] == "error"

    def test_refuses_an_async_function_loudly(self):
        """Wrapping a coroutine function would time its CREATION, not its run.

        Silently recording ~0ms for every async call is worse than not measuring:
        it looks like the DB is instant.
        """
        with pytest.raises(TypeError) as exc:

            @db_metrics.timed("memory", "read")
            async def fetch():
                return 1

        assert "coroutine" in str(exc.value)


class TestBucketRegistration:
    def test_the_histogram_has_explicit_buckets(self):
        """A histogram missing from the registry silently falls back to OTEL's
        10s-ceiling defaults, which floors every reported percentile at the top
        bound. The provider's own guard test enforces this too; this pins the
        specific metric."""
        from kiro_crew.metrics.provider import _HISTOGRAM_BUCKETS_MS

        assert db_metrics.DB_METRIC in _HISTOGRAM_BUCKETS_MS

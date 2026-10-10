"""Tests for the catalog ranking document: fetch, cache, fallback, and no-data guards.

These tests verify every hard requirement:
1. k=fresh only (verified by schema design: only fresh counts in schema)
2. Cumulative + windowed support (trending view)
3. Uncounted apps get NO rank, never zero
4. Never trust a zero (empty/all-zero is NO DATA)
5. Rank is not self-assertable (no registry field can influence rank)
6. Stale-safe cache with fallback
7. Aggregates only (schema carries counts, nothing else)
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps import catalog_ranking
from kiro_crew.apps.catalog_ranking import (
    _is_empty_or_all_zero,
    _parse_document,
    _safe_count,
    _validate_document,
    load_ranking_document,
)


@pytest.fixture
def tmp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override config_dir to use a temp directory."""
    monkeypatch.setattr(catalog_ranking, "config_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def valid_ranking_doc() -> dict[str, Any]:
    """A valid ranking document with non-zero counts."""
    return {
        "schemaVersion": 1,
        "generatedAt": "2026-10-07T12:00:00Z",
        "windowDays": 30,
        "rankings": [
            {"appSlug": "alpha-app", "cumulativeInstalls": 1500, "windowedInstalls": 120},
            {"appSlug": "beta-app", "cumulativeInstalls": 980, "windowedInstalls": 250},
            {"appSlug": "gamma-app", "cumulativeInstalls": 750, "windowedInstalls": 45},
        ],
    }


def _make_stale(cache_path: Path) -> None:
    """Age a cache file past CACHE_TTL so a load treats it as stale."""
    old = time.time() - catalog_ranking.CACHE_TTL - 100
    import os

    os.utime(cache_path, (old, old))


def _write_doc_cache(tmp_config_dir: Path, doc: dict[str, Any], *, stale: bool) -> Path:
    """Write *doc* to the document cache, optionally aging it to stale."""
    cache_path = tmp_config_dir / "cache" / "catalog-ranking.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(doc), encoding="utf-8")
    if stale:
        _make_stale(cache_path)
    return cache_path


class TestDocumentValidation:
    """Test the document validation logic."""

    def test_valid_document_passes(self, valid_ranking_doc: dict[str, Any]) -> None:
        """A well-formed document passes validation."""
        assert _validate_document(valid_ranking_doc) is None

    def test_unsupported_schema_version_fails(self, valid_ranking_doc: dict[str, Any]) -> None:
        """An unknown schema version is rejected."""
        valid_ranking_doc["schemaVersion"] = 99
        error = _validate_document(valid_ranking_doc)
        assert error is not None
        assert "schemaVersion" in error

    def test_missing_generated_at_fails(self, valid_ranking_doc: dict[str, Any]) -> None:
        """Missing generatedAt is rejected."""
        del valid_ranking_doc["generatedAt"]
        error = _validate_document(valid_ranking_doc)
        assert error is not None
        assert "generatedAt" in error

    def test_invalid_window_days_fails(self, valid_ranking_doc: dict[str, Any]) -> None:
        """Non-positive windowDays is rejected."""
        valid_ranking_doc["windowDays"] = 0
        error = _validate_document(valid_ranking_doc)
        assert error is not None
        assert "windowDays" in error

        valid_ranking_doc["windowDays"] = -1
        error = _validate_document(valid_ranking_doc)
        assert error is not None

    def test_rankings_must_be_list(self, valid_ranking_doc: dict[str, Any]) -> None:
        """rankings field must be a list."""
        valid_ranking_doc["rankings"] = {"alpha-app": 100}
        error = _validate_document(valid_ranking_doc)
        assert error is not None
        assert "list" in error


class TestSafeCount:
    """Test the count-type coercion that keeps malformed counts from raising."""

    def test_valid_non_negative_int_passes(self) -> None:
        assert _safe_count(100) == 100
        assert _safe_count(0) == 0

    def test_non_int_types_become_zero(self) -> None:
        """A string, None, or float count coerces to 0 rather than raising."""
        assert _safe_count("100") == 0
        assert _safe_count(None) == 0
        assert _safe_count(0.5) == 0
        assert _safe_count(5.0) == 0

    def test_bool_is_not_a_count(self) -> None:
        """bool subclasses int but is not a valid count."""
        assert _safe_count(True) == 0
        assert _safe_count(False) == 0

    def test_negative_becomes_zero(self) -> None:
        assert _safe_count(-10) == 0


class TestEmptyOrAllZeroGuard:
    """Test the 'never trust a zero' guard.

    An empty or all-zero document is NO DATA, not a legitimate ranking of
    zeros: a rollup over an empty partition can report success and publish all
    zeros, and the guard rejects that input so it never overwrites the last
    good ranking.
    """

    def test_empty_rankings_is_no_data(self) -> None:
        """An empty rankings list is treated as NO DATA."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [],
        }
        assert _is_empty_or_all_zero(doc) is True

    def test_all_zero_counts_is_no_data(self) -> None:
        """All entries with zero counts is treated as NO DATA."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
                {"appSlug": "beta-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
            ],
        }
        assert _is_empty_or_all_zero(doc) is True

    def test_at_least_one_nonzero_is_valid(self) -> None:
        """A document with at least one non-zero count is valid."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
                {"appSlug": "beta-app", "cumulativeInstalls": 1, "windowedInstalls": 0},
            ],
        }
        assert _is_empty_or_all_zero(doc) is False

    def test_windowed_nonzero_is_valid(self) -> None:
        """A non-zero windowed count is sufficient."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 5},
            ],
        }
        assert _is_empty_or_all_zero(doc) is False

    def test_malformed_count_does_not_raise(self) -> None:
        """A string/null count is read as absence, not a crash, in the guard."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": "100", "windowedInstalls": None},
            ],
        }
        # No TypeError, and the unusable counts read as no data.
        assert _is_empty_or_all_zero(doc) is True

    def test_parse_empty_returns_none(self) -> None:
        """_parse_document returns None for empty/all-zero documents."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [],
        }
        result = _parse_document(doc)
        assert result is None


class TestNoRankNotZeroRank:
    """Test that uncounted apps get NO rank, never zero.

    This is hard requirement #3: an app not in the ranking document should
    not appear at the bottom of a popularity list.
    """

    def test_missing_app_returns_none(self, valid_ranking_doc: dict[str, Any]) -> None:
        """An app not in the document returns None, not zero."""
        ranking = _parse_document(valid_ranking_doc)
        assert ranking is not None

        # App exists.
        assert ranking.get_cumulative_rank("alpha-app") == 1500
        assert ranking.get_windowed_rank("alpha-app") == 120

        # App does not exist: returns None, not zero.
        assert ranking.get_cumulative_rank("nonexistent-app") is None
        assert ranking.get_windowed_rank("nonexistent-app") is None

    def test_zero_count_entries_are_filtered(self) -> None:
        """Entries with zero counts are filtered out, not returned as zero."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 100, "windowedInstalls": 10},
                # This entry has zero counts and should be filtered.
                {"appSlug": "zero-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        # alpha-app is present.
        assert ranking.get_cumulative_rank("alpha-app") == 100

        # zero-app should return None, not zero.
        assert ranking.get_cumulative_rank("zero-app") is None

    def test_malformed_count_entry_is_unranked_not_zero(self) -> None:
        """An entry whose only counts are malformed is unranked, never zero-ranked."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "good-app", "cumulativeInstalls": 100, "windowedInstalls": 10},
                # A float slips past no zero-filter and must not clamp to a 0 rank.
                {"appSlug": "float-app", "cumulativeInstalls": 0.5, "windowedInstalls": 0.0},
                # A string count must not raise.
                {"appSlug": "str-app", "cumulativeInstalls": "999", "windowedInstalls": None},
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        assert ranking.get_cumulative_rank("good-app") == 100
        # Neither malformed entry becomes a zero-ranked app.
        assert ranking.get_cumulative_rank("float-app") is None
        assert ranking.get_cumulative_rank("str-app") is None


class TestRankNotSelfAssertable:
    """Test that rank cannot be influenced by registry fields.

    Hard requirement #5: NO registry-supplied field may influence rank.
    The ranking document is the ONLY source of rank.
    """

    def test_registry_entry_cannot_set_rank(self) -> None:
        """A registry entry with a 'rank' field does not affect ranking.

        This test documents the contract: ranking comes ONLY from the ranking
        document. The code under test (catalog_ranking.py) does not read from
        registry entries at all, so this test verifies the interface contract.
        """
        ranking = _parse_document(
            {
                "schemaVersion": 1,
                "generatedAt": "2026-10-07T12:00:00Z",
                "windowDays": 30,
                "rankings": [
                    {"appSlug": "alpha-app", "cumulativeInstalls": 50, "windowedInstalls": 5},
                ],
            }
        )
        assert ranking is not None

        # A registry entry's self-asserted fields have no effect: an app not in
        # the ranking document has no rank.
        assert ranking.get_cumulative_rank("malicious-app") is None

        # Only apps in the ranking document have ranks.
        assert ranking.get_cumulative_rank("alpha-app") == 50


class TestCacheAndFallback:
    """Test the cache-and-fallback behavior mirroring the registry index."""

    def test_fresh_cache_is_used(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A fresh cache hit does not fetch from network."""
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=False)

        # The fetcher should not be called.
        fetcher = mock.Mock(return_value=None)
        ranking = load_ranking_document(fetcher=fetcher)

        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500
        fetcher.assert_not_called()

    def test_stale_cache_triggers_fetch(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A stale cache triggers a network fetch."""
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)

        # Fetcher returns new data.
        new_doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-08T12:00:00Z",
            "windowDays": 30,
            "rankings": [{"appSlug": "new-app", "cumulativeInstalls": 999, "windowedInstalls": 99}],
        }
        fetcher = mock.Mock(return_value=new_doc)

        ranking = load_ranking_document(fetcher=fetcher)

        assert ranking is not None
        assert ranking.get_cumulative_rank("new-app") == 999
        fetcher.assert_called_once()

    def test_fetch_failure_falls_back_to_stale(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A fetch failure falls back to stale cache."""
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)

        # Fetcher fails.
        fetcher = mock.Mock(return_value=None)

        ranking = load_ranking_document(fetcher=fetcher)

        # Should fall back to stale cache.
        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500

    def test_no_cache_no_fallback_returns_none(self, tmp_config_dir: Path) -> None:
        """No cache and fetch failure returns None."""
        fetcher = mock.Mock(return_value=None)

        ranking = load_ranking_document(fetcher=fetcher)

        assert ranking is None

    def test_invalid_document_falls_back_to_stale(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """An invalid new document falls back to stale cache.

        This implements the 'absent ranking beats a wrong one' contract.
        """
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)

        # Fetcher returns an invalid document (all zeros: NO DATA).
        invalid_doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-08T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
            ],
        }
        fetcher = mock.Mock(return_value=invalid_doc)

        ranking = load_ranking_document(fetcher=fetcher)

        # Should fall back to stale cache, not use the invalid document.
        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500

    def test_fetch_failure_preserves_last_good_cache(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A failure must not overwrite the last-good document on disk.

        The failure marker lives in its own file, so the stale ranking survives
        a failed fetch and remains available to every later call. A stale ranking
        beats none.
        """
        cache_path = _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)
        fetcher = mock.Mock(return_value=None)

        load_ranking_document(fetcher=fetcher)

        # The document cache file is intact (not replaced by a failure sentinel).
        on_disk = json.loads(cache_path.read_text(encoding="utf-8"))
        assert on_disk == valid_ranking_doc
        # A separate failure marker was written.
        assert (tmp_config_dir / "cache" / "catalog-ranking.failed").is_file()

    def test_failure_ttl_prevents_hammering_and_keeps_stale(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A recent failure backs off AND still serves the stale ranking.

        Because the failure marker lives in its own file apart from the document
        cache, the second call within FAILURE_TTL does not fetch again (no
        hammering) and still returns the stale ranking rather than None.
        """
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)

        # First call: fetcher fails but falls back to stale cache.
        fetcher = mock.Mock(return_value=None)
        ranking1 = load_ranking_document(fetcher=fetcher)
        assert ranking1 is not None
        assert ranking1.get_cumulative_rank("alpha-app") == 1500
        assert fetcher.call_count == 1

        # Second call within FAILURE_TTL: backs off (no new fetch) and the stale
        # ranking is still available.
        ranking2 = load_ranking_document(fetcher=fetcher)
        assert ranking2 is not None
        assert ranking2.get_cumulative_rank("alpha-app") == 1500
        assert fetcher.call_count == 1

    def test_expired_failure_marker_allows_refetch(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """Once the failure marker ages past FAILURE_TTL, a fetch is attempted again."""
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)

        # Simulate an old failure marker.
        failure_path = tmp_config_dir / "cache" / "catalog-ranking.failed"
        failure_path.write_text(str(time.time()), encoding="utf-8")
        old = time.time() - catalog_ranking.FAILURE_TTL - 100
        import os

        os.utime(failure_path, (old, old))

        new_doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-08T12:00:00Z",
            "windowDays": 30,
            "rankings": [{"appSlug": "new-app", "cumulativeInstalls": 42, "windowedInstalls": 7}],
        }
        fetcher = mock.Mock(return_value=new_doc)

        ranking = load_ranking_document(fetcher=fetcher)

        assert ranking is not None
        assert ranking.get_cumulative_rank("new-app") == 42
        fetcher.assert_called_once()

    def test_successful_fetch_clears_failure_marker(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A successful fetch writes the cache and clears any failure memory."""
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)
        failure_path = tmp_config_dir / "cache" / "catalog-ranking.failed"

        # First fetch fails and records a failure.
        load_ranking_document(fetcher=mock.Mock(return_value=None))
        assert failure_path.is_file()

        # Age the marker so the next call does not back off.
        old = time.time() - catalog_ranking.FAILURE_TTL - 100
        import os

        os.utime(failure_path, (old, old))

        new_doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-08T12:00:00Z",
            "windowDays": 30,
            "rankings": [{"appSlug": "new-app", "cumulativeInstalls": 10, "windowedInstalls": 1}],
        }
        ranking = load_ranking_document(fetcher=mock.Mock(return_value=new_doc))

        assert ranking is not None
        # The failure marker is gone after a successful fetch.
        assert not failure_path.exists()

    def test_invalid_fresh_cache_triggers_fetch(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A fresh but structurally invalid cache falls through to a fetch."""
        bad_doc = {"schemaVersion": 99, "generatedAt": "x", "windowDays": 30, "rankings": []}
        _write_doc_cache(tmp_config_dir, bad_doc, stale=False)

        fetcher = mock.Mock(return_value=valid_ranking_doc)
        ranking = load_ranking_document(fetcher=fetcher)

        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500
        fetcher.assert_called_once()

    def test_default_fetcher_is_shared_catalog_seam(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """With no injected fetcher, the shared official_catalog.fetch_document is used."""
        with mock.patch.object(
            catalog_ranking, "fetch_document", return_value=valid_ranking_doc
        ) as fetch:
            ranking = load_ranking_document()

        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500
        fetch.assert_called_once_with(catalog_ranking.RANKING_DOCUMENT_URL)


class TestCumulativeAndWindowed:
    """Test cumulative and windowed (trending) views."""

    def test_both_views_available(self, valid_ranking_doc: dict[str, Any]) -> None:
        """Both cumulative and windowed counts are accessible."""
        ranking = _parse_document(valid_ranking_doc)
        assert ranking is not None

        # alpha-app has highest cumulative (1500) but moderate windowed (120).
        assert ranking.get_cumulative_rank("alpha-app") == 1500
        assert ranking.get_windowed_rank("alpha-app") == 120

        # beta-app has highest windowed (250) but lower cumulative (980).
        assert ranking.get_cumulative_rank("beta-app") == 980
        assert ranking.get_windowed_rank("beta-app") == 250

    def test_window_days_is_available(self, valid_ranking_doc: dict[str, Any]) -> None:
        """The window size is part of the document metadata."""
        ranking = _parse_document(valid_ranking_doc)
        assert ranking is not None
        assert ranking.window_days == 30


class TestKFreshOnly:
    """Test that only k=fresh installs are counted (by schema design).

    Hard requirement #1: k=update receipts must never enter install rank.
    This is enforced by the schema: the ranking document carries only
    install counts, not update counts. The server-side aggregation
    (outside this repo) filters k=fresh.
    """

    def test_schema_has_no_update_count_field(self) -> None:
        """The schema design excludes update counts.

        The ranking document schema has cumulativeInstalls and windowedInstalls
        for k=fresh only. There is no field for k=update counts. This test
        documents the schema contract.
        """
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {
                    "appSlug": "alpha-app",
                    "cumulativeInstalls": 100,  # Fresh installs only.
                    "windowedInstalls": 10,  # Fresh installs only.
                },
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        app_ranking = ranking.rankings["alpha-app"]
        assert hasattr(app_ranking, "cumulative_installs")
        assert hasattr(app_ranking, "windowed_installs")
        # Update counts are not part of the schema.
        assert not hasattr(app_ranking, "update_installs")
        assert not hasattr(app_ranking, "cumulative_updates")


class TestAggregatesOnly:
    """Test that the document carries aggregates only.

    Hard requirement #7: never expose or join raw tokens.
    """

    def test_only_counts_in_schema(self) -> None:
        """The schema carries only aggregate counts, no raw data."""
        ranking = _parse_document(
            {
                "schemaVersion": 1,
                "generatedAt": "2026-10-07T12:00:00Z",
                "windowDays": 30,
                "rankings": [
                    {"appSlug": "alpha-app", "cumulativeInstalls": 100, "windowedInstalls": 10},
                ],
            }
        )
        assert ranking is not None

        app = ranking.rankings["alpha-app"]
        assert app.cumulative_installs == 100
        assert app.windowed_installs == 10

        # No tokens, user data, or raw records are exposed.
        assert not hasattr(app, "tokens")
        assert not hasattr(app, "raw_receipts")
        assert not hasattr(app, "user_ids")


class TestOfficialCatalogOnly:
    """Test that rankings apply only to official catalog apps.

    Hard requirement #3 (privacy boundary): keeps private/corporate app names
    off the wire. An uncounted app (not in the official catalog) gets NO rank.
    """

    def test_private_app_not_ranked(self, valid_ranking_doc: dict[str, Any]) -> None:
        """Apps not in the ranking document (e.g. private apps) have no rank."""
        ranking = _parse_document(valid_ranking_doc)
        assert ranking is not None

        # Only official catalog apps are in the ranking document.
        assert ranking.get_cumulative_rank("private-corporate-app") is None
        assert ranking.get_windowed_rank("private-corporate-app") is None


class TestEdgeCases:
    """Test edge cases and malformed inputs."""

    def test_malformed_entry_is_skipped(self) -> None:
        """Malformed entries are skipped without crashing."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "good-app", "cumulativeInstalls": 100, "windowedInstalls": 10},
                "not a dict",  # Malformed.
                {"noAppSlug": True},  # Missing appSlug.
                {"appSlug": "", "cumulativeInstalls": 50},  # Empty appSlug.
                {"appSlug": 123, "cumulativeInstalls": 50},  # Non-string appSlug.
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        # Only the good entry survives.
        assert len(ranking.rankings) == 1
        assert ranking.get_cumulative_rank("good-app") == 100

    def test_negative_counts_are_clamped(self) -> None:
        """Negative counts are treated as zero (and filtered)."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [
                {"appSlug": "neg-app", "cumulativeInstalls": -10, "windowedInstalls": -5},
                {"appSlug": "good-app", "cumulativeInstalls": 100, "windowedInstalls": 10},
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        # neg-app has negative counts, which are treated as zero and filtered.
        assert ranking.get_cumulative_rank("neg-app") is None
        # good-app is fine.
        assert ranking.get_cumulative_rank("good-app") == 100


class TestGuardFailsWithoutGuard:
    """Verify that tests would fail without the guards.

    These tests confirm that the guards are load-bearing by showing
    what would happen without them.
    """

    def test_empty_would_be_accepted_without_guard(self) -> None:
        """Without the empty/all-zero guard, empty would parse successfully.

        This test shows the guard is load-bearing: if we remove the
        _is_empty_or_all_zero check, empty documents would be accepted.
        """
        doc = {
            "schemaVersion": 1,
            "generatedAt": "2026-10-07T12:00:00Z",
            "windowDays": 30,
            "rankings": [],
        }
        # With the guard, this returns None.
        result = _parse_document(doc)
        assert result is None

        # Validation alone would pass; the guard is what catches it.
        assert _validate_document(doc) is None
        assert _is_empty_or_all_zero(doc) is True

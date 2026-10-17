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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps import catalog_ranking
from kiro_crew.apps.catalog_ranking import (
    MAX_AGE_WINDOWS,
    MAX_WINDOW_DAYS,
    _is_no_data,
    _is_withheld,
    _parse_document,
    _read_count,
    _validate_document,
    load_ranking_document,
)


def _recent_timestamp() -> str:
    """A generatedAt an hour old: well inside the age cap, and it never ages out.

    A literal date in a fixture passes the cap while the date is recent and
    starts failing it once MAX_AGE_WINDOWS windows have elapsed in real time, so
    every fixture derives its timestamp from the clock.
    """
    return (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()


def _timestamp_days_ago(days: float) -> str:
    """A generatedAt *days* old, for exercising the age cap from both sides."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


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
        "generatedAt": _recent_timestamp(),
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


class TestReadCount:
    """Test the count reader that keeps malformed counts from raising."""

    def test_valid_non_negative_int_passes(self) -> None:
        assert _read_count(100) == 100
        assert _read_count(0) == 0

    def test_unusable_types_have_no_number(self) -> None:
        """A string or float count reads as no number rather than raising."""
        assert _read_count("100") is None
        assert _read_count(0.5) is None
        assert _read_count(5.0) is None

    def test_null_is_a_withheld_count_not_a_zero(self) -> None:
        """A withheld count reads as no number, never as a measured zero.

        A producer applying a small-count floor publishes ``null`` for a count
        below it. Coercing that to 0 would present the app as measured at zero
        and sort it to the bottom of a popularity list, undoing the floor one
        layer below the producer that applied it.
        """
        assert _read_count(None) is None

    def test_bool_is_not_a_count(self) -> None:
        """bool subclasses int but is not a valid count."""
        assert _read_count(True) is None
        assert _read_count(False) is None

    def test_negative_has_no_number(self) -> None:
        assert _read_count(-10) is None


class TestWithheldCountDetection:
    """Test telling a withheld count apart from an absent or zero one."""

    def test_explicit_null_is_withheld(self) -> None:
        entry = {"appSlug": "alpha-app", "cumulativeInstalls": 41, "windowedInstalls": None}
        assert _is_withheld(entry) is True

    def test_absent_field_is_not_withheld(self) -> None:
        """An absent count says nothing; only an explicit null withholds."""
        entry = {"appSlug": "alpha-app", "cumulativeInstalls": 41}
        assert _is_withheld(entry) is False

    def test_explicit_zero_is_not_withheld(self) -> None:
        """A zero is what an empty rollup partition publishes, not a floor."""
        entry = {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0}
        assert _is_withheld(entry) is False


class TestNoDataGuard:
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
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [],
        }
        assert _is_no_data(doc) is True

    def test_all_zero_counts_is_no_data(self) -> None:
        """All entries with zero counts is treated as NO DATA."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
                {"appSlug": "beta-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
            ],
        }
        assert _is_no_data(doc) is True

    def test_at_least_one_nonzero_is_valid(self) -> None:
        """A document with at least one non-zero count is valid."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
                {"appSlug": "beta-app", "cumulativeInstalls": 1, "windowedInstalls": 0},
            ],
        }
        assert _is_no_data(doc) is False

    def test_windowed_nonzero_is_valid(self) -> None:
        """A non-zero windowed count is sufficient."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 5},
            ],
        }
        assert _is_no_data(doc) is False

    def test_malformed_count_does_not_raise(self) -> None:
        """A malformed count is read as absence, not a crash, in the guard."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": "100", "windowedInstalls": 0.5},
            ],
        }
        # No TypeError, and the unusable counts read as no data.
        assert _is_no_data(doc) is True

    def test_withheld_count_is_data_not_absence(self) -> None:
        """An entry with a withheld count makes the document data, not no-data.

        The producer published this app and hid its number. Reading that as
        no-data would send the loader down the failure path and keep serving the
        last-good copy, whose counts predate the floor.
        """
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": None, "windowedInstalls": None},
            ],
        }
        assert _is_no_data(doc) is False

    def test_parse_empty_returns_none(self) -> None:
        """_parse_document returns None for empty/all-zero documents."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
                "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
                "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
            "generatedAt": _recent_timestamp(),
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
        _is_no_data check, empty documents would be accepted.
        """
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [],
        }
        # With the guard, this returns None.
        result = _parse_document(doc)
        assert result is None

        # Validation alone would pass; the guard is what catches it.
        assert _validate_document(doc) is None
        assert _is_no_data(doc) is True


class TestSmallCountFloor:
    """Test that a count withheld under a small-count floor survives the client.

    A producer applying a floor publishes ``null`` for any count below it, so a
    small count cannot be read off the document. The client has to carry that
    through: a withheld count must reach a caller as "no number", because the
    two ways of losing it are to report it as ``0`` (which invents a measured
    value and sorts the app last) and to treat the whole document as no-data
    (which falls back to a cached copy whose counts predate the floor).
    """

    def test_withheld_window_does_not_become_zero(self) -> None:
        """An app ranked cumulatively and withheld in the window reports no number."""
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 41, "windowedInstalls": None},
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        assert ranking.get_cumulative_rank("alpha-app") == 41
        # The withheld count is absent, not a zero that would sort the app last.
        assert ranking.get_windowed_rank("alpha-app") is None

    def test_fully_withheld_document_is_accepted_and_ranks_nothing(self) -> None:
        """A document whose every count is withheld is an answer, not a failure.

        It reports that every app sits below the floor. No app is rankable, and
        the document is still valid.
        """
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": None, "windowedInstalls": None},
                {"appSlug": "beta-app", "cumulativeInstalls": None, "windowedInstalls": None},
            ],
        }
        ranking = _parse_document(doc)
        assert ranking is not None

        assert ranking.rankings == {}
        assert ranking.get_cumulative_rank("alpha-app") is None
        assert ranking.get_windowed_rank("beta-app") is None

    def test_floored_document_replaces_unfloored_cache(self, tmp_config_dir: Path) -> None:
        """A fetched floored document evicts a cached copy with exact counts.

        This is the case the floor exists for. The cache holds a copy taken
        before the producer applied the floor, with the exact small counts in
        it. Treating the floored document as no-data would record a failure and
        keep serving those counts, so the floor would raise the exposure it was
        added to lower.
        """
        unfloored = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 3, "windowedInstalls": 2},
            ],
        }
        cache_path = _write_doc_cache(tmp_config_dir, unfloored, stale=True)

        floored = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": None, "windowedInstalls": None},
            ],
        }
        result = load_ranking_document(fetcher=lambda _url: floored)

        # The exact counts are gone from what callers see.
        assert result is not None
        assert result.get_cumulative_rank("alpha-app") is None

        # And gone from the cache, so a later stale read cannot resurrect them.
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        assert cached["rankings"][0]["cumulativeInstalls"] is None

        # No failure was recorded: this document is data, not a fetch problem.
        assert not (tmp_config_dir / "cache" / "catalog-ranking.failed").exists()

    def test_all_zero_document_is_still_no_data(self) -> None:
        """Withholding does not weaken the zero guard.

        An explicit zero is what a rollup over an empty partition publishes, and
        it still has to be refused.
        """
        doc = {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": 30,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 0, "windowedInstalls": 0},
            ],
        }
        assert _parse_document(doc) is None


class TestDocumentAgeCap:
    """Test the bound on how stale the last-good fallback may get.

    ``windowedInstalls`` describes a ``windowDays`` window ending at
    ``generatedAt``. Without a cap, a producer that stops publishing leaves its
    final document served as the current ranking for as long as the cache
    survives, and a trending figure for a window that closed months ago is the
    wrong-ranking case the fallback exists to avoid.
    """

    def _doc(self, generated_at: str, window_days: int = 30) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "generatedAt": generated_at,
            "windowDays": window_days,
            "rankings": [
                {"appSlug": "alpha-app", "cumulativeInstalls": 1500, "windowedInstalls": 120},
            ],
        }

    def test_document_inside_the_cap_is_accepted(self) -> None:
        """A document a little under the cap still ranks."""
        age = 30 * MAX_AGE_WINDOWS - 1
        ranking = _parse_document(self._doc(_timestamp_days_ago(age)))
        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500

    def test_document_past_the_cap_is_refused(self) -> None:
        """A document past the cap is refused outright, counts and all."""
        age = 30 * MAX_AGE_WINDOWS + 1
        assert _parse_document(self._doc(_timestamp_days_ago(age))) is None

    def test_cap_scales_with_the_declared_window(self) -> None:
        """The cap is a multiple of windowDays, not a fixed number of days.

        A document declaring a 7-day window is stale sooner than one declaring
        30 days, because its windowed count goes out of date sooner.
        """
        age = 7 * MAX_AGE_WINDOWS + 1
        assert _parse_document(self._doc(_timestamp_days_ago(age), window_days=7)) is None
        # The same age is still inside the cap for a 30-day window.
        assert _parse_document(self._doc(_timestamp_days_ago(age), window_days=30)) is not None

    def test_overage_stale_cache_is_not_served(self, tmp_config_dir: Path) -> None:
        """A fetch failure does not resurrect a cached document past the cap.

        The fallback is the whole reason the cap matters: this is the path that
        would otherwise serve an abandoned ranking indefinitely.
        """
        ancient = self._doc(_timestamp_days_ago(30 * MAX_AGE_WINDOWS + 10))
        _write_doc_cache(tmp_config_dir, ancient, stale=True)

        assert load_ranking_document(fetcher=lambda _url: None) is None

    def test_unparseable_generated_at_is_rejected(self) -> None:
        """A timestamp that cannot be read is a document whose age is unknown."""
        error = _validate_document(self._doc("not a timestamp"))
        assert error is not None
        assert "generatedAt" in error

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        """A timestamp without an offset is read as UTC rather than refused."""
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        ranking = _parse_document(self._doc(naive.isoformat()))
        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500

    def test_future_timestamp_is_not_refused(self) -> None:
        """Clock skew between producer and host is not grounds to drop a ranking."""
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        assert _parse_document(self._doc(future)) is not None


class TestWindowDaysBound:
    """Test that an oversized windowDays is refused rather than crashing.

    The age cap is built as a ``timedelta``, which cannot represent a span past
    roughly a billion days. A document declaring a window large enough to
    overflow that would raise out of validation and escape the loader, so the
    caller would get an exception where the whole point of the module is to hand
    back the last-good ranking when a document is unusable.
    """

    def _doc(self, window_days: int) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "generatedAt": _recent_timestamp(),
            "windowDays": window_days,
            "rankings": [
                {"appSlug": "hostile-app", "cumulativeInstalls": 10, "windowedInstalls": 5},
            ],
        }

    def test_bound_is_derived_from_timedelta(self) -> None:
        """The bound is the largest window whose cap is representable.

        Deriving it keeps it correct if MAX_AGE_WINDOWS changes, where a chosen
        number would silently stop matching.
        """
        assert MAX_WINDOW_DAYS == timedelta.max.days // MAX_AGE_WINDOWS
        # The bound itself is representable, and one past it is not.
        assert timedelta(days=MAX_WINDOW_DAYS * MAX_AGE_WINDOWS) <= timedelta.max
        with pytest.raises(OverflowError):
            timedelta(days=(MAX_WINDOW_DAYS + 1) * MAX_AGE_WINDOWS)

    def test_window_at_the_bound_is_accepted(self) -> None:
        assert _validate_document(self._doc(MAX_WINDOW_DAYS)) is None

    def test_window_past_the_bound_is_a_validation_error(self) -> None:
        """One past the bound is rejected by validation, not by an exception."""
        error = _validate_document(self._doc(MAX_WINDOW_DAYS + 1))
        assert error is not None
        assert "windowDays" in error

    def test_oversized_window_falls_back_instead_of_raising(
        self, tmp_config_dir: Path, valid_ranking_doc: dict[str, Any]
    ) -> None:
        """A hostile window leaves the last-good ranking served, not an exception.

        This is the behaviour the bound protects. Without it the cap arithmetic
        raises through _validate_document, _parse_document and the loader, so a
        caller holding a perfectly good cached ranking gets an OverflowError.
        """
        _write_doc_cache(tmp_config_dir, valid_ranking_doc, stale=True)

        ranking = load_ranking_document(fetcher=lambda _url: self._doc(MAX_WINDOW_DAYS + 1))

        assert ranking is not None
        assert ranking.get_cumulative_rank("alpha-app") == 1500
        # The hostile document never reached the cache.
        assert ranking.get_cumulative_rank("hostile-app") is None

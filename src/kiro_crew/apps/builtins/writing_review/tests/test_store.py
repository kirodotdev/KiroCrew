"""Unit tests for the Writing Review persistence layer.

* :class:`TestReviewsStore`   -> Behaviours #12, #13
* :class:`TestSettingsStore`  -> Settings CRUD (Slice 5 addition)
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kiro_crew.apps.builtins.writing_review import (
    Finding,
    ReviewContext,
    ScanResult,
    Section,
)
from kiro_crew.apps.builtins.writing_review.backend.store import (
    build_scan_result_from_record,
    delete_review,
    list_reviews,
    load_review,
    load_settings,
    save_review,
    update_settings,
)


def _build_scan_result(
    *,
    doc_name: str = "example.md",
    verdict: str = "yellow",
    findings: list[Finding] | None = None,
) -> ScanResult:
    return ScanResult(
        doc_path=f"/tmp/{doc_name}",
        doc_name=doc_name,
        doc_context=ReviewContext(audience="team", doc_type="update", tone="neutral"),
        sections=[Section(heading="Intro", body="Hello")],
        findings=findings or [],
        verdict=verdict,
        scanners_run=["clarity", "structure"],
        partial_failure=False,
        failed_scanners=[],
    )


class TestReviewsStore(unittest.TestCase):
    """Behaviours #12, #13 -- reviews persist to disk and can be retrieved."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.data_dir = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_save_and_load_review(self) -> None:
        scan_result = _build_scan_result()

        review_id = save_review(scan_result, data_dir=self.data_dir)
        loaded_record = load_review(review_id, data_dir=self.data_dir)

        self.assertIsNotNone(loaded_record)
        assert loaded_record is not None  # narrow for the type checker
        self.assertEqual(loaded_record["doc_name"], scan_result.doc_name)
        self.assertEqual(loaded_record["verdict"], scan_result.verdict)

    def test_list_reviews(self) -> None:
        save_review(_build_scan_result(doc_name="doc1.md"), data_dir=self.data_dir)
        save_review(_build_scan_result(doc_name="doc2.md"), data_dir=self.data_dir)

        summaries = list_reviews(data_dir=self.data_dir)

        self.assertEqual(len(summaries), 2)
        doc_names_seen = {summary["doc_name"] for summary in summaries}
        self.assertEqual(doc_names_seen, {"doc1.md", "doc2.md"})

    def test_load_missing_review_returns_none(self) -> None:
        self.assertIsNone(load_review("nonexistent", data_dir=self.data_dir))

    def test_delete_review(self) -> None:
        review_id = save_review(_build_scan_result(), data_dir=self.data_dir)

        self.assertTrue(delete_review(review_id, data_dir=self.data_dir))
        self.assertIsNone(load_review(review_id, data_dir=self.data_dir))

    def test_delete_missing_review_returns_false(self) -> None:
        self.assertFalse(delete_review("nonexistent", data_dir=self.data_dir))

    def test_ask_field_round_trips_through_save_and_load(self) -> None:
        # An ask value supplied at scan time MUST survive the round trip
        # to disk so the review-detail header can render it later and
        # the discussion agent's context bundle can see what the author
        # was asking about. Persisted under the ``context.ask`` key
        # inside the review record.
        scan_result = ScanResult(
            doc_path="/tmp/example.md",
            doc_name="example.md",
            doc_context=ReviewContext(
                audience="team",
                doc_type="update",
                tone="neutral",
                ask="Should we ship or hold for Q3?",
            ),
            sections=[Section(heading="Intro", body="Hello")],
            findings=[],
            verdict="green",
            scanners_run=["clarity"],
        )

        review_id = save_review(scan_result, data_dir=self.data_dir)
        loaded_record = load_review(review_id, data_dir=self.data_dir)
        assert loaded_record is not None  # narrow for the type checker

        self.assertEqual(
            loaded_record["context"]["ask"],
            "Should we ship or hold for Q3?",
        )

        # Rehydrating back into a ScanResult must reconstitute the field
        # so callers reading the record programmatically (discussion
        # agent context handler, artifact export) see it too.
        rehydrated = build_scan_result_from_record(loaded_record)
        self.assertEqual(rehydrated.doc_context.ask, "Should we ship or hold for Q3?")


class TestBackwardCompatibility(unittest.TestCase):
    """Behaviour #14 -- old records deserialise cleanly with sensible defaults."""

    def test_build_scan_result_handles_old_flat_failed_scanners(self) -> None:
        record = {
            "doc_path": "/tmp/x.md",
            "doc_name": "x.md",
            "verdict": "yellow",
            "scanners_run": ["clarity"],
            "context": {"audience": "", "doc_type": "", "tone": "", "additional_context": []},
            "findings": [
                {
                    "id": "abc",
                    "scanner": "clarity",
                    "section": "A",
                    "paragraph": 1,
                    "issue": "x",
                    "rule": "1",
                    "severity": "high",
                    "proposed_fix": "y",
                    "cross_validation": "clean",
                    "conflicts": [],
                }
            ],
            "partial_failure": False,
            "failed_scanners": ["naturalness"],  # OLD FORMAT: bare strings
        }
        result = build_scan_result_from_record(record)
        self.assertEqual(len(result.failed_scanners), 1)
        self.assertEqual(result.failed_scanners[0].name, "naturalness")
        self.assertEqual(result.failed_scanners[0].reason_class, "other")

    def test_build_scan_result_handles_missing_confidence(self) -> None:
        record = {
            "doc_path": "/tmp/x.md",
            "doc_name": "x.md",
            "verdict": "green",
            "scanners_run": ["clarity"],
            "context": {"audience": "", "doc_type": "", "tone": "", "additional_context": []},
            "findings": [
                {
                    "id": "abc",
                    "scanner": "clarity",
                    "section": "A",
                    "paragraph": 1,
                    "issue": "x",
                    "rule": "1",
                    "severity": "low",
                    "proposed_fix": "y",
                    "cross_validation": "clean",
                    "conflicts": [],
                }
            ],
            "partial_failure": False,
            "failed_scanners": [],
        }
        result = build_scan_result_from_record(record)
        self.assertEqual(result.findings[0].confidence, "medium")

    def test_build_scan_result_handles_missing_log_reference(self) -> None:
        record = {
            "doc_path": "/tmp/x.md",
            "doc_name": "x.md",
            "verdict": "green",
            "scanners_run": [],
            "context": {"audience": "", "doc_type": "", "tone": "", "additional_context": []},
            "findings": [],
            "partial_failure": False,
            "failed_scanners": [],
        }
        result = build_scan_result_from_record(record)
        self.assertEqual(result.log_reference, {})

    def test_build_scan_result_handles_new_structured_failed_scanners(self) -> None:
        record = {
            "doc_path": "/tmp/x.md",
            "doc_name": "x.md",
            "verdict": "green",
            "scanners_run": ["clarity", "naturalness"],
            "context": {"audience": "", "doc_type": "", "tone": "", "additional_context": []},
            "findings": [],
            "partial_failure": False,
            "failed_scanners": [
                {
                    "name": "naturalness",
                    "reason_class": "provider_timeout",
                    "message": "timeout after 60s",
                    "at": "2026-08-25T15:00:00+00:00",
                    "duration_ms": 60123,
                }
            ],
        }
        result = build_scan_result_from_record(record)
        self.assertEqual(len(result.failed_scanners), 1)
        failed = result.failed_scanners[0]
        self.assertEqual(failed.reason_class, "provider_timeout")
        self.assertEqual(failed.duration_ms, 60123)


class TestRelatedLocationsRoundTrip(unittest.TestCase):
    """Slice 4 -- ``related_locations`` survive JSON round-trip as dataclasses.

    ``dataclasses.asdict`` recursively converts nested dataclasses into
    dicts on the way to disk. On the way back, ``Finding(**dict)`` would
    otherwise assign a ``list[dict]`` to ``related_locations``, silently
    breaking attribute access (``location.section`` would raise
    ``AttributeError`` on a dict). The store's rehydrator must
    reconstruct each entry as a :class:`RelatedLocation` so downstream
    code -- artifact rendering, discussion context -- can iterate the
    list uniformly.
    """

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.data_dir = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_related_locations_reconstructed_as_dataclass_instances(self) -> None:
        from kiro_crew.apps.builtins.writing_review import RelatedLocation

        finding_with_related = Finding(
            id="primary_id",
            scanner="clarity",
            section="Intro",
            paragraph=1,
            issue="wordy",
            rule="1",
            severity="high",
            proposed_fix="shorten",
            related_locations=[
                RelatedLocation(
                    section="Body",
                    paragraph=3,
                    scanner="naturalness",
                    issue="same idea, different section",
                )
            ],
        )
        review_id = save_review(
            _build_scan_result(findings=[finding_with_related]),
            data_dir=self.data_dir,
        )
        stored_record = load_review(review_id, data_dir=self.data_dir)
        assert stored_record is not None
        rehydrated_scan_result = build_scan_result_from_record(stored_record)

        rehydrated_finding = rehydrated_scan_result.findings[0]
        self.assertEqual(len(rehydrated_finding.related_locations), 1)
        # Attribute access -- the whole point of this test. A ``dict``
        # would raise ``AttributeError`` on the next line.
        rehydrated_location = rehydrated_finding.related_locations[0]
        self.assertIsInstance(rehydrated_location, RelatedLocation)
        self.assertEqual(rehydrated_location.section, "Body")
        self.assertEqual(rehydrated_location.paragraph, 3)
        self.assertEqual(rehydrated_location.scanner, "naturalness")


class TestSettingsStore(unittest.TestCase):
    """Settings load with defaults; patch merges into stored config."""

    def setUp(self) -> None:
        self._tempdir = TemporaryDirectory()
        self.data_dir = Path(self._tempdir.name)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def test_load_default_settings(self) -> None:
        settings = load_settings(data_dir=self.data_dir)

        self.assertEqual(settings["default_audience"], "")
        self.assertTrue(settings["scanner_toggles"]["clarity"])
        # Conditional scanners are off by default.
        self.assertFalse(settings["scanner_toggles"]["design"])
        self.assertFalse(settings["scanner_toggles"]["email"])

    def test_update_settings_merges(self) -> None:
        update_settings({"default_audience": "VP"}, data_dir=self.data_dir)

        reloaded_settings = load_settings(data_dir=self.data_dir)

        self.assertEqual(reloaded_settings["default_audience"], "VP")
        # Other defaults must survive the patch.
        self.assertEqual(reloaded_settings["default_tone"], "")
        self.assertTrue(reloaded_settings["scanner_toggles"]["clarity"])

    def test_update_settings_patches_scanner_toggles(self) -> None:
        update_settings({"scanner_toggles": {"design": True}}, data_dir=self.data_dir)

        reloaded_settings = load_settings(data_dir=self.data_dir)

        self.assertTrue(reloaded_settings["scanner_toggles"]["design"])
        # Other toggles unchanged.
        self.assertTrue(reloaded_settings["scanner_toggles"]["clarity"])

    def test_defaults_include_max_concurrent(self) -> None:
        from kiro_crew.apps.builtins.writing_review.pool import DEFAULT_MAX_CONCURRENT

        settings = load_settings(data_dir=self.data_dir)
        self.assertEqual(settings["max_concurrent"], DEFAULT_MAX_CONCURRENT)

    def test_update_settings_clamps_max_concurrent_above_ceiling(self) -> None:
        from kiro_crew.apps.builtins.writing_review.pool import MAX_CONCURRENT_CEIL

        update_settings({"max_concurrent": 9999}, data_dir=self.data_dir)
        reloaded_settings = load_settings(data_dir=self.data_dir)
        self.assertEqual(reloaded_settings["max_concurrent"], MAX_CONCURRENT_CEIL)

    def test_update_settings_clamps_max_concurrent_below_one(self) -> None:
        update_settings({"max_concurrent": 0}, data_dir=self.data_dir)
        reloaded_settings = load_settings(data_dir=self.data_dir)
        self.assertEqual(reloaded_settings["max_concurrent"], 1)

    def test_update_settings_ignores_malformed_max_concurrent(self) -> None:
        from kiro_crew.apps.builtins.writing_review.pool import DEFAULT_MAX_CONCURRENT

        update_settings({"max_concurrent": "not a number"}, data_dir=self.data_dir)
        reloaded_settings = load_settings(data_dir=self.data_dir)
        # Malformed value ignored -- default preserved.
        self.assertEqual(reloaded_settings["max_concurrent"], DEFAULT_MAX_CONCURRENT)

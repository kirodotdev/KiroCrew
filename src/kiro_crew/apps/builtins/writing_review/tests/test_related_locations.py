"""Data-model tests for Slice 1 of the dedup collation spec.

Verifies that:
* A new ``RelatedLocation`` dataclass exists and is constructible with the
  four fields the spec calls out (section, paragraph, scanner, issue).
* ``Finding`` gains two new fields: ``primary_id`` (string default "")
  and ``related_locations`` (list default empty).

These are pure structural tests -- they do NOT touch pool / driver /
frontend / prompts. Higher slices exercise the collation logic and the
prompt update; this slice pins the shape of the data that everything
else in the spec builds on.
"""

from __future__ import annotations

import unittest

from kiro_crew.apps.builtins.writing_review import Finding, RelatedLocation


class TestRelatedLocation(unittest.TestCase):
    """The new dataclass carrying one related-location entry."""

    def test_related_location_constructible_with_all_fields(self) -> None:
        related_location_record = RelatedLocation(
            section="Monitoring",
            paragraph=5,
            scanner="evidence",
            issue="no quantified threshold",
        )
        self.assertEqual(related_location_record.section, "Monitoring")
        self.assertEqual(related_location_record.paragraph, 5)
        self.assertEqual(related_location_record.scanner, "evidence")
        self.assertEqual(related_location_record.issue, "no quantified threshold")


class TestFindingNewFields(unittest.TestCase):
    """``Finding`` gets ``primary_id`` and ``related_locations`` defaults."""

    def _base_finding_kwargs(self) -> dict:
        # The minimum kwargs any Finding needs today; new tests should
        # not have to enumerate every existing field, so this helper keeps
        # the slice scope small while covering required fields.
        return {
            "id": "test_id",
            "scanner": "clarity",
            "section": "Intro",
            "paragraph": 1,
            "issue": "verbose sentence",
            "rule": "1",
            "severity": "medium",
            "proposed_fix": "shorten",
        }

    def test_finding_primary_id_defaults_to_empty_string(self) -> None:
        default_finding = Finding(**self._base_finding_kwargs())
        self.assertEqual(default_finding.primary_id, "")

    def test_finding_related_locations_defaults_to_empty_list(self) -> None:
        default_finding = Finding(**self._base_finding_kwargs())
        self.assertEqual(default_finding.related_locations, [])

    def test_finding_accepts_related_locations_of_related_location_type(self) -> None:
        finding_with_related = Finding(
            **self._base_finding_kwargs(),
            related_locations=[
                RelatedLocation(
                    section="Body",
                    paragraph=3,
                    scanner="evidence",
                    issue="no numbers",
                )
            ],
        )
        self.assertEqual(len(finding_with_related.related_locations), 1)
        self.assertEqual(finding_with_related.related_locations[0].scanner, "evidence")

    def test_related_locations_lists_are_not_shared_across_instances(self) -> None:
        # ``field(default_factory=list)`` protects against the classic
        # ``= []`` bug where every instance shares one list. Constructing
        # two findings and appending to one MUST NOT touch the other.
        finding_one = Finding(**self._base_finding_kwargs())
        finding_two = Finding(**self._base_finding_kwargs())
        finding_one.related_locations.append(
            RelatedLocation(section="X", paragraph=1, scanner="a", issue="b")
        )
        self.assertEqual(finding_two.related_locations, [])

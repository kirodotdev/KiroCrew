"""Unit tests for the artifact-integration helpers (Slice 6).

* :class:`TestCommentBody`   -> Behaviour #18 (comment content)
* :class:`TestCommentAnchor` -> Behaviour #18 (anchor selection)
"""

from __future__ import annotations

import unittest

from kiro_crew.apps.builtins.writing_review import (
    Finding,
    build_comment_anchor,
    build_comment_body,
)


def _build_finding(
    *,
    scanner: str = "structure",
    section: str = "Introduction",
    paragraph: int = 1,
    severity: str = "high",
    issue: str = "Opening buries the ask",
    rule: str = "1",
    proposed_fix: str = "Lead with the recommendation: We propose ...",
    conflicts: list[str] | None = None,
) -> Finding:
    return Finding(
        id=f"{scanner}-{section}-{paragraph}-{rule}",
        scanner=scanner,
        section=section,
        paragraph=paragraph,
        issue=issue,
        rule=rule,
        severity=severity,
        proposed_fix=proposed_fix,
        conflicts=conflicts or [],
    )


class TestCommentBody(unittest.TestCase):
    """Behaviour #18 -- comment body carries severity, issue, and fix."""

    def test_body_includes_severity_scanner_and_rule(self) -> None:
        body = build_comment_body(_build_finding())

        self.assertIn("[HIGH]", body)
        self.assertIn("Structure", body)
        self.assertIn("Rule 1", body)

    def test_body_includes_issue_and_proposed_fix(self) -> None:
        body = build_comment_body(_build_finding())

        self.assertIn("Opening buries the ask", body)
        self.assertIn("Lead with the recommendation", body)

    def test_body_omits_fix_block_when_absent(self) -> None:
        body = build_comment_body(_build_finding(proposed_fix=""))

        self.assertNotIn("Proposed fix", body)

    def test_body_includes_cross_validation_conflicts(self) -> None:
        body = build_comment_body(_build_finding(conflicts=["structure vs clarity"]))

        self.assertIn("Cross-validation", body)
        self.assertIn("structure vs clarity", body)


class TestCommentAnchor(unittest.TestCase):
    """Behaviour #18 -- anchor quote/prefix/suffix land on the flagged paragraph."""

    def test_anchor_targets_flagged_section_paragraph(self) -> None:
        document_text = (
            "# Introduction\n\n"
            "The project started in Q1 2026.\n\n"
            "## Details\n\n"
            "Delivery is on track.\n"
        )
        finding = _build_finding(section="Introduction", paragraph=1)

        anchor = build_comment_anchor(finding, document_text)

        self.assertIn("The project started in Q1 2026.", anchor["quote"])

    def test_anchor_falls_back_when_section_missing(self) -> None:
        document_text = "Plain paragraph with no headings.\n"
        finding = _build_finding(section="Nowhere", paragraph=1)

        anchor = build_comment_anchor(finding, document_text)

        # Fallback anchors on the first non-empty non-heading paragraph.
        self.assertIn("Plain paragraph", anchor["quote"])

    def test_anchor_returns_empty_dict_for_empty_document(self) -> None:
        anchor = build_comment_anchor(_build_finding(), "")

        self.assertEqual(anchor["quote"], "")

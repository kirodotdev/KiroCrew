"""Unit tests for deterministic helpers: dedup, verdict, exceptions.

Each ``TestCase`` maps to Behaviour Table rows:

* :class:`TestDedupFindings`   -> Behaviour #9
* :class:`TestComputeVerdict`  -> Behaviour #11
* :class:`TestMatchAdditionalContext` -> Behaviour #20
"""

from __future__ import annotations

import unittest

from kiro_crew.apps.builtins.writing_review import Finding
from kiro_crew.apps.builtins.writing_review.deterministic import (
    compute_verdict,
    dedup_findings,
    match_additional_context,
)


def _build_finding(
    *,
    scanner: str = "clarity",
    section: str = "Intro",
    paragraph: int = 1,
    severity: str = "medium",
    issue: str = "an issue",
    rule: str = "1",
) -> Finding:
    return Finding(
        id=f"{scanner}-{section}-{paragraph}-{rule}",
        scanner=scanner,
        section=section,
        paragraph=paragraph,
        issue=issue,
        rule=rule,
        severity=severity,
        proposed_fix="rewrite as X",
    )


class TestDedupFindings(unittest.TestCase):
    """Behaviour #9 -- structural dedup keyed on ``(scanner, section, paragraph)``.

    The dedup pass is a same-scanner LLM-verbosity safety net: a single
    scanner occasionally emits multiple findings at the same location
    with different severities (LLM noise), and the higher-severity
    framing is preserved. Cross-scanner overlaps at the same location
    are NOT collapsed here -- those are the input signal for the
    downstream cross-validation pass to tag as ``redundant`` (same root
    cause) or ``conflicts`` (real disagreement). Collapsing them at
    this stage would make both tag paths unreachable and turn the
    ``Scanners disagree`` UI pill into dead code.
    """

    def test_collapses_same_scanner_same_paragraph_keeping_higher_severity(self) -> None:
        # Two findings from the SAME scanner at the same (section,
        # paragraph). This is the LLM-verbosity case dedup exists to
        # catch: one call to the clarity model returned near-identical
        # findings with different severity framings. Collapse to the
        # higher-severity representation so the user sees the more
        # urgent framing exactly once.
        high_severity_clarity = _build_finding(scanner="clarity", severity="high", rule="1")
        medium_severity_clarity = _build_finding(scanner="clarity", severity="medium", rule="2")

        deduped = dedup_findings([medium_severity_clarity, high_severity_clarity])

        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0].severity, "high")
        self.assertEqual(deduped[0].scanner, "clarity")

    def test_keeps_cross_scanner_findings_at_same_paragraph(self) -> None:
        # Two DIFFERENT scanners flagging the same (section, paragraph).
        # Both survive so the cross-validation pass can tag them as
        # ``redundant`` (same root cause -> collate onto the primary) or
        # ``conflicts`` (real tension -> user sees both with the
        # ``Scanners disagree`` pill). Collapsing them at the structural
        # dedup stage would make both downstream paths dead code.
        high_severity_clarity = _build_finding(scanner="clarity", severity="high")
        medium_severity_structure = _build_finding(scanner="structure", severity="medium")

        deduped = dedup_findings([medium_severity_structure, high_severity_clarity])

        self.assertEqual(len(deduped), 2)
        surviving_scanners = {finding.scanner for finding in deduped}
        self.assertEqual(surviving_scanners, {"clarity", "structure"})

    def test_keeps_both_when_different_paragraphs(self) -> None:
        first_paragraph = _build_finding(paragraph=1)
        second_paragraph = _build_finding(paragraph=2)

        deduped = dedup_findings([first_paragraph, second_paragraph])

        self.assertEqual(len(deduped), 2)

    def test_empty_list_returns_empty_list(self) -> None:
        self.assertEqual(dedup_findings([]), [])


class TestComputeVerdict(unittest.TestCase):
    """Behaviour #11 -- verdict reflects the highest severity present."""

    def test_red_when_any_high_severity(self) -> None:
        findings = [_build_finding(severity="high"), _build_finding(severity="low")]
        self.assertEqual(compute_verdict(findings), "red")

    def test_yellow_when_medium_only(self) -> None:
        findings = [_build_finding(severity="medium"), _build_finding(severity="medium")]
        self.assertEqual(compute_verdict(findings), "yellow")

    def test_green_when_low_or_advisory_only(self) -> None:
        findings = [_build_finding(severity="low"), _build_finding(severity="advisory")]
        self.assertEqual(compute_verdict(findings), "green")

    def test_green_when_no_findings(self) -> None:
        self.assertEqual(compute_verdict([]), "green")


class TestMatchAdditionalContext(unittest.TestCase):
    """Behaviour #20 -- findings matching an additional-context note drop out."""

    def test_dismisses_matching_finding(self) -> None:
        findings = [_build_finding(issue="FY2025 numbers are wrong")]

        remaining = match_additional_context(findings, ["FY2025 is correct"])

        self.assertEqual(remaining, [])

    def test_keeps_non_matching_finding(self) -> None:
        findings = [_build_finding(issue="Passive voice detected")]

        remaining = match_additional_context(findings, ["FY2025 is correct"])

        self.assertEqual(len(remaining), 1)

    def test_empty_additional_context_returns_all_findings(self) -> None:
        findings = [_build_finding(), _build_finding(paragraph=2)]

        remaining = match_additional_context(findings, [])

        self.assertEqual(len(remaining), 2)

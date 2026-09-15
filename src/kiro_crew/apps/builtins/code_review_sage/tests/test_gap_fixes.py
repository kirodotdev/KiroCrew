"""Tests for band override, adapter validation, and parse provenance."""
import unittest

from sage_lib import adapters as A  # noqa: N812
from sage_lib import learning as L  # noqa: N812
from sage_lib import report as RP  # noqa: N812

from tests.fixtures import GITHUB_PAYLOAD


def _rec(cid, **k):
    r = {"schema": "code-review-sage-result", "version": 1, "change_id": cid, "platform": "github",
         "repo_identity": "github.com/o/r", "url": "u", "title": "t",
         "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
         "blast_radius": {"rating": "SMALL", "signals": {}}, "counts": {"red": 0, "yellow": 0}}
    r["phase1"].update(k.get("phase1", {}))
    return r


class TestBandOverride(unittest.TestCase):
    def test_baseline_when_no_override(self):
        self.assertEqual(RP.classify(_rec("CR-1"))["band"], "green")

    def test_ai_override_promotes(self):
        rec = _rec("CR-2", phase1={"band_override": "red",
                                   "band_override_reason": "foundational path, gut check"})
        c = RP.classify(rec)
        self.assertEqual(c["band"], "red")
        self.assertIn("AI override", c["why"])
        self.assertIn("baseline green", c["why"])

    def test_override_ignored_when_same_as_baseline(self):
        rec = _rec("CR-3", phase1={"band_override": "green"})
        c = RP.classify(rec)
        self.assertEqual(c["band"], "green")
        self.assertNotIn("AI override", c["why"])

    def test_invalid_override_ignored(self):
        rec = _rec("CR-4", phase1={"band_override": "purple"})
        self.assertEqual(RP.classify(rec)["band"], "green")


class TestAdapterValidation(unittest.TestCase):
    def test_clean_target_no_warnings(self):
        t = A.parse_github_payload(GITHUB_PAYLOAD)
        self.assertEqual(A.validate_review_target(t), [])

    def test_warns_on_no_files(self):
        payload = dict(GITHUB_PAYLOAD)
        payload["files"] = []
        t = A.parse_github_payload(payload)
        warns = A.validate_review_target(t)
        self.assertTrue(any("no files" in w for w in warns))

    def test_warns_on_missing_target_branch(self):
        payload = {k: v for k, v in GITHUB_PAYLOAD.items() if k != "base"}
        t = A.parse_github_payload(payload)
        self.assertTrue(any("target branch" in w for w in A.validate_review_target(t)))


class TestGuidanceOnlyPatterns(unittest.TestCase):
    def test_render_is_guidance_only(self):
        p = {"title": "X pattern", "scope": "common", "impact": "high",
             "guidance": "do X on every path"}
        md = L.render_pattern(p)
        # Guidance-only: no Symptom / Example lines are emitted.
        self.assertNotIn("**Symptom", md)
        self.assertNotIn("**Example", md)
        parsed = L.parse_patterns(md)[0]
        self.assertEqual(parsed["title"], "X pattern")
        self.assertEqual(parsed["guidance"], "do X on every path")
        self.assertNotIn("symptom_why", parsed)
        self.assertNotIn("example", parsed)

    def test_legacy_symptom_example_lines_ignored(self):
        # A file still carrying the old format parses cleanly (guidance only).
        legacy = ("### Old rule <!-- scope:common --> <!-- impact:high -->\n"
                  "guard carefully on every path\n\n"
                  "**Symptom & why it mattered:** it broke prod\n"
                  "**Example:** (from r@CR-1) snippet here\n")
        parsed = L.parse_patterns(legacy)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["guidance"], "guard carefully on every path")

    def test_multiline_guidance_roundtrip_lossless(self):
        # Guidance that spans multiple lines must round-trip fully (all lines
        # captured, joined by a space) — not silently truncated to the first line.
        p = {"title": "Multi", "scope": "common", "impact": "medium",
             "guidance": "first clause of the rule\nsecond clause of the rule"}
        md = L.render_pattern(p)
        # render normalizes guidance to a single line (idempotent format).
        self.assertNotIn("\n", md.split("-->\n", 1)[1].rstrip("\n"))
        parsed = L.parse_patterns(md)[0]
        self.assertEqual(parsed["guidance"],
                         "first clause of the rule second clause of the rule")
        # render(parse(render(p))) is stable — no drift across consolidation cycles.
        self.assertEqual(L.render_pattern(parsed), md)


if __name__ == "__main__":
    unittest.main()

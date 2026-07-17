"""Unit tests for the Focus Report generator."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sage_lib import pipeline as PL  # noqa: N812
from sage_lib import report as RP  # noqa: N812
from sage_lib import results, store


def _rec(change_id, verdict="PASS", risk="low", blast="SMALL", red=0, yellow=0,
         branch=False, regression=False, deep=True, title="t"):
    return {
        "schema": "code-review-sage-result", "version": 1, "change_id": change_id,
        "platform": "github", "repo_identity": "github.com/o/r",
        "url": f"https://github.com/o/r/pull/{change_id}", "title": title,
        "phase1": {"gate_verdict": verdict, "design_risk": risk, "criticality": "low"},
        "blast_radius": {"rating": blast, "signals": {}},
        "counts": {"red": red, "yellow": yellow},
        "branch_gate_violation": branch, "regression_detected": regression,
        "deep_reviewed": deep,
    }


class TestLlmRedaction(unittest.TestCase):
    """Security-controls: LLM-authored text written to the dashboard-served
    rows.json / focus-report.html MUST be routed through redaction. We assert the
    wiring (every LLM field passes through pipeline._redact) deterministically by
    stubbing _redact, independent of the real redaction lib's availability."""

    def _rec_with_llm(self):
        # title comes from the (untrusted) CR payload, so it is treated as an
        # LLM/external field and must be redacted alongside the phase1 text.
        r = _rec("42", red=1, title="leak http://evil.example/title")
        r["phase1"].update({
            "design_headline": "leak http://evil.example/headline",
            "problem": "leak http://evil.example/x",
            "why_it_matters": "matters",
            "solution_assessment": "assess",
            "rationale": "why",
        })
        r["findings"] = [{
            "dimension": "security", "severity": "red", "file": "a.py", "line": 1,
            "snippet": "token=AKIA...", "observation": "obs",
            "consequence": "cons", "suggestion": "fix",
        }]
        return r

    def test_build_report_redacts_all_llm_fields(self):
        with mock.patch.object(PL, "_redact", lambda s: "[R]" + str(s)):
            report = RP.build_report([self._rec_with_llm()])
        row = report["rows"][0]
        for k in ("title", "design_headline", "problem", "why_it_matters",
                  "solution_assessment", "rationale"):
            self.assertTrue(row[k].startswith("[R]"), f"{k} not redacted")
        f = row["findings"][0]
        for k in ("observation", "consequence", "suggestion", "snippet"):
            self.assertTrue(f[k].startswith("[R]"), f"finding.{k} not redacted")
        # Non-LLM metadata must NOT be mangled (the PR link especially).
        self.assertEqual(row["url"], "https://github.com/o/r/pull/42")


class TestClassify(unittest.TestCase):
    def test_red_on_block(self):
        c = RP.classify(_rec("CR-1", verdict="BLOCK"))
        self.assertEqual(c["band"], "red")
        self.assertIn("design=BLOCK", c["why"])

    def test_red_on_large_blast(self):
        c = RP.classify(_rec("CR-2", blast="LARGE"))
        self.assertEqual(c["band"], "red")
        self.assertIn("blast=LARGE", c["why"])

    def test_red_on_open_critical(self):
        c = RP.classify(_rec("CR-3", red=2))
        self.assertEqual(c["band"], "red")
        self.assertIn("2× 🔴", c["why"])

    def test_red_on_regression(self):
        self.assertEqual(RP.classify(_rec("CR-3b", regression=True))["band"], "red")

    def test_yellow_on_medium(self):
        c = RP.classify(_rec("CR-4", risk="medium", blast="MEDIUM"))
        self.assertEqual(c["band"], "yellow")

    def test_yellow_on_two_yellows(self):
        self.assertEqual(RP.classify(_rec("CR-5", yellow=2))["band"], "yellow")

    def test_green_when_clean(self):
        c = RP.classify(_rec("CR-6"))
        self.assertEqual(c["band"], "green")

    def test_score_monotonic(self):
        low = RP.focus_score(_rec("a"))
        high = RP.focus_score(_rec("b", risk="high", blast="LARGE", red=2))
        self.assertGreater(high, low)
        self.assertLessEqual(high, 100)


class TestBuildRender(unittest.TestCase):
    def setUp(self):
        self.records = [
            _rec("CR-RED", risk="high", blast="LARGE", red=2, title="risky"),
            _rec("CR-YEL", risk="medium", blast="MEDIUM", yellow=2, title="meh"),
            _rec("CR-GRN1", title="clean1"),
            _rec("CR-GRN2", title="clean2"),
        ]

    def test_band_counts(self):
        rep = RP.build_report(self.records)
        self.assertEqual(rep["bands"], {"red": 1, "yellow": 1, "green": 2})

    def test_sorted_red_first(self):
        rep = RP.build_report(self.records)
        self.assertEqual(rep["rows"][0]["band"], "red")

    def test_rationale_present(self):
        rep = RP.build_report(self.records)
        for row in rep["rows"]:
            self.assertTrue(row["why"])

    def test_html_hides_green_behind_count(self):
        rep = RP.build_report(self.records)
        h = RP.render_html(rep)
        self.assertIn("Needs review (1)", h)
        self.assertIn("2 clean", h)
        # green changes are inside a <details> (collapsed), not in the open red list
        self.assertIn("<details", h)
        self.assertIn("CR-RED", h)

    def test_html_escapes_title(self):
        rep = RP.build_report([_rec("CR-X", title="<script>bad</script>")])
        h = RP.render_html(rep)
        self.assertNotIn("<script>bad", h)
        self.assertIn("&lt;script&gt;", h)

    def test_html_includes_findings_and_rationale(self):
        rec = _rec("CR-F", risk="medium", blast="MEDIUM", yellow=1, title="fix")
        rec["phase1"]["rationale"] = "Relaxes a guard on the permission path."
        rec["findings"] = [{"dimension": "security", "severity": "yellow",
                            "file": "a.py", "line": 9, "snippet": "raise ValueError",
                            "observation": "fail-open truncation", "consequence": "hook misses",
                            "suggestion": "match full name"}]
        h = RP.render_html(RP.build_report([rec]))
        self.assertIn("fail-open truncation", h)       # observation surfaced
        self.assertIn("hook misses", h)                # consequence surfaced
        self.assertIn("match full name", h)            # suggestion surfaced
        self.assertIn("Relaxes a guard", h)            # rationale fallback surfaced

    def test_html_structured_design_chain(self):
        rec = _rec("CR-D", risk="medium", blast="MEDIUM", yellow=1, title="fix")
        rec["phase1"]["problem"] = "Long commands abort with a cryptic refusal."
        rec["phase1"]["why_it_matters"] = "Any bash command >=256 chars fails for all users."
        rec["phase1"]["solution_assessment"] = "Resolves it but relaxes a guard -> possible bypass."
        h = RP.render_html(RP.build_report([rec]))
        self.assertIn("Long commands abort", h)        # problem
        self.assertIn("for all users", h)              # why it matters
        self.assertIn("possible bypass", h)            # solution assessment
        self.assertIn("Problem", h)                    # labeled chain
        self.assertIn("Why it matters", h)
        self.assertIn("Solution fit", h)

    def test_html_design_headline_leads(self):
        rec = _rec("CR-H", risk="high", blast="LARGE", red=1, title="fix")
        rec["phase1"]["design_headline"] = "Relaxes the auth guard; gate on the owner instead."
        rec["phase1"]["problem"] = "Long commands abort."
        h = RP.render_html(RP.build_report([rec]))
        self.assertIn("Relaxes the auth guard", h)     # design-issue line surfaced first
        self.assertIn("Long commands abort", h)        # chain still shown below

    def test_design_facets_split_newlines_and_legacy_prose(self):
        # Newline-separated facets -> one entry per line.
        self.assertEqual(
            RP._design_facets("Resolution: x\nTradeoffs: y\nAlternatives: z"),
            ["Resolution: x", "Tradeoffs: y", "Alternatives: z"])
        # A long single-paragraph (legacy) assessment is sentence-split so it
        # doesn't render as one dense block.
        legacy = ("The fix resolves the reported crash by adding a length guard "
                  "before the call. However it introduces a subtle race on the "
                  "shared counter that can drop events under load. A lock-free "
                  "counter would avoid the regression entirely.")
        self.assertGreater(len(RP._design_facets(legacy)), 1)
        # A short single line is kept intact (no over-splitting).
        self.assertEqual(RP._design_facets("Short note."), ["Short note."])

    def test_html_design_facets_render_as_labeled_lines(self):
        rec = _rec("CR-FAC", risk="medium", blast="MEDIUM", yellow=1, title="fix")
        rec["phase1"]["solution_assessment"] = (
            "Resolution: fixes the root cause.\n"
            "Tradeoffs: relaxes a guard on the permission path.\n"
            "Alternatives: a scoped check would be safer.")
        h = RP.render_html(RP.build_report([rec]))
        self.assertIn("<strong>Resolution:</strong>", h)   # each facet labeled
        self.assertIn("<strong>Tradeoffs:</strong>", h)
        self.assertIn("<strong>Alternatives:</strong>", h)

    def test_design_facets_applies_redaction(self):
        # Belt-and-suspenders: _design_facets routes the value through the
        # redaction chokepoint before rendering. Patch _redact to a sentinel so
        # the test proves the wiring, not the redaction lib's specific patterns.
        with mock.patch("sage_lib.pipeline._redact",
                        lambda s: s.replace("XSECRETX", "[redacted]")):
            facets = RP._design_facets("Tradeoffs: XSECRETX here")
        joined = " ".join(facets)
        self.assertIn("[redacted]", joined)
        self.assertNotIn("XSECRETX", joined)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reset_clears_index_and_rows(self):
        results.write_result(_rec("CR-RED", risk="high", blast="LARGE", red=1), self.root)
        RP.generate(self.root, slug="old-report")
        RP.reset(self.root)
        idx = json.loads((RP.reports_dir(self.root) / "index.json").read_text())
        self.assertIsNone(idx["report_slug"])
        self.assertEqual(idx["total"], 0)
        self.assertEqual(idx["bands"], {"red": 0, "yellow": 0, "green": 0})
        rows = json.loads((RP.reports_dir(self.root) / "rows.json").read_text())
        self.assertEqual(rows, [])

    def test_generate_writes_index_and_html(self):
        results.write_result(_rec("CR-RED", risk="high", blast="LARGE", red=1), self.root)
        results.write_result(_rec("CR-GRN", title="clean"), self.root)
        out = RP.generate(self.root)
        idx = out["index"]
        self.assertEqual(idx["bands"]["red"], 1)
        self.assertEqual(idx["bands"]["green"], 1)
        self.assertIsNone(idx["report_slug"])
        # files exist
        self.assertTrue((RP.reports_dir(self.root) / "focus-report.html").exists())

    def test_generate_preserves_existing_slug(self):
        results.write_result(_rec("CR-A", risk="high", blast="LARGE", red=1), self.root)
        RP.generate(self.root, slug="my-report")              # explicit slug set
        idx = RP.generate(self.root)["index"]                  # re-run without a slug
        self.assertEqual(idx["report_slug"], "my-report")     # slug survives regeneration
        self.assertTrue((RP.reports_dir(self.root) / "index.json").exists())

    def test_set_slug(self):
        RP.generate(self.root)
        idx = RP.set_report_slug("code-review-sage-focus-report", self.root)
        self.assertEqual(idx["report_slug"], "code-review-sage-focus-report")
        on_disk = json.loads((RP.reports_dir(self.root) / "index.json").read_text())
        self.assertEqual(on_disk["report_slug"], "code-review-sage-focus-report")


if __name__ == "__main__":
    unittest.main()

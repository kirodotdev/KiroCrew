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
        # A finding's KEY NAMES are model-written too -- the boundary validator
        # requires the fields it needs but does not forbid extras, so a worker can
        # name a field anything, a credential included. The REAL redactor leaves
        # plain names like `observation` byte-identical (see
        # test_a_credential_shaped_finding_key_is_redacted); under this stub they
        # are prefixed, and prefixed TWICE because a finding is scrubbed by
        # `_redact_finding` and then again when the row walk reaches it. Real
        # redaction is idempotent, so match on the trailing plain name rather than
        # on a fixed number of prefixes.
        for k in ("observation", "consequence", "suggestion", "snippet"):
            hit = [kk for kk in f if str(kk).endswith(k)]
            self.assertEqual(len(hit), 1, f"finding.{k} key not found: {list(f)}")
            self.assertTrue(str(hit[0]).startswith("[R]"),
                            f"finding.{k} key not redacted")
            self.assertTrue(f[hit[0]].startswith("[R]"),
                            f"finding.{k} not redacted")
        # `url` goes through the redactors too. It lives in the worker-written
        # record, so it is not trusted metadata: an injected reviewer could put an
        # exfiltration link there and the report view renders it as a link. The
        # structural fields the app keys on are the ones held back.
        self.assertTrue(row["url"].startswith("[R]"), "url not redacted")
        # `band` is redacted like every other worker-written string. It used to be
        # the one exemption, on the argument that `bands[]` and `BAND_DOT[]` index
        # on its exact value — but that made it the single field in a row that
        # reached the dashboard verbatim, so a planted "red <credential>" leaked
        # while the prose beside it was scrubbed. Keying is protected instead by
        # admitting only the three known bands on the untrusted read path, and by
        # the fact that the REAL redactor leaves those three byte-identical (see
        # test_a_legitimate_band_survives_redaction_unchanged). Under this test's
        # stub redactor every string is prefixed, band included.
        self.assertTrue(str(row["band"]).startswith("[R]"),
                        "band is worker-authored and must be redacted like the rest")
        # Everything else the worker writes goes through the redactors, including
        # the fields an earlier version of this set wrongly exempted.
        for k in ("change_id", "platform", "gate_verdict", "blast", "design_risk"):
            self.assertTrue(str(row[k]).startswith("[R]"),
                            f"{k} is worker-authored and must be redacted")

    def test_a_credential_shaped_finding_key_is_redacted(self):
        """The security property, with the REAL redactor.

        A worker controls key names as well as values, so `{"<credential>": "..."}`
        puts the secret in the KEY -- which reached report.json and the dashboard
        verbatim while the value beside it was scrubbed. Asserted at both levels
        because they are scrubbed by different code paths: this finding's own names
        by `_redact_finding`, the nested ones by `_redact_deep`. Plain schema names
        must survive unchanged, or redaction would rewrite the report's structure
        instead of its contents.
        """
        cred = "ghp_0123456789abcdefghijklmnopqrstuvwxyzA"
        rec = _rec("42", red=1)
        rec["findings"] = [{
            "dimension": "security", "severity": "red", "file": "a.py", "line": 1,
            "observation": "obs", cred: "planted in the key",
            "nested": {cred: "planted one level down"},
        }]
        f = RP.build_report([rec])["rows"][0]["findings"][0]
        self.assertIn("observation", f)                 # plain name survives
        self.assertNotIn(cred, f)
        self.assertNotIn(cred, " ".join(str(k) for k in f))
        self.assertNotIn(cred, " ".join(str(k) for k in f["nested"]))

    def test_redact_finding_scrubs_its_own_key_names(self):
        """`_redact_finding` holds the guarantee on its own.

        Called from `build_report` the row walk scrubs a finding's keys as well, so
        this level's redaction is not observable end-to-end. Asserted here so the
        helper cannot quietly start depending on a later pass to cover for it.
        """
        cred = "ghp_0123456789abcdefghijklmnopqrstuvwxyzA"
        out = RP._redact_finding({"observation": "obs", cred: "in the key"})
        self.assertIn("observation", out)
        self.assertNotIn(cred, out)
        self.assertNotIn(cred, " ".join(str(k) for k in out))

    def test_a_credential_in_a_worker_written_metadata_field_is_scrubbed(self):
        """`validate_result` enforces a vocabulary for `gate_verdict` only.

        The other metadata fields are worker-authored free strings, so exempting
        them from redaction let an injected value carry a secret into report.json
        and the dashboard.
        """
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        rec = self._rec_with_llm()
        rec["platform"] = f"github {secret}"
        rec["change_id"] = f"GH-o-r-1 {secret}"
        row = RP.build_report([rec])["rows"][0]
        self.assertNotIn(secret, row["platform"])
        self.assertNotIn(secret, row["change_id"])

    def test_legitimate_metadata_values_are_unchanged_by_redaction(self):
        """Redaction is shape-based, so scrubbing these costs nothing."""
        rec = self._rec_with_llm()
        rec["platform"] = "github"
        rec["change_id"] = "GH-acme-widgets-7"
        row = RP.build_report([rec])["rows"][0]
        self.assertEqual(row["platform"], "github")
        self.assertEqual(row["change_id"], "GH-acme-widgets-7")
        self.assertEqual(row["gate_verdict"], "PASS")

    def test_a_real_pr_link_survives_redaction_but_an_exfil_link_does_not(self):
        """The redactors are shape-based, so the UI link keeps working.

        Guards the reason `url` can be redacted at all: ordinary provider links are
        untouched, and only credential/exfiltration-shaped URLs are rewritten.
        """
        rec = self._rec_with_llm()
        rec["url"] = "https://github.com/o/r/pull/42"
        row = RP.build_report([rec])["rows"][0]
        self.assertEqual(row["url"], "https://github.com/o/r/pull/42")

        rec["url"] = "https://evil.example/collect?k=AKIA" + "IOSFODNN7EXAMPLE"
        row = RP.build_report([rec])["rows"][0]
        self.assertNotIn("AKIA", row["url"])


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


class TestOverrideReasonRedaction(unittest.TestCase):
    """``why`` is our own text plus ONE model-written field.

    Every other row field goes through ``_redact`` in ``build_report``; the
    override reason did not, so a credential the reviewer echoed into it reached
    report.json and the in-app report view verbatim.
    """

    def _rec(self, reason: str) -> dict:
        return {
            "schema": "sage.result", "version": 1, "change_id": "CR-1",
            "platform": "github", "repo_identity": "o/r", "url": "",
            "title": "t", "counts": {"red": 0, "yellow": 0},
            "blast_radius": {"rating": "SMALL"},
            "phase1": {
                "gate_verdict": "PASS", "design_risk": "low",
                "criticality": "low",
                "band_override": "red", "band_override_reason": reason,
            },
        }

    def test_a_credential_in_the_override_reason_is_scrubbed(self):
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        row = RP.classify(self._rec("leaks " + secret + " here"))
        self.assertIn("AI override", row["why"])
        self.assertNotIn(secret, row["why"])

    def test_an_ordinary_reason_survives(self):
        row = RP.classify(self._rec("touches the auth boundary"))
        self.assertIn("touches the auth boundary", row["why"])


class TestFindingRedactionCoversEveryString(unittest.TestCase):
    """`file` is model-written too, and it was not in the redacted set.

    The finding fields were enumerated by name, so a credential in `file` reached
    report.json and the dashboard. Redaction now covers every string value.
    """

    def test_a_credential_in_the_file_field_is_scrubbed(self):
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        out = RP._redact_finding({
            "dimension": "Security", "severity": "red",
            "file": f"src/{secret}.py", "line": 5,
            "observation": "o", "consequence": "c", "suggestion": "s",
        })
        self.assertNotIn(secret, out["file"])
        # The numeric line is left as-is.
        self.assertEqual(out["line"], 5)

    def test_an_ordinary_path_survives(self):
        out = RP._redact_finding({"file": "src/app/main.py", "line": 1})
        self.assertEqual(out["file"], "src/app/main.py")

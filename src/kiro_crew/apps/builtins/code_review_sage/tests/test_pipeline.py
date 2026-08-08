"""Unit tests for the review pipeline + result store."""
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from sage_lib import pipeline as P  # noqa: N812
from sage_lib import results as R  # noqa: N812
from sage_lib import store

from kiro_crew import platform_compat


class TestBatchParse(unittest.TestCase):
    def test_mixed_separators_and_dedup(self):
        text = ("https://code.amazon.com/reviews/CR-1\n"
                "CR-2, CR-2\n"
                "  https://github.com/o/r/pull/3  \n"
                "garbage not a link\n"
                "https://github.com/o/r/pull/3/files")
        out = P.parse_batch(text)
        # GitHub-PR-only: non-PR tokens dropped; the PR URL is normalized + deduped.
        self.assertEqual(out, ["https://github.com/o/r/pull/3"])

    def test_empty(self):
        self.assertEqual(P.parse_batch(""), [])


class TestRulePack(unittest.TestCase):
    def test_unmapped_repo_returns_none(self):
        cfg = {"rule_packs": {"github.com/org/other": "some-pack"}}
        self.assertIsNone(P.rule_pack_for_repo("github.com/org/repo", cfg))

    def test_resolve_missing_pack(self):
        self.assertIsNone(P.resolve_rule_pack("no-such-pack-xyz"))

    def test_resolve_rejects_traversal(self):
        self.assertIsNone(P.resolve_rule_pack("../../etc/passwd"))


class TestCommentPayload(unittest.TestCase):
    def test_publish_always_false(self):
        f = {"severity": "red", "file": "a.py", "line": 12, "lang": "python",
             "snippet": "x = 1", "observation": "obs", "consequence": "harm",
             "suggestion": "fix"}
        p = P.build_comment_payload(f, "GH-o-r-9", "sha", platform="github")
        self.assertIs(p["publish"], False)
        self.assertIn("🔴", p["content"])
        self.assertIn("x = 1", p["content"])
        self.assertIn("[code-review-sage]", p["content"])
        self.assertEqual(p["path"], "a.py")

    def test_yellow_severity(self):
        p = P.build_comment_payload({"severity": "yellow", "observation": "o"}, "GH-o-r-1", "s")
        self.assertIn("🟡", p["content"])
        self.assertIs(p["publish"], False)

    def test_posting_spec_is_platform_keyed(self):
        self.assertIn("gh api", P.posting_spec("github")["tool"])
        self.assertIn("gh api", P.posting_spec("unknown")["tool"])  # github fallback

    def test_github_comment_payload_anchor(self):
        f = {"severity": "red", "file": "src/a.rs", "line": 12, "lang": "rust",
             "snippet": "x", "observation": "o", "consequence": "c", "suggestion": "s"}
        p = P.build_comment_payload(f, "GH-o-r-5", "deadbeef", platform="github")
        self.assertIs(p["publish"], False)          # PENDING review — human submits
        self.assertEqual(p["path"], "src/a.rs")
        self.assertEqual(p["line"], 12)
        self.assertEqual(p["side"], "RIGHT")
        self.assertEqual(p["commit_id"], "deadbeef")  # head SHA
        self.assertIn("🔴", p["content"])

    def test_unsupported_posting_platform_raises(self):
        with self.assertRaises(ValueError):
            P.build_comment_payload({"severity": "yellow", "observation": "o"},
                                    "x", "1", platform="gitlab")


class TestGithubReviewPayload(unittest.TestCase):
    def _rec(self):
        return {
            "revision": "abc123sha",
            "pending_comments": [
                {"kind": "finding", "file": "src/a.rs", "line": 5, "body": "F1 body"},
                {"kind": "finding", "file": "src/b.rs", "line": 9, "body": "F2 body"},
                {"kind": "design", "body": "Ship summary body"},
            ],
        }

    def test_builds_pending_review_envelope(self):
        pay = P.build_github_review_payload(self._rec())
        # No `event` key -> PENDING (unsubmitted) review — the draft invariant.
        self.assertNotIn("event", pay)
        self.assertEqual(pay["commit_id"], "abc123sha")
        self.assertEqual(pay["body"], "Ship summary body")
        self.assertEqual(len(pay["comments"]), 2)
        self.assertEqual(pay["comments"][0],
                         {"path": "src/a.rs", "line": 5, "side": "RIGHT", "body": "F1 body"})

    def test_unanchored_finding_folds_into_body(self):
        rec = {"revision": "s", "pending_comments": [
            {"kind": "finding", "file": "", "line": 0, "body": "no-anchor finding"},
            {"kind": "design", "body": "summary"},
        ]}
        pay = P.build_github_review_payload(rec)
        self.assertEqual(pay["comments"], [])          # nothing anchorable
        self.assertIn("summary", pay["body"])
        self.assertIn("no-anchor finding", pay["body"])  # folded in, not dropped

    def test_refuses_to_build_an_unanchored_payload(self):
        """No `revision` must refuse, not omit `commit_id`.

        GitHub anchors a review with no `commit_id` to the pull request's CURRENT
        head. The submit guard's stale-head check then compares the draft's head to
        the live head and matches — because GitHub stamped it at post time, not
        because anything reviewed that code. APPROVE would authorize an unreviewed
        head. `revision` is not a required result-contract key, so a contract-valid
        record can arrive without one; this is the refusal for that case.
        """
        rec = {"pending_comments": [{"kind": "design", "body": "s"}]}
        with self.assertRaises(ValueError) as ctx:
            P.build_github_review_payload(rec)
        self.assertIn("commit_id", str(ctx.exception))

    def test_refuses_when_revision_is_empty_or_whitespace(self):
        """An empty or blank `revision` is as unanchored as a missing one."""
        for rev in ("", "   ", None):
            with self.subTest(revision=rev):
                rec = {"revision": rev,
                       "pending_comments": [{"kind": "design", "body": "s"}]}
                with self.assertRaises(ValueError):
                    P.build_github_review_payload(rec)

    def test_redacts_bodies_at_egress(self):
        # Defense-in-depth: even if a body reaches the payload builder unredacted,
        # the external-egress point re-runs _redact.
        rec = {"revision": "XSECRETX", "pending_comments": [
            {"kind": "finding", "file": "src/XSECRETX.py", "line": 1, "body": "leak XSECRETX"},
            {"kind": "design", "body": "summary XSECRETX"},
        ]}
        with mock.patch("sage_lib.pipeline._redact",
                        lambda s: s.replace("XSECRETX", "[redacted]")):
            pay = P.build_github_review_payload(rec)
        blob = (pay["body"] + " " + " ".join(c["body"] for c in pay["comments"])
                + " " + " ".join(c["path"] for c in pay["comments"]) + " " + pay["commit_id"])
        self.assertNotIn("XSECRETX", blob)
        self.assertIn("[redacted]", blob)
        self.assertEqual(pay["comments"][0]["path"], "src/[redacted].py")  # path redacted
        self.assertEqual(pay["commit_id"], "[redacted]")                    # commit_id redacted


class TestFetchSpec(unittest.TestCase):
    def test_github_uses_gh_cli(self):
        spec = P.fetch_spec("github")
        self.assertIn("gh api", spec)
        self.assertIn("pulls/<number>/files", spec)

    def test_unknown_platform_falls_back_to_github(self):
        self.assertEqual(P.fetch_spec("gitlab"), P.fetch_spec("github"))


class TestResultStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)
        self.rec = {
            "schema": "code-review-sage-result", "version": 1,
            "change_id": "GH-o-r-555", "platform": "github",
            "repo_identity": "github.com/o/r", "revision": "2",
            "phase1": {"gate_verdict": "CONCERNS", "design_risk": "medium",
                       "criticality": "medium", "rationale": "ok"},
            "findings": [{"dimension": "security", "severity": "red"}],
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_read_roundtrip(self):
        R.write_result(self.rec, self.root)
        got = R.read_result("GH-o-r-555", self.root)
        self.assertEqual(got["phase1"]["gate_verdict"], "CONCERNS")

    @unittest.skipUnless(
        platform_compat.IS_POSIX,
        "POSIX mode bits are unobservable on Windows: the owner-only lockdown there is an "
        "ACL (platform_compat.restrict_to_owner), and st_mode always reports 0o666.",
    )
    def test_mode_0600(self):
        p = R.write_result(self.rec, self.root)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_list_results(self):
        R.write_result(self.rec, self.root)
        self.assertEqual(len(R.list_results(self.root)), 1)

    def test_validation_rejects_missing(self):
        bad = {"schema": "x", "version": 1, "change_id": "CR-1"}
        with self.assertRaises(ValueError):
            R.write_result(bad, self.root)

    def test_validation_bad_verdict(self):
        bad = dict(self.rec)
        bad["phase1"] = {"gate_verdict": "MAYBE", "design_risk": "low", "criticality": "low"}
        self.assertTrue(any("gate_verdict" in e for e in R.validate_result(bad)))

    def test_safe_change_id(self):
        self.assertEqual(R.safe_change_id("github:org/repo#123"), "github_org_repo_123")
        self.assertEqual(R.safe_change_id("CR-12345678"), "CR-12345678")


class TestCommentBuilders(unittest.TestCase):
    """The driver builds the CR comment bodies in Python and redacts them HERE
    (deterministic chokepoint); the poster posts them verbatim."""

    def test_build_ship_comment_redacts(self):
        rec: dict[str, Any] = {"phase1": {"gate_verdict": "CONCERNS"},
               "counts": {"red": 0, "yellow": 1}, "ship_summary": "leak XSECRETX"}
        with mock.patch("sage_lib.pipeline._redact",
                        lambda s: s.replace("XSECRETX", "[redacted]")):
            body = P.build_ship_comment(rec)
        self.assertIn("[redacted]", body)
        self.assertNotIn("XSECRETX", body)

    def test_build_ship_comment_ship_decision(self):
        # Good to ship: PASS + zero red -> "Good to ship"; should-fix counted but
        # never gates the call.
        ready = {"phase1": {"gate_verdict": "PASS"}, "counts": {"red": 0, "yellow": 2},
                 "ship_summary": "No blocking issues; 2 optional notes."}
        rbody = P.build_ship_comment(ready)
        self.assertIn("Good to ship", rbody)
        self.assertIn("2 should-fix", rbody)
        self.assertNotIn("Not ready", rbody)
        # A red must-fix flips it to not-ready.
        blocked = {"phase1": {"gate_verdict": "PASS"}, "counts": {"red": 1, "yellow": 0},
                   "ship_summary": "1 must-fix: unbounded cache."}
        bbody = P.build_ship_comment(blocked)
        self.assertIn("Not ready to ship", bbody)
        self.assertIn("1 must-fix", bbody)
        # A genuine design BLOCK is not-ready even with zero red findings.
        design = {"phase1": {"gate_verdict": "BLOCK"}, "counts": {"red": 0, "yellow": 0},
                  "ship_summary": "Wrong layer."}
        dbody = P.build_ship_comment(design)
        self.assertIn("Not ready to ship", dbody)
        self.assertIn("design flagged", dbody)

    def test_build_ship_comment_null_ship_summary_falls_back(self):
        # An explicit JSON null must NOT leak the literal "None" — the fallback
        # (design headline, then a deterministic phrase) has to fire.
        rec = {"phase1": {"gate_verdict": "PASS", "design_headline": None},
               "counts": {"red": 0, "yellow": 0}, "ship_summary": None}
        body = P.build_ship_comment(rec)
        self.assertNotIn("None", body)
        self.assertIn("No blocking must-fix issues found.", body)
        # Explicit-null ship_summary with a real design headline uses the headline.
        rec2 = {"phase1": {"gate_verdict": "PASS", "design_headline": "solid design"},
                "counts": {"red": 0, "yellow": 0}, "ship_summary": None}
        self.assertIn("solid design", P.build_ship_comment(rec2))

    def test_build_pending_comments_structure(self):
        rec: dict[str, Any] = {"phase1": {"gate_verdict": "CONCERNS"},
               "counts": {"red": 0, "yellow": 1}, "ship_summary": "s",
               "findings": [{"file": "a.py", "line": 5, "severity": "yellow",
                             "observation": "o", "consequence": "c",
                             "suggestion": "s", "snippet": "x"}]}
        pend = P.build_pending_comments(rec)
        self.assertEqual([e["kind"] for e in pend], ["finding", "design"])
        self.assertEqual((pend[0]["file"], pend[0]["line"]), ("a.py", 5))
        self.assertTrue(pend[0]["body"] and pend[1]["body"])
        # PASS -> the ship-readiness (design) comment is STILL emitted (always-on)
        rec["phase1"]["gate_verdict"] = "PASS"
        self.assertEqual([e["kind"] for e in P.build_pending_comments(rec)], ["finding", "design"])

    def test_build_pending_comments_redacts_all_bodies(self):
        rec = {"phase1": {"gate_verdict": "BLOCK", "solution_assessment": "XSECRETX"},
               "findings": [{"file": "a.py", "line": 1, "severity": "red",
                             "observation": "XSECRETX", "consequence": "c",
                             "suggestion": "s", "snippet": "y"}]}
        with mock.patch("sage_lib.pipeline._redact",
                        lambda s: s.replace("XSECRETX", "[redacted]")):
            pend = P.build_pending_comments(rec)
        joined = " ".join(e["body"] for e in pend)
        self.assertNotIn("XSECRETX", joined)
        self.assertIn("[redacted]", joined)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""A review must not write to the pull request unless asked to.

Reviews are READ in the app now, so publishing findings back to someone else's
PR is an opt-in side effect (``review.auto_post``), not a consequence of running
a review. These tests pin the default OFF and the accounting that goes with it —
in particular that the durable dedup index still records the PR as reviewed, so
a repo review does not re-review it forever.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sage_lib import results
from sage_lib import review_driver as D
from sage_lib import store


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dispatch(self, tasks: list[str], run_id: str | None = None):
        """Records every dispatched task; a review task writes its result record.

        Task kinds are told apart by the same marker strings the real prompts
        carry (see review_driver.build_*_task), so "was a poster dispatched?" is
        answered the way the driver actually distinguishes them.
        """
        def dispatch(task: str, timeout: int = 0):
            tasks.append(task)
            if "SINGLE thorough pass" in task:
                results.write_result({
                    "schema": "code-review-sage-result", "version": 1,
                    "change_id": "CR-1", "platform": "github",
                    "repo_identity": "github.com/o/r", "revision": "1",
                    "phase1": {"gate_verdict": "PASS", "design_risk": "low",
                               "criticality": "low"},
                    "blast_radius": {"rating": "SMALL", "signals": {}},
                    "counts": {"red": 0, "yellow": 1},
                    "findings": [{"dimension": "correctness", "severity": "yellow",
                                  "file": "f", "line": 1, "snippet": "x",
                                  "observation": "o", "consequence": "c",
                                  "suggestion": "s"}],
                    "deep_reviewed": True, "title": "CR-1",
                    "files_covered": ["f"], "coverage_complete": True,
                }, self.root, run_id)
            return {"ok": True, "output": "done", "error": ""}
        return dispatch

    @staticmethod
    def _posters(tasks: list[str]) -> list[str]:
        return [t for t in tasks if "pre-redacted DRAFT review comments" in t]


class TestNoAutoPost(_Base):
    def test_default_dispatches_no_poster(self):
        tasks: list[str] = []
        out = D.run_review(["CR-1"], dispatch=self._dispatch(tasks),
                           generate_report=False, root=self.root)
        self.assertEqual(out["changes"], 1)
        # The review task ran; no posting task was ever dispatched.
        self.assertTrue(tasks)
        self.assertFalse(self._posters(tasks), "a poster was dispatched")

    def test_default_marks_posting_skipped(self):
        out = D.run_review(["CR-1"], dispatch=self._dispatch([]),
                           generate_report=False, root=self.root)
        rec = out["per_change"][0]
        self.assertTrue(rec["posting_skipped"])
        self.assertEqual(rec["posted_comments"], 0)
        # 0 expected, not red+yellow+1: nothing was meant to be delivered, so the
        # dedup index's posted >= expected check must not read this as a failure.
        self.assertEqual(rec["posting_expected"], 0)
        self.assertTrue(rec["post_ok"])
        self.assertFalse(rec["design_comment_posted"])

    def test_reviewed_index_still_accepts_the_change(self):
        # The guard that refuses to mark a PR reviewed when a post half-failed
        # must not also refuse a run that deliberately posted nothing, or every
        # repo review would re-review the same PRs forever.
        out = D.run_review(["CR-1"], dispatch=self._dispatch([]),
                           generate_report=False, root=self.root)
        rec = out["per_change"][0]
        self.assertTrue(rec["deep_reviewed"])
        self.assertGreaterEqual(rec["posted_comments"], rec["posting_expected"])

    def test_progress_reports_zero_posted(self):
        seen: dict = {}

        def prog(cid, phase, extra=None):
            if phase == "done":
                seen[cid] = extra or {}
        D.run_review(["CR-1"], dispatch=self._dispatch([]), generate_report=False,
                     root=self.root, progress=prog)
        self.assertEqual(seen["CR-1"]["posted"], 0)
        self.assertEqual(seen["CR-1"]["expected"], 0)
        # The findings themselves are still reported — they are what the app shows.
        self.assertEqual(seen["CR-1"]["counts"], {"red": 0, "yellow": 1})

    def test_findings_still_land_in_the_report(self):
        # Not posting must not mean not reviewing: the report is the deliverable.
        out = D.run_review(["CR-1"], dispatch=self._dispatch([], run_id="r1"),
                           archiver=lambda *_a, **_k: None,
                           generate_report=True, root=self.root, run_id="r1")
        self.assertEqual(out["result_records"], 1)
        payload = json.loads(
            (store.run_dir("r1", self.root) / "report" / "report.json")
            .read_text(encoding="utf-8"))
        self.assertTrue(payload["rows"])

    def test_opt_in_restores_posting(self):
        tasks: list[str] = []
        D.run_review(["CR-1"], dispatch=self._dispatch(tasks),
                     generate_report=False, root=self.root, post=True)
        self.assertTrue(self._posters(tasks), "posting was enabled but no poster ran")

    def test_config_flag_enables_posting(self):
        cfg_path = store.data_dir(self.root) / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["review"]["auto_post"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        tasks: list[str] = []
        D.run_review(["CR-1"], dispatch=self._dispatch(tasks),
                     generate_report=False, root=self.root)
        self.assertTrue(self._posters(tasks))

    def test_non_boolean_config_does_not_enable_posting(self):
        # A stray string must never be enough to start writing to pull requests.
        cfg_path = store.data_dir(self.root) / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["review"]["auto_post"] = "true"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        tasks: list[str] = []
        D.run_review(["CR-1"], dispatch=self._dispatch(tasks),
                     generate_report=False, root=self.root)
        self.assertFalse(self._posters(tasks))

    def test_default_config_has_posting_off(self):
        cfg = store.load_config(self.root)
        self.assertIs(cfg["review"]["auto_post"], False)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestSilentRunIsStillRecordedReviewed(unittest.TestCase):
    """A non-posting run must still be indexed as reviewed at its head.

    The completion guard read `posting_expected or 1`, which turned the silent
    path's legitimate 0 into 1: `0 >= 1` was false, the change never entered
    reviewed.json, and every unchanged PR was re-reviewed forever.
    """

    def _indexed(self, rec: dict) -> bool:
        """Calls the REAL guard from backend/routes.py.

        An earlier version of this test re-implemented the comparison locally,
        which passed with the bug still present — it was testing the mirror.
        """
        from backend import routes
        return (bool(rec.get("deep_reviewed")) and bool(rec.get("post_ok"))
                and (rec.get("posted_comments") or 0)
                >= routes._posting_expected(rec))

    def test_a_silent_review_counts_as_reviewed(self):
        self.assertTrue(self._indexed({
            "deep_reviewed": True, "post_ok": True,
            "posted_comments": 0, "posting_expected": 0}))

    def test_a_partial_post_is_not_recorded(self):
        self.assertFalse(self._indexed({
            "deep_reviewed": True, "post_ok": True,
            "posted_comments": 1, "posting_expected": 3}))

    def test_a_record_without_the_field_still_needs_one_delivery(self):
        # Records written before `posting_expected` existed keep the old meaning.
        self.assertFalse(self._indexed({
            "deep_reviewed": True, "post_ok": True, "posted_comments": 0}))
        self.assertTrue(self._indexed({
            "deep_reviewed": True, "post_ok": True, "posted_comments": 1}))


class TestRecordsKeptWhenPostingFails(_Base):
    """The cleanup after archiving must be gated on DELIVERY, not on intent.

    ``review.auto_post`` says the user wants findings posted; it does not say the
    post succeeded. Records hold the only copy of the redacted comment payload, so
    deleting them after a failed post leaves the explicit "post comments" retry
    with nothing to send while the PR shows only part of the review.
    """

    def _dispatch_failing_poster(self, tasks: list[str]):
        """Reviews succeed and record a result; the POSTER dispatch fails."""
        inner = self._dispatch(tasks)

        def dispatch(task: str, timeout: int = 0):
            if "pre-redacted DRAFT review comments" in task:
                tasks.append(task)
                return {"ok": False, "output": "", "error": "poster dispatch failed"}
            return inner(task, timeout)
        return dispatch

    def test_a_failed_post_keeps_the_result_records(self):
        tasks: list[str] = []
        out = D.run_review(["CR-1"], dispatch=self._dispatch_failing_poster(tasks),
                           generate_report=True, archiver=lambda html, root: "slug-1",
                           root=self.root, post=True)
        # A poster WAS dispatched (posting was intended) and it failed.
        self.assertTrue(self._posters(tasks), "no poster was dispatched")
        self.assertFalse(out["per_change"][0]["post_ok"])
        # The records the retry needs are still on disk, and the summary says why.
        self.assertNotIn("results_cleaned", out)
        self.assertTrue(out.get("results_kept_undelivered"))
        self.assertTrue(results.list_results(self.root, out.get("run_id")),
                        "result records were deleted after a failed post")

    def test_the_cleanup_still_runs_once_delivery_is_complete(self):
        # The guard must not become a leak: on real delivery the records ARE
        # redundant (their content is in the archived report and on the PR).
        # The fake poster in this harness never writes delivery evidence, so the
        # predicate is pinned here to prove the WIRING — that a delivered run
        # still reaches clear_results — rather than re-testing the predicate.
        tasks: list[str] = []
        with mock.patch.object(D, "_all_delivered", return_value=True):
            out = D.run_review(["CR-1"], dispatch=self._dispatch(tasks),
                               generate_report=True,
                               archiver=lambda html, root: "slug-1",
                               root=self.root, post=True)
        self.assertIn("results_cleaned", out)
        self.assertFalse(out.get("results_kept_undelivered"))

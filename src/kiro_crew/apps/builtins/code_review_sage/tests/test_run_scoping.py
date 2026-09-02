"""Tests for per-run storage isolation + driver cancellation.

Covers the ``run_id`` scoping added across store.py / results.py / report.py and
the cooperative ``cancelled=`` predicate added to ``review_driver.run_review``:

  * every run owns a private ``data/runs/<run-id>/`` subtree, so two runs never
    clobber each other's result records or reports;
  * ``run_id=None`` keeps the legacy shared ``data/results`` + ``data/reports``
    layout (back-compat with the standalone CLI path);
  * ``reviewed.json`` + ``config.json`` stay GLOBAL (durable cross-run state);
  * ``safe_run_id`` collapses path separators so a run id can't escape its root;
  * ``run_review`` scopes its records/report to the run dir and honors a
    ``cancelled`` predicate by skipping unstarted changes (no dispatch).
"""
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from sage_lib import report as RP  # noqa: N812
from sage_lib import results
from sage_lib import review_driver as D  # noqa: N812
from sage_lib import store


def _rec(change_id, verdict="PASS", risk="low", blast="SMALL", red=0, yellow=0,
         deep=True, title="t"):
    """A minimal but contract-valid result record (mirrors test_report._rec)."""
    return {
        "schema": "code-review-sage-result", "version": 1, "change_id": change_id,
        "platform": "github", "repo_identity": "github.com/o/r",
        "url": f"https://github.com/o/r/pull/{change_id}", "title": title,
        "phase1": {"gate_verdict": verdict, "design_risk": risk, "criticality": "low"},
        "blast_radius": {"rating": blast, "signals": {}},
        "counts": {"red": red, "yellow": yellow},
        "deep_reviewed": deep,
    }


class _RootTest(unittest.TestCase):
    """Shared tmpdir root, self-healed to a fresh data layout (like the other
    persistence tests in this package)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRunScopedResults(_RootTest):
    """(1) run isolation, (2) legacy shared paths, (8) global dedup/config."""

    def test_two_runs_get_separate_results_dirs(self):
        results.write_result(_rec("CR-A"), self.root, run_id="run-a")
        results.write_result(_rec("CR-B"), self.root, run_id="run-b")

        dir_a = results.results_dir(self.root, "run-a")
        dir_b = results.results_dir(self.root, "run-b")
        self.assertNotEqual(dir_a, dir_b)
        self.assertIn("run-a", str(dir_a))
        self.assertIn("run-b", str(dir_b))

        self.assertEqual([r["change_id"] for r in results.list_results(self.root, "run-a")],
                         ["CR-A"])
        self.assertEqual([r["change_id"] for r in results.list_results(self.root, "run-b")],
                         ["CR-B"])

    def test_clearing_one_run_leaves_the_other_intact(self):
        results.write_result(_rec("CR-A"), self.root, run_id="run-a")
        results.write_result(_rec("CR-B"), self.root, run_id="run-b")

        removed = results.clear_results(self.root, "run-a")
        self.assertEqual(removed, 1)
        self.assertEqual(results.list_results(self.root, "run-a"), [])
        # sibling run untouched
        self.assertEqual([r["change_id"] for r in results.list_results(self.root, "run-b")],
                         ["CR-B"])

    def test_run_id_none_uses_legacy_shared_paths(self):
        # Back-compat: no run_id == the pre-existing shared dirs.
        self.assertEqual(results.results_dir(self.root, None),
                         store.data_dir(self.root) / "results")
        self.assertEqual(RP.reports_dir(self.root, None),
                         store.data_dir(self.root) / "reports")

        results.write_result(_rec("CR-LEG"), self.root)   # no run_id
        self.assertTrue((store.data_dir(self.root) / "results" / "CR-LEG.json").exists())
        self.assertEqual([r["change_id"] for r in results.list_results(self.root)], ["CR-LEG"])
        # a shared-dir write is invisible to any run-scoped view
        self.assertEqual(results.list_results(self.root, "run-a"), [])

    def test_reviewed_and_config_stay_global_not_per_run(self):
        rid = "run-xyz"
        # reviewed.json + config.json live in data/, never under data/runs/<id>/.
        self.assertEqual(results.reviewed_path(self.root),
                         store.data_dir(self.root) / "reviewed.json")
        self.assertNotIn(rid, str(results.reviewed_path(self.root)))
        cfg_path = store.data_dir(self.root) / "config.json"
        self.assertNotIn(rid, str(cfg_path))

        results.mark_reviewed(
            {"CR-A": {"head_sha": "abc", "reviewed_at": "t", "run_id": rid}}, self.root)
        self.assertTrue((store.data_dir(self.root) / "reviewed.json").exists())
        # not duplicated into the run subtree
        self.assertFalse((store.runs_root(self.root) / rid / "reviewed.json").exists())


class TestSafeRunId(_RootTest):
    """(3) traversal defense in safe_run_id / run_dir."""

    def test_collapses_to_single_path_segment(self):
        # Every id becomes ONE filesystem segment (no separators survive).
        for raw in ("../../etc", "a/b", ".."):
            sid = store.safe_run_id(raw)
            self.assertNotIn("/", sid)
            self.assertNotIn(os.sep, sid)
            self.assertTrue(sid)   # never empty

    def test_run_dir_stays_inside_runs_root(self):
        rr = store.runs_root(self.root).resolve()
        for raw in ("../../etc", "a/b", "normal-id"):
            self.assertEqual(store.run_dir(raw, self.root).resolve().parent, rr,
                             f"{raw!r} escaped runs_root")

    def test_run_dir_dotdot_is_contained(self):
        # Regression: the _UNSAFE_RUN_ID character filter treats '.' as SAFE, so a
        # run id of exactly '..' used to pass through unchanged and run_dir('..')
        # resolved to the parent data/ dir — escaping runs_root entirely. The
        # all-dots rejection in safe_run_id is what closes that.
        rr = store.runs_root(self.root).resolve()
        for raw in ("..", ".", "...", "./.."):
            self.assertEqual(store.run_dir(raw, self.root).resolve().parent, rr,
                             f"{raw!r} escaped runs_root")
        self.assertEqual(store.safe_run_id(".."), "unknown")


class TestRunDirLifecycle(_RootTest):
    """(4) remove_run_dir, (5) list_run_ids."""

    def test_remove_run_dir_removes_only_its_own_subtree(self):
        results.write_result(_rec("CR-A"), self.root, run_id="run-a")
        results.write_result(_rec("CR-B"), self.root, run_id="run-b")
        self.assertTrue(store.run_dir("run-a", self.root).exists())

        self.assertTrue(store.remove_run_dir("run-a", self.root))
        self.assertFalse(store.run_dir("run-a", self.root).exists())
        # sibling + parent survive
        self.assertTrue(store.run_dir("run-b", self.root).exists())
        self.assertTrue(store.runs_root(self.root).exists())

    def test_remove_run_dir_missing_returns_false(self):
        self.assertFalse(store.remove_run_dir("nope", self.root))

    def test_remove_run_dir_never_raises(self):
        # Idempotent teardown: removing a run twice (already gone) must not raise.
        store.ensure_run_layout("run-a", self.root)
        self.assertTrue(store.remove_run_dir("run-a", self.root))
        try:
            self.assertFalse(store.remove_run_dir("run-a", self.root))
        except Exception as e:  # noqa: BLE001 - the whole point is that it must not raise
            self.fail(f"remove_run_dir raised on a missing run: {e!r}")

    def test_list_run_ids_reflects_on_disk_dirs(self):
        self.assertEqual(store.list_run_ids(self.root), [])
        store.ensure_run_layout("run-a", self.root)
        store.ensure_run_layout("run-b", self.root)
        self.assertEqual(store.list_run_ids(self.root), ["run-a", "run-b"])   # sorted
        store.remove_run_dir("run-a", self.root)
        self.assertEqual(store.list_run_ids(self.root), ["run-b"])


class TestRunScopedReport(_RootTest):
    """(6) write_outputs report.json (all bands) vs rows.json (focus), round-trip;
    (7) read_report None when the run produced no report."""

    def _mixed(self):
        return [
            _rec("CR-RED", risk="high", blast="LARGE", red=2, title="risky"),
            _rec("CR-YEL", risk="medium", blast="MEDIUM", yellow=2, title="meh"),
            _rec("CR-GRN", title="clean"),
        ]

    def test_write_outputs_report_json_all_bands_rows_json_focus_only(self):
        rep = RP.build_report(self._mixed())
        RP.write_outputs(rep, "<html/>", self.root, slug="s1", run_id="run-x")
        rd = RP.reports_dir(self.root, "run-x")
        self.assertIn("run-x", str(rd))

        rows = json.loads((rd / "rows.json").read_text(encoding="utf-8"))
        full = json.loads((rd / "report.json").read_text(encoding="utf-8"))

        # rows.json keeps ONLY the red + yellow focus subset
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["band"] for r in rows}, {"red", "yellow"})
        # report.json keeps EVERY band
        self.assertEqual(full["bands"], {"red": 1, "yellow": 1, "green": 1})
        self.assertEqual(len(full["rows"]), 3)
        self.assertEqual({r["band"] for r in full["rows"]}, {"red", "yellow", "green"})

    def test_read_report_round_trips_bands_rows_total_slug(self):
        rep = RP.build_report(self._mixed())
        RP.write_outputs(rep, "<html/>", self.root, slug="slug-42", run_id="run-x")

        got = RP.read_report(self.root, "run-x")
        self.assertIsNotNone(got)
        self.assertEqual(got["bands"], {"red": 1, "yellow": 1, "green": 1})
        self.assertEqual(got["total"], 3)
        self.assertEqual(len(got["rows"]), 3)
        self.assertEqual(got["report_slug"], "slug-42")

    def test_read_report_none_when_no_report(self):
        # A run dir exists (layout ensured) but generate/write_outputs never ran.
        store.ensure_run_layout("run-empty", self.root)
        self.assertIsNone(RP.read_report(self.root, "run-empty"))


class TestRunReviewScoping(unittest.TestCase):
    """(9) run_review writes into the run dir, (10) cancelled skips unstarted
    changes with no dispatch, (11) cancelled=lambda: False behaves normally."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "apps" / "code-review-sage"
        store.ensure_layout(self.root)
        self.calls = []
        self.lock = threading.Lock()
        self.archived = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archiver(self, html, root=None):
        self.archived.append(html)
        return "sage-report-test"

    def _fake_dispatch(self, run_id=None, verdict="PASS"):
        """Single-pass reviewer model (mirrors test_review_driver._fake_dispatch)
        but writes/reads under ``run_id`` so the driver's run-scoped read hits the
        same file the (fake) worker wrote."""
        def dispatch(task, timeout=0):
            with self.lock:
                self.calls.append(task)
            m = re.search(r"CR-\d+", task)
            if m:
                cid = m.group(0)
                if "SINGLE thorough pass" in task:
                    results.write_result({
                        "schema": "code-review-sage-result", "version": 1,
                        "change_id": cid, "platform": "github",
                        "repo_identity": "github.com/o/r", "revision": "1",
                        "phase1": {"gate_verdict": verdict, "design_risk": "low",
                                   "criticality": "low"},
                        "blast_radius": {"rating": "SMALL", "signals": {}},
                        "counts": {"red": 0, "yellow": 1},
                        "findings": [{"dimension": "correctness", "severity": "yellow",
                                      "file": "f", "line": 1, "snippet": "x",
                                      "observation": "o", "consequence": "c",
                                      "suggestion": "s"}],
                        "deep_reviewed": True, "title": cid,
                        "files_covered": ["f"], "coverage_complete": True,
                    }, self.root, run_id=run_id)
                elif "pre-redacted DRAFT review comments" in task:
                    rec = results.read_result(cid, self.root, run_id=run_id) or {}
                    pending = rec.get("pending_comments", []) or []
                    rec["posted_comments"] = len(pending)
                    rec["design_comment_posted"] = any(
                        e.get("kind") == "design" for e in pending)
                    results.write_result(rec, self.root, run_id=run_id)
            return {"ok": True, "output": "done", "error": ""}
        return dispatch

    def test_run_review_run_id_writes_report_into_run_dir(self):
        out = D.run_review(["CR-1"], dispatch=self._fake_dispatch(run_id="run-x"),
                           archiver=self._archiver, root=self.root, run_id="run-x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["report_slug"], "sage-report-test")

        rd = RP.reports_dir(self.root, "run-x")
        self.assertIn("run-x", str(rd))
        self.assertTrue((rd / "report.json").is_file())
        self.assertIsNotNone(RP.read_report(self.root, "run-x"))
        # the legacy shared report dir got nothing from this run-scoped run
        self.assertFalse((store.data_dir(self.root) / "reports" / "report.json").is_file())

    def test_run_review_run_id_scopes_result_records(self):
        D.run_review(["CR-1", "CR-2"], dispatch=self._fake_dispatch(run_id="run-y"),
                     generate_report=False, root=self.root, run_id="run-y")
        # records live under the run's private dir, not the shared one
        self.assertEqual(len(results.list_results(self.root, "run-y")), 2)
        self.assertEqual(results.list_results(self.root), [])
        self.assertIn("run-y", str(results.results_dir(self.root, "run-y")))

    def test_run_review_cancelled_skips_unstarted_changes(self):
        seen = []
        plock = threading.Lock()

        def prog(cid, phase, extra=None):
            with plock:
                seen.append((cid, phase))

        out = D.run_review(["CR-1", "CR-2", "CR-3"],
                           dispatch=self._fake_dispatch(run_id="run-c"),
                           generate_report=False, root=self.root, run_id="run-c",
                           cancelled=lambda: True, progress=prog)

        # every change reported cancelled, none dispatched
        self.assertEqual(out["cancelled"], 3)
        self.assertEqual(self.calls, [])   # NO review/poster task dispatched at all
        self.assertFalse(any("SINGLE thorough pass" in c for c in self.calls))

        # each change surfaced via the progress callback as 'cancelled'
        cancelled_ids = sorted({c for (c, p) in seen if p == "cancelled"})
        self.assertEqual(cancelled_ids, ["CR-1", "CR-2", "CR-3"])
        self.assertNotIn("reviewing", [p for (_c, p) in seen])

        # per-change records marked cancelled with nothing recorded
        for r in out["per_change"]:
            self.assertTrue(r["cancelled"])
            self.assertFalse(r["result_recorded"])
            self.assertEqual(r["gate_verdict"], "CANCELLED")
        # no report generated for a fully-cancelled run
        self.assertNotIn("report_slug", out)

    def test_run_review_cancelled_false_behaves_normally(self):
        out = D.run_review(["CR-1", "CR-2"],
                           dispatch=self._fake_dispatch(run_id="run-n"),
                           archiver=self._archiver, root=self.root, run_id="run-n",
                           cancelled=lambda: False)
        self.assertTrue(out["ok"])
        self.assertEqual(out["changes"], 2)
        self.assertEqual(out["cancelled"], 0)
        self.assertEqual(out["deep_reviewed"], 2)
        self.assertEqual(out["report_slug"], "sage-report-test")
        # a review pass was dispatched for the changes (not short-circuited)
        self.assertTrue(any("SINGLE thorough pass" in c for c in self.calls))


if __name__ == "__main__":
    unittest.main()

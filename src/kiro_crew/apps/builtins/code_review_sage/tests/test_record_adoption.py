#!/usr/bin/env python3
"""The worker writes ``data/results/<id>.json``; the run reads its own dir.

Regression for a real failure: per-run isolation moved the READ path to
``data/runs/<run_id>/results/`` but the reviewing worker's prompt (and the
`sage-review` skill) still name the shared ``data/results/<id>.json``. The run
dir stayed empty, so a review that had genuinely completed reported
``result_records: 0`` and the UI showed an empty report while claiming "done".

The driver owns run scoping, so it adopts the worker's record after each turn.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import results
from sage_lib import review_driver as D
from sage_lib import store


def _record(cid: str = "CR-1") -> dict:
    return {
        "schema": "code-review-sage-result", "version": 1, "change_id": cid,
        "platform": "github", "repo_identity": "github.com/o/r", "revision": "1",
        "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
        "blast_radius": {"rating": "SMALL", "signals": {}},
        "counts": {"red": 0, "yellow": 1},
        "findings": [{"dimension": "correctness", "severity": "yellow",
                      "file": "f", "line": 1, "snippet": "x", "observation": "o",
                      "consequence": "c", "suggestion": "s"}],
        "deep_reviewed": True, "title": cid,
        "files_covered": ["f"], "coverage_complete": True,
    }


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


class TestRecordContractAtRead(_Base):
    """A dict is not enough: the record must satisfy the contract to be returned."""

    def _write_shared(self, cid: str, payload) -> None:
        d = store.data_dir(self.root) / "results"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{cid}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_a_valid_record_is_returned(self):
        self._write_shared("CR-ok", _record("CR-ok"))
        self.assertIsNotNone(results.read_result("CR-ok", self.root))

    def test_a_non_object_record_is_refused(self):
        self._write_shared("CR-list", [])
        self.assertIsNone(results.read_result("CR-list", self.root))

    def test_a_dict_that_violates_the_contract_is_refused(self):
        # Shape-plausible but unusable: `classify()` reads `phase1.gate_verdict`,
        # so returning this leaves a finished run with no report instead of a
        # report that says nothing was found.
        self._write_shared("CR-bad", {"phase1": []})
        self.assertIsNone(results.read_result("CR-bad", self.root))

    def test_a_record_missing_phase1_entirely_is_refused(self):
        rec = _record("CR-nophase")
        rec.pop("phase1")
        self._write_shared("CR-nophase", rec)
        self.assertIsNone(results.read_result("CR-nophase", self.root))


class TestAdoption(_Base):
    def test_adopts_a_shared_record_into_the_run_dir(self):
        results.write_result(_record(), self.root)          # worker's path
        self.assertTrue(results.adopt_from_shared("CR-1", self.root, "run-a"))
        # Present in the run, gone from the staging dir.
        self.assertIsNotNone(results.read_result("CR-1", self.root, "run-a"))
        self.assertIsNone(results.read_result("CR-1", self.root, None))

    def test_adoption_is_a_no_op_when_the_worker_wrote_nothing(self):
        # Must be reported as a failed change, not a silently empty report.
        self.assertFalse(results.adopt_from_shared("CR-1", self.root, "run-a"))
        self.assertIsNone(results.read_result("CR-1", self.root, "run-a"))

    def test_adoption_without_a_run_id_does_nothing(self):
        results.write_result(_record(), self.root)
        self.assertFalse(results.adopt_from_shared("CR-1", self.root, None))
        # The legacy path is untouched, so the standalone CLI still works.
        self.assertIsNotNone(results.read_result("CR-1", self.root, None))

    def test_publish_makes_a_run_record_visible_to_the_poster(self):
        results.write_result(_record(), self.root, "run-a")
        self.assertTrue(results.publish_to_shared("CR-1", self.root, "run-a"))
        self.assertIsNotNone(results.read_result("CR-1", self.root, None))
        # And the run keeps its own copy.
        self.assertIsNotNone(results.read_result("CR-1", self.root, "run-a"))

    def test_records_do_not_leak_between_runs(self):
        results.write_result(_record(), self.root)
        results.adopt_from_shared("CR-1", self.root, "run-a")
        self.assertIsNone(results.read_result("CR-1", self.root, "run-b"))


class TestDriverAdopts(_Base):
    """End-to-end: a worker that writes ONLY the shared path must still produce a
    report for the run."""

    def _worker_dispatch(self):
        def dispatch(task: str, timeout: int = 0):
            if "SINGLE thorough pass" in task:
                # Exactly what the real worker does: the path its prompt names.
                results.write_result(_record(), self.root)
            return {"ok": True, "output": "done", "error": ""}
        return dispatch

    def test_run_records_the_review_and_writes_a_report(self):
        out = D.run_review(["CR-1"], dispatch=self._worker_dispatch(),
                           archiver=lambda *_a, **_k: None,
                           generate_report=True, root=self.root, run_id="run-a")
        # This was 0 before the bridge existed.
        self.assertEqual(out["result_records"], 1)
        self.assertEqual(out["deep_reviewed"], 1)
        payload = json.loads(
            (store.run_dir("run-a", self.root) / "report" / "report.json")
            .read_text(encoding="utf-8"))
        self.assertTrue(payload["rows"], "the report has no rows")

    def test_progress_reports_done_not_failed(self):
        seen: dict = {}

        def prog(cid, phase, extra=None):
            seen[cid] = phase
        D.run_review(["CR-1"], dispatch=self._worker_dispatch(),
                     generate_report=False, root=self.root, run_id="run-a",
                     progress=prog)
        self.assertEqual(seen["CR-1"], "done")

    def test_a_worker_that_writes_nothing_is_reported_failed(self):
        # The honest counterpart: no record means the change failed, and the
        # phase must say so rather than counting as reviewed.
        seen: dict = {}

        def prog(cid, phase, extra=None):
            seen[cid] = phase

        def silent(task: str, timeout: int = 0):
            return {"ok": True, "output": "", "error": ""}

        out = D.run_review(["CR-1"], dispatch=silent, generate_report=False,
                           root=self.root, run_id="run-a", progress=prog)
        self.assertEqual(seen["CR-1"], "failed")
        self.assertEqual(out["result_records"], 0)
        self.assertEqual(out["per_change"][0]["skipped_reason"], "no_review_recorded")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestStagingResidue(unittest.TestCase):
    """A crash between the worker's staging write and adoption leaves an orphan.

    Nothing reaps the shared dir (the orphan reaper walks only ``data/runs/``), so
    without a sweep at run start the next review of that change whose worker
    records nothing adopts the residue and reports a stale review as a fresh
    success -- and the change is then durably marked reviewed at the NEW head, so
    it is never re-reviewed there.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clear_staged_removes_only_the_named_changes(self):
        results.write_result(_record("CR-1"), self.root, None)
        results.write_result(_record("CR-2"), self.root, None)

        removed = results.clear_staged(["CR-1"], self.root)

        self.assertEqual(removed, 1)
        self.assertIsNone(results.read_result("CR-1", self.root, None))
        # A concurrent run's staging is left alone.
        self.assertIsNotNone(results.read_result("CR-2", self.root, None))

    def test_a_run_does_not_adopt_crash_residue_as_its_own_review(self):
        """End-to-end: the DRIVER must sweep staging, not just the helper.

        Residue is staged under this change id, then a run whose worker records
        nothing reviews it. Without the sweep the run adopts the orphan and
        reports a stale review as a fresh success.
        """
        stale = _record("CR-1")
        stale["phase1"]["design_headline"] = "from the crashed run"
        results.write_result(stale, self.root, None)

        seen: dict = {}

        def silent(task: str, timeout: int = 0):
            return {"ok": True, "output": "", "error": ""}

        out = D.run_review(["CR-1"], dispatch=silent, generate_report=False,
                           root=self.root, run_id="run-new",
                           progress=lambda cid, phase, extra=None:
                           seen.__setitem__(cid, phase))

        self.assertEqual(out["result_records"], 0)
        self.assertEqual(seen["CR-1"], "failed")
        self.assertIsNone(results.read_result("CR-1", self.root, "run-new"))


class TestStakedSlot(_Base):
    """A record present before a change's own reviewer runs is not its findings.

    Adoption proves a payload NAMES a change, never who wrote it, and every reviewer
    worker can write any change's path in the shared dir. So the slot is cleared right
    before dispatch; whatever is adopted afterwards was written after that point.
    """

    def test_a_planted_record_is_cleared(self):
        # A worker reviewing some other change writes the victim's path, naming the
        # victim so the adoption payload check would pass.
        results.write_result(_record("CR-victim"), self.root)
        planted = results.result_path("CR-victim", self.root, None)
        self.assertTrue(planted.exists())

        self.assertTrue(results.stake_shared("CR-victim", self.root))
        self.assertFalse(planted.exists())

    def test_an_empty_slot_is_already_the_wanted_state(self):
        self.assertTrue(results.stake_shared("CR-never-seen", self.root))

    def test_a_planted_symlink_is_removed_not_followed(self):
        # os.unlink removes the link itself, so the target it aimed at must survive.
        target = self.root / "elsewhere.json"
        target.write_text('{"change_id": "CR-other"}', encoding="utf-8")
        link = results.result_path("CR-victim", self.root, None)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)

        self.assertTrue(results.stake_shared("CR-victim", self.root))
        self.assertFalse(link.is_symlink(), "the planted link survived")
        self.assertTrue(target.exists(), "unlink followed the link and removed the target")

    def test_an_unclearable_slot_is_not_adopted_from(self):
        """A slot that could not be emptied has no provenance, so nothing is taken."""
        first = "https://github.com/o/r/pull/1"
        victim = "https://github.com/o/r/pull/2"
        vid = D._cid(victim)
        planted = _record(vid)
        planted["findings"] = [{"dimension": "correctness", "severity": "red",
                                "title": "UNCLEARABLE-SLOT", "detail": "x",
                                "recommendation": "x"}]

        planted_by: list[bool] = []

        def dispatch(task, timeout):
            if not planted_by:
                results.write_result(planted, self.root)
                planted_by.append(True)
            return {"ok": True, "output": "", "error": ""}

        real_stake = results.stake_shared
        results.stake_shared = lambda change_id, root=None: False   # EPERM on the unlink
        try:
            out = D.run_review([first, victim], dispatch=dispatch, generate_report=False,
                               root=self.root, run_id="run-1", post=False, concurrency=1)
        finally:
            results.stake_shared = real_stake

        self.assertTrue(planted_by, "the plant was never written")
        self.assertIsNone(results.read_result(vid, self.root, "run-1"),
                          "adopted from a slot that could not be cleared")
        self.assertEqual(int(out.get("result_records") or 0), 0)

    def test_a_worker_cannot_plant_a_later_change_in_the_same_run(self):
        # Reviewers are serialized, so the live attack is an earlier worker writing a
        # LATER change's slot mid-run: the run-start sweep has already happened, and
        # nothing else distinguishes that record from one the victim's own worker wrote.
        first = "https://github.com/o/r/pull/1"
        victim = "https://github.com/o/r/pull/2"
        vid = D._cid(victim)
        planted = _record(vid)
        planted["findings"] = [{"dimension": "correctness", "severity": "red",
                                "title": "PLANTED-BY-SIBLING", "detail": "x",
                                "recommendation": "x"}]

        planted_by: list[bool] = []

        def dispatch(task, timeout):
            # The injected worker reviewing the FIRST change writes the victim's slot
            # and records nothing for itself. Plant on the first dispatch only, keyed on
            # call order rather than on the task's text, which the driver owns.
            if not planted_by:
                results.write_result(planted, self.root)
                planted_by.append(True)
            return {"ok": True, "output": "", "error": ""}

        out = D.run_review([first, victim], dispatch=dispatch, generate_report=False,
                           root=self.root, run_id="run-1", post=False, concurrency=1)
        # A test that never plants proves nothing.
        self.assertTrue(planted_by, "the plant was never written")

        adopted = results.read_result(vid, self.root, "run-1")
        self.assertIsNone(adopted, "the victim adopted a sibling worker's planted record")
        run_file = results.result_path(vid, self.root, "run-1")
        if run_file.exists():
            self.assertNotIn("PLANTED-BY-SIBLING", run_file.read_text(encoding="utf-8"))
        self.assertEqual(int(out.get("result_records") or 0), 0)

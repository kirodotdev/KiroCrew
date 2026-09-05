"""Regressions for response-bound Code Review Sage result handoff."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import results
from sage_lib import review_driver as driver
from sage_lib import store


def _record(change_id: str) -> dict:
    return {
        "schema": "code-review-sage-result",
        "version": 1,
        "change_id": change_id,
        "platform": "github",
        "repo_identity": "github.com/o/r",
        "revision": "1",
        "phase1": {"gate_verdict": "PASS", "design_risk": "low", "criticality": "low"},
        "blast_radius": {"rating": "SMALL", "signals": {}},
        "counts": {"red": 0, "yellow": 0},
        "findings": [],
        "deep_reviewed": True,
        "files_covered": ["a.py"],
        "coverage_complete": True,
    }


def _handoff(record: dict) -> str:
    return "<code-review-sage-result>" + json.dumps(record) + "</code-review-sage-result>"


class TestResponseHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sibling_result_planting_cannot_replace_a_dispatch_response(self):
        victim = "CR-2"
        planted = _record(victim)
        planted["findings"] = [{"headline": "PLANTED-BY-SIBLING"}]

        def dispatch(task, timeout=0):
            if "CR-1" in task:
                results.write_result(planted, self.root, "run-a")
                return {"ok": True, "output": _handoff(_record("CR-1")), "error": ""}
            return {"ok": True, "output": "completed without a handoff", "error": ""}

        out = driver.run_review(
            ["CR-1", victim],
            dispatch=dispatch,
            root=self.root,
            run_id="run-a",
            generate_report=False,
            concurrency=1,
        )
        adopted = results.read_result(victim, self.root, "run-a") or {}
        self.assertEqual(out["result_records"], 1)
        self.assertNotIn("PLANTED-BY-SIBLING", json.dumps(adopted))

    def test_malformed_unicode_capability_fails_one_change_not_the_batch(self):
        malformed = _record("CR-1")
        malformed["result_capability"] = "malformed-\ud800"

        def dispatch(task, timeout=0):
            record = malformed if "CR-1" in task else _record("CR-2")
            return {"ok": True, "output": _handoff(record), "error": ""}

        out = driver.run_review(
            ["CR-1", "CR-2"],
            dispatch=dispatch,
            root=self.root,
            run_id="run-a",
            generate_report=False,
        )
        self.assertFalse(out["per_change"][0]["result_recorded"])
        self.assertTrue(out["per_change"][1]["result_recorded"])

    def test_prompt_exposes_no_result_file_or_capability(self):
        task = driver.build_review_task("CR-1")
        self.assertIn("<code-review-sage-result>", task)
        self.assertNotIn("result_capability", task)
        self.assertNotIn("data/results", task)

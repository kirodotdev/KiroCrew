#!/usr/bin/env python3
"""A single-PR review is ONE worker turn, so the only real evidence of forward
motion is the reviewer's tool stream. The driver relays it onto the change's
phase entry, and does so ONLY when the dispatcher accepts the reporter — the
standalone CLI and test fakes pass a plain ``(task, timeout)`` callable.
"""
from __future__ import annotations

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
        "phase1": {"gate_verdict": "PASS", "design_risk": "low",
                   "criticality": "low"},
        "blast_radius": {"rating": "SMALL", "signals": {}},
        "counts": {"red": 0, "yellow": 0}, "findings": [],
        "deep_reviewed": True, "title": cid,
        "files_covered": ["f"], "coverage_complete": True,
    }


class TestActivityRelay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = self.tmp
        self.root = Path(self.tmp) / "app"
        store.ensure_layout(self.root)
        self.seen: list = []

    def tearDown(self):
        if self._old is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _progress(self, cid, phase, extra=None):
        self.seen.append((cid, phase, dict(extra or {})))

    def test_relays_the_reviewers_tool_calls_as_activity(self):
        def dispatch(task, timeout=0, on_activity=None):
            if on_activity is not None:
                on_activity("execute_bash", 1)
                on_activity("fs_read", 2)
            results.write_result(_record(), self.root)
            return {"ok": True, "output": "done", "error": ""}

        D.run_review(["CR-1"], dispatch=dispatch, generate_report=False,
                     root=self.root, run_id="run-a", progress=self._progress)
        acts = [e[2]["activity"] for e in self.seen if "activity" in e[2]]
        self.assertEqual([a["tool"] for a in acts], ["execute_bash", "fs_read"])
        self.assertEqual([a["step"] for a in acts], [1, 2])
        # Reported under the reviewing phase, so the UI keeps its spinner.
        self.assertTrue(all(e[1] == "reviewing" for e in self.seen
                            if "activity" in e[2]))

    def test_a_plain_dispatcher_still_works(self):
        # The standalone CLI and every existing test fake take (task, timeout).
        calls: list = []

        def dispatch(task, timeout=0):
            calls.append(task)
            results.write_result(_record(), self.root)
            return {"ok": True, "output": "done", "error": ""}

        out = D.run_review(["CR-1"], dispatch=dispatch, generate_report=False,
                           root=self.root, run_id="run-a",
                           progress=self._progress)
        self.assertEqual(out["result_records"], 1)
        self.assertTrue(calls)
        self.assertFalse([e for e in self.seen if "activity" in e[2]])

    def test_probe_detects_the_reporter_by_signature(self):
        self.assertTrue(D._accepts_activity(
            lambda task, timeout=0, on_activity=None: None))
        self.assertFalse(D._accepts_activity(lambda task, timeout=0: None))
        # A builtin with no introspectable signature must not crash the probe.
        self.assertFalse(D._accepts_activity(len))

    def test_a_raising_reporter_never_fails_the_review(self):
        def dispatch(task, timeout=0, on_activity=None):
            # Invoke it for real: the driver's reporter has to contain the raise
            # itself, not rely on the pool's guard being the only net.
            if on_activity is not None:
                on_activity("execute_bash", 1)
            results.write_result(_record(), self.root)
            return {"ok": True, "output": "done", "error": ""}

        def boom(cid, phase, extra=None):
            if extra and "activity" in extra:
                raise RuntimeError("progress writer exploded")

        out = D.run_review(["CR-1"], dispatch=dispatch, generate_report=False,
                           root=self.root, run_id="run-a", progress=boom)
        self.assertEqual(out["result_records"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

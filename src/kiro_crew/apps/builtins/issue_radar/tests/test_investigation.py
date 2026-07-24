"""Tests for issue_radar investigation records (store.py).

Locks in the "Investigate" persistence contract: one small per-issue record
(``investigation-{n}.json`` under the repo cache dir — no shared ledger), keyed
by number; ``write_investigation`` is a MERGE upsert (partial patches keep prior
fields, ``started_at`` is stamped once, ``last_opened_at`` bumps every write);
``status`` is constrained; ``findings`` is normalized (trimmed strings,
de-duplicated label list, all-empty collapses to None). Records live under the
repo cache dir, so a disconnect's ``rmtree`` removes them too.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from kiro_crew.apps.builtins.issue_radar.backend import store


class TestInvestigationStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absent_returns_none(self):
        self.assertIsNone(store.read_investigation("o", "r", 1, self.tmp))

    def test_create_stamps_defaults(self):
        rec = store.write_investigation(
            "o", "r", 7, {"slot_key": "slot-abc", "folder_id": "fold-1"}, root=self.tmp
        )
        self.assertEqual(rec["number"], 7)
        self.assertEqual(rec["slot_key"], "slot-abc")
        self.assertEqual(rec["folder_id"], "fold-1")
        self.assertEqual(rec["status"], "investigating")  # default
        self.assertIsNone(rec["findings"])
        self.assertTrue(rec["started_at"])
        self.assertEqual(rec["started_at"], rec["last_opened_at"])
        # round-trips from disk
        self.assertEqual(store.read_investigation("o", "r", 7, self.tmp), rec)

    def test_merge_preserves_slot_and_bumps_last_opened(self):
        first = store.write_investigation("o", "r", 7, {"slot_key": "slot-abc"}, root=self.tmp)
        # An empty patch (the resume path) keeps slot_key + started_at, only
        # bumping last_opened_at.
        second = store.write_investigation("o", "r", 7, {}, root=self.tmp)
        self.assertEqual(second["slot_key"], "slot-abc")
        self.assertEqual(second["started_at"], first["started_at"])
        self.assertGreaterEqual(second["last_opened_at"], first["last_opened_at"])

    def test_status_constrained(self):
        store.write_investigation("o", "r", 7, {"slot_key": "s"}, root=self.tmp)
        ok = store.write_investigation("o", "r", 7, {"status": "resolved"}, root=self.tmp)
        self.assertEqual(ok["status"], "resolved")
        # an unknown status is ignored (keeps the prior value)
        bad = store.write_investigation("o", "r", 7, {"status": "bogus"}, root=self.tmp)
        self.assertEqual(bad["status"], "resolved")

    def test_empty_string_clears_slot(self):
        store.write_investigation("o", "r", 7, {"slot_key": "s"}, root=self.tmp)
        cleared = store.write_investigation("o", "r", 7, {"slot_key": ""}, root=self.tmp)
        self.assertIsNone(cleared["slot_key"])

    def test_findings_normalized(self):
        rec = store.write_investigation(
            "o", "r", 7,
            {
                "slot_key": "s",
                "findings": {
                    "verdict": "  bug  ",
                    "root_cause": "off-by-one",
                    "suggested_labels": ["bug", "bug", " needs-repro ", "", 5, None],
                    "next_action": "add a test",
                    "summary": "It crashes on empty input.",
                    "junk": "dropped",
                },
            },
            root=self.tmp,
        )
        f = rec["findings"]
        self.assertEqual(f["verdict"], "bug")
        self.assertEqual(f["suggested_labels"], ["bug", "needs-repro"])
        self.assertEqual(f["summary"], "It crashes on empty input.")
        self.assertNotIn("junk", f)

    def test_empty_findings_collapses_to_none(self):
        rec = store.write_investigation(
            "o", "r", 7,
            {"slot_key": "s", "findings": {"verdict": "  ", "suggested_labels": []}},
            root=self.tmp,
        )
        self.assertIsNone(rec["findings"])

    def test_findings_survive_status_only_patch(self):
        store.write_investigation(
            "o", "r", 7, {"slot_key": "s", "findings": {"summary": "done"}}, root=self.tmp
        )
        rec = store.write_investigation("o", "r", 7, {"status": "resolved"}, root=self.tmp)
        self.assertEqual(rec["findings"]["summary"], "done")
        self.assertEqual(rec["status"], "resolved")

    def test_removed_with_repo_cache(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_investigation("o", "r", 7, {"slot_key": "s"}, root=self.tmp)
        self.assertTrue(store.investigation_path("o", "r", 7, self.tmp).is_file())
        store.remove_connected_repo("o", "r", root=self.tmp)
        self.assertIsNone(store.read_investigation("o", "r", 7, self.tmp))


if __name__ == "__main__":
    unittest.main()

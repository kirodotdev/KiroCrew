"""Tests for issue_radar investigation records (store.py).

Locks in the "Investigate" persistence contract: one small per-issue record
(``investigation-{n}.json`` under the repo cache dir — no shared ledger), keyed
by number; ``write_investigation`` is a MERGE upsert (partial patches keep prior
fields, ``started_at`` is stamped once, ``last_opened_at`` bumps every write);
``status`` is constrained; ``findings`` is normalized (trimmed strings,
de-duplicated label list, all-empty collapses to None). Records live under the
repo cache dir, so a disconnect's ``rmtree`` removes them too.

``TestInvestigationRunBoundary`` covers the one place merging is NOT the
contract: across an investigation run boundary, where a re-run's findings
REPLACE the previous run's rather than blending with them.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

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

    def test_partial_findings_patch_does_not_destroy_the_other_fields(self):
        """A later patch carrying ONE finding must not wipe the rest.

        ``findings`` is merged, not replaced wholesale: a second write with only a
        ``verdict`` must not lose the root cause, summary and labels an earlier
        write stored — and the record is the only copy. Reachable from the
        ``issue_radar_record_investigation`` MCP tool, whose contract is that a
        partial update is fine.
        """
        store.write_investigation(
            "o", "r", 7,
            {"findings": {
                "verdict": "needs-info",
                "root_cause": "off-by-one",
                "suggested_labels": ["bug"],
                "next_action": "add a test",
                "summary": "It crashes on empty input.",
            }},
            root=self.tmp,
        )
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "bug"}}, root=self.tmp
        )
        f = rec["findings"]
        self.assertEqual(f["verdict"], "bug")           # overridden
        self.assertEqual(f["root_cause"], "off-by-one")  # preserved
        self.assertEqual(f["summary"], "It crashes on empty input.")
        self.assertEqual(f["next_action"], "add a test")
        self.assertEqual(f["suggested_labels"], ["bug"])

    def test_empty_string_leaves_a_stored_finding_alone(self):
        # No per-field clear: "" means "leave this alone", which is what makes a
        # partial patch safe for an LLM writer.
        store.write_investigation(
            "o", "r", 7, {"findings": {"summary": "kept"}}, root=self.tmp
        )
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"summary": "   "}}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["summary"], "kept")

    def test_empty_findings_dict_is_a_no_op_not_a_wipe(self):
        store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "bug"}}, root=self.tmp
        )
        rec = store.write_investigation("o", "r", 7, {"findings": {}}, root=self.tmp)
        self.assertEqual(rec["findings"]["verdict"], "bug")

    def test_explicit_null_findings_clears_everything(self):
        # The UI's clear path — putInvestigation types findings as
        # Partial<InvestigationFindings> | null.
        store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "bug", "summary": "s"}}, root=self.tmp
        )
        rec = store.write_investigation("o", "r", 7, {"findings": None}, root=self.tmp)
        self.assertIsNone(rec["findings"])

    def test_malformed_findings_keeps_the_stored_object(self):
        # Garbage must not be a data-loss path; the route + tool schema reject it
        # upstream, so this is the conservative floor.
        store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "bug"}}, root=self.tmp
        )
        rec = store.write_investigation(
            "o", "r", 7, {"findings": "not a dict"}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["verdict"], "bug")

    def test_new_labels_replace_rather_than_append(self):
        # A recommendation set is a whole value, not an additive list.
        store.write_investigation(
            "o", "r", 7, {"findings": {"suggested_labels": ["bug", "old"]}}, root=self.tmp
        )
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"suggested_labels": ["area:apps"]}}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["suggested_labels"], ["area:apps"])

    def test_removed_with_repo_cache(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_investigation("o", "r", 7, {"slot_key": "s"}, root=self.tmp)
        self.assertTrue(store.investigation_path("o", "r", 7, self.tmp).is_file())
        store.remove_connected_repo("o", "r", root=self.tmp)
        self.assertIsNone(store.read_investigation("o", "r", 7, self.tmp))


class TestInvestigationRunBoundary(unittest.TestCase):
    """A re-run's findings REPLACE the previous run's; same-run writes merge.

    Per-key merging is right within one run (the MCP tool's contract is "a
    partial update is fine") and wrong across runs: a replacement session that
    records a verdict but no root_cause would otherwise inherit the previous
    run's, leaving the record — the only copy — holding a verdict assembled from
    two investigations. The store owns the boundary because only there is it
    atomic with the write; the session the findings were written under
    (``findings_slot_key``) is what marks it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Registered at creation, not in tearDown: an interrupt between the
        # allocation and the end of setUp would skip tearDown and leave the
        # directory behind.
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _stored(self) -> dict:
        """The record as it is on disk. ``read_investigation`` returns
        ``dict | None``, and every caller here has just written the record, so
        this asserts it exists and hands back a plain dict."""
        rec = store.read_investigation("o", "r", 7, self.tmp) or {}
        self.assertTrue(rec, "the record must exist on disk")
        return rec

    def _run_one(self):
        """Investigate: the SPA links the session, then the agent records."""
        store.write_investigation(
            "o", "r", 7, {"slot_key": "chat-1-100", "status": "investigating"}, root=self.tmp
        )
        return store.write_investigation(
            "o", "r", 7,
            {
                "status": "resolved",
                "findings": {
                    "verdict": "bug",
                    "root_cause": "run one's cause",
                    "summary": "run one's summary",
                    "suggested_labels": ["area: apps"],
                },
            },
            root=self.tmp,
        )

    def test_rerun_findings_replace_rather_than_blend(self):
        self._run_one()

        # Start over: a replacement session is linked (deliberately WITHOUT
        # clearing findings — the record is the only copy).
        store.write_investigation(
            "o", "r", 7, {"slot_key": "chat-2-200", "status": "investigating"}, root=self.tmp
        )
        self.assertEqual(
            self._stored()["findings"]["root_cause"],
            "run one's cause",
            "the prior verdict must survive until a new one exists",
        )

        # The new run's first record omits root_cause and labels.
        rec = store.write_investigation(
            "o", "r", 7,
            {"status": "resolved", "findings": {"verdict": "question", "summary": "run two"}},
            root=self.tmp,
        )
        self.assertIsNone(rec["findings"]["root_cause"], "inherited from the previous run")
        self.assertEqual(rec["findings"]["suggested_labels"], [], "inherited from the previous run")
        self.assertEqual(rec["findings"]["verdict"], "question")
        self.assertEqual(rec["findings"]["summary"], "run two")
        self.assertEqual(rec["findings_slot_key"], "chat-2-200")

    def test_same_run_partial_updates_still_merge(self):
        self._run_one()
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "confirmed bug"}}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["verdict"], "confirmed bug")
        self.assertEqual(rec["findings"]["root_cause"], "run one's cause")
        self.assertEqual(rec["findings"]["summary"], "run one's summary")

    def test_second_record_of_the_new_run_merges_again(self):
        self._run_one()
        store.write_investigation("o", "r", 7, {"slot_key": "chat-2-200"}, root=self.tmp)
        store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "question", "summary": "run two"}}, root=self.tmp
        )
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"root_cause": "run two's cause"}}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["root_cause"], "run two's cause")
        self.assertEqual(rec["findings"]["summary"], "run two", "same run: merge, not replace")

    def test_legacy_record_without_the_stamp_crosses_on_the_next_slot(self):
        # Records written before findings_slot_key existed carry findings but no
        # stamp. The backfill is the record's own slot_key, so a mid-run partial
        # update still merges...
        path = store.investigation_path("o", "r", 7, self.tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "owner": "o", "repo": "r", "number": 7,
                "slot_key": "chat-1-100", "folder_id": None, "status": "resolved",
                "started_at": "2020-01-01T00:00:00.000000Z",
                "last_opened_at": "2020-01-01T00:00:00.000000Z",
                "findings": {
                    "verdict": "bug", "root_cause": "legacy cause",
                    "suggested_labels": [], "next_action": None, "summary": "legacy",
                },
            }),
            encoding="utf-8",
        )
        merged = store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "still bug"}}, root=self.tmp
        )
        self.assertEqual(merged["findings"]["root_cause"], "legacy cause")

        # ...and a replacement session still moves the boundary.
        store.write_investigation("o", "r", 7, {"slot_key": "chat-2-200"}, root=self.tmp)
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "question"}}, root=self.tmp
        )
        self.assertIsNone(rec["findings"]["root_cause"])

    def test_empty_and_malformed_patches_never_cross_the_boundary(self):
        # An empty dict is a no-op and garbage is rejected upstream: neither is a
        # new verdict, so neither may destroy the old one just because the slot
        # changed...
        self._run_one()
        store.write_investigation("o", "r", 7, {"slot_key": "chat-2-200"}, root=self.tmp)
        patch: dict[str, Any]
        for patch in ({"findings": {}}, {"findings": "not a dict"}, {"findings": {"summary": "  "}}):
            rec = store.write_investigation("o", "r", 7, patch, root=self.tmp)
            self.assertEqual(
                rec["findings"]["root_cause"], "run one's cause", f"wiped by {patch!r}"
            )
            # ...and neither may ADVANCE the stamp either. Re-homing the previous
            # run's findings onto the new session would make the next real write
            # merge into them instead of replacing them, arming the blend through
            # a no-op.
            self.assertEqual(rec["findings_slot_key"], "chat-1-100", f"re-homed by {patch!r}")

        # The new run's real first record still replaces, after all three no-ops.
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "question", "summary": "run two"}}, root=self.tmp
        )
        self.assertIsNone(rec["findings"]["root_cause"], "a no-op re-armed the blend")
        self.assertEqual(rec["findings_slot_key"], "chat-2-200")

    def test_unknown_owner_and_cleared_link_fall_back_to_merging(self):
        # Findings recorded through the MCP tool for an item with no session
        # linked have no owning run. The store cannot tell them from ones the
        # about-to-be-linked session just wrote, so it merges.
        store.write_investigation(
            "o", "r", 7, {"findings": {"root_cause": "unowned cause"}}, root=self.tmp
        )
        self.assertIsNone(self._stored().get("findings_slot_key"))
        store.write_investigation("o", "r", 7, {"slot_key": "chat-1-100"}, root=self.tmp)
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"verdict": "bug"}}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["root_cause"], "unowned cause")

        # Unlinking is not a new run either.
        store.write_investigation("o", "r", 7, {"slot_key": ""}, root=self.tmp)
        rec = store.write_investigation(
            "o", "r", 7, {"findings": {"summary": "s"}}, root=self.tmp
        )
        self.assertEqual(rec["findings"]["root_cause"], "unowned cause")

    def test_clearing_findings_drops_the_stamp(self):
        self._run_one()
        rec = store.write_investigation("o", "r", 7, {"findings": None}, root=self.tmp)
        self.assertIsNone(rec["findings"])
        self.assertIsNone(rec["findings_slot_key"])


if __name__ == "__main__":
    unittest.main()

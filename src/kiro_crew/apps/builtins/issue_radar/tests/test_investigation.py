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


class TestRecordVerbNamespace(unittest.TestCase):
    """One item can carry two concurrent session verbs.

    The record holds exactly one ``slot_key``, so reviewing a change request and
    answering the feedback it received must not share a record: the second click
    would resume the first verb's session and overwrite its link. ``verb`` is
    orthogonal to ``kind`` -- ``kind`` says which number sequence the item comes
    from, ``verb`` says which job is being done on it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_verb_record_does_not_share_the_primary_records_session_link(self):
        store.write_investigation("o", "r", 5, {"slot_key": "review-slot"}, root=self.tmp)
        store.write_investigation(
            "o", "r", 5, {"slot_key": "respond-slot"}, root=self.tmp, verb="respond"
        )

        primary = store.read_investigation("o", "r", 5, self.tmp)
        respond = store.read_investigation("o", "r", 5, self.tmp, verb="respond")
        assert primary is not None and respond is not None
        self.assertEqual(primary["slot_key"], "review-slot")
        self.assertEqual(respond["slot_key"], "respond-slot")

    def test_the_primary_record_keeps_the_historical_filename(self):
        # An omitted verb must resolve to the file every existing record already
        # lives in, so nothing needs migrating.
        self.assertEqual(
            store.investigation_path("o", "r", 5, self.tmp).name, "investigation-5.json"
        )
        self.assertEqual(
            store.investigation_path("o", "r", 5, self.tmp, verb="respond").name,
            "investigation-respond-5.json",
        )

    def test_a_verb_record_is_absent_until_written(self):
        store.write_investigation("o", "r", 5, {"slot_key": "review-slot"}, root=self.tmp)
        self.assertIsNone(store.read_investigation("o", "r", 5, self.tmp, verb="respond"))

    def test_an_unknown_verb_is_refused_rather_than_folded(self):
        # Folding an unrecognized verb to the primary record would silently produce
        # the collision the dimension exists to prevent, so the store fails closed
        # behind the route validation.
        with self.assertRaises(ValueError):
            store.investigation_path("o", "r", 5, self.tmp, verb="reviwe")
        with self.assertRaises(ValueError):
            store.write_investigation("o", "r", 5, {}, root=self.tmp, verb="reviwe")
        with self.assertRaises(ValueError):
            store.read_investigation("o", "r", 5, self.tmp, verb="reviwe")

    def test_a_verb_write_leaves_no_stray_lock_beside_the_primary_record(self):
        # The write resolves the path three times (lock, read-back, atomic write).
        # If any one of them drops the verb, the record splits across two files.
        store.write_investigation(
            "o", "r", 5, {"slot_key": "respond-slot"}, root=self.tmp, verb="respond"
        )
        self.assertFalse(store.investigation_path("o", "r", 5, self.tmp).is_file())
        self.assertTrue(
            store.investigation_path("o", "r", 5, self.tmp, verb="respond").is_file()
        )

    def test_the_write_lock_is_per_record(self):
        # A lock shared with the primary record would serialize two independent
        # records against each other, so the lock's identity tracks the record's.
        store.write_investigation("o", "r", 5, {}, root=self.tmp, verb="respond")
        locks = {p.name for p in self.tmp.rglob("*.lock")}
        self.assertIn("investigation-respond-5.lock", locks)
        self.assertNotIn("investigation-5.lock", locks)

    def test_a_verb_write_does_not_inherit_the_primary_records_fields(self):
        # The read-back that feeds the merge must target the SAME record the write
        # lands in. Reading the primary record here would leak one verb's findings
        # and folder into the other -- invisible in any field the patch overwrites,
        # which is why this asserts on fields the patch omits.
        store.write_investigation(
            "o", "r", 5,
            {
                "slot_key": "review-slot",
                "folder_id": "review-folder",
                "findings": {"verdict": "review-verdict"},
            },
            root=self.tmp,
        )
        respond = store.write_investigation(
            "o", "r", 5, {"slot_key": "respond-slot"}, root=self.tmp, verb="respond"
        )
        self.assertEqual(respond["slot_key"], "respond-slot")
        self.assertIsNone(respond["folder_id"])
        self.assertIsNone(respond["findings"])

    def test_findings_merge_independently_per_verb(self):
        store.write_investigation(
            "o", "r", 5, {"findings": {"verdict": "primary-verdict"}}, root=self.tmp
        )
        store.write_investigation(
            "o", "r", 5, {"findings": {"verdict": "respond-verdict"}},
            root=self.tmp, verb="respond",
        )
        primary = store.read_investigation("o", "r", 5, self.tmp)
        respond = store.read_investigation("o", "r", 5, self.tmp, verb="respond")
        assert primary is not None and respond is not None
        self.assertEqual(primary["findings"]["verdict"], "primary-verdict")
        self.assertEqual(respond["findings"]["verdict"], "respond-verdict")

    def test_a_verb_record_is_removed_with_the_repo_cache(self):
        store.add_connected_repo("o", "r", root=self.tmp)
        store.write_investigation("o", "r", 7, {"slot_key": "s"}, root=self.tmp, verb="respond")
        store.remove_connected_repo("o", "r", root=self.tmp)
        self.assertIsNone(store.read_investigation("o", "r", 7, self.tmp, verb="respond"))


class TestSlotClaim(unittest.TestCase):
    """Claiming the session link is how two browser tabs stop double-creating.

    A re-entry guard lives in one tab's memory, so it cannot see a click in
    another. Without a compare-and-set both tabs read "no session", both create
    one, and the later write replaces the first tab's link -- leaving an agent
    running that nothing points at.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_claiming_an_unclaimed_record_succeeds(self):
        rec = store.write_investigation(
            "o", "r", 5, {"slot_key": "tab-one"}, root=self.tmp, expected_slot_key=None,
        )
        self.assertEqual(rec["slot_key"], "tab-one")

    def test_the_second_tab_is_refused_and_told_who_won(self):
        store.write_investigation(
            "o", "r", 5, {"slot_key": "tab-one"}, root=self.tmp, expected_slot_key=None,
        )
        with self.assertRaises(store.SlotClaimConflict) as caught:
            store.write_investigation(
                "o", "r", 5, {"slot_key": "tab-two"}, root=self.tmp, expected_slot_key=None,
            )
        # The loser needs the winner's link to adopt that session instead.
        self.assertEqual(caught.exception.record["slot_key"], "tab-one")
        stored = store.read_investigation("o", "r", 5, self.tmp)
        assert stored is not None
        self.assertEqual(stored["slot_key"], "tab-one")

    def test_naming_the_stale_link_allows_a_deliberate_replacement(self):
        # Resuming a session whose slot was deleted legitimately rewrites the link;
        # the caller proves it is not racing by naming the value it saw.
        store.write_investigation(
            "o", "r", 5, {"slot_key": "dead-slot"}, root=self.tmp, expected_slot_key=None,
        )
        rec = store.write_investigation(
            "o", "r", 5, {"slot_key": "fresh-slot"}, root=self.tmp,
            expected_slot_key="dead-slot",
        )
        self.assertEqual(rec["slot_key"], "fresh-slot")

    def test_a_cleared_link_compares_equal_to_an_absent_one(self):
        store.write_investigation("o", "r", 5, {"slot_key": ""}, root=self.tmp)
        rec = store.write_investigation(
            "o", "r", 5, {"slot_key": "tab-one"}, root=self.tmp, expected_slot_key=None,
        )
        self.assertEqual(rec["slot_key"], "tab-one")

    def test_omitting_the_claim_keeps_last_writer_wins(self):
        # Every agent-side findings write omits it and must never fail on somebody
        # else's session link.
        store.write_investigation(
            "o", "r", 5, {"slot_key": "tab-one"}, root=self.tmp, expected_slot_key=None,
        )
        rec = store.write_investigation(
            "o", "r", 5, {"findings": {"verdict": "a dupe"}}, root=self.tmp,
        )
        self.assertEqual(rec["slot_key"], "tab-one")
        self.assertEqual(rec["findings"]["verdict"], "a dupe")

    def test_the_claim_is_scoped_to_the_verb_record(self):
        store.write_investigation(
            "o", "r", 5, {"slot_key": "review-slot"}, root=self.tmp, expected_slot_key=None,
        )
        # The respond record is a different file, so its first claim is unclaimed.
        rec = store.write_investigation(
            "o", "r", 5, {"slot_key": "respond-slot"}, root=self.tmp,
            verb="respond", expected_slot_key=None,
        )
        self.assertEqual(rec["slot_key"], "respond-slot")


if __name__ == "__main__":
    unittest.main()

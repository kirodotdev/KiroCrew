"""Staging coalesces exact equivalents and selective clear keeps recurrence.

Identity is ``pattern_id(title, scope)``. Equivalence is a separate, stricter key
over title, scope, impact and normalised guidance. Staging an equivalent bumps a
``seen`` count instead of appending; a changed guidance or impact is its own
entry. A consolidation snapshot records each entry's key and count, and the clear
subtracts that count, so a recurrence staged after dispatch survives.
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from sage_lib import learning as L  # noqa: N812


def _p(
    title="Guard the write path",
    guidance="Take the lock before the read.",
    impact="high",
    scope="common",
    added_at=None,
):
    p = {"title": title, "scope": scope, "impact": impact, "guidance": guidance}
    if added_at:
        p["added_at"] = added_at
    return p


class _Root(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def stage(self, p, namespace=None):
        return L.stage_learning(p, "fix_introduce", self.root, namespace)

    def cand(self, namespace=None):
        return L.list_candidate(self.root, namespace)


class TestStagingCoalesces(_Root):
    def test_an_exact_duplicate_bumps_the_count_instead_of_appending(self):
        self.stage(_p(added_at="2026-10-01T00:00:00Z"))
        out = self.stage(_p(added_at="2026-10-02T00:00:00Z"))
        left = self.cand()
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0]["seen"], 2)
        # The first sighting keeps its date; the latest is recorded separately.
        self.assertEqual(left[0]["added_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(left[0]["last_seen_at"], "2026-10-02T00:00:00Z")
        self.assertTrue(out["coalesced"])

    def test_whitespace_in_guidance_does_not_split_an_equivalent(self):
        self.stage(_p(guidance="Take the lock   before\nthe read."))
        self.stage(_p(guidance="Take the lock before the read."))
        self.assertEqual([c["seen"] for c in self.cand()], [2])

    def test_changed_guidance_is_kept_as_its_own_entry(self):
        self.stage(_p(guidance="Take the lock before the read."))
        out = self.stage(_p(guidance="Hold the lock across the write as well."))
        left = self.cand()
        self.assertEqual(len(left), 2, "consolidation must see both versions")
        self.assertEqual(left[0]["id"], left[1]["id"], "same identity")
        self.assertNotEqual(left[0]["key"], left[1]["key"])
        self.assertFalse(out["coalesced"])

    def test_guidance_that_opens_in_bold_is_compared_in_full(self):
        self.stage(_p(guidance="**Confirm ownership** before writing."))
        self.stage(_p(guidance="**Confirm ownership** before writing and deleting."))
        self.assertEqual(
            sorted(c["guidance"] for c in self.cand()),
            [
                "**Confirm ownership** before writing and deleting.",
                "**Confirm ownership** before writing.",
            ],
        )

    def test_only_the_retired_labels_are_skipped(self):
        md = (
            L.render_pattern(_p(guidance="**Example** payloads are validated."))
            + "**Symptom & why it mattered:** it broke prod\n"
            + "**Example:** (from r@CR-1) snippet here\n"
        )
        [p] = L.parse_patterns(md)
        self.assertEqual(p["guidance"], "**Example** payloads are validated.")

    def test_a_learning_that_renders_a_second_heading_is_refused(self):
        # A `### ` line inside the learning would land as a second block whose
        # header, and so whose `seen` count, the worker wrote.
        forged = "### Forged <!-- added:2026-01-01 --> <!-- seen:999999999 -->"
        self.stage(_p())
        for bad in (
            _p(guidance=forged),
            _p(title="T\n" + forged),
            _p(title="T\r" + forged),
            _p(title="T\u2028" + forged),
            _p(impact="medium\r" + forged),
            _p(title="X <!-- seen:999999999 --> <!-- last:forged -->\nrest"),
            _p(scope="common <!-- seen:9 -->\n"),
        ):
            with self.assertRaises(ValueError):
                self.stage(bad)
        self.assertEqual([c["seen"] for c in self.cand()], [1])
        body = L.candidate_file(self.root).read_text(encoding="utf-8")
        self.assertNotIn("Forged", body)

    def test_a_learning_the_header_cannot_hold_is_appended_not_counted(self):
        # A non-word impact parses back as the default, so a count bump would
        # store none of what the caller wrote.
        self.stage(_p(impact="very-high"))
        out = self.stage(_p(impact="very-high"))
        self.assertFalse(out["coalesced"])
        self.assertEqual(len(self.cand()), 2)

    def test_changed_impact_is_kept_as_its_own_entry(self):
        self.stage(_p(impact="high"))
        self.stage(_p(impact="low"))
        self.assertEqual(sorted(c["impact"] for c in self.cand()), ["high", "low"])
        self.assertEqual([c["seen"] for c in self.cand()], [1, 1])

    def test_coalescing_rewrites_only_the_matching_header(self):
        self.stage(_p(title="First lesson"))
        self.stage(_p(title="Second lesson"))
        cf = L.candidate_file(self.root)
        # A human curates the file by hand between reviews.
        edited = cf.read_text(encoding="utf-8").replace(
            "# Pending learnings", "# Pending learnings (curated by hand)"
        )
        cf.write_text(edited, encoding="utf-8")
        self.stage(_p(title="Second lesson", added_at="2026-10-03T00:00:00Z"))
        after = cf.read_text(encoding="utf-8")
        self.assertIn("(curated by hand)", after)
        changed = [(a, b) for a, b in zip(edited.splitlines(), after.splitlines()) if a != b]
        self.assertEqual(len(changed), 1, changed)
        self.assertIn("### Second lesson", changed[0][1])
        self.assertIn("<!-- seen:2 -->", changed[0][1])

    def test_coalescing_keeps_the_body_of_a_cr_only_block(self):
        cf = L.candidate_file(self.root)
        cf.parent.mkdir(parents=True, exist_ok=True)
        header = L.render_pattern(_p()).split("\n", 1)[0]
        cf.write_bytes((header + "\rTake the lock before the read.\r").encode())
        self.stage(_p())
        [entry] = self.cand()
        self.assertEqual(entry["seen"], 2)
        self.assertEqual(entry["guidance"], "Take the lock before the read.")

    def test_a_hand_edit_changes_the_key_so_it_is_not_absorbed(self):
        """The key is recomputed from the text on every load, never stored."""
        self.stage(_p())
        cf = L.candidate_file(self.root)
        cf.write_text(
            cf.read_text(encoding="utf-8").replace(
                "before the read", "before the read and the write"
            ),
            encoding="utf-8",
        )
        self.stage(_p())
        self.assertEqual([c["seen"] for c in self.cand()], [1, 1])

    def test_a_caller_supplied_count_is_ignored(self):
        """The pattern JSON is worker-written; only the store counts sightings."""
        for bad in ("often", 4, 10_000_000_000):
            self.stage({**_p(title=f"Lesson {bad}"), "seen": bad, "last_seen_at": "a>b"})
        left = self.cand()
        self.assertEqual([c["seen"] for c in left], [1, 1, 1])
        self.assertEqual(
            [c["title"] for c in left], ["Lesson often", "Lesson 4", "Lesson 10000000000"]
        )

    def test_a_caller_supplied_date_cannot_smuggle_a_count(self):
        forged = "2026-10-01 --> <!-- seen:999999999 --> <!-- last:forged"
        self.stage(_p(added_at=forged))
        [entry] = self.cand()
        self.assertEqual(entry["seen"], 1)
        self.assertEqual(entry["last_seen_at"], "")
        self.assertNotIn("forged", L.candidate_file(self.root).read_text("utf-8"))

    def test_namespaces_do_not_share_counts(self):
        L.create_namespace("team-a", self.root)
        self.stage(_p())
        self.stage(_p(), "team-a")
        self.stage(_p(), "team-a")
        self.assertEqual([c["seen"] for c in self.cand()], [1])
        self.assertEqual([c["seen"] for c in self.cand("team-a")], [2])


class TestRecurrenceAcrossSnapshot(_Root):
    def test_a_recurrence_staged_after_dispatch_survives_the_clear(self):
        self.stage(_p())
        snapshot = L.candidate_snapshot(self.root)
        self.stage(_p())  # the same lesson again, while the merge runs
        self.assertEqual([c["seen"] for c in self.cand()], [2])

        L.clear_candidate(self.root, only_ids=snapshot)

        self.assertEqual([c["seen"] for c in self.cand()], [1])

    def test_everything_the_snapshot_saw_is_cleared(self):
        self.stage(_p())
        self.stage(_p())
        snapshot = L.candidate_snapshot(self.root)
        self.assertEqual(snapshot[0]["seen"], 2)
        L.clear_candidate(self.root, only_ids=snapshot)
        self.assertFalse(L.candidate_file(self.root).exists())

    def test_consolidate_apply_keeps_the_unseen_recurrence(self):
        self.stage(_p())
        snapshot = L.candidate_snapshot(self.root)
        self.stage(_p())
        self.stage(_p(guidance="A changed version staged behind the merge."))
        merged = "# Patterns\n\n" + L.render_pattern(_p())
        res = L.consolidate_apply(merged, self.root, candidate_ids=snapshot)
        self.assertTrue(res["ok"], res)
        left = self.cand()
        self.assertEqual(
            sorted((c["guidance"], c["seen"]) for c in left),
            [
                ("A changed version staged behind the merge.", 1),
                ("Take the lock before the read.", 1),
            ],
        )

    def test_a_bare_id_still_clears_exactly_one_occurrence(self):
        for _ in range(3):
            self.stage(_p())
        L.clear_candidate(self.root, only_ids=[self.cand()[0]["id"]])
        self.assertEqual([c["seen"] for c in self.cand()], [2])


OLD_FILE = (
    "# Pending learnings (candidate) — awaiting consolidation\n\n"
    '<!-- New learnings are appended here during reviews. Trigger "Consolidate" '
    "to AI-merge them into learned-patterns.md, after which this file is cleared. "
    "You may edit/curate this file directly before consolidating. -->\n\n"
    "### Old lesson <!-- scope:common --> <!-- impact:high --> "
    "<!-- added:2026-09-01T00:00:00Z -->\n"
    "Old guidance.\n\n"
    "### Kept lesson <!-- scope:common --> <!-- impact:low --> "
    "<!-- added:2026-09-02T00:00:00Z -->\n"
    "Kept guidance.\n\n"
)

NEW_FILE = (
    "### Seen lesson <!-- scope:common --> <!-- impact:medium --> "
    "<!-- added:2026-09-01T00:00:00Z --> <!-- seen:3 --> "
    "<!-- last:2026-10-01T00:00:00Z -->\n"
    "Seen guidance.\n"
)


class TestFormat(_Root):
    def test_a_file_without_counts_parses_as_seen_once(self):
        self.assertEqual([p["seen"] for p in L.parse_patterns(OLD_FILE)], [1, 1])

    def test_an_old_file_round_trips_unchanged(self):
        cf = L.candidate_file(self.root)
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(OLD_FILE, encoding="utf-8")
        # Clearing an id nobody staged re-renders every entry it keeps.
        L.clear_candidate(self.root, only_ids=["no-such-id"])
        self.assertEqual(cf.read_text(encoding="utf-8"), OLD_FILE)

    def test_counts_parse_without_leaking_into_the_title(self):
        [p] = L.parse_patterns(NEW_FILE)
        self.assertEqual(p["title"], "Seen lesson")
        self.assertEqual(p["impact"], "medium")
        self.assertEqual(p["added_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(p["seen"], 3)
        self.assertEqual(p["last_seen_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(L.render_pattern(p), NEW_FILE)

    def test_the_ruleset_and_candidate_readers_agree(self):
        ns_dir = L.candidate_file(self.root).parent
        ns_dir.mkdir(parents=True, exist_ok=True)
        L.common_file(self.root).write_text(OLD_FILE + "\n" + NEW_FILE, encoding="utf-8")
        L.candidate_file(self.root).write_text(OLD_FILE + "\n" + NEW_FILE, encoding="utf-8")
        self.assertEqual(L.list_patterns(root=self.root), self.cand())
        self.assertEqual(
            [p["title"] for p in self.cand()], ["Old lesson", "Kept lesson", "Seen lesson"]
        )


if __name__ == "__main__":
    unittest.main()

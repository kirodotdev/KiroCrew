"""Tests for the ledger: entries, the track record, git merges and contradictions.

A git merge of two ledgers must dedupe rather than double, and neither a re-POST nor a merge may
lower an entry's recorded miss count. The shape hash provably collides on alarms that differ
only in a number, so an exact provider key has to tell them apart, and the consolidation pass
can list contradicting entries. ``_HomeIsolated`` redirects ``KIROCREW_HOME`` to a temp dir so
nothing here touches the real data home.
"""

import json
from datetime import datetime, timedelta, timezone

from kiro_crew.apps.builtins.ops_mission_control.tests.store_and_gate_helpers import _HomeIsolated


class TestLedgerGitMerge(_HomeIsolated):
    """A git merge of two ledgers must be a dedupe, not a doubling.

    The design argument for content-addressed ids on append-only JSONL is that two
    people who learn the same lesson write the same id, so merging is safe. But git
    resolves that as BOTH lines present — and `read_entries` appended every line, so a
    shared lesson counted twice: inflated `stats()`, `match()` returning the same entry
    twice, and the handover digest listing one pattern as two.
    """

    @staticmethod
    def _duplicate_every_line() -> None:
        """What a git merge of two branches that each appended the entry leaves."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        path = ledger.ledger_path()
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        path.write_text("\n".join(lines + lines) + "\n", encoding="utf-8")

    def test_duplicate_lines_collapse_to_one_entry(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="shared lesson", fix="the fix"))
        self._duplicate_every_line()

        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1, "one lesson must read as one entry")
        self.assertEqual(ledger.stats()["total"], 1)

    def test_hygiene_counts_and_compacts_duplicate_lines(self):
        """``deduped`` counts the raw lines a merge left behind, and the pass rewrites them away.

        The read already collapses duplicate ids, so counting duplicates among the READ
        entries always found none: the pass reported ``deduped: 0`` and left the merged
        lines on disk (and in the synced repo) until some unrelated change forced a rewrite.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="shared lesson", fix="the fix"))
        ledger.upsert(LedgerEntry.create(pattern="second lesson", fix="another fix"))
        self._duplicate_every_line()
        path = ledger.ledger_path()

        def lines() -> list[str]:
            return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

        self.assertEqual(len(lines()), 4)

        summary = ledger.hygiene()

        self.assertEqual(summary["deduped"], 2, "two duplicate lines were collapsed")
        self.assertEqual(summary["before"], 2)
        self.assertEqual(summary["after"], 2)
        self.assertEqual(summary["pruned"], 0)
        self.assertEqual(len(lines()), 2, "the file is compacted to one line per id")
        self.assertEqual(len({json.loads(ln)["entry_id"] for ln in lines()}), 2)

        again = ledger.hygiene()
        self.assertEqual(again["deduped"], 0, "a compacted file has nothing left to dedupe")

    def test_hygiene_compaction_keeps_the_merged_counters(self):
        """The rewrite writes the reconciled entry, so neither branch's counters are lost."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        entry = LedgerEntry.create(pattern="shared lesson", fix="the fix", fingerprints=["fp-a"])
        ours = entry.to_dict()
        ours.update(use_count=5, miss_count=0)
        theirs = dict(entry.to_dict())
        theirs.update(use_count=1, miss_count=3, fingerprints=["fp-b"])
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(ours, sort_keys=True) + "\n" + json.dumps(theirs, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        summary = ledger.hygiene()

        self.assertEqual(summary["deduped"], 1)
        on_disk = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]["use_count"], 5)
        self.assertEqual(on_disk[0]["miss_count"], 3)
        self.assertEqual(sorted(on_disk[0]["fingerprints"]), ["fp-a", "fp-b"])

    def test_hygiene_does_not_count_a_malformed_line_as_a_duplicate(self):
        """A skipped line is damage, not a duplicate, and alone does not force a rewrite."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="p", fix="f"))
        with ledger.ledger_path().open("a", encoding="utf-8") as handle:
            handle.write("{ not json\n\n")

        summary = ledger.hygiene()

        self.assertEqual(summary["deduped"], 0)
        self.assertIn("{ not json", ledger.ledger_path().read_text(encoding="utf-8"))

    def test_reconcile_unions_provider_keys_not_only_fingerprints(self):
        """A git merge must not drop one branch's provider identities.

        `upsert` unions `provider_keys` and `_reconcile` — the READ path that collapses the
        duplicate ids a real `git merge` leaves — did not, so a merge permanently wrote
        incomplete identity data. That is not cosmetic: `match()` treats a provider key as
        the EXACT-identity signal, so the very next recurrence on the dropped alert would
        have matched by shape hash alone, or not at all. Found in review.

        Written as two RAW lines with different keys, because that is the git-merge shape;
        the pre-existing `provider_keys` merge test went through `upsert`, which is exactly
        why it never caught this.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        mine = LedgerEntry.create(pattern="p", fix="f", provider_keys=["dd:monitor/111"])
        theirs = LedgerEntry.create(pattern="p", fix="f", provider_keys=["pd:incident/222"])
        self.assertEqual(mine.entry_id, theirs.entry_id, "content addressing must agree")

        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(mine.to_dict(), sort_keys=True)
            + "\n"
            + json.dumps(theirs.to_dict(), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1, "one lesson must read as one entry")
        self.assertEqual(
            set(entries[0].provider_keys),
            {"dd:monitor/111", "pd:incident/222"},
            "both branches' provider identities must survive the merge",
        )
        # And the match path must actually honour the recovered key as an exact identity.
        self.assertTrue(ledger.is_exact_match(entries[0], "pd:incident/222"))

    def test_reconcile_caps_both_identity_lists(self):
        """Two already-capped lists unioned are up to 2x the cap.

        `upsert` bounds that and `_reconcile` did not, so a merge could grow a line past
        the limit and keep it there.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        cap = ledger.MAX_KEYS_PER_ENTRY
        mine = LedgerEntry.create(
            pattern="p",
            fix="f",
            fingerprints=[f"a{i}" for i in range(cap)],
            provider_keys=[f"ka{i}" for i in range(cap)],
        )
        theirs = LedgerEntry.create(
            pattern="p",
            fix="f",
            fingerprints=[f"b{i}" for i in range(cap)],
            provider_keys=[f"kb{i}" for i in range(cap)],
        )
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(mine.to_dict(), sort_keys=True)
            + "\n"
            + json.dumps(theirs.to_dict(), sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        merged = ledger.read_entries()[0]
        self.assertLessEqual(len(merged.fingerprints), cap)
        self.assertLessEqual(len(merged.provider_keys), cap)
        # Newest kept: the recent identity is the one about to recur.
        self.assertIn(f"kb{cap - 1}", merged.provider_keys)

    def test_merge_takes_the_stronger_trust_and_confidence(self):
        """Learning a lesson again must never weaken what is known."""
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        weak = LedgerEntry.create(pattern="p", fix="f", confidence="low", trust="observed")
        ledger.upsert(weak)
        strong = LedgerEntry.create(pattern="p", fix="f", confidence="high", trust="verified")
        strong.use_count = 7
        # Same id, stronger record — exactly what the other branch appended.
        path = ledger.ledger_path()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(strong.to_dict()) + "\n")

        merged = ledger.read_entries()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].confidence, "high")
        self.assertEqual(merged[0].trust, "verified")
        self.assertEqual(merged[0].use_count, 7, "use_count takes the max, not the last seen")

    def test_fingerprints_union_across_branches(self):
        """Two people hit the same failure on different resources.

        Losing one branch's fingerprint means that recurrence stops matching — the
        ledger keeps working while silently failing to recognize half its own history.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        mine = LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp-a"])
        ledger.upsert(mine)
        theirs = LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp-b"])
        with ledger.ledger_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(theirs.to_dict()) + "\n")

        merged = ledger.read_entries()
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].fingerprints), {"fp-a", "fp-b"})
        # And BOTH fingerprints still match the merged entry.
        self.assertTrue(ledger.match("fp-a"))
        self.assertTrue(ledger.match("fp-b"))

    def test_match_does_not_return_the_same_entry_twice(self):
        """A duplicated entry would be presented to the agent as two hypotheses."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp-x"]))
        self._duplicate_every_line()

        hits = ledger.match("fp-x")
        self.assertEqual(len(hits), 1)
        self.assertEqual(len({h.entry_id for h in hits}), 1)

    def test_distinct_lessons_are_not_collapsed(self):
        """Only identical pattern+fix shares an id; a different fix must survive."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        ledger.upsert(LedgerEntry.create(pattern="p", fix="fix one"))
        ledger.upsert(LedgerEntry.create(pattern="p", fix="fix two"))
        self.assertEqual(len(ledger.read_entries()), 2)

    def test_survives_an_unresolved_git_conflict(self):
        """A real divergent merge produces CONFLICT MARKERS, not a clean dedupe.

        The spec claimed content-addressed ids make a merge "a dedupe rather than a
        conflict". Verified against an actual `git merge` of two divergent ledgers: git
        emits `<<<<<<< HEAD` / `=======` / `>>>>>>>` because both branches appended to
        the same region. The ids ARE identical, so the *entries* dedupe correctly — but
        only if reading tolerates the markers and reconciles the duplicate.

        This is the state a user's working tree is actually in mid-merge, so the app
        must stay usable: markers skipped as malformed lines, every real entry kept,
        the shared lesson collapsed with its fingerprints unioned.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        shared_a = LedgerEntry.create(pattern="shared", fix="f", fingerprints=["fp-a"])
        shared_b = LedgerEntry.create(pattern="shared", fix="f", fingerprints=["fp-b"])
        self.assertEqual(shared_a.entry_id, shared_b.entry_id, "same lesson, same id")
        mine = LedgerEntry.create(pattern="only-mine", fix="f")
        theirs = LedgerEntry.create(pattern="only-theirs", fix="f")

        conflicted = "\n".join(
            [
                "<<<<<<< HEAD",
                json.dumps(shared_a.to_dict()),
                json.dumps(mine.to_dict()),
                "=======",
                json.dumps(shared_b.to_dict()),
                json.dumps(theirs.to_dict()),
                ">>>>>>> their-branch",
            ]
        )
        ledger.ledger_path().parent.mkdir(parents=True, exist_ok=True)
        ledger.ledger_path().write_text(conflicted + "\n", encoding="utf-8")

        entries = ledger.read_entries()
        self.assertEqual(len(entries), 3, "shared lesson collapses; both locals survive")
        patterns = {e.pattern for e in entries}
        self.assertEqual(patterns, {"shared", "only-mine", "only-theirs"})
        merged = [e for e in entries if e.pattern == "shared"][0]
        self.assertEqual(set(merged.fingerprints), {"fp-a", "fp-b"})

    def test_first_occurrence_keeps_its_position(self):
        """Callers rank by read order; a merge must not reshuffle the ledger."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        for n in range(3):
            ledger.upsert(LedgerEntry.create(pattern=f"p{n}", fix="f"))
        before = [e.pattern for e in ledger.read_entries()]
        self._duplicate_every_line()
        self.assertEqual([e.pattern for e in ledger.read_entries()], before)


class TestLedger(_HomeIsolated):
    def test_upsert_then_match(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(
            pattern="DLQ fills", fix="clear and redrive", fingerprints=["fp1"]
        )
        ledger.upsert(entry)
        matches = ledger.match("fp1")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].pattern, "DLQ fills")

    def test_upsert_same_content_dedupes(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["a"]))
        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["b"]))
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0].fingerprints), {"a", "b"})

    def test_a_failed_mutation_read_refuses_instead_of_truncating(self):
        """Every read-modify-rewrite must refuse when it could not read what it rewrites.

        The lenient ``read_entries`` answers a failed open with ``[]``, and each mutation
        path then calls ``_write_all`` with what it read — so before the strict read, a
        transient ``EACCES`` at hygiene time silently truncated the team's whole shared
        ledger, ``remove`` answered "not found" about an entry still on disk, and
        ``upsert`` re-created an entry instead of merging into it. Found in review
        (GPT 5.6). The property pinned here is the refusal ORDER: the ``OSError``
        escapes before any rewrite, so the file survives the fault untouched.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["a"]))
        refuse = mock.Mock(side_effect=PermissionError(13, "Permission denied"))
        # The innermost strict read: ``read_entries_for_update`` wraps it and ``hygiene``
        # calls it directly for its duplicate-line count, so one patch covers every mutator.
        with mock.patch.object(ledger, "_read_counted_for_update", refuse):
            with self.assertRaises(OSError):
                ledger.hygiene()
            with self.assertRaises(OSError):
                ledger.remove("any-id")
            with self.assertRaises(OSError):
                ledger.upsert(models.LedgerEntry.create(pattern="q", fix="g"))
            with self.assertRaises(OSError):
                ledger.record_use("any-id")
            with self.assertRaises(OSError):
                ledger.record_miss("any-id")
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1, "the ledger must survive a refused read intact")
        self.assertEqual(entries[0].pattern, "p")

    def test_relearning_does_not_weaken_trust(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_OBSERVED,
                confidence=models.CONFIDENCE_LOW,
            )
        )
        entry = ledger.read_entries()[0]
        self.assertEqual(entry.trust, models.TRUST_VERIFIED)
        self.assertEqual(entry.confidence, models.CONFIDENCE_HIGH)

    def _proven(self, **overrides):
        """A verified/high entry that has already earned the fast path."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(
            pattern=overrides.pop("pattern", "p"),
            fix=overrides.pop("fix", "f"),
            trust=models.TRUST_VERIFIED,
            confidence=models.CONFIDENCE_HIGH,
        )
        entry.use_count = ledger.MIN_USES_FOR_FAST_PATH
        for key, value in overrides.items():
            setattr(entry, key, value)
        return entry

    def test_fast_path_requires_verified_and_high(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        weak = self._proven()
        weak.trust = models.TRUST_OBSERVED
        self.assertFalse(ledger.is_fast_path([weak]))
        self.assertTrue(ledger.is_fast_path([self._proven()]))

    def test_fast_path_also_requires_a_track_record(self):
        """Verified + high on an entry nobody has ever used is a claim, not a record.

        `POST /ledger` takes `confidence` and `trust` verbatim, so without this floor one
        hand-authored line unlocked "propose this fix directly" for a production failure
        on its very first match — and `record_use` then binds the provider key, so every
        later occurrence presents that same single piece of evidence as an EXACT match.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        fresh = self._proven(use_count=ledger.MIN_USES_FOR_FAST_PATH - 1)
        self.assertFalse(ledger.is_fast_path([fresh]))
        self.assertTrue(ledger.is_fast_path([self._proven()]))

    def test_one_recorded_failure_relocks_the_fast_path(self):
        """Unlocking needs corroboration; re-locking needs one counterexample.

        The asymmetry is deliberate. A fix observed not to hold must stop being the thing
        an agent proposes without checking — and it stays in the ledger with its full
        text, because a fix that works sometimes is worth more than nothing.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        missed = self._proven(miss_count=1)
        self.assertFalse(ledger.is_fast_path([missed]))
        self.assertTrue(ledger.is_demoted(missed))
        self.assertFalse(ledger.is_demoted(self._proven()))

    def test_record_miss_does_not_inflate_the_use_count(self):
        """A miss is not a use — that inversion is the original defect turned inside out."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        ledger.record_use(entry.entry_id)
        updated = ledger.record_miss(entry.entry_id)
        assert updated is not None
        self.assertEqual(updated.use_count, 1)
        self.assertEqual(updated.miss_count, 1)
        self.assertTrue(updated.last_miss)

    def test_record_miss_on_a_pruned_entry_is_not_an_error(self):
        """Hygiene prunes; a miss charged to a gone entry must be a no-op, not a raise."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        self.assertIsNone(ledger.record_miss("nope"))

    def test_reposting_an_entry_cannot_erase_its_recorded_failures(self):
        """The promotion route must not double as a way to launder counter-evidence.

        `ledger-hygiene.md` promotes observed → verified by re-POSTing the same
        pattern+fix, which merges by content-addressed id. If that merge took the incoming
        `miss_count` of 0, the nightly promotion step would clear every recorded failure
        on exactly the entries most likely to have them — with one curl.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        ledger.record_miss(entry.entry_id)
        merged = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        self.assertEqual(merged.miss_count, 1)
        self.assertEqual(ledger.read_entries()[0].miss_count, 1)

    def test_a_git_merge_cannot_launder_a_teammates_recorded_failure(self):
        """Duplicate ids reconcile on READ, and miss_count must take the max there too.

        Confidence and trust take the STRONGEST of two records, so a merge that took the
        lower miss_count would make "pull the team ledger" a way to clear a demotion —
        the one direction a shared append-only knowledge base must never move by itself.
        """
        import json

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        mine = models.LedgerEntry.create(pattern="p", fix="f")
        theirs = models.LedgerEntry.create(pattern="p", fix="f")
        theirs.miss_count = 2
        theirs.last_miss = "2026-07-01T00:00:00Z"
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Two lines, same content-addressed id — exactly what a real `git merge` leaves.
        path.write_text(
            json.dumps(mine.to_dict()) + "\n" + json.dumps(theirs.to_dict()) + "\n",
            encoding="utf-8",
        )
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].miss_count, 2)

    def test_hygiene_demotes_an_entry_whose_fix_stopped_working(self):
        """The mechanical downward path §5.9 asked for, on the nightly pass not the hot one."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        ledger.record_use(entry.entry_id)
        ledger.record_use(entry.entry_id)
        ledger.record_miss(entry.entry_id)
        ledger.record_miss(entry.entry_id)
        summary = ledger.hygiene()
        self.assertEqual(summary["demoted"], 1)
        stored = ledger.read_entries()[0]
        self.assertEqual(stored.confidence, models.CONFIDENCE_MEDIUM)
        # Trust is NOT rewritten: "somebody saw this work" stays true even after it
        # failed elsewhere, and overwriting a human's own observation is editorialising.
        self.assertEqual(stored.trust, models.TRUST_VERIFIED)

    def test_one_failure_costs_exactly_one_confidence_step(self):
        """Hygiene runs nightly and its test is a ratio, which stays true once true.

        Without the spent-evidence guard a single miss would walk an entry
        high → medium → low across three nights on no new evidence at all, arriving at
        the bottom of the scale for one failure.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        ledger.record_miss(entry.entry_id)
        self.assertEqual(ledger.hygiene()["demoted"], 1)
        self.assertEqual(ledger.hygiene()["demoted"], 0)
        self.assertEqual(ledger.hygiene()["demoted"], 0)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_MEDIUM)
        # A SECOND, genuinely new failure spends a second step.
        ledger.record_miss(entry.entry_id)
        self.assertEqual(ledger.hygiene()["demoted"], 1)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_LOW)

    def test_a_well_used_entry_is_not_condemned_by_one_bad_night(self):
        """The ratio's whole purpose: more evidence it works, more it takes to overturn."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        for _ in range(8):
            ledger.record_use(entry.entry_id)
        ledger.record_miss(entry.entry_id)
        self.assertEqual(ledger.hygiene()["demoted"], 0)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_HIGH)

    def test_stats_reports_proven_separately_from_its_two_halves(self):
        """`verified` and `high_confidence` are each HALF the bar, so neither is the bar.

        A board showing only those two overstated the ledger's authority: an entry can be
        counted in both while being something nobody has ever successfully applied.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = ledger.upsert(
            models.LedgerEntry.create(
                pattern="p",
                fix="f",
                trust=models.TRUST_VERIFIED,
                confidence=models.CONFIDENCE_HIGH,
            )
        )
        stats = ledger.stats()
        self.assertEqual(stats["verified"], 1)
        self.assertEqual(stats["high_confidence"], 1)
        self.assertEqual(stats["proven"], 0)
        for _ in range(ledger.MIN_USES_FOR_FAST_PATH):
            ledger.record_use(entry.entry_id)
        self.assertEqual(ledger.stats()["proven"], 1)
        ledger.record_miss(entry.entry_id)
        after = ledger.stats()
        self.assertEqual(after["proven"], 0)
        self.assertEqual(after["demoted"], 1)
        self.assertEqual(after["total_misses"], 1)

    def test_prune_order_stops_preferring_the_most_misleading_entries(self):
        """The prune sorted by `-use_count` alone, so a false match was pruned LAST.

        `use_count` incremented at claim time, before any outcome existed — so an entry
        that kept matching the wrong failure climbed the ranking on every mismatch and
        was the last thing the cap dropped. The ledger preferentially kept its worst rows.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        misleading = ledger.upsert(models.LedgerEntry.create(pattern="wrong", fix="a"))
        useful = ledger.upsert(models.LedgerEntry.create(pattern="right", fix="b"))
        for _ in range(4):
            ledger.record_use(misleading.entry_id)
            ledger.record_miss(misleading.entry_id)
        for _ in range(3):
            ledger.record_use(useful.entry_id)
        ledger.hygiene()
        # Hygiene writes in prune order, so position IS survival order at the cap.
        self.assertEqual([e.pattern for e in ledger.read_entries()], ["right", "wrong"])

    def test_record_use_binds_new_fingerprint(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["a"])
        ledger.upsert(entry)
        updated = ledger.record_use(entry.entry_id, fingerprint="b")
        assert updated is not None
        self.assertEqual(updated.use_count, 1)
        self.assertIn("b", updated.fingerprints)


class TestShapeHashOverMerges(_HomeIsolated):
    """The motivating defect for the exact-identity layer, asserted as a fact.

    ``compute_fingerprint`` strips every bare digit, so alarms that differ only in a
    number are indistinguishable to it. This is not a bug to fix in the hash — the
    stripping is deliberate, so that a DLQ at 500 and at 900 match — it is a reason the
    hash cannot be the ONLY key. Pinned so nobody later "fixes" the collision and
    quietly removes the reason the provider_key path exists.
    """

    def test_distinct_failures_collide_on_the_shape_hash(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
            compute_fingerprint as fp,
        )

        self.assertEqual(
            fp("cloudwatch", "svc/api", "4xx error rate above 5"),
            fp("cloudwatch", "svc/api", "5xx error rate above 1"),
        )
        self.assertEqual(
            fp("cloudwatch", "svc/api", "p99 latency above 500ms"),
            fp("cloudwatch", "svc/api", "p50 latency above 100ms"),
        )

    def test_an_exact_provider_key_separates_them(self):
        """What the collision costs, and what the exact key buys."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        four = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/four",
            title="4xx error rate above 5",
            resource="svc/api",
            provider_key="us-east-1/four-xx",
        )
        five = models.Signal.create(
            source="cloudwatch",
            native_id="alarm/five",
            title="5xx error rate above 1",
            resource="svc/api",
            provider_key="us-east-1/five-xx",
        )
        # Same shape hash, different provider identity — the premise of the fix.
        self.assertEqual(four.fingerprint, five.fingerprint)
        self.assertNotEqual(four.provider_key, five.provider_key)

        # A lesson learned about the 4xx alarm, bound to BOTH its keys.
        ledger.upsert(
            models.LedgerEntry.create(
                pattern="4xx spike from a bad deploy",
                fix="roll back the canary",
                fingerprints=[four.fingerprint],
                provider_keys=[four.provider_key],
                confidence=models.CONFIDENCE_HIGH,
                trust=models.TRUST_VERIFIED,
            )
        )
        # A different, weaker lesson about the 5xx alarm.
        ledger.upsert(
            models.LedgerEntry.create(
                pattern="5xx from upstream timeouts",
                fix="raise the pool size",
                fingerprints=[five.fingerprint],
                provider_keys=[five.provider_key],
            )
        )

        # Both still match by shape (they share a fingerprint) — but the 5xx signal's
        # OWN entry must rank first, ahead of the verified/high one that merely collides.
        ranked = ledger.match(five.fingerprint, provider_key=five.provider_key)
        self.assertEqual(ranked[0].pattern, "5xx from upstream timeouts")
        self.assertTrue(ledger.is_exact_match(ranked[0], five.provider_key))
        self.assertFalse(ledger.is_exact_match(ranked[1], five.provider_key))

    def test_without_a_provider_key_behaviour_is_unchanged(self):
        """Adapters that publish no stable identity must keep working exactly as before."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp"]))
        self.assertEqual(len(ledger.match("fp")), 1)
        self.assertEqual(len(ledger.match("fp", provider_key="")), 1)

    def test_a_provider_key_alone_matches_when_the_shape_drifted(self):
        """The point of the exact key: a reworded alarm still finds its own lesson."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(
            models.LedgerEntry.create(
                pattern="p", fix="f", fingerprints=["old-shape"], provider_keys=["cw:alarm-7"]
            )
        )
        found = ledger.match("a-totally-different-shape", provider_key="cw:alarm-7")
        self.assertEqual(len(found), 1)

    def test_record_use_binds_the_provider_key(self):
        """The first fuzzy match teaches the entry the provider's identity."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", fingerprints=["fp"])
        ledger.upsert(entry)
        updated = ledger.record_use(entry.entry_id, fingerprint="fp", provider_key="cw:alarm-9")
        assert updated is not None
        self.assertIn("cw:alarm-9", updated.provider_keys)
        # And the next occurrence is now an EXACT match rather than a shape guess.
        self.assertTrue(
            ledger.is_exact_match(ledger.match("fp", provider_key="cw:alarm-9")[0], "cw:alarm-9")
        )

    def test_provider_keys_union_on_merge_like_fingerprints(self):
        """Preserves the conflict-free git dedupe property for the new field."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", provider_keys=["k1"]))
        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", provider_keys=["k2"]))
        entries = ledger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0].provider_keys), {"k1", "k2"})

    def test_keys_bound_to_one_entry_are_capped(self):
        """A per-occurrence identity (PagerDuty) must not grow a JSONL line forever."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f")
        ledger.upsert(entry)
        for i in range(ledger.MAX_KEYS_PER_ENTRY + 25):
            ledger.record_use(entry.entry_id, provider_key=f"pd:incident/{i}")
        stored = ledger.read_entries()[0]
        self.assertLessEqual(len(stored.provider_keys), ledger.MAX_KEYS_PER_ENTRY)
        # Newest kept, oldest dropped — the recent identity is the one about to recur.
        self.assertIn(f"pd:incident/{ledger.MAX_KEYS_PER_ENTRY + 24}", stored.provider_keys)
        self.assertNotIn("pd:incident/0", stored.provider_keys)

    def test_a_ledger_line_written_before_the_field_existed_still_loads(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        legacy = {
            "entry_id": "abc",
            "pattern": "p",
            "fix": "f",
            "fingerprints": ["fp"],
            "confidence": "medium",
            "trust": "observed",
            "use_count": 2,
        }
        path = ledger.ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        loaded = ledger.read_entries()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].provider_keys, [])
        self.assertEqual(len(ledger.match("fp")), 1)

    def test_hygiene_decays_unused_confidence(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        entry = models.LedgerEntry.create(pattern="p", fix="f", confidence=models.CONFIDENCE_HIGH)
        entry.last_used = (
            # flake-ok: pre-existing wall-clock read, moved verbatim; tracked for de-flaking
            datetime.now(timezone.utc)
            - timedelta(days=ledger.DECAY_AFTER_DAYS + 10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        ledger._write_all([entry])
        summary = ledger.hygiene()
        self.assertEqual(summary["decayed"], 1)
        self.assertEqual(ledger.read_entries()[0].confidence, models.CONFIDENCE_MEDIUM)

    def test_prune_drops_the_stalest_of_tied_entries(self):
        """On a tie in use, trust and confidence, the prune drops the entry used longest ago.

        That is the ``MAX_LEDGER_ENTRIES`` contract (least used, weakest confidence, oldest).
        Sorting ``last_used`` ascending kept the stalest entry and pruned the freshest one.
        Both file orders are run so the result cannot come from the sort being stable.
        """
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        now = datetime(2026, 10, 1, tzinfo=timezone.utc)

        def stamped(pattern: str, days_ago: int) -> models.LedgerEntry:
            entry = models.LedgerEntry.create(pattern=pattern, fix="f")
            entry.last_used = (now - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return entry

        for order in ("older-first", "newer-first"):
            with self.subTest(order=order):
                older, newer = stamped("older", 20), stamped("newer", 2)
                ledger._write_all([older, newer] if order == "older-first" else [newer, older])
                with mock.patch.object(ledger, "MAX_LEDGER_ENTRIES", 1):
                    summary = ledger.hygiene(now=now)
                self.assertEqual(summary["pruned"], 1)
                self.assertEqual(summary["decayed"], 0, "neither entry is past the decay window")
                self.assertEqual([e.pattern for e in ledger.read_entries()], ["newer"])

        # Recency only breaks ties: an older entry with more net uses still outranks a
        # fresher one with none.
        useful, fresh = stamped("useful", 20), stamped("fresh", 2)
        useful.use_count = 3
        ledger._write_all([fresh, useful])
        with mock.patch.object(ledger, "MAX_LEDGER_ENTRIES", 1):
            ledger.hygiene(now=now)
        self.assertEqual([e.pattern for e in ledger.read_entries()], ["useful"])

    def test_hygiene_is_a_noop_when_clean(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        summary = ledger.hygiene()
        self.assertEqual(summary["deduped"], 0)
        self.assertEqual(summary["decayed"], 0)
        self.assertEqual(summary["pruned"], 0)

    def test_malformed_line_is_skipped(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f"))
        with ledger.ledger_path().open("a", encoding="utf-8") as handle:
            handle.write("{ not json\n")
        self.assertEqual(len(ledger.read_entries()), 1)

    def test_match_on_empty_fingerprint_returns_nothing(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        ledger.upsert(models.LedgerEntry.create(pattern="p", fix="f", fingerprints=[""]))
        self.assertEqual(ledger.match(""), [])


class TestContradictionDetection(_HomeIsolated):
    """A consolidation pass has to "resolve contradictions" somehow.

    Ours asked the same of the hygiene agent but gave it no tooling — finding them meant an
    O(n²) eye-scan across the whole ledger, which is both a token cost and the kind of
    search a model skims once the ledger is longer than a screenful.

    Detection is deterministic and belongs in Python. RESOLUTION deliberately does not:
    splitting a pattern requires knowing what the two causes actually are.
    """

    @staticmethod
    def _entry(pattern, fix, fingerprints, uses=0):
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry

        entry = LedgerEntry.create(pattern=pattern, fix=fix, fingerprints=fingerprints)
        entry.use_count = uses
        return entry

    def test_two_fixes_for_one_fingerprint_is_a_contradiction(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("DLQ high", "reattach sqs:DeleteMessage", ["fp1"]))
        ledger.upsert(self._entry("DLQ high", "raise the visibility timeout", ["fp1"]))
        found = ledger.find_contradictions()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["fingerprint"], "fp1")

    def test_the_same_fix_twice_is_not_a_contradiction(self):
        """That is the duplicate case, which dedupe already merges by content id.

        Conflating the two would make every deduplicated pair look like a conflict and
        bury the real ones.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("failure A", "the one true fix", ["fp1"]))
        ledger.upsert(self._entry("failure A described differently", "the one true fix", ["fp1"]))
        self.assertEqual(ledger.find_contradictions(), [])

    def test_different_fingerprints_are_not_compared(self):
        """Two unrelated failures with different fixes are just two lessons."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("failure A", "fix A", ["fp1"]))
        ledger.upsert(self._entry("failure B", "fix B", ["fp2"]))
        self.assertEqual(ledger.find_contradictions(), [])

    def test_most_used_pairs_come_first(self):
        """A responder reviewing a long list must see what is actively misleading people
        before the speculative conflicts."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("rare failure", "rare fix one", ["rare"], uses=0))
        ledger.upsert(self._entry("rare failure", "rare fix two", ["rare"], uses=1))
        ledger.upsert(self._entry("hot failure", "hot fix one", ["hot"], uses=9))
        ledger.upsert(self._entry("hot failure", "hot fix two", ["hot"], uses=8))
        found = ledger.find_contradictions()
        self.assertEqual([r["fingerprint"] for r in found], ["hot", "rare"])

    def test_a_pair_sharing_two_fingerprints_is_reported_once(self):
        """Entries can share several fingerprints; the pair is still one conflict."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("failure", "fix one", ["fpA", "fpB"]))
        ledger.upsert(self._entry("failure", "fix two", ["fpA", "fpB"]))
        self.assertEqual(len(ledger.find_contradictions()), 1)

    def test_hygiene_reports_the_count_without_resolving_them(self):
        """Detected, never auto-resolved — deleting a fix that worked for somebody would
        make the next responder rediscover it."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(self._entry("DLQ high", "fix one", ["fp1"]))
        ledger.upsert(self._entry("DLQ high", "fix two", ["fp1"]))
        summary = ledger.hygiene()
        self.assertEqual(summary["contradictions"], 1)
        self.assertEqual(len(ledger.read_entries()), 2, "both entries must survive")

    def test_an_empty_ledger_has_no_contradictions(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        self.assertEqual(ledger.find_contradictions(), [])


class TestConfidenceRank(_HomeIsolated):
    """Every ranking in ``ledger.py`` that compares ``confidence`` orders it on one scale.

    ``_reconcile``, ``upsert``, ``match`` and the hygiene prune all rank through
    ``ledger._confidence_rank``. The first two are pinned by the merge and relearning tests
    above; these pin the other two, plus the rank of a value off the scale, which
    ``LedgerEntry.from_dict`` reads from a raw line without clamping.
    """

    def test_rank_orders_the_scale_and_puts_an_unknown_value_last(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        scale = (models.CONFIDENCE_HIGH, models.CONFIDENCE_MEDIUM, models.CONFIDENCE_LOW)
        ranks = [ledger._confidence_rank(value) for value in (*scale, "unrecognised")]
        self.assertEqual(ranks, [0, 1, 2, 3])

    def test_match_breaks_a_trust_tie_on_confidence(self):
        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        low = models.LedgerEntry.create(
            pattern="p-low", fix="f", fingerprints=["fp"], confidence=models.CONFIDENCE_LOW
        )
        high = models.LedgerEntry.create(
            pattern="p-high", fix="f", fingerprints=["fp"], confidence=models.CONFIDENCE_HIGH
        )
        # Written weaker-first, so file order alone would rank `low` first.
        ledger._write_all([low, high])
        self.assertEqual([e.entry_id for e in ledger.match("fp")], [high.entry_id, low.entry_id])

    def test_hygiene_prunes_the_weaker_confidence_on_a_tie(self):
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, models

        low = models.LedgerEntry.create(pattern="p-low", fix="f", confidence=models.CONFIDENCE_LOW)
        high = models.LedgerEntry.create(
            pattern="p-high", fix="f", confidence=models.CONFIDENCE_HIGH
        )
        for entry in (low, high):
            entry.last_used = "2026-01-01T00:00:00Z"
        # Weaker-first again, with use, miss and trust all equal, so only confidence can
        # decide which of the two the cap keeps.
        ledger._write_all([low, high])
        with mock.patch.object(ledger, "MAX_LEDGER_ENTRIES", 1):
            summary = ledger.hygiene(now=datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(summary["pruned"], 1)
        self.assertEqual([e.entry_id for e in ledger.read_entries()], [high.entry_id])

"""Tests for the ledger → vector index adaptor.

The owner's requirement was that this "will allow large amount of memories to be stored
+ vectorization", so the properties tested here are the ones that decide whether it
scales:

1. **Import is incremental.** A second import of an unchanged ledger must embed nothing.
   This is the whole difference between a 100k-entry ledger being usable and being a
   multi-hour stall on every dispatch cycle.
2. **Embedding is deferred and batched.** One sweep per import, not one inference per
   row.
3. **Import is merge-only.** It must never tombstone a row another writer owns.
4. **Nothing is fatal.** A broken store, a bad row, or a failed sweep degrades to "no
   semantic search", never to a failed cycle.

A fake store is used rather than a real SQLite/FAISS pair: these assertions are about
*how the adaptor calls the store*, and a fake is the only way to assert "embedded
exactly once" or "never called with preserve_existing=False".
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, ledger_index
from kiro_crew.apps.builtins.ops_mission_control.backend.models import LedgerEntry


class _FakeStore:
    """Records how it was called. Mirrors only the surface the adaptor uses."""

    def __init__(self, *, fail_write: bool = False, fail_backfill: bool = False) -> None:
        self.writes: list[dict[str, Any]] = []
        self.texts: set[str] = set()
        self.backfill_calls = 0
        self._fail_write = fail_write
        self._fail_backfill = fail_backfill

    def write_episodic(self, text: str, **kw: Any) -> bool:
        if self._fail_write:
            raise RuntimeError("store is broken")
        self.writes.append({"text": text, **kw})
        if text in self.texts:
            return False  # already present, as the real store reports
        self.texts.add(text)
        return True

    def backfill_missing_embeddings(self) -> int:
        if self._fail_backfill:
            raise RuntimeError("model unavailable")
        self.backfill_calls += 1
        return len(self.texts)

    def search_episodic(self, **kw: Any) -> list[dict]:
        self.last_search = kw
        return [{"text": t} for t in sorted(self.texts)]


class _Env(unittest.TestCase):
    """Isolated data home: the cursor and ledger are real files."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("KIROCREW_HOME")
        os.environ["KIROCREW_HOME"] = str(self.tmp)

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = self._prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _seed(n: int, *, prefix: str = "lesson") -> None:
        """Seed n entries.

        Writes the file in ONE pass rather than calling ``upsert`` per entry.
        ``upsert`` re-reads the whole ledger to merge by id, so seeding N entries
        through it is O(N^2) — 21s for 2000 rows here. That cost is fine in production
        (a route calls it once per recorded lesson, never in a loop) but it makes a
        scale test measure the seeding rather than the thing under test.
        """
        entries = [
            LedgerEntry.create(
                pattern=f"{prefix} {i}: a failure that recurs in the pipeline",
                fix=f"apply remediation number {i} to the upstream config",
            )
            for i in range(n)
        ]
        existing = ledger.read_entries() if ledger.ledger_path().exists() else []
        ledger._write_all(existing + entries)


class TestIncrementalImport(_Env):
    def test_second_import_of_an_unchanged_ledger_does_nothing(self) -> None:
        """The property that makes a large ledger viable at all."""
        self._seed(20)
        store = _FakeStore()

        first = ledger_index.import_pending(store)
        self.assertEqual(first["written"], 20)
        self.assertEqual(store.backfill_calls, 1, "one batched sweep, not 20")

        second = ledger_index.import_pending(store)
        self.assertEqual(second["written"], 0, "an unchanged ledger must re-embed nothing")
        self.assertEqual(store.backfill_calls, 1, "no second sweep when nothing was written")
        self.assertEqual(len(store.writes), 20, "the store was not touched again")

    def test_only_new_entries_are_imported(self) -> None:
        self._seed(5)
        store = _FakeStore()
        ledger_index.import_pending(store)
        self._seed(3, prefix="newer")

        again = ledger_index.import_pending(store)
        self.assertEqual(again["written"], 3)
        self.assertEqual(again["scanned"], 8, "scanning is cheap; embedding is not")

    def test_import_is_bounded_per_call(self) -> None:
        """A 100k first import must drain over cycles, not stall one."""
        self._seed(30)
        store = _FakeStore()
        result = ledger_index.import_pending(store, limit=10)
        self.assertEqual(result["written"], 10)
        self.assertEqual(result["scanned"], 30, "the remainder is known, just not yet done")

        rest = ledger_index.import_pending(store, limit=10)
        self.assertEqual(rest["written"], 10, "the next call continues where it stopped")

    def test_a_deleted_cursor_re_projects_without_duplicating(self) -> None:
        """Cursor loss must be recoverable: the store's own check is the backstop."""
        self._seed(4)
        store = _FakeStore()
        ledger_index.import_pending(store)
        ledger_index.reset_cursor()

        after = ledger_index.import_pending(store)
        self.assertEqual(after["written"], 0, "the store reports these already exist")
        self.assertEqual(after["skipped"], 4)

    def test_corrupt_cursor_degrades_to_rescanning(self) -> None:
        """A bad cursor must not mean 'assume everything is imported' — that would
        silently leave the index permanently stale."""
        self._seed(3)
        (self.tmp / "apps/ops-mission-control/data").mkdir(parents=True, exist_ok=True)
        cursor = ledger_index._cursor_path()
        cursor.write_text("{ not json", encoding="utf-8")

        store = _FakeStore()
        self.assertEqual(ledger_index.import_pending(store)["written"], 3)


class TestStoreContract(_Env):
    def test_writes_are_merge_only_and_deferred(self) -> None:
        """Both flags are load-bearing and easy to drop in a refactor.

        `preserve_existing=False` would let an import tombstone a teammate's row;
        `defer_embedding=False` would embed inline and turn a bulk import into an
        hours-long stall.
        """
        self._seed(2)
        store = _FakeStore()
        ledger_index.import_pending(store)

        self.assertTrue(store.writes)
        for call in store.writes:
            self.assertTrue(call["preserve_existing"], "import must never tombstone")
            self.assertTrue(call["defer_embedding"], "bulk import must not embed inline")

    def test_rows_are_tagged_so_ops_search_stays_scoped(self) -> None:
        self._seed(1)
        store = _FakeStore()
        ledger_index.import_pending(store)
        self.assertIn(ledger_index.SOURCE_TAG, store.writes[0]["tags"])

    def test_text_carries_both_pattern_and_fix(self) -> None:
        """Matching the right lesson and returning no remedy is a half-answer."""
        ledger.upsert(LedgerEntry.create(pattern="DLQ fills up", fix="repair trust policy"))
        store = _FakeStore()
        ledger_index.import_pending(store)
        text = store.writes[0]["text"]
        self.assertIn("DLQ fills up", text)
        self.assertIn("repair trust policy", text)

    def test_overlong_text_is_truncated_not_dropped(self) -> None:
        """The store rejects >2000 chars; a truncated fix still points at the answer."""
        ledger.upsert(LedgerEntry.create(pattern="p" * 1500, fix="f" * 1500))
        store = _FakeStore()
        result = ledger_index.import_pending(store)
        self.assertEqual(result["written"], 1)
        self.assertLessEqual(len(store.writes[0]["text"]), ledger_index.TEXT_MAX)

    def test_importance_reflects_ledger_quality(self) -> None:
        """Otherwise the index ranks by recency and throws away trust/confidence."""
        weak = LedgerEntry.create(pattern="weak lesson here", fix="maybe this helps")
        strong = LedgerEntry.create(
            pattern="strong lesson here", fix="this definitely works", confidence="high"
        )
        strong.trust = "verified"
        strong.use_count = 5
        self.assertGreater(
            ledger_index._importance(strong),
            ledger_index._importance(weak),
        )
        self.assertLessEqual(ledger_index._importance(strong), 1.0)


class TestNeverFatal(_Env):
    def test_a_broken_store_is_survived(self) -> None:
        self._seed(3)
        result = ledger_index.import_pending(_FakeStore(fail_write=True))
        self.assertEqual(result["written"], 0)

    def test_a_failed_embedding_sweep_leaves_rows_written(self) -> None:
        """Rows stay keyword-searchable; only vector search waits for the model."""
        self._seed(2)
        store = _FakeStore(fail_backfill=True)
        result = ledger_index.import_pending(store)
        self.assertEqual(result["written"], 2)
        self.assertEqual(result["embedded"], 0)

    def test_an_empty_ledger_is_a_quiet_noop(self) -> None:
        store = _FakeStore()
        result = ledger_index.import_pending(store)
        self.assertEqual(result, {"scanned": 0, "written": 0, "skipped": 0, "embedded": 0})
        self.assertEqual(store.backfill_calls, 0)


class TestSearch(_Env):
    def test_search_is_scoped_to_ledger_rows(self) -> None:
        """The index is shared with the rest of Kiro Crew; an ops query wants ops
        knowledge, not unrelated conversational memories."""
        store = _FakeStore()
        ledger_index.search_similar(store, "DLQ AccessDenied")
        self.assertEqual(store.last_search["tag_filter"], [ledger_index.SOURCE_TAG])

    def test_blank_query_does_not_hit_the_store(self) -> None:
        store = _FakeStore()
        self.assertEqual(ledger_index.search_similar(store, "   "), [])
        self.assertFalse(hasattr(store, "last_search"))

    def test_search_failure_returns_empty_rather_than_raising(self) -> None:
        """Semantic recall is additive to fingerprint matching, never a prerequisite."""

        class _Broken:
            def search_episodic(self, **kw: Any) -> list[dict]:
                raise RuntimeError("faiss index corrupt")

        self.assertEqual(ledger_index.search_similar(_Broken(), "anything"), [])


class TestScale(_Env):
    """The owner asked specifically about large volumes, so assert the shape of the
    cost rather than just correctness."""

    def test_ten_thousand_entries_import_incrementally(self) -> None:
        self._seed(2000)
        store = _FakeStore()

        # Drain in bounded batches, as successive dispatch cycles would.
        total = 0
        for _ in range(4):
            total += ledger_index.import_pending(store, limit=500)["written"]
        self.assertEqual(total, 2000)
        self.assertEqual(store.backfill_calls, 4, "one sweep per batch, not per row")

        # The property that matters: a full re-import after everything is indexed
        # costs zero embeddings.
        again = ledger_index.import_pending(store, limit=500)
        self.assertEqual(again["written"], 0)
        self.assertEqual(store.backfill_calls, 4, "no extra sweep")

    def test_cursor_stays_proportional_to_entry_count(self) -> None:
        """The cursor is the scaling risk: it must hold ids, never texts."""
        self._seed(500)
        store = _FakeStore()
        ledger_index.import_pending(store, limit=500)
        size = ledger_index._cursor_path().stat().st_size
        # 500 x 16-hex id + JSON overhead. A cursor that accidentally stored texts
        # would be an order of magnitude larger.
        self.assertLess(size, 500 * 40, f"cursor is {size} bytes for 500 entries")


class TestSemanticRecallWiring(_Env):
    """`attach_similar_lessons` is where the index finally reaches an investigation.

    The property that matters most is the SEPARATION: a semantic hit must never be
    presented, counted, or ranked as though the fingerprint had matched.
    """

    @staticmethod
    def _claimed(title: str = "DLQ fills on AccessDenied", resource: str = "my-queue"):
        from kiro_crew.apps.builtins.ops_mission_control.backend import store as inc_store
        from kiro_crew.apps.builtins.ops_mission_control.backend.dispatch import (
            ClaimedIncident,
        )
        from kiro_crew.apps.builtins.ops_mission_control.backend.models import Signal

        signal = Signal.create(
            source="cloudwatch", native_id="alarm/x", title=title, resource=resource
        )
        incident = inc_store.claim(signal, operating_mode="observe")
        assert incident is not None
        return ClaimedIncident(incident=incident)

    def test_similar_entries_are_attached(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(3)
        store = _FakeStore()
        ledger_index.import_pending(store)

        claimed = dispatch.attach_similar_lessons(self._claimed(), store, limit=2)
        self.assertEqual(len(claimed.similar), 2, "capped at the requested limit")
        self.assertEqual(claimed.matches, [], "semantic recall must not touch matches")

    def test_a_fingerprint_match_is_never_repeated_as_similar(self) -> None:
        """The brief must not list one entry twice under two confidence framings."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(2)
        entries = ledger.read_entries()
        store = _FakeStore()
        ledger_index.import_pending(store)

        claimed = self._claimed()
        claimed.matches = [entries[0]]
        dispatch.attach_similar_lessons(claimed, store, limit=5)

        similar_ids = {e.entry_id for e in claimed.similar}
        self.assertNotIn(entries[0].entry_id, similar_ids)

    def test_recall_does_not_inflate_use_counts(self) -> None:
        """A similar hit is a lead, not a use.

        `use_count` decides `is_fast_path`, which is the one thing between a remembered
        fix and a confidently-wrong one — so a near-miss must not increment it.
        """
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(2)
        before = {e.entry_id: e.use_count for e in ledger.read_entries()}
        store = _FakeStore()
        ledger_index.import_pending(store)

        dispatch.attach_similar_lessons(self._claimed(), store, limit=5)

        after = {e.entry_id: e.use_count for e in ledger.read_entries()}
        self.assertEqual(before, after, "recall must not record a use")

    def test_no_store_is_a_quiet_noop(self) -> None:
        """An install with no vector store must dispatch exactly as before."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(1)
        claimed = dispatch.attach_similar_lessons(self._claimed(), None)
        self.assertEqual(claimed.similar, [])

    def test_a_broken_store_leaves_matches_intact(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(1)
        entries = ledger.read_entries()

        class _Broken:
            def search_episodic(self, **kw: Any) -> list[dict]:
                raise RuntimeError("index corrupt")

        claimed = self._claimed()
        claimed.matches = list(entries)
        dispatch.attach_similar_lessons(claimed, _Broken())
        self.assertEqual(claimed.similar, [])
        self.assertEqual(len(claimed.matches), 1, "fingerprint matches survive")

    def test_brief_frames_similar_as_leads_not_fixes(self) -> None:
        """Wording is the control here: a ranked list invites applying the top hit."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(2)
        store = _FakeStore()
        ledger_index.import_pending(store)
        claimed = dispatch.attach_similar_lessons(self._claimed(), store, limit=2)

        brief = dispatch.investigation_brief(claimed)
        self.assertIn("Related lessons", brief)
        self.assertIn("fingerprints do NOT match", brief)
        self.assertIn("never as a fix to apply", brief)

    def test_brief_omits_the_section_when_there_is_nothing_similar(self) -> None:
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        claimed = self._claimed()
        self.assertNotIn("Related lessons", dispatch.investigation_brief(claimed))

    def test_similar_is_serialized_for_the_cron(self) -> None:
        """The dispatch route returns this to the cron, which passes it to the agent."""
        from kiro_crew.apps.builtins.ops_mission_control.backend import dispatch

        self._seed(1)
        store = _FakeStore()
        ledger_index.import_pending(store)
        claimed = dispatch.attach_similar_lessons(self._claimed(), store, limit=1)

        payload = claimed.to_dict()
        self.assertIn("similar", payload)
        self.assertEqual(len(payload["similar"]), 1)


if __name__ == "__main__":
    unittest.main()


class TestLedgerMutationsAreLocked(_Env):
    """Every read-modify-write of the ledger holds one exclusive file lock.

    `hygiene` reads, dedupes/prunes, and calls `_write_all`, which OVERWRITES the file. A
    `POST /ledger` (`upsert`) or a `record_use` landing between the pass's `read_entries` and
    its write was silently erased — the ledger analogue of the incident-index race, and the
    write half of the peek/ack lesson (a rewrite from a stale snapshot drops everything
    appended since). The ledger had no lock at all. Found in review.
    """

    def test_hygiene_does_not_erase_an_append_that_races_its_read(self):
        """Drive the exact interleaving deterministically: a new entry is appended DURING
        hygiene's `read_entries`, in the window `_write_all` would otherwise clobber.

        A real thread race would be flaky; patching `read_entries` to append-then-return
        reproduces it every run. The lock does not prevent the interleaving here (same
        process, re-entrant open) — the assertion is that the appended entry SURVIVES, which
        it cannot if hygiene rewrites from a snapshot taken before it."""
        from unittest import mock

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        ledger.upsert(LedgerEntry.create(pattern="original", fix="f"))
        latecomer = LedgerEntry.create(pattern="arrived during hygiene", fix="f2")

        real_read = ledger.read_entries
        fired = {"done": False}

        def read_then_append():
            rows = real_read()
            if not fired["done"]:
                fired["done"] = True
                ledger._append(latecomer)  # the concurrent POST
            return rows

        with mock.patch.object(ledger, "read_entries", read_then_append):
            ledger.hygiene()

        patterns = {e.pattern for e in ledger.read_entries()}
        self.assertIn(
            "arrived during hygiene",
            patterns,
            "hygiene rewrote from a stale snapshot and erased a concurrent append",
        )
        self.assertIn("original", patterns)

    def test_each_mutator_takes_the_lock(self):
        """Structural: a behavioural cross-process race needs two processes inside one file
        lock and produces a single winner in-process, so what is assertable is that every
        read-modify-write path enters `_LedgerLock`."""
        import inspect

        from kiro_crew.apps.builtins.ops_mission_control.backend import ledger

        for fn in (ledger.upsert, ledger.record_use, ledger.record_miss, ledger.remove,
                   ledger.hygiene):
            src = inspect.getsource(fn)
            self.assertIn(
                "_LedgerLock()",
                src,
                f"{fn.__name__} rewrites the ledger without holding _LedgerLock",
            )

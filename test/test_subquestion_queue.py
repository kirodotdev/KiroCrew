"""Pure-logic tests for the RL v2 runtime sub-question queue (no agent calls)."""
from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.builtins.auto_research.subquestion_queue import (
    QUEUE_FILENAME,
    analyzed_count,
    dequeue_top_k,
    enqueue,
    is_known,
    load_queue,
    mark_analyzed,
    new_queue,
    pending_count,
    save_queue,
)


class TestQueueShape:
    def test_new_queue_has_both_buckets(self):
        q = new_queue()
        assert q == {"pending": [], "analyzed": []}


class TestEnqueue:
    def test_assigns_fields(self):
        q = new_queue()
        added = enqueue(q, [{"text": "Why does X fail?", "priority": 0.9}],
                        parent_id="root", depth=2)
        assert len(added) == 1
        it = added[0]
        assert it["text"] == "Why does X fail?"
        assert it["depth"] == 2
        assert it["parent_id"] == "root"
        assert it["priority"] == 0.9
        assert it["origin"] == "emergent"
        assert it["status"] == "pending"
        assert it["id"].startswith("q")
        assert pending_count(q) == 1

    def test_accepts_plain_strings(self):
        q = new_queue()
        added = enqueue(q, ["plain question one", "plain question two"])
        assert len(added) == 2
        assert all(i["priority"] == 0.0 for i in added)

    def test_dedup_against_existing(self):
        q = new_queue()
        enqueue(q, [{"text": "Same Question"}])
        added = enqueue(q, [{"text": "  same   question "}])  # normalized dup
        assert added == []
        assert pending_count(q) == 1

    def test_dedup_within_batch(self):
        q = new_queue()
        added = enqueue(q, [{"text": "dup"}, {"text": "DUP"}, {"text": "unique"}])
        assert len(added) == 2
        assert {i["text"] for i in added} == {"dup", "unique"}

    def test_skips_empty_text(self):
        q = new_queue()
        added = enqueue(q, [{"text": "   "}, "", {"text": "real"}])
        assert [i["text"] for i in added] == ["real"]

    def test_max_admit_keeps_highest_priority(self):
        q = new_queue()
        added = enqueue(q, [
            {"text": "low", "priority": 0.1},
            {"text": "high", "priority": 0.9},
            {"text": "mid", "priority": 0.5},
        ], max_admit=2)
        assert [i["text"] for i in added] == ["high", "mid"]
        assert pending_count(q) == 2

    def test_equal_priority_is_stable(self):
        q = new_queue()
        added = enqueue(q, [
            {"text": "first", "priority": 0.5},
            {"text": "second", "priority": 0.5},
        ])
        assert [i["text"] for i in added] == ["first", "second"]


class TestDequeue:
    def test_top_k_returns_highest_priority_and_removes(self):
        q = new_queue()
        enqueue(q, [
            {"text": "a", "priority": 0.2},
            {"text": "b", "priority": 0.8},
            {"text": "c", "priority": 0.5},
        ])
        out = dequeue_top_k(q, 2)
        assert [i["text"] for i in out] == ["b", "c"]
        assert pending_count(q) == 1
        assert q["pending"][0]["text"] == "a"

    def test_zero_and_empty(self):
        q = new_queue()
        assert dequeue_top_k(q, 0) == []
        assert dequeue_top_k(q, 5) == []
        enqueue(q, [{"text": "only"}])
        assert dequeue_top_k(q, -1) == []
        assert pending_count(q) == 1


class TestAnalyzedAndDedup:
    def test_mark_analyzed_then_blocks_reenqueue(self):
        q = new_queue()
        assert dequeue_top_k(q, 0) == []  # no-op
        items = enqueue(q, [{"text": "investigate me"}])
        popped = dequeue_top_k(q, 1)
        assert popped == items
        mark_analyzed(q, popped)
        assert analyzed_count(q) == 1
        assert q["analyzed"][0]["status"] == "analyzed"
        # Re-generating the same sub-question must not be re-admitted.
        again = enqueue(q, [{"text": "Investigate ME"}])
        assert again == []
        assert pending_count(q) == 0

    def test_is_known(self):
        q = new_queue()
        enqueue(q, [{"text": "Known one"}])
        assert is_known(q, "  known   ONE ")
        assert not is_known(q, "unseen")


class TestPersistence:
    def test_roundtrip(self, tmp_path: Path):
        q = new_queue()
        enqueue(q, [{"text": "persist me", "priority": 0.3}], depth=1)
        save_queue(tmp_path, q)
        assert (tmp_path / QUEUE_FILENAME).exists()
        loaded = load_queue(tmp_path)
        assert pending_count(loaded) == 1
        assert loaded["pending"][0]["text"] == "persist me"
        assert loaded["pending"][0]["depth"] == 1

    def test_load_missing_returns_empty(self, tmp_path: Path):
        assert load_queue(tmp_path) == {"pending": [], "analyzed": []}

    def test_load_corrupt_returns_empty(self, tmp_path: Path):
        (tmp_path / QUEUE_FILENAME).write_text("{not json")
        assert load_queue(tmp_path) == {"pending": [], "analyzed": []}

    def test_load_non_dict_returns_empty(self, tmp_path: Path):
        (tmp_path / QUEUE_FILENAME).write_text("[1, 2, 3]")
        assert load_queue(tmp_path) == {"pending": [], "analyzed": []}

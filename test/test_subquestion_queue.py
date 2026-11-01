"""Pure-logic tests for the RL v2 runtime sub-question queue (no agent calls)."""
from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_research.subquestion_queue import (
    QUEUE_FILENAME,
    dequeue_top_k,
    enqueue,
    load_queue,
    mark_analyzed,
    new_queue,
    next_depth,
    pending_count,
    save_queue,
)


def _no_raise(fn, *args):
    """Call a queue reader on a hand-edited queue; a raise fails the round."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 -- any raise is the defect under test
        pytest.fail(f"{fn.__name__} raised {exc!r} on a malformed queue entry")


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

    # The queue file lives in the agent-writable campaign dir, and load_queue
    # only checks the top-level shape. A hand-rewritten item must be skipped,
    # not raised out of the caller's open BEGIN IMMEDIATE transaction.
    def test_null_priority_is_read_as_zero(self):
        q = {"pending": [
            {"text": "nulled", "priority": None},
            {"text": "ranked", "priority": 0.9},
        ], "analyzed": []}
        out = _no_raise(dequeue_top_k, q, 2)
        assert [i["text"] for i in out] == ["ranked", "nulled"]
        assert q["pending"] == []

    def test_non_numeric_priority_is_read_as_zero(self):
        q = {"pending": [
            {"text": "words", "priority": "high"},
            {"text": "ranked", "priority": 0.1},
        ], "analyzed": []}
        out = _no_raise(dequeue_top_k, q, 1)
        assert [i["text"] for i in out] == ["ranked"]
        assert [i["text"] for i in q["pending"]] == ["words"]

    def test_non_finite_priority_is_read_as_zero(self):
        q = {"pending": [
            {"text": "inf", "priority": "inf"},
            {"text": "nan", "priority": "nan"},
            {"text": "ranked", "priority": 0.1},
        ], "analyzed": []}
        out = _no_raise(dequeue_top_k, q, 1)
        assert [i["text"] for i in out] == ["ranked"]
        assert [i["text"] for i in q["pending"]] == ["inf", "nan"]

    def test_malformed_items_are_skipped_and_dropped(self):
        q = {"pending": [
            "a bare string",
            None,
            {"priority": 0.99},
            {"text": "", "priority": 0.98},
            {"text": "good", "priority": 0.5},
        ], "analyzed": []}
        out = _no_raise(dequeue_top_k, q, 5)
        assert [i["text"] for i in out] == ["good"]
        assert q["pending"] == []


class TestAnalyzedAndDedup:
    def test_mark_analyzed_then_blocks_reenqueue(self):
        q = new_queue()
        assert dequeue_top_k(q, 0) == []  # no-op
        items = enqueue(q, [{"text": "investigate me"}])
        popped = dequeue_top_k(q, 1)
        assert popped == items
        mark_analyzed(q, popped)
        assert len(q["analyzed"]) == 1
        assert q["analyzed"][0]["status"] == "analyzed"
        # Re-generating the same sub-question must not be re-admitted.
        again = enqueue(q, [{"text": "Investigate ME"}])
        assert again == []
        assert pending_count(q) == 0


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

    def test_load_non_list_buckets_read_as_empty(self, tmp_path: Path):
        (tmp_path / QUEUE_FILENAME).write_text('{"pending": null, "analyzed": "x"}')
        q = load_queue(tmp_path)
        assert q["pending"] == [] and q["analyzed"] == []
        # Every reader the exploration round calls must then work on it.
        assert pending_count(q) == 0
        assert next_depth(q) == 1
        assert enqueue(q, [{"text": "fresh"}])[0]["text"] == "fresh"

    def test_next_depth_skips_a_malformed_depth(self):
        q = {"pending": [], "analyzed": [
            {"text": "a", "depth": None},
            {"text": "b", "depth": "deep"},
            {"text": "c", "depth": 2},
        ]}
        assert _no_raise(next_depth, q) == 3

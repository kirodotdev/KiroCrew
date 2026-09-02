"""Characterization tests for the extracted subagent scheduler boundary."""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.subagent_scheduler import SubagentScheduler


@dataclass
class SlotOwner:
    _slot_released: bool = False


def test_admission_combines_capacity_and_stagger_without_mutation() -> None:
    scheduler = SubagentScheduler(max_concurrent=2, stagger_seconds=5.0)

    initial = scheduler.admission(now=10.0)
    scheduler.occupy(SlotOwner(), now=10.0)
    staggered = scheduler.admission(now=12.0)
    ready = scheduler.admission(now=15.0)
    scheduler.occupy(SlotOwner(), now=15.0)
    full = scheduler.admission(now=30.0)

    assert initial.should_queue is False
    assert initial.slot_free is True
    assert initial.retry_after == 0.0
    assert staggered.should_queue is True
    assert staggered.slot_free is True
    assert staggered.retry_after == 3.0
    assert ready.should_queue is False
    assert full.should_queue is True
    assert full.slot_free is False
    assert full.retry_after is None


def test_take_ready_is_fifo_and_preserves_opaque_payload() -> None:
    scheduler = SubagentScheduler(max_concurrent=1, stagger_seconds=2.0)
    first = {"_preassigned_id": "run-1", "allowed_tools": ["read"], "bare": True}
    second = {"_preassigned_id": "run-2", "model": "served-model"}
    scheduler.enqueue(first)
    scheduler.enqueue(second)

    delayed = scheduler.take_ready(now=1.0)
    ready = scheduler.take_ready(now=2.0)

    assert delayed.entry is None
    assert delayed.retry_after == 1.0
    assert scheduler.queue == [second]
    assert ready.entry is first
    assert ready.retry_after == 0.0


def test_queue_continuation_delay_is_scheduler_policy() -> None:
    scheduler = SubagentScheduler(max_concurrent=1, stagger_seconds=2.0)

    assert scheduler.continuation_delay() is None
    scheduler.enqueue({"_preassigned_id": "run-1"})
    assert scheduler.continuation_delay() == 2.0

    scheduler.occupy(SlotOwner(), now=10.0)
    assert scheduler.continuation_delay() is None


def test_slot_release_and_reoccupation_are_exactly_accounted() -> None:
    scheduler = SubagentScheduler(max_concurrent=2, stagger_seconds=4.0)
    owner = SlotOwner()
    scheduler.occupy(owner, now=20.0)

    assert scheduler.release(owner) is True
    assert scheduler.release(owner) is False
    assert scheduler.running_count == 0

    scheduler.reoccupy(owner)

    assert scheduler.running_count == 1
    assert scheduler.last_start == 20.0
    assert owner._slot_released is False


def test_recovery_reoccupation_refuses_capacity_atomically() -> None:
    scheduler = SubagentScheduler(max_concurrent=1, stagger_seconds=4.0)
    live = SlotOwner()
    recovering = SlotOwner(_slot_released=True)
    scheduler.occupy(live, now=20.0)

    assert scheduler.try_reoccupy(recovering) is False
    assert scheduler.running_count == 1
    assert recovering._slot_released is True

    assert scheduler.release(live) is True
    assert scheduler.try_reoccupy(recovering) is True
    assert scheduler.running_count == 1
    assert recovering._slot_released is False
    assert scheduler.last_start == 20.0


def test_queue_queries_and_targeted_remove_preserve_other_entries() -> None:
    scheduler = SubagentScheduler(max_concurrent=2, stagger_seconds=0.0)
    first = {
        "_preassigned_id": "run-1",
        "parent_session_key": "parent-a",
        "batch_id": "batch-1",
        "conversation_key": "subagent:conversation-1",
    }
    second = {
        "_preassigned_id": "run-2",
        "parent_session_key": "parent-b",
        "batch_id": "batch-2",
        "conversation_key": "subagent:conversation-2",
    }
    scheduler.enqueue(first)
    scheduler.enqueue(second)

    removed = scheduler.remove("run-1")

    assert removed == [first]
    assert scheduler.queue == [second]
    assert scheduler.queued_depth("parent-a") == 0
    assert scheduler.queued_depth("parent-b") == 1
    assert scheduler.contains_batch("batch-2") is True
    assert scheduler.find_conversation("subagent:conversation-2") is second

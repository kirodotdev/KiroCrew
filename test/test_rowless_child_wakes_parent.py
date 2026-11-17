"""A parent blocked in ``spawn_sub_agents`` resumes when its last child ends,
whether or not the task store holds a row for that child.

Only a persistent run gets a store row. A run in a temporary or incognito chat
gets none, so the ledger cannot count its children: the wait is settled from the
parent's in-memory wait record and the manager's run records.

The manager, its admission pump and the durable store are real; the run is a
fake worker the test finishes on demand (``overload_fakes.ManagerHarness``).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from overload_fakes import ManagerHarness

import kiro_crew.subagent as subagent_mod
from kiro_crew.taskq import model
from kiro_crew.taskq.waits import WaitRecord

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_ROWLESS_MODES = ["temporary", "incognito"]


class _Memory:
    """The host memory the admission gate reads, switchable mid-test."""

    def __init__(self) -> None:
        self.low = False

    def __call__(self, min_gb: float | None = None, **_kw: Any) -> tuple[bool, float]:
        avail = 0.1 if self.low else 8.0
        return (min_gb is None or avail >= min_gb), avail


@pytest.fixture
def memory() -> _Memory:
    return _Memory()


@pytest.fixture
def h(memory):
    harness: ManagerHarness | None = None
    # Installed over the harness's own fixed 8 GB reading, so it starts after it.
    reading = patch.object(subagent_mod, "check_memory_available", memory)

    async def make(mode: str, max_concurrent: int = 2) -> ManagerHarness:
        nonlocal harness
        harness = ManagerHarness(max_concurrent)
        reading.start()
        await harness.mgr.wait_taskq_ready()
        harness.mgr._memory_mode_for_session = lambda _key: mode
        return harness

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield make
        if harness is not None:
            reading.stop()
            harness.close()
            harness.mgr.close()


async def _blocked_parent(hz: ManagerHarness):
    parent = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(parent)
    return parent


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _ROWLESS_MODES)
async def test_the_only_child_ending_resumes_the_parent(h, mode) -> None:
    hz = await h(mode)
    parent = await _blocked_parent(hz)
    child = hz.child_of(parent, "A")
    await hz.settle()
    assert hz.state(parent) is None and hz.state(child) is None, "neither run has a store row"
    assert hz.live(parent)._slot_released is True, "the parent yielded its slot to wait"

    await hz.end(child)
    await hz.settle()

    assert hz.live(parent)._slot_released is False
    assert hz.mgr._admission.resume_granted(parent.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _ROWLESS_MODES)
async def test_the_parent_waits_for_its_last_child(h, mode) -> None:
    hz = await h(mode, max_concurrent=3)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    second = hz.child_of(parent, "B")
    await hz.settle()

    await hz.end(first)
    await hz.settle()
    assert hz.live(parent)._slot_released is True, "B still runs"

    await hz.end(second)
    await hz.settle()
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_a_child_still_waiting_for_a_slot_keeps_the_parent_waiting(h) -> None:
    hz = await h("temporary", max_concurrent=1)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    second = hz.child_of(parent, "B")
    await hz.settle()
    assert second.id not in hz.mgr._agents, "B is queued behind A"

    await hz.end(first)
    await hz.settle()
    assert hz.live(parent)._slot_released is True, "B has not ended"

    await hz.end(hz.live(second))
    await hz.settle()
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_a_persistent_parent_still_resumes_through_the_ledger(h) -> None:
    hz = await h("persistent")
    parent = await _blocked_parent(hz)
    child = hz.child_of(parent, "A")
    await hz.settle()
    assert hz.state(parent) == model.WAITING_CHILDREN

    await hz.end(child)
    await hz.settle()

    assert hz.state(parent) == model.RUNNING
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_an_ended_child_whose_record_was_dropped_still_counts(h) -> None:
    hz = await h("temporary", max_concurrent=3)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    second = hz.child_of(parent, "B")
    await hz.settle()

    await hz.end(first)
    await hz.settle()
    # Dismissing A's card drops its record from the manager.
    hz.mgr._agents.pop(first.id)

    await hz.end(second)
    await hz.settle()
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_an_ended_child_evicted_from_the_manager_still_counts(h) -> None:
    from kiro_crew.context_management import evict_completed_agents

    hz = await h("incognito", max_concurrent=3)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    second = hz.child_of(parent, "B")
    await hz.settle()

    await hz.end(first)
    await hz.settle()
    assert evict_completed_agents(hz.mgr._agents, max_retained=0) == 1
    assert first.id not in hz.mgr._agents

    await hz.end(second)
    await hz.settle()
    assert hz.live(parent)._slot_released is False


@pytest.mark.asyncio
async def test_a_second_wait_starts_with_no_ended_children(h) -> None:
    hz = await h("temporary", max_concurrent=3)
    parent = await _blocked_parent(hz)
    first = hz.child_of(parent, "A")
    await hz.settle()
    await hz.end(first)
    await hz.settle()
    assert hz.live(parent)._slot_released is False
    assert hz.live(parent)._rowless_ended == {first.id}

    hz.block_in_spawn_sub_agents(parent, call_id="call-2")
    second = hz.child_of(parent, "B")
    await hz.settle()
    assert hz.live(parent)._slot_released is True, "the second wait yielded the slot"
    assert hz.live(parent)._rowless_ended == set()

    await hz.end(second)
    await hz.settle()
    assert hz.live(parent)._slot_released is False


async def _blocked_on_running_a_and_deferred_b(hz: ManagerHarness, memory: _Memory):
    """A rowless parent blocked on running A and on B, which the memory gate deferred."""
    parent = await _blocked_parent(hz)
    memory.low = True
    b = hz.child_of(parent, "B")
    await hz.settle()
    memory.low = False
    a = hz.child_of(parent, "A")
    await hz.settle()
    assert hz.state(parent) is None, "the parent has no store row"
    assert b.id not in hz.mgr._agents and not b.done, "B is deferred"
    record = WaitRecord.from_dict(hz.live(parent)._wait_record)
    assert record is not None and sorted(record.resume_condition.ids) == sorted([a.id, b.id])
    assert hz.live(parent)._slot_released is True
    return parent, a, b


async def _release(hz: ManagerHarness, parent: Any, *ids: str) -> dict[str, Any]:
    out = await hz.mgr._admission.taskq_release_deferred_async(parent.id, list(ids))
    await hz.settle()
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _ROWLESS_MODES)
async def test_releasing_a_deferred_child_after_the_sibling_ended_resumes_the_parent(
    h, memory, mode
) -> None:
    hz = await h(mode)
    parent, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    await hz.end(a)
    assert hz.live(parent)._slot_released is True, "B is still awaited"

    out = await _release(hz, parent, b.id)
    assert out == {"released": [b.id], "resumed": True}
    assert hz.live(parent)._slot_released is False
    assert hz.mgr._admission.resume_granted(parent.id)


@pytest.mark.asyncio
async def test_the_sibling_ending_after_the_deferred_release_resumes_the_parent(h, memory) -> None:
    hz = await h("temporary")
    parent, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)

    out = await _release(hz, parent, b.id)
    assert out == {"released": [b.id], "resumed": False}, "A still runs"
    assert hz.live(parent)._slot_released is True

    await hz.end(a)
    assert hz.live(parent)._slot_released is False
    assert hz.mgr._admission.resume_granted(parent.id)


class TestTheInMemoryCount:
    """``_rowless_wait_is_over`` on records built by hand."""

    @staticmethod
    def _admission(runs: dict):
        from kiro_crew.subagent_manager.admission.waits import _WaitsMixin

        admission = SimpleNamespace(
            _manager=SimpleNamespace(_agents=runs),
            _rowless_wait_is_over=_WaitsMixin._rowless_wait_is_over,
            _ended_in_memory=lambda parent, awaited: _WaitsMixin._ended_in_memory(
                admission, parent, awaited
            ),
        )
        return admission

    @staticmethod
    def _parent(awaited: list[str], **overrides):
        record = WaitRecord.children(awaited, since=0.0, tool_call_id="call", deadline_at=None)
        fields = dict(
            done=False,
            _slot_released=True,
            _wait_record=record.to_dict(),
            _deferred_released=set(),
            _rowless_ended=set(),
        )
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def _over(self, parent, runs: dict, child_id: str = "a") -> bool:
        admission = self._admission(runs)
        return admission._rowless_wait_is_over(admission, parent, SimpleNamespace(id=child_id))

    def test_an_awaited_run_this_manager_does_not_hold_is_not_ended(self):
        assert self._over(self._parent(["a", "gone"]), {}) is False

    def test_an_awaited_run_that_is_not_done_is_not_ended(self):
        runs = {"b": SimpleNamespace(done=False)}
        assert self._over(self._parent(["a", "b"]), runs) is False

    def test_every_other_awaited_run_done_ends_the_wait(self):
        runs = {"b": SimpleNamespace(done=True)}
        assert self._over(self._parent(["a", "b"]), runs) is True

    def test_a_child_released_as_deferred_is_not_awaited(self):
        parent = self._parent(["a", "held"], _deferred_released={"held"})
        assert self._over(parent, {}) is True

    def test_a_parent_that_holds_its_slot_is_not_resumed(self):
        assert self._over(self._parent(["a"], _slot_released=False), {}) is False

    def test_a_finished_parent_is_not_resumed(self):
        assert self._over(self._parent(["a"], done=True), {}) is False

    def test_a_parent_with_no_wait_record_is_not_resumed(self):
        assert self._over(self._parent(["a"], _wait_record=None), {}) is False

    def test_a_child_that_reported_ending_counts_without_its_record(self):
        parent = self._parent(["a", "b"], _rowless_ended={"b"})
        assert self._over(parent, {}) is True

    def test_the_ending_child_joins_the_ended_set(self):
        parent = self._parent(["a", "b"])
        assert self._over(parent, {"b": SimpleNamespace(done=False)}) is False
        assert parent._rowless_ended == {"a"}

    def test_a_parent_not_waiting_records_nothing(self):
        parent = self._parent(["a"], _slot_released=False)
        self._over(parent, {})
        assert parent._rowless_ended == set()

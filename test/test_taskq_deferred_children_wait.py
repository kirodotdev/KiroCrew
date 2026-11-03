"""A blocked parent's tool releases its deferral-held children from the wait.

``spawn_sub_agents`` settles a member the gate DEFERRED (memory floor) without
waiting for it: the deferral can outlast the call, and the member's completion
event arrives on its own. The gateway never infers that for the parent's
``waiting_children`` record. The tool names those members once, and
``taskq_release_deferred_async`` stops awaiting them, so the last awaited
sibling ending (or the release itself) resumes the parent.

The manager, its admission pump and the durable store are real; the run is a
fake worker the test finishes on demand (``overload_fakes.ManagerHarness``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from overload_fakes import ManagerHarness, settle_store_writes

import kiro_crew.subagent as subagent_mod
from kiro_crew.taskq import TaskStoreUnavailable, model
from kiro_crew.taskq.waits import WaitRecord
from kiro_crew.validation_parts.core_agent_schemas import SPAWN_BATCH_MEMBERS_MAX

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def quiet():
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        yield


class _Memory:
    """The host memory the gate reads, switchable mid-test."""

    def __init__(self) -> None:
        self.low = False

    def __call__(self, min_gb: float | None = None, **_kw: Any) -> tuple[bool, float]:
        avail = 0.1 if self.low else 8.0
        return (min_gb is None or avail >= min_gb), avail


@pytest.fixture
def memory() -> _Memory:
    return _Memory()


@pytest.fixture
def h(quiet, memory):
    harness: ManagerHarness | None = None
    # Installed over the harness's own fixed 8 GB reading, so it starts after it.
    reading = patch.object(subagent_mod, "check_memory_available", memory)

    async def make(max_concurrent: int = 2) -> ManagerHarness:
        nonlocal harness
        harness = ManagerHarness(max_concurrent)
        reading.start()
        await harness.mgr.wait_taskq_ready()
        return harness

    yield make
    if harness is not None:
        reading.stop()
        harness.close()


def _awaited(hz: ManagerHarness, parent: Any) -> list[str]:
    """The children *parent*'s in-memory ``waiting_children`` record awaits."""
    record = WaitRecord.from_dict(hz.live(parent)._wait_record)
    return list(record.resume_condition.ids) if record is not None else []


async def _release(hz: ManagerHarness, parent: Any, *ids: str) -> dict[str, Any]:
    out = await hz.mgr._admission.taskq_release_deferred_async(parent.id, list(ids))
    await settle_store_writes(hz.store, rounds=2)
    return out


async def _blocked_on_running_a_and_deferred_b(hz: ManagerHarness, memory: _Memory):
    """S blocked in ``spawn_sub_agents`` on running A and gate-deferred B."""
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    memory.low = True
    b = hz.child_of(s, "B")
    await hz.settle()
    memory.low = False
    a = hz.child_of(s, "A")
    await hz.settle()
    assert hz.state(b) not in model.TERMINAL and b.id not in hz.mgr._agents, "B is deferred"
    assert hz.state(s) == model.WAITING_CHILDREN
    assert sorted(_awaited(hz, s)) == sorted([a.id, b.id]), "the registration awaits B"
    return s, a, b


@pytest.mark.asyncio
async def test_released_deferred_child_leaves_the_wait_and_the_sibling_resumes(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)

    out = await _release(hz, s, b.id)
    assert out == {"released": [b.id], "resumed": False}
    assert _awaited(hz, s) == [a.id]
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id]

    await hz.end(a)
    assert hz.state(s) == model.RUNNING, "the last awaited child ended"
    assert hz.live(s)._slot_released is False
    assert hz.mgr._admission.resume_granted(s.id)


@pytest.mark.asyncio
async def test_a_later_registration_does_not_await_a_released_child_again(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    await _release(hz, s, b.id)

    # A sibling registering later reads the ledger's outstanding children,
    # which still list B: the released id stays out of the wait.
    c = hz.child_of(s, "C")
    await hz.settle()
    assert sorted(_awaited(hz, s)) == sorted([a.id, c.id])
    await hz.end(a)
    await hz.end(c)
    assert hz.state(s) == model.RUNNING


@pytest.mark.asyncio
async def test_releasing_the_only_awaited_child_resumes_the_parent(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    await hz.end(a)
    assert hz.state(s) == model.WAITING_CHILDREN, "S still awaits the deferred B"
    assert not hz.mgr._admission.resume_granted(s.id)

    out = await _release(hz, s, b.id)
    assert out == {"released": [b.id], "resumed": True}
    await hz.settle()
    assert hz.state(s) == model.RUNNING
    assert hz.mgr._admission.resume_granted(s.id)


@pytest.mark.asyncio
async def test_a_released_child_does_not_reopen_the_wait_after_the_resume(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    await _release(hz, s, b.id)
    await hz.end(a)
    assert hz.state(s) == model.RUNNING and hz.live(s)._slot_released is False

    # B registering after the resume during the same call must not make S
    # yield its slot again.
    hz.mgr._admission.taskq_child_registered(b)
    await settle_store_writes(hz.store, rounds=2)
    assert hz.live(s)._slot_released is False
    assert hz.state(s) == model.RUNNING

    # A new call's child C registers while B remains outstanding. The released
    # B stays excluded, so this call awaits only C and resumes when C ends.
    hz.block_in_spawn_sub_agents(s, call_id="new-call")
    c = hz.child_of(s, "C")
    await hz.settle()
    assert hz.state(s) == model.WAITING_CHILDREN
    assert _awaited(hz, s) == [c.id]

    await hz.end(c)
    assert hz.state(s) == model.RUNNING
    assert hz.live(s)._slot_released is False


@pytest.mark.asyncio
async def test_an_id_that_is_not_a_live_child_is_ignored(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    stranger = hz.spawn("X")
    await hz.settle()

    out = await _release(hz, s, stranger.id, "no-such-id")
    assert out == {"released": [], "resumed": False}
    assert sorted(_awaited(hz, s)) == sorted([a.id, b.id])

    # A terminal awaited child is not released either; it is already settled.
    await hz.end(a)
    out = await _release(hz, s, a.id)
    assert out["released"] == []
    assert hz.state(s) == model.WAITING_CHILDREN, "B is still awaited"


@pytest.mark.asyncio
async def test_a_parent_holding_its_slot_or_not_waiting_is_left_alone(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s = hz.spawn("S")
    await hz.settle()
    memory.low = True
    b = hz.child_of(s, "B")  # S is not blocked in spawn_sub_agents: it keeps its slot
    await hz.settle()
    memory.low = False
    assert hz.live(s)._slot_released is False

    assert await _release(hz, s, b.id) == {"released": [], "resumed": False}
    assert hz.live(s)._wait_record is None
    assert not hz.live(s)._resume_pending

    # Slot released for a wait that is not ``waiting_children``: nothing changes.
    s2, a2, b2 = await _blocked_on_running_a_and_deferred_b(hz, memory)
    live = hz.live(s2)
    record = dict(live._wait_record)
    with patch.dict(live._wait_record, {"state": model.WAITING_DEPENDENCY}):
        assert await _release(hz, s2, b2.id) == {"released": [], "resumed": False}
    assert live._wait_record == record
    assert sorted(hz.ledger.record_of(s2.id).resume_condition.ids) == sorted([a2.id, b2.id])


@pytest.mark.asyncio
async def test_the_release_is_idempotent(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    first = await _release(hz, s, b.id)
    second = await _release(hz, s, b.id)
    assert first == {"released": [b.id], "resumed": False}
    assert second == {"released": [], "resumed": False}
    assert _awaited(hz, s) == [a.id]
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id]

    await hz.end(a)
    assert hz.state(s) == model.RUNNING


@pytest.mark.asyncio
async def test_a_registration_overlapping_the_release_does_not_restore_the_child(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    real_run = hz.store.run

    async def run_then_register(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        out = await real_run(fn, *args, **kwargs)
        if hz.store.run is not real_run:
            hz.store.run = real_run
            # The production registration runs after the release's store write
            # and before the release resumes. It POSTS a write of the record
            # that still awaits B, which lands after the release's own write.
            await hz.mgr._admission.taskq_child_registered_async(a)
        return out

    hz.store.run = run_then_register
    try:
        out = await _release(hz, s, b.id)
    finally:
        hz.store.run = real_run

    assert out == {"released": [b.id], "resumed": False}
    assert _awaited(hz, s) == [a.id]
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id], "B left the stored wait"
    await hz.end(a)
    assert hz.state(s) == model.RUNNING


@pytest.mark.asyncio
async def test_a_registration_after_the_store_write_does_not_restore_the_child(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    real_run = hz.store.run

    async def register_after_the_write(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        out = await real_run(fn, *args, **kwargs)
        if hz.store.run is not real_run:
            hz.store.run = real_run
            # The release's store write has landed but memory still lists B: a
            # registration that copies memory now must not post B back.
            await hz.mgr._admission.taskq_child_registered_async(a)
        return out

    hz.store.run = register_after_the_write
    try:
        out = await _release(hz, s, b.id)
    finally:
        hz.store.run = real_run

    assert out == {"released": [b.id], "resumed": False}
    assert _awaited(hz, s) == [a.id]
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id], "B left the stored wait"
    await hz.end(a)
    assert hz.state(s) == model.RUNNING


@pytest.mark.asyncio
async def test_overlapping_releases_of_different_children_both_leave_the_wait(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s = hz.spawn("S")
    await hz.settle()
    hz.block_in_spawn_sub_agents(s)
    memory.low = True
    b = hz.child_of(s, "B")
    c = hz.child_of(s, "C")
    await hz.settle()
    memory.low = False
    a = hz.child_of(s, "A")
    await hz.settle()
    assert sorted(_awaited(hz, s)) == sorted([a.id, b.id, c.id])

    first, second = await asyncio.gather(_release(hz, s, b.id), _release(hz, s, c.id))
    assert sorted(first["released"] + second["released"]) == sorted([b.id, c.id])
    assert _awaited(hz, s) == [a.id]
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id]
    await hz.end(a)
    assert hz.state(s) == model.RUNNING


@pytest.mark.asyncio
async def test_a_resend_corrects_the_stored_wait_when_memory_no_longer_lists_the_child(
    h, memory
) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    with patch.object(
        hz.store, "run", AsyncMock(side_effect=TaskStoreUnavailable("store unavailable"))
    ):
        first = await hz.mgr._admission.taskq_release_deferred_async(s.id, [b.id])
    assert first == {"released": [], "resumed": False, "retry": True}

    # A registration filtered B from memory, but its own store write failed:
    # memory awaits [A] while the stored wait still awaits [A, B].
    live = hz.live(s)
    record = WaitRecord.from_dict(live._wait_record)
    record.resume_condition.ids = [a.id]
    live._wait_record = record.to_dict()
    assert sorted(hz.ledger.record_of(s.id).resume_condition.ids) == sorted([a.id, b.id])

    second = await _release(hz, s, b.id)
    assert second == {"released": [b.id], "resumed": False}
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id], "B left the stored wait"
    await hz.end(a)
    assert hz.state(s) == model.RUNNING


@pytest.mark.asyncio
async def test_a_fenced_store_write_answers_retry_and_changes_nothing(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)

    with patch.object(hz.store, "update_wait", return_value=False):
        first = await hz.mgr._admission.taskq_release_deferred_async(s.id, [b.id])
    assert first == {"released": [], "resumed": False, "retry": True}
    assert sorted(_awaited(hz, s)) == sorted([a.id, b.id]), "memory follows the store"
    assert sorted(hz.ledger.record_of(s.id).resume_condition.ids) == sorted([a.id, b.id])

    second = await _release(hz, s, b.id)
    assert second == {"released": [b.id], "resumed": False}
    assert hz.ledger.record_of(s.id).resume_condition.ids == [a.id]


@pytest.mark.asyncio
async def test_an_awaited_id_with_no_child_row_stays_in_the_stored_wait(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    live = hz.live(s)
    record = WaitRecord.from_dict(live._wait_record)
    record.resume_condition.ids = [a.id, b.id, "no-row-yet"]
    live._wait_record = record.to_dict()
    assert hz.store.update_wait(s.id, record.to_dict(), generation=live._taskq_generation or None)

    out = await _release(hz, s, "no-row-yet")
    assert out == {"released": [], "resumed": False}
    assert "no-row-yet" in hz.ledger.record_of(s.id).resume_condition.ids
    assert "no-row-yet" in _awaited(hz, s)
    assert "no-row-yet" not in live._deferred_released


@pytest.mark.asyncio
async def test_a_store_outage_marks_the_release_for_retry(h, memory) -> None:
    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)

    with patch.object(
        hz.store,
        "run",
        AsyncMock(side_effect=TaskStoreUnavailable("store unavailable")),
    ):
        out = await hz.mgr._admission.taskq_release_deferred_async(s.id, [b.id])

    assert out == {"released": [], "resumed": False, "retry": True}
    assert sorted(_awaited(hz, s)) == sorted([a.id, b.id])
    assert not hz.live(s)._resume_pending


class _Req(dict):
    """The parts of an aiohttp request ``api_spawn_release_deferred`` reads."""

    def __init__(self, state: Any, agent_id: str, body: Any, headers: dict[str, str]) -> None:
        super().__init__(internal_auth=True)
        self.app = {"state": state}
        self.match_info = {"agent_id": agent_id}
        self.headers = headers
        self._body = body

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.mark.asyncio
async def test_release_route_answers_only_the_attested_run_itself(h, memory) -> None:
    import json

    from kiro_crew.dashboard.handlers import spawn_resume

    hz = await h(max_concurrent=2)
    s, a, b = await _blocked_on_running_a_and_deferred_b(hz, memory)
    state = SimpleNamespace(subagents=hz.mgr)
    own = {"X-Session-Key": f"subagent:{s.id}"}
    attested = {"ok": True}

    def call(body: Any, headers: dict[str, str], agent_id: str | None = None, **extra: Any):
        req = _Req(state, agent_id or s.id, body, headers)
        req.update(extra)
        return spawn_resume.api_spawn_release_deferred(req)  # type: ignore[arg-type]

    with patch(
        "kiro_crew.member_memory_auth.session_key_is_attested",
        lambda _req, _key: attested["ok"],
    ):
        refusals = [
            await call({"ids": [b.id]}, {}),
            await call({"ids": [b.id]}, {"X-Session-Key": f"subagent:{a.id}"}),
            await call({"ids": [b.id]}, {"X-Session-Key": "dashboard:chat-1"}),
            await call({"ids": [b.id]}, own, internal_auth=False),
            await call(
                {"ids": [b.id]}, {"X-Session-Key": "subagent:no-such-run"}, agent_id="no-such-run"
            ),
        ]
        attested["ok"] = False
        refusals.append(await call({"ids": [b.id]}, own))
        attested["ok"] = True
        for resp in refusals:
            assert resp.status == 404
            assert json.loads(resp.text)["known"] is False
        assert sorted(_awaited(hz, s)) == sorted([a.id, b.id]), "no refusal released anything"

        resp = await call({"ids": [f"child-{i}" for i in range(SPAWN_BATCH_MEMBERS_MAX)]}, own)
        assert resp.status == 200
        assert json.loads(resp.text) == {"id": s.id, "released": [], "resumed": False}

        for bad in (
            None,
            [],
            {"ids": "x"},
            {"ids": [""]},
            {"ids": [1]},
            {"ids": ["x"] * (SPAWN_BATCH_MEMBERS_MAX + 1)},
            ValueError("not json"),
        ):
            resp = await call(bad, own)
            assert resp.status == 400, bad
            assert json.loads(resp.text)["code"] == spawn_resume.INVALID_RELEASE_CODE

        with patch.object(
            type(hz.mgr._admission),
            "taskq_release_deferred_async",
            AsyncMock(return_value={"released": [], "resumed": False, "retry": True}),
        ):
            resp = await call({"ids": [b.id]}, own)
        assert resp.status == 503
        assert json.loads(resp.text) == {
            "error": "task store unavailable; retry later",
            "code": "task_store_unavailable",
            "retry": True,
        }
        assert sorted(_awaited(hz, s)) == sorted([a.id, b.id])

        resp = await call({"ids": [b.id]}, own)
        await settle_store_writes(hz.store, rounds=2)
        assert resp.status == 200
        assert json.loads(resp.text) == {"id": s.id, "released": [b.id], "resumed": False}
        assert _awaited(hz, s) == [a.id]

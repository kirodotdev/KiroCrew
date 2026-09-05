"""Session-scoped stop for issue #8270 — Stop all must also unqueue.

The chip's "Stop all" used to be a client-side per-id loop over RUNNING
agents, so members of the wave still WAITING behind the stagger / concurrency
gate (which exist only as ``_queue`` entries, with no client-visible ids)
later started and continued the batch after the user asked to stop it.

``SubagentManager.cancel_session(parent_session_key)`` is the server-side
fix: one call cancels the session's live runs AND drops its queued entries.
These tests pin its contract:

  * running agents of the session are cancelled (neutral user-stop);
  * queued entries of the session are dropped, with the queued depth
    re-emitted per (parent, batch) exactly as ``_unqueue_impl`` does;
  * a run parked on its spawn-approval prompt is left untouched — the
    approval card is where the user decides it;
  * another session's agents and queue entries are untouched;
  * ``_shutting_down`` is never set (that is ``cancel_all``'s shutdown path).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager

SESSION = "dashboard:chat-1"
OTHER_SESSION = "dashboard:chat-2"


def _manager() -> SubagentManager:
    return SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())


def _running(agent_id: str, parent: str) -> SubagentInfo:
    return SubagentInfo(id=agent_id, task=f"task {agent_id}", parent_session_key=parent)


def _parked(agent_id: str, parent: str) -> SubagentInfo:
    info = _running(agent_id, parent)
    # The spawn-approval gate's pair: registered, prompt raised, never executed.
    info._awaiting_approval = True
    info._exec_started = None
    return info


def _queue_entry(agent_id: str, parent: str, batch_id: str = "", batch_total: int = 0) -> dict:
    return {
        "task": f"queued {agent_id}",
        "parent_session_key": parent,
        "batch_id": batch_id,
        "batch_total": batch_total,
        "_preassigned_id": agent_id,
    }


class TestCancelSession:
    @pytest.mark.asyncio
    async def test_cancels_running_and_unqueues_queued_for_the_session(self) -> None:
        mgr = _manager()
        mgr._agents = {"r1": _running("r1", SESSION), "r2": _running("r2", SESSION)}
        mgr._queue = [_queue_entry("q1", SESSION, "batchA"), _queue_entry("q2", SESSION, "batchA")]
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)

        assert sorted(result["cancelled"]) == ["r1", "r2"]
        assert sorted(result["unqueued"]) == ["q1", "q2"]
        assert mgr._queue == []
        # A user stop is neutral: the record carries the marker cancel() sets.
        assert mgr._agents["r1"].user_stopped and mgr._agents["r2"].user_stopped
        assert mgr._force_reap.await_count == 2

    @pytest.mark.asyncio
    async def test_live_cancellations_are_initiated_concurrently(self) -> None:
        """Every live cancel is initiated before any completion is awaited.

        ``_force_reap`` yields during session teardown (bounded by
        ``_RESET_TIMEOUT``); a sequential ``await cancel()`` loop would keep
        agent N running through N-1 of those windows after Stop all. The
        rendezvous below deadlocks (and times out) under a sequential loop:
        each reap waits for the OTHER agent's reap to have started.
        """
        mgr = _manager()
        mgr._agents = {"r1": _running("r1", SESSION), "r2": _running("r2", SESSION)}
        started: set[str] = set()
        both_started = asyncio.Event()

        async def _reap(agent_id: str, info: SubagentInfo, elapsed: float, **_: object) -> None:
            started.add(agent_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)

        mgr._force_reap = _reap  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)

        assert sorted(result["cancelled"]) == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_reemits_queue_depth_for_every_dropped_entry(self) -> None:
        mgr = _manager()
        mgr._queue = [
            _queue_entry("q1", SESSION, "batchA"),
            _queue_entry("q2", SESSION, "batchA"),
            _queue_entry("q3", SESSION, "batchB"),
        ]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)

        assert sorted(result["unqueued"]) == ["q1", "q2", "q3"]
        calls = {call.args for call in mgr._emit_queue_depth.call_args_list}
        assert calls == {(SESSION, "batchA"), (SESSION, "batchB")}

    @pytest.mark.asyncio
    async def test_unqueued_members_are_announced_as_stopped_completions(self) -> None:
        # NEVER SILENT: queued members were already counted as submitted, so a
        # silent drop would leave the wave's accounting pending forever and the
        # parent waiting for completion events that never arrive. Every dropped
        # entry must route through the single completion consumer as a neutral
        # user stop, carrying its pre-assigned id and batch identity.
        on_done = AsyncMock()
        mgr = _manager()
        mgr._on_done = on_done
        mgr._queue = [
            _queue_entry("q1", SESSION, "batchA", batch_total=2),
            _queue_entry("q2", SESSION, "batchA", batch_total=2),
        ]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)
        for _ in range(25):  # drain the ensure_future'd announces
            await asyncio.sleep(0)

        assert sorted(result["unqueued"]) == ["q1", "q2"]
        assert on_done.await_count == 2
        # The announce tasks self-prune from _tasks on completion — repeated
        # stops must not accrete dead "unqueued-" entries until shutdown.
        assert not [k for k in mgr._tasks if k.startswith("unqueued-")]
        announced = {call.args[0].id: call.args[0] for call in on_done.await_args_list}
        assert set(announced) == {"q1", "q2"}
        for info in announced.values():
            assert info.done is True
            assert info.user_stopped is True
            assert info.error == ""
            assert info.parent_session_key == SESSION
            assert info.batch_id == "batchA"
            assert info.batch_total == 2

    @pytest.mark.asyncio
    async def test_per_id_cancel_of_a_queued_run_announces_too(self) -> None:
        # The DELETE /api/spawn/{id} fall-through reaches cancel() -> _unqueue;
        # that path must be no more silent than the session-scoped one.
        on_done = AsyncMock()
        mgr = _manager()
        mgr._on_done = on_done
        mgr._queue = [_queue_entry("q1", SESSION, "batchA")]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        assert await mgr.cancel("q1") is True
        for _ in range(25):
            await asyncio.sleep(0)

        assert on_done.await_count == 1
        assert on_done.await_args_list[0].args[0].id == "q1"
        assert on_done.await_args_list[0].args[0].user_stopped is True

    @pytest.mark.asyncio
    async def test_wave_stays_pending_until_every_announce_has_run(self) -> None:
        # The entries leave _queue synchronously but the synthetic announces
        # run on scheduled tasks. In that window neither _agents nor _queue
        # shows the members, so without the pending count the consumer's
        # last-member fallback would finalize the wave on the FIRST announce
        # (done=1 < total=2) and then once more per remaining announce.
        # The consumer must observe: pending while a sibling's announce is
        # still outstanding, not pending on the last one.
        mgr = _manager()
        seen_pending: list[bool] = []

        async def _consumer(info: SubagentInfo) -> None:
            seen_pending.append(mgr.batch_members_pending("batchA"))

        mgr._on_done = AsyncMock(side_effect=_consumer)
        mgr._queue = [
            _queue_entry("q1", SESSION, "batchA", batch_total=2),
            _queue_entry("q2", SESSION, "batchA", batch_total=2),
        ]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        await mgr.cancel_session(SESSION)
        # Announces scheduled but not yet run: the wave must still be pending.
        assert mgr.batch_members_pending("batchA") is True
        for _ in range(25):
            await asyncio.sleep(0)

        assert seen_pending == [True, False]
        assert mgr._batch_unqueued_pending == {}
        assert mgr.batch_members_pending("batchA") is False

    @pytest.mark.asyncio
    async def test_leaves_an_approval_parked_run_untouched(self) -> None:
        mgr = _manager()
        parked = _parked("p1", SESSION)
        mgr._agents = {"p1": parked, "r1": _running("r1", SESSION)}
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)

        assert result["cancelled"] == ["r1"]
        assert not parked.user_stopped and not parked.done
        assert "p1" in mgr._agents

    @pytest.mark.asyncio
    async def test_a_parked_run_that_started_executing_is_cancellable(self) -> None:
        # Mid-run TOOL approvals also raise _awaiting_approval; _exec_started
        # is the discriminator (same pair as terminal.py's reap message).
        mgr = _manager()
        executing = _parked("e1", SESSION)
        executing._exec_started = 1_700_000_000.0
        mgr._agents = {"e1": executing}
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)

        assert result["cancelled"] == ["e1"]

    @pytest.mark.asyncio
    async def test_other_sessions_and_done_agents_are_untouched(self) -> None:
        mgr = _manager()
        other = _running("o1", OTHER_SESSION)
        finished = _running("f1", SESSION)
        finished.done = True
        mgr._agents = {"o1": other, "f1": finished, "r1": _running("r1", SESSION)}
        mgr._queue = [_queue_entry("oq1", OTHER_SESSION)]
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        result = await mgr.cancel_session(SESSION)

        assert result["cancelled"] == ["r1"]
        assert result["unqueued"] == []
        assert not other.user_stopped
        assert mgr._queue == [_queue_entry("oq1", OTHER_SESSION)]
        mgr._emit_queue_depth.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_sets_shutting_down(self) -> None:
        mgr = _manager()
        mgr._agents = {"r1": _running("r1", SESSION)}
        mgr._queue = [_queue_entry("q1", SESSION)]
        mgr._force_reap = AsyncMock()  # type: ignore[method-assign]
        mgr._emit_queue_depth = MagicMock()  # type: ignore[method-assign]

        await mgr.cancel_session(SESSION)

        assert mgr._shutting_down is False

    @pytest.mark.asyncio
    async def test_empty_session_answers_empty_lists(self) -> None:
        mgr = _manager()
        result = await mgr.cancel_session(SESSION)
        assert result == {"cancelled": [], "unqueued": []}
